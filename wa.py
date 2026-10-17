"""WhatsApp в Кореше: отправка сообщений контактам хозяина по его просьбе и только после его «да».

Устройство: Node-демон wa/wa-daemon.js (Baileys 7.0.0-rc14) работает сайдкаром в том же контейнере, слушает 127.0.0.1,
на каждый запрос ждёт заголовок X-WA-Token (случайный на запуск этого процесса). Python его поднимает и перезапускает.
Отправка по просьбе хозяина. Входящие демон пишет только для секретаря (ТЗ-36, secretary.py) и только пока лежит файл-флаг inbox.json: без него не читаем и не храним. Сессия WhatsApp лежит на Volume (MEMORY_DIR/wa/auth, права 0700):
это полный доступ к аккаунту, секрет уровня пароля, в логи, ответы и модель она не идёт.

Что здесь жёстко решает код, а не промпт:
- Номера в модель не уходят. Модель оперирует ИМЕНЕМ из списка контактов, номер подставляет сервер.
- Отправка в два вызова. whatsapp_draft ничего не шлёт, а запоминает черновик {контакт, текст}; whatsapp_confirm
  без параметров шлёт РОВНО этот черновик и только если хозяин следующей репликой сказал «да». Черновик живёт две минуты.
- Лимиты: десять сообщений в час, тридцать в сутки (счётчик на Volume, переживает перезапуск).
- Голые номера голосом (распознавание путает цифры) не принимаются.
"""
import asyncio
import json
import os
import re
import secrets
import shutil
import socket
import time
from pathlib import Path

import httpx

import config as C
import memory

DRAFT_TTL = 120.0              # секунд: черновик старше этого не отправляется
LIMIT_HOUR = 10
LIMIT_DAY = 30
MAX_TEXT = 500
SEND_TIMEOUT = 12.0
CALL_TIMEOUT = 2.5

R_LOST = "WhatsApp отвалился, перепривяжи на странице настройки"
R_DOWN = "WhatsApp сейчас не на связи, попробуй чуть позже"
R_HOUR = "лимит: не больше десяти сообщений в час, подожди"
R_DAY = "лимит: не больше тридцати сообщений в сутки, до завтра"
R_NOT_ON = "этого номера нет в WhatsApp"
R_SEND = "WhatsApp не принял сообщение"
R_AMBIG = "не знаю, дошло ли сообщение: WhatsApp не ответил вовремя"
R_NO_DRAFT = "нечего отправлять: нет черновика"
R_EXPIRED = "черновик устарел, прошло больше двух минут"
R_NOT_CONFIRMED = "хозяин ещё не сказал «да»"

CONSENT_TEXT = ("Насколько это безопасно. Кореш подключается к WhatsApp как связанное устройство — так же, как WhatsApp Web на компьютере. "
                "Блокируют не за подключение, а за спам: если пишешь своим людям и по делу — риск минимальный.\n"
                "Чего не делать:\n"
                "• рассылки незнакомым и одинаковые сообщения многим подряд;\n"
                "• писать людям, которые тебя не знают и могут нажать «пожаловаться»;\n"
                "• подключать номер, заведённый вчера — дай ему «пожить» пару недель;\n"
                "• пытаться обойти лимит Кореша (10 сообщений в час) — он стоит как раз для твоей защиты.\n"
                "Кореш отправляет сообщения только по твоей просьбе и только после твоего «да». Чужие сообщения он читает вслух лишь если ты сам подключил чтение WhatsApp и выбрал людей.")

TOKEN = secrets.token_urlsafe(32)       # знает только этот процесс и запущенный им демон
DAEMON_CMD = None                       # ворота подставляют команду фейкового демона
_TRANSPORT = None                       # ворота подставляют httpx.MockTransport для вызовов демона
_S = {"task": None, "proc": None, "port": 0, "restarts": 0, "stop": False, "last_exit": None}
DRAFTS: dict[str, dict] = {}
TURNS: dict[str, int] = {}


def _now() -> float:
    return time.time()


def reset_for_tests() -> None:
    _S.update(task=None, proc=None, port=0, restarts=0, stop=False, last_exit=None)
    DRAFTS.clear()
    TURNS.clear()


# ---------- пути и признаки ----------
def wa_dir() -> Path:
    return Path(C.wa_dir())


def auth_dir() -> Path:
    return wa_dir() / "auth"


def daemon_file() -> Path:
    return Path(__file__).resolve().parent / "wa" / "wa-daemon.js"


def _ensure_dirs() -> None:
    for d in (wa_dir(), auth_dir()):
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass


def runnable() -> bool:
    """Есть чем запускать демон: либо подставленная команда (ворота), либо node и сам файл демона."""
    return bool(DAEMON_CMD) or (bool(shutil.which("node")) and daemon_file().is_file())


def enabled() -> bool:
    return C.wa_enabled() and runnable()


def linked() -> bool:
    """Номер хоть раз был привязан (демон пишет linked.json, когда соединение открылось). Стирается только явной отвязкой."""
    try:
        return bool(json.loads((wa_dir() / "linked.json").read_text(encoding="utf-8")).get("linked"))
    except (OSError, ValueError):
        return False


def available() -> bool:
    """Умение показывается модели и попадает в промпт: включено, номер привязан и есть кому писать."""
    return enabled() and linked() and bool(book())


# ---------- согласие на риск ----------
def _state_file() -> Path:
    return wa_dir() / "state.json"


def _read_state() -> dict:
    try:
        d = json.loads(_state_file().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def consent_given() -> bool:
    return bool(_read_state().get("consent_at"))


def give_consent() -> None:
    _ensure_dirs()
    d = _read_state()
    d["consent_at"] = int(_now())
    memory.atomic_write(str(_state_file()), json.dumps(d))
    try:
        os.chmod(_state_file(), 0o600)
    except OSError:
        pass


# ---------- контакты ----------
# Книга = контакты из синхронизации WhatsApp (демон кладёт contacts-sync.json) без скрытых + контакты, вписанные руками
# (WA_CONTACTS), + прозвища (WA_NICKS: номер -> «жена»). Поиск по имени делает СЕРВЕР: модель получает от инструмента только
# полное имя найденного контакта (для зачитывания) или список имён при неоднозначности. Номера в модель не уходят никогда.
_CASE_ENDINGS = ("ой", "ей", "ою", "ею", "ом", "ем", "ы", "и", "е", "у", "ю", "а", "я")        # родительный: «от Сергея Карпова», «от жены»
SYNC_WAIT = 90                 # секунд после подключения, дольше которых молчание WhatsApp про контакты считаем отказом


def _norm(s: str) -> str:
    return (s or "").lower().replace("ё", "е")


def _words(s: str) -> list[str]:
    return re.findall(r"\w+", _norm(s))


def word_variants(word: str) -> list[str]:
    """Именительные формы: Оле/Олю/Олей -> Оля, маме -> мама. Первым идёт само слово. Длинные слова (>10) падежом не считаем."""
    w = _norm(word)
    out = [w]
    if 3 <= len(w) <= 10:
        for end in _CASE_ENDINGS:
            if w.endswith(end) and len(w) - len(end) >= 2:
                stem = w[: -len(end)]
                for tail in ("а", "я", "", "ь", "й"):
                    v = stem + tail
                    if v not in out:
                        out.append(v)
    return out


def normalize_number(raw: str) -> str:
    """Любая запись номера -> только цифры с кодом страны, или '' если на номер не похоже.
    8XXXXXXXXXX -> 7XXXXXXXXXX; десять цифр без кода -> 7 + цифры (Казахстан/Россия); иначе 11-15 цифр как есть."""
    d = re.sub(r"\D", "", str(raw or ""))
    if len(d) == 11 and d.startswith("8"):
        d = "7" + d[1:]
    elif len(d) == 10:
        d = "7" + d
    return d if 11 <= len(d) <= 15 else ""


def manual_contacts() -> dict[str, str]:
    """{имя: номер цифрами} -- вписанные руками. Битые записи пропускаем."""
    out = {}
    for name, num in C.wa_contacts().items():
        n = normalize_number(num)
        nm = re.sub(r"\s+", " ", str(name)).strip()
        if n and nm:
            out[nm] = n
    return out


def _sync_file() -> Path:
    return wa_dir() / "contacts-sync.json"


def synced() -> list[dict]:
    """Контакты, которые WhatsApp отдал при синхронизации: [{name, number, saved}], saved=имя из записной книжки (не прозвище владельца)."""
    try:
        data = json.loads(_sync_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for c in (data.get("contacts") if isinstance(data, dict) else None) or []:
        if isinstance(c, dict) and c.get("name") and re.fullmatch(r"\d{8,15}", str(c.get("number") or "")):
            out.append({"name": str(c["name"])[:60], "number": str(c["number"]), "saved": bool(c.get("saved"))})
    return out


def book() -> list[dict]:
    """Всё, о чём Кореш знает: [{name, number, nicks, manual}]. Скрытые из синхронизации исключены; вписанное руками главнее."""
    hidden = C.wa_hidden()
    nicks = C.wa_nicks()
    out: dict[str, dict] = {}
    for name, num in manual_contacts().items():
        out[num] = {"name": name, "number": num, "nicks": nicks.get(num, []), "manual": True}
    for c in synced():
        if c["number"] in hidden or c["number"] in out:
            continue
        out[c["number"]] = {"name": c["name"], "number": c["number"], "nicks": nicks.get(c["number"], []), "manual": False}
    return list(out.values())


def contacts() -> dict[str, str]:
    """{полное имя: номер}. При одинаковых именах остаётся первое: для разбора есть find_contact."""
    out: dict[str, str] = {}
    for c in book():
        out.setdefault(c["name"], c["number"])
    return out


def looks_like_number(s: str) -> bool:
    return len(re.sub(r"\D", "", str(s or ""))) >= 6


def _match(toks: list[str], target_words: list[str]) -> str:
    """'exact' -- слова запроса (с падежами) по порядку и числу равны словам цели; 'part' -- каждое слово запроса есть в цели; '' -- нет."""
    variants = [word_variants(t) for t in toks]
    if len(target_words) == len(toks) and all(w in vs for vs, w in zip(variants, target_words)):
        return "exact"
    if all(any(v in target_words for v in vs) for vs in variants):
        return "part"
    return ""


def find_contact(query: str) -> tuple:
    """('one', полное имя, номер) | ('many', [полные имена]) | ('number',) | ('none',).
    Ищем по имени и прозвищам, с падежами («Оле» -> Оля, «жене» -> жена). Слово совпадает целиком, не подстрока.
    Полное совпадение главнее частичного («Оля» против «Оля работа»). Одна персона (один номер) -- одна запись."""
    q = (query or "").strip()
    if len(q) < 2:
        return ("none",)
    toks = _words(q)
    if looks_like_number(q) and not any(re.search(r"[^\W\d_]", t) for t in toks):
        return ("number",)                                   # голый номер голосом: не принимаем
    if not toks:
        return ("none",)
    exact: dict[str, str] = {}
    part: dict[str, str] = {}
    shown: dict[str, str] = {}
    for c in book():
        best = ""
        for target in [c["name"]] + list(c["nicks"]):
            m = _match(toks, _words(target))
            if m == "exact":
                best = "exact"
                break
            if m == "part":
                best = best or "part"
        if best == "exact":
            exact[c["number"]] = c["name"]
        elif best == "part":
            part[c["number"]] = c["name"]
    hits = exact or part
    if len(hits) == 1:
        num, name = next(iter(hits.items()))
        return ("one", name, num)
    if len(hits) > 1:
        return ("many", sorted(hits.values()))
    return ("none",)


def _clean_name(v, limit: int = 40) -> str:
    n = re.sub(r"[\x00-\x1f\x7f]+", " ", str(v or "")).strip()
    return re.sub(r"\s{2,}", " ", n)[:limit]


def clean_manual(rows) -> tuple[dict, str]:
    """Строки страницы [[имя, номер]...] -> ({имя: '+7XXXXXXXXXX'}, ошибка). Номер строго +7 и десять цифр, до 30 штук."""
    out: dict[str, str] = {}
    for item in (rows or []):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        name = _clean_name(item[0])
        raw = str(item[1] or "").strip()
        if not name and not re.sub(r"\D", "", raw):
            continue
        if not name:
            return {}, "У номера нет имени: впиши, как к этому человеку обращаются («Оля», «Мама»)."
        if re.search(r"\d", name):
            return {}, f"В имени «{name}» цифры: имя нужно словами, номер впиши справа."
        num = normalize_number(raw)
        if not re.fullmatch(r"7\d{10}", num or ""):
            return {}, f"Номер у «{name}» не в том формате. Нужно +7XXXXXXXXXX: плюс, семёрка и ещё десять цифр."
        if any(_norm(k) == _norm(name) for k in out):
            return {}, f"Имя «{name}» повторяется. Назови контакты по-разному («Оля», «Оля с работы»)."
        out[name] = "+" + num
        if len(out) > 30:
            return {}, "Вручную можно добавить не больше тридцати контактов."
    return out, ""


def clean_book_edits(hidden, nicks, extra_known=()) -> tuple[list, dict, str]:
    """Скрытые номера и прозвища с страницы -> (список цифр, {номер: [прозвища]}, ошибка)."""
    hid = []
    for x in (hidden or [])[:6000]:
        d = re.sub(r"\D", "", str(x))
        if re.fullmatch(r"\d{8,15}", d) and d not in hid:
            hid.append(d)
    known = {c["number"] for c in synced()} | set(manual_contacts().values()) | set(extra_known)
    nk: dict[str, list] = {}
    for num, val in (nicks or {}).items():
        d = re.sub(r"\D", "", str(num))
        if d not in known:
            continue
        parts = val if isinstance(val, list) else re.split(r"[,;\n]", str(val or ""))
        lst = []
        for p in parts:
            p = _clean_name(p, 30)
            if not p:
                continue
            if re.search(r"\d", p):
                return [], {}, f"В прозвище «{p}» цифры: прозвище нужно словами («жена», «мама»)."
            if p not in lst:
                lst.append(p)
        if lst:
            nk[d] = lst[:5]
        if len(nk) > 200:
            return [], {}, "Слишком много прозвищ."
    return hid, nk, ""


def sync_status(st: dict) -> str:
    """ok (имена пришли) | pending (подключились недавно, ждём) | empty (WhatsApp промолчал) | wait (не подключён)."""
    if synced():
        return "ok"
    if st.get("state") != "connected":
        return "wait"
    cf = st.get("connected_for")
    return "empty" if isinstance(cf, (int, float)) and cf >= SYNC_WAIT else "pending"


# ---------- лимиты ----------
def _limits_file() -> Path:
    return wa_dir() / "limits.json"


def _load_sends(now: float) -> list[float]:
    try:
        data = json.loads(_limits_file().read_text(encoding="utf-8"))
        ts = [float(x) for x in data.get("sent", [])]
    except (OSError, ValueError, AttributeError):
        ts = []
    return [t for t in ts if now - t < 86400]


def limit_reason() -> str:
    """'' | R_HOUR | R_DAY. Скользящие окна: час и сутки."""
    now = _now()
    sends = _load_sends(now)
    if len(sends) >= LIMIT_DAY:
        return R_DAY
    if sum(1 for t in sends if now - t < 3600) >= LIMIT_HOUR:
        return R_HOUR
    return ""


def record_send() -> None:
    now = _now()
    sends = _load_sends(now) + [now]
    try:
        _ensure_dirs()
        memory.atomic_write(str(_limits_file()), json.dumps({"sent": sends}))
    except OSError as e:
        print(f"wa: счётчик лимитов не записался ({type(e).__name__})", flush=True)


# ---------- демон: запуск и надзор ----------
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def daemon_command() -> list[str]:
    return list(DAEMON_CMD) if DAEMON_CMD else [shutil.which("node") or "node", str(daemon_file())]


def daemon_env() -> dict:
    """Демону отдаём только нужное: ключ Anthropic, секрет вебхука и прочее ему ни к чему."""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp"),
           "WA_TOKEN": TOKEN, "WA_PORT": str(_S["port"]), "WA_AUTH_DIR": str(auth_dir()), "WA_DATA_DIR": str(wa_dir())}
    if os.environ.get("TMPDIR"):
        env["TMPDIR"] = os.environ["TMPDIR"]
    return env


async def _supervise() -> None:
    backoff = 1.0
    while not _S["stop"]:
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(*daemon_command(), env=daemon_env())
        except OSError as e:
            print(f"wa: демон не запустился ({type(e).__name__}), повтор через {backoff:.0f}с", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            continue
        _S["proc"] = proc
        print(f"wa: демон запущен (pid {proc.pid})", flush=True)
        code = await proc.wait()
        _S["proc"] = None
        _S["last_exit"] = code
        if _S["stop"]:
            break
        _S["restarts"] += 1
        ran = time.monotonic() - started
        backoff = 1.0 if ran > 60 else min(backoff * 2, 30.0)
        print(f"wa: демон завершился (код {code}), поднимаю через {backoff:.0f}с", flush=True)
        await asyncio.sleep(backoff)


def ensure_started() -> bool:
    """Поднять демон, если ещё не поднят. Зовётся при старте (если номер привязан) и когда клиентка открыла привязку."""
    if not enabled():
        return False
    t = _S["task"]
    if t is not None and not t.done():
        return True
    _ensure_dirs()
    if not _S["port"]:
        _S["port"] = _free_port()
    _S["stop"] = False
    _S["task"] = asyncio.get_running_loop().create_task(_supervise())
    return True


def autostart() -> bool:
    """Старт сервера: демон нужен, только если номер уже привязан. Нет привязки -- ни процесса, ни памяти не тратим."""
    if enabled() and linked():
        return ensure_started()
    return False


def running() -> bool:
    t = _S["task"]
    return t is not None and not t.done()


async def shutdown() -> None:
    """Остановить надзор и ТОЛЬКО свой процесс демона (по его pid, не по имени)."""
    _S["stop"] = True
    proc = _S["proc"]
    if proc is not None and proc.returncode is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
    t = _S["task"]
    if t is not None:
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _S.update(task=None, proc=None)


# ---------- вызовы демона ----------
async def _call(method: str, path: str, body: dict | None = None, timeout: float = CALL_TIMEOUT):
    """JSON-ответ демона или None, если он недоступен. Тела запросов и ответов не логируем."""
    port = _S["port"] or (1 if _TRANSPORT is not None else 0)
    if not port:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=_TRANSPORT) as c:
            r = await c.request(method, f"http://127.0.0.1:{port}{path}", json=body, headers={"X-WA-Token": TOKEN})
        j = r.json()
        return j if isinstance(j, dict) else None
    except httpx.TimeoutException:
        return {"ok": False, "error": "timeout"}
    except Exception:  # noqa: BLE001
        return None


async def status() -> dict:
    """{state: off|down|connecting|need_qr|connected|logged_out, linked, lost}. lost: номер был привязан, а сессии больше нет."""
    was = linked()
    if not enabled():
        return {"state": "off", "linked": was, "lost": False}
    j = await _call("GET", "/status")
    if not j or not j.get("ok") or "state" not in j:
        return {"state": "down" if running() else "off", "linked": was, "lost": False}
    st = str(j.get("state"))
    lk = was or bool(j.get("linked"))
    return {"state": st, "linked": lk, "lost": lk and st in ("logged_out", "need_qr"),
            "contacts": j.get("contacts"), "connected_for": j.get("connected_for")}


async def qr_string() -> str | None:
    j = await _call("GET", "/qr")
    q = j.get("qr") if j else None
    return q if isinstance(q, str) and q else None


def qr_data_uri(text: str) -> str:
    """QR картинкой (SVG, data-URI) -- рисуем на сервере, без CDN и без внешних скриптов."""
    import segno
    return segno.make(text, error="m").svg_data_uri(scale=6, border=2, dark="#20201e", light="#ffffff")


async def pair(phone: str) -> dict:
    j = await _call("POST", "/pair", {"phone": phone}, timeout=15.0)
    return j or {"ok": False, "error": "down"}


async def inbox(after: int) -> dict | None:
    """Очередь входящих демона для секретаря (ТЗ-36): {ok, events, latest, ready} или None, если демон не ответил."""
    j = await _call("GET", f"/inbox?after={int(after)}")
    return j if j and j.get("ok") and isinstance(j.get("events"), list) else None


def inbox_flag_file() -> Path:
    return wa_dir() / "inbox.json"


async def logout() -> bool:
    j = await _call("POST", "/logout", {}, timeout=15.0)
    return bool(j and j.get("ok"))


# ---------- ход диалога: черновики и подтверждение ----------
AFFIRM_WORD = (r"(?:ну|да|ага|угу|ок|окей|окэй|хорошо|ладно|конечно|точно|верно|давай|давайте|отправляй|отправь|отправить|"
               r"отправляйте|шли|пиши|пошли|погнали|вперёд|вперед|валяй|подтверждаю|согласен|согласна|именно|отлично|супер)")
AFFIRM_RE = re.compile(rf"^(?:{AFFIRM_WORD}[\s,.!;:\-—…]*)+$", re.I)
NEG_RE = re.compile(r"(?<![а-яё])(?:нет|не|стоп|отмена|отмени|отбой|подожди|погоди|стой|хватит|ни)(?![а-яё])", re.I)


def is_affirmation(text: str) -> bool:
    """Ответ целиком состоит из согласия («да», «давай, отправляй»). «Да, но поменяй текст» согласием не считается."""
    t = (text or "").strip().lower().replace("ё", "е")
    return bool(t) and bool(AFFIRM_RE.match(t.replace("ё", "е"))) and not NEG_RE.search(t)


def note_turn(uid: str | None, user_text: str) -> None:
    """Начало хода мозга: считаем реплику хозяина; «нет/стоп/отмена» стирает черновик сразу."""
    k = uid or "-"
    TURNS[k] = TURNS.get(k, 0) + 1
    if k in DRAFTS and NEG_RE.search(user_text or "") and not is_affirmation(user_text):
        DRAFTS.pop(k, None)
        print("wa: черновик отменён словом хозяина", flush=True)


def clear_draft(uid: str | None) -> None:
    DRAFTS.pop(uid or "-", None)


def current_draft(uid: str | None) -> dict | None:
    d = DRAFTS.get(uid or "-")
    if d and _now() - d["ts"] > DRAFT_TTL:
        DRAFTS.pop(uid or "-", None)
        return None
    return d


def clean_text(text) -> str:
    t = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+", " ", str(text or ""))
    return re.sub(r"[ \t]{2,}", " ", t).strip()


class Result:
    """Итог инструмента: текст для модели, успех, причина (для журнала честности)."""

    def __init__(self, text: str, ok: bool, reason: str = ""):
        self.text, self.ok, self.reason = text, ok, reason


def _fail(reason: str) -> Result:
    return Result("error: " + reason, False, reason)


async def _connection_problem() -> str:
    st = await status()
    if st["state"] == "connected":
        return ""
    return R_LOST if st["lost"] else R_DOWN


async def tool_draft(uid: str | None, to: str, text: str) -> Result:
    """Первый вызов: ничего не шлёт. Находит контакт, проверяет связь и лимиты, запоминает черновик."""
    if not available():
        return _fail("WhatsApp не подключён")
    body = clean_text(text)
    if not body:
        return _fail("пустой текст сообщения")
    if len(body) > MAX_TEXT:
        return _fail(f"текст длиннее {MAX_TEXT} символов, сократи")
    found = find_contact(str(to or ""))
    if found[0] == "number":
        return _fail("голые номера голосом не принимаются: назови имя контакта")
    if found[0] == "none":
        return _fail("такого контакта не нашёл. Спроси хозяина, кому именно, и ничего не шли")
    if found[0] == "many":
        return _fail("подходит несколько контактов: " + "; ".join(found[1][:8]) + ". Спроси хозяина, кому именно (назвать полное имя)")
    _, name, number = found
    problem = await _connection_problem()
    if problem:
        return _fail(problem)
    lim = limit_reason()
    if lim:
        return _fail(lim)
    DRAFTS[uid or "-"] = {"name": name, "number": number, "text": body, "ts": _now(), "turn": TURNS.get(uid or "-", 0)}
    print("wa: черновик создан", flush=True)
    return Result(f"черновик готов, НИЧЕГО не отправлено. Скажи хозяину ровно так: Пишу {name}: «{body}». Отправить? "
                  "И остановись: жди его ответа. Текст и получателя не меняй.", True, "")


async def tool_confirm(uid: str | None, last_user_text: str) -> Result:
    """Второй вызов, без параметров: уходит ровно запомненный черновик, и только если хозяин ответил «да»."""
    k = uid or "-"
    existed = k in DRAFTS
    d = current_draft(uid)
    if d is None:
        return _fail(R_EXPIRED if existed else R_NO_DRAFT)
    if TURNS.get(k, 0) <= d["turn"] or not is_affirmation(last_user_text):
        return _fail("NO_CONFIRM: " + R_NOT_CONFIRMED + ". Зачитай черновик и спроси «Отправить?». Ничего не отправлено")
    DRAFTS.pop(k, None)                       # дальше черновика нет: второй «да» ничего не отправит
    problem = await _connection_problem()
    if problem:
        return _fail(problem)
    lim = limit_reason()
    if lim:
        return _fail(lim)
    j = await _call("POST", "/send", {"to": d["number"], "text": d["text"]}, timeout=SEND_TIMEOUT)
    if j is None:
        return _fail(R_DOWN)
    if j.get("ok"):
        record_send()
        print("wa: отправлено", flush=True)
        return Result(f"отправлено ({d['name']})", True, "")
    err = str(j.get("error") or "")
    if err == "timeout":
        record_send()                          # могло уйти: в лимит засчитываем
        return _fail(R_AMBIG)
    if err == "not_on_whatsapp":
        return _fail(R_NOT_ON)
    if err == "not_connected":
        return _fail(R_DOWN)
    print(f"wa: отправка не вышла ({err[:30]})", flush=True)
    return _fail(R_SEND)


def _alnum(s: str) -> str:
    return " ".join(re.findall(r"[0-9a-zа-я]+", _norm(s)))


def ensure_readback(uid: str | None, reply: str) -> str:
    """Ход закончился черновиком: хозяин ДОЛЖЕН услышать ровно то, что уйдёт. Если в ответе модели нет дословного текста
    черновика, имени получателя и вопроса, подставляем каноничную фразу. Что уйдёт, решает черновик, не пересказ модели."""
    d = current_draft(uid)
    if d is None:
        return reply
    low = _alnum(reply)
    text_ok = _alnum(d["text"]) in low
    stems = [w[:max(3, len(w) - 1)] for w in _words(d["name"]) if len(w) >= 2]
    name_ok = any(any(rw.startswith(s) for rw in low.split()) for s in stems) if stems else True
    if text_ok and name_ok and "?" in reply:
        return reply
    return f"Сообщение для «{d['name']}»: «{d['text']}». Отправить?"


async def notify_owner(text: str) -> bool:
    """Служебное сообщение хозяину «себе»: на собственный номер привязанного аккаунта (демон знает его сам, мы номера не видим).
    Без подтверждения и без черновика: это не письмо третьему лицу. В лимит сообщений контактам не засчитывается.
    WhatsApp не привязан / не на связи -> False, молча."""
    if not (enabled() and linked()):
        return False
    body = clean_text(text)[:MAX_TEXT]
    if not body:
        return False
    j = await _call("POST", "/send", {"self": True, "text": body}, timeout=SEND_TIMEOUT)
    ok = bool(j and j.get("ok"))
    print("wa: служебное сообщение хозяину " + ("отправлено" if ok else "не ушло"), flush=True)
    return ok


# ---------- что видит модель ----------
TOOLS = [
    {
        "name": "whatsapp_draft",
        "description": "Составить черновик сообщения в WhatsApp контакту хозяина. НИЧЕГО не отправляет. to: имя или прозвище "
                       "контакта так, как сказал хозяин (не номер). text: текст от лица хозяина, коротко, без мата и отсебятины. "
                       "После вызова прочитай хозяину черновик ровно как велит ответ инструмента и спроси «Отправить?».",
        "input_schema": {
            "type": "object",
            "properties": {"to": {"type": "string", "description": "имя или прозвище контакта (не номер)"},
                           "text": {"type": "string", "description": "текст сообщения"}},
            "required": ["to", "text"],
        },
    },
    {
        "name": "whatsapp_confirm",
        "description": "Отправить ровно тот черновик, который ты зачитал хозяину, ПОСЛЕ его ответа «да». Без параметров: "
                       "текст и получателя изменить нельзя. Без ответа «да» не вызывай.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


def prompt_block() -> str:
    """Кусок системного промпта. Пусто, пока умения нет: тогда промпт байт в байт как раньше. Имён контактов здесь НЕТ:
    поиск по имени делает сервер, модель передаёт имя так, как его сказал хозяин."""
    if not available():
        return ""
    return ("\n- WhatsApp: ты умеешь писать сообщения людям из контактов хозяина. Это единственное исключение из правила про "
            "сообщения выше. Порядок строгий. Первое: вызови whatsapp_draft (to: имя или прозвище так, как его сказал хозяин, "
            "например «Оле» или «жене»; text: что написать от лица хозяина, коротко, без мата и отсебятины). Второе: зачитай "
            "хозяину то, что вернул инструмент: «Пишу <полное имя>: «<текст>». Отправить?» и остановись, жди ответа. Третье: "
            "только после его «да» вызови whatsapp_confirm, ничего не меняя; на «нет» не отправляй. Номеров телефонов ты не знаешь "
            "и не называешь, для тебя контакт это только имя. Инструмент нашёл несколько контактов или ни одного: скажи об этом, "
            "спроси, кому именно (полное имя), ничего не шли. Голые номера, которые продиктует хозяин, не принимай. "
            "Сказать «отправил» можно лишь после ответа whatsapp_confirm, который начинается со слова «отправлено». "
            "Не получилось: назови причину из ответа инструмента.")
