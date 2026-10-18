"""WhatsApp-секретарь Кореша (ТЗ-36): «свои» написали в WhatsApp -> Станция сама открывает навык и говорит первой.

Откуда события. Демон (wa/wa-daemon.js) складывает входящие в очередь, но только пока лежит файл-флаг MEMORY_DIR/wa/inbox.json (его пишет sync_flag():
модуль secretary включён и список «читать» не пуст), и текст хранит только у номеров из списка. Python раз в пару секунд забирает очередь (poll_once), отбрасывает
всё лишнее (каждое отброшенное событие = строка в журнале с причиной), склеивает пачку и отдаёт событие в очередь «Кореш говорит первым» (proactive.py, ТЗ-35):
звонок через сценарий Алисы, тихие часы, лимиты 1/10 мин и 6/час, «человек уже в сессии», фолбэк в Телеграм с причиной. Первая фраза новой сессии -- готовый
текст («<имя> написала: «…». Ответить?»), модель не зовётся: у навыка на ответ 4,5 с.

Голос (без страниц): «читай мне сообщения от жены» (контакт ищет СЕРВЕР: падежи и прозвища, как у отправки WhatsApp) -> переспрос «Читать вслух сообщения от …, правильно?»
-> «да» -> человек в списке. Так же «перестань читать маму», «кого ты читаешь вслух?», «не читай вотсап сегодня» / «больше не читай» / «можешь читать».
Ответ: «ответь ей: скоро буду» уходит ТОЛЬКО автору последнего озвученного события (адресата выбирает сервер по id события, а не модель и не слова хозяина),
только после «Пишу …: «…». Отправить?» и «да», через ту же отправку WhatsApp (лимиты, связь, черновик на две минуты).

Что где лежит. Список «читать», кто озвучен, курсор очереди и отметка «уведомление отправлено» -- MEMORY_DIR/secretary.json (Volume, права 600). Текстов чужих
сообщений на диске в этом файле НЕТ: они живут в оперативке, пока событие не озвучено и ещё 30 минут после (для «Прочитать?»). В журнал идут причина, имя и
последние четыре цифры номера; тексты и номера целиком -- никогда. Без флага модуля secretary входящие не принимаются вообще, а на просьбу честный ответ
«подключается отдельно». Время подменяется в воротах (_now)."""
import asyncio
import json
import os
import re
import time

import config as C
import memory
import notify
import proactive
import wa

FILE = "secretary.json"
GLUE_SEC = 60               # сообщения одного человека в пределах минуты = одно событие
GLUE_WAIT = 8.0             # копим пачку столько секунд, потом звоним
SPOKEN_TEXT_MAX = 300       # одно длинное сообщение вслух читаем до этого предела
REPLY_TTL = 1800            # отвечать можно тем, кого озвучили за последние 30 минут
PENDING_TTL = 120           # переспрос «правильно?» / «Отправить?» живёт две минуты
KEEP_SEC = 2 * 3600         # запись «кого озвучили» живёт два часа
POLL_SEC = 2.0
TEST_GAP = 15               # «Проверить» не чаще раза в 15 секунд
ANNOUNCE_TRIES = 5          # уведомление «Новое: читаю тебе WhatsApp вслух» пробуем не больше пяти раз (раз в час)
ANNOUNCE_GAP = 3600
TEST_TEXT = "Это проверка чтения WhatsApp: на самом деле никто не писал"
NEW_TEXT = "Новое: читаю тебе WhatsApp вслух. Скажи мне «читай сообщения от жены» или нажми ссылку."
OFF_SAY = "Чтение WhatsApp вслух у меня подключается отдельно. Напиши в бот, он подскажет."
MODES = ("text", "name_only")


def _now() -> float:
    return time.time()


# ---------- хранилище ----------
_S = {"path": "", "data": None, "seq": 0}
_PENDING: dict = {}          # uid -> переспрос; только оперативка
_HELD: dict = {}             # номер -> копится пачка (ещё не звонили)
_OPEN: dict = {}             # номер -> событие, уже отданное в очередь звонков и ещё не озвученное (в него вливаются новые сообщения)
_MSGS: dict = {}             # sec_id -> [{kind, text}] только в оперативке: для «Прочитать?»
_TEST = {"started": 0.0}


def reset_for_tests() -> None:
    _S.update(path="", data=None, seq=0)
    for d in (_PENDING, _HELD, _OPEN, _MSGS):
        d.clear()
    _TEST["started"] = 0.0


def reload() -> None:
    """Забыть оперативную копию: следующее обращение читает диск (как после перезапуска)."""
    _S.update(path="", data=None)
    for d in (_PENDING, _HELD, _OPEN, _MSGS):
        d.clear()


def _path() -> str:
    return os.path.join(C.memory_dir(), FILE)


def _data() -> dict:
    p = _path()
    if _S["data"] is None or _S["path"] != p:
        d: dict = {}
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        if not isinstance(d, dict):
            d = {}
        people = []
        for x in d.get("list") or []:
            if isinstance(x, dict) and re.fullmatch(r"\d{10,15}", str(x.get("number") or "")) and str(x.get("name") or "").strip():
                people.append({"number": str(x["number"]), "name": str(x["name"])[:60], "mode": x.get("mode") if x.get("mode") in MODES else "text",
                               "as": str(x.get("as") or "")[:40], "g": x.get("g") if x.get("g") in ("m", "f") else "auto"})
        events = {k: v for k, v in (d.get("events") or {}).items() if isinstance(v, dict) and isinstance(k, str)} if isinstance(d.get("events"), dict) else {}
        _S.update(path=p, data={"list": people, "off": d.get("off") is True, "pause_until": float(d.get("pause_until") or 0), "events": events,
                                "cursor": int(d.get("cursor") or 0), "notified": d.get("notified") is True,
                                "notify_tries": int(d.get("notify_tries") or 0), "notify_at": float(d.get("notify_at") or 0)})
    return _S["data"]


def _save() -> bool:
    d = _data()
    now = _now()
    d["events"] = {k: v for k, v in d["events"].items() if now - float(v.get("at") or 0) < KEEP_SEC}
    try:
        memory.atomic_write(_path(), json.dumps(d, ensure_ascii=False))
        try:
            os.chmod(_path(), 0o600)
        except OSError:
            pass
        return True
    except OSError as e:
        print(f"secretary: диск недоступен ({type(e).__name__})", flush=True)
        return False


def module_on() -> bool:
    return C.module_on("secretary")


# ---------- список «читать» ----------
def tail10(num) -> str:
    return re.sub(r"\D", "", str(num or ""))[-10:]


def people() -> list:
    return [dict(p) for p in _data()["list"]]


def _person(number) -> dict | None:
    t = tail10(number)
    return next((p for p in _data()["list"] if tail10(p["number"]) == t), None) if t else None


def add_person(number: str, name: str, mode: str = "text", alias: str = "", g: str = "auto") -> dict:
    """Добавить человека (или обновить режим/имя). number -- цифры с кодом страны, name -- как он записан в контактах."""
    num = re.sub(r"\D", "", str(number or ""))
    nm = re.sub(r"\s+", " ", str(name or "")).strip()[:60]
    mode = mode if mode in MODES else "text"
    d = _data()
    cur = _person(num)
    if cur is None:
        cur = {"number": num, "name": nm, "mode": mode, "as": str(alias or "").strip()[:40], "g": g if g in ("m", "f") else "auto"}
        d["list"].append(cur)
    else:
        cur.update(name=nm or cur["name"], mode=mode)
        if alias:
            cur["as"] = str(alias).strip()[:40]
    _save()
    sync_flag()
    return dict(cur)


PHONE_MSG = "Это не похоже на номер. Нужен номер с кодом страны, например +7 707 101 99 36, или 8 707 101 99 36."


def parse_phone(raw) -> str:
    """Что вписал человек -> цифры с кодом страны (как хранит wa.normalize_number: 8XXXXXXXXXX -> 7XXXXXXXXXX, десять цифр -> 7 + цифры) или ''.
    Допустимы только цифры, пробелы, скобки, дефисы, точки и ведущий «+»: буквы и мусор -> ''."""
    s = str(raw or "").strip()
    if not re.fullmatch(r"\+?[\d\s\-().]{6,30}", s):
        return ""
    return wa.normalize_number(s)


def fmt_phone(num) -> str:
    d = re.sub(r"\D", "", str(num or ""))
    if len(d) == 11 and d.startswith("7"):
        return f"+7 {d[1:4]} {d[4:7]} {d[7:9]} {d[9:]}"
    return "+" + d if d else ""


def is_phone_label(s) -> bool:
    return bool(re.fullmatch(r"[+\d\s\-().]+", str(s or "").strip()))


def add_by_number(raw, name: str = "", mode: str = "text", g: str = "auto") -> dict:
    """Добавить человека по номеру (ТЗ-36 п.2: «выбор из контактов ИЛИ ввод номера»). Он может быть и не в контактах WhatsApp.
    -> {ok, person, in_contacts, existed} | {ok: False, error, message}. Имя: из контактов, иначе вписанное, иначе сам номер."""
    num = parse_phone(raw)
    if not num:
        return {"ok": False, "error": "bad_number", "message": PHONE_MSG}
    if mode not in MODES:
        return {"ok": False, "error": "bad_mode", "message": "Режим: читать текст или только кто написал."}
    nm = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]+", " ", str(name or ""))).strip()[:40]
    t = tail10(num)
    contact = next((c for c in wa.book() if tail10(c["number"]) == t), None)
    full = contact["name"] if contact else (nm or fmt_phone(num))
    existed = _person(num) is not None
    person = add_person(contact["number"] if contact else num, full, mode, nm if (nm and nm != full) else "", g)
    return {"ok": True, "person": person, "in_contacts": contact is not None, "existed": existed}


def remove_person(number: str) -> bool:
    d = _data()
    cur = _person(number)
    if cur is None:
        return False
    d["list"].remove(cur)
    _save()
    sync_flag()
    return True


def set_person(number: str, mode: str | None = None, alias: str | None = None, g: str | None = None) -> bool:
    cur = _person(number)
    if cur is None:
        return False
    if mode in MODES:
        cur["mode"] = mode
    if alias is not None:
        cur["as"] = str(alias).strip()[:40]
    if g in ("auto", "m", "f"):
        cur["g"] = g
    _save()
    return True


def display(p: dict) -> str:
    return (p.get("as") or p["name"]).strip()


_MALE_A = {"никита", "илья", "данила", "лука", "фома", "кузьма", "ваня", "вася", "коля", "петя", "миша", "гоша", "дима", "паша", "леша", "лёша", "серёжа", "сережа",
           "саня", "саша", "толя", "федя", "костя", "юра", "жора", "гриша", "боря", "витя", "толик", "тема", "андрюша", "слава", "папа", "дедушка", "дядя", "дед", "батя",
           "брат", "сын", "муж"}


def gender_of(p: dict) -> str:
    """m | f. Явный выбор на странице главнее; иначе по имени: на «а/я» женское, кроме мужских уменьшительных (Ваня, Дима, Никита, папа…)."""
    if p.get("g") in ("m", "f"):
        return p["g"]
    first = (re.findall(r"[^\W\d_]+", display(p).lower()) or [""])[0]
    if first in _MALE_A:
        return "m"
    return "f" if first.endswith(("а", "я")) else "m"


def _gen_word(w: str, fem: bool) -> str:
    """Родительный падеж одного слова имени: «Оля» -> «Оли», «Сергей» -> «Сергея», «Карпова» -> «Карповой». Без претензий на идеал: это только фраза переспроса."""
    lw = w.lower()
    if len(w) < 3:
        return w
    if fem and lw.endswith(("ова", "ева", "ёва", "ина", "ына")):
        return w[:-1] + "ой"
    if fem and lw.endswith(("ская", "цкая")):
        return w[:-2] + "ой"
    last = lw[-1]
    if last == "а":
        return w[:-1] + ("и" if lw[-2] in "гкхжчшщ" else "ы")
    if last == "я":
        return w[:-1] + "и"
    if last == "й":
        return w[:-1] + "я"
    if last == "ь":
        return w[:-1] + ("и" if fem else "я")
    if last in "бвгджзклмнпрстфхцчшщ":
        return w + "а"
    return w


def genitive(name: str, fem: bool) -> str:
    return " ".join(_gen_word(w, fem) for w in str(name).split())


# ---------- файл-флаг для демона ----------
def sync_flag() -> None:
    """Демон пишет входящие только пока лежит inbox.json с номерами списка. Модуль выключен или список пуст -> файла нет -> демон входящие не читает."""
    f = wa.inbox_flag_file()
    nums = sorted({tail10(p["number"]) for p in _data()["list"]})
    if not (module_on() and nums):
        try:
            f.unlink()
        except OSError:
            pass
        return
    try:
        cur = json.loads(f.read_text(encoding="utf-8")).get("numbers")
    except (OSError, ValueError, AttributeError):
        cur = None
    if cur == nums:
        return
    try:
        wa._ensure_dirs()
        memory.atomic_write(str(f), json.dumps({"numbers": nums, "at": int(_now())}))
        os.chmod(f, 0o600)
    except OSError as e:
        print(f"secretary: флаг для демона не записался ({type(e).__name__})", flush=True)


# ---------- журнал ----------
def _mask(number) -> str:
    d = re.sub(r"\D", "", str(number or ""))
    return ("••" + d[-4:]) if d else "••"


def _who(number, push="") -> str:
    """Для журнала: имя из списка/контактов (или имя профиля) и последние четыре цифры. Тексты и номера целиком не пишем."""
    p = _person(number)
    if p:
        nm = display(p)
        return f"{'номер' if is_phone_label(nm) else nm} {_mask(number)}"
    t = tail10(number)
    for c in wa.book():
        if tail10(c["number"]) == t:
            return f"{c['name']} {_mask(number)}"
    nick = re.sub(r"[^\w ]+", "", str(push or "")).strip()[:30] or "?"
    return f"{nick} {_mask(number)}"


def _drop(reason: str, number="", push="") -> None:
    who = (" " + _who(number, push)) if number else ""
    print(f"secretary: отброшено ({reason}){who}", flush=True)


# ---------- приём событий демона ----------
SCOPE_REASON = {"group": "group", "status": "status", "broadcast": "broadcast", "newsletter": "newsletter", "self": "from_me", "stale": "stale"}


def ingest(events: list, now: float | None = None) -> int:
    """Разобрать события демона. -> сколько принято в работу (остальные отброшены с причиной в журнале)."""
    now = _now() if now is None else now
    if not module_on():
        return 0
    took = 0
    for e in events or []:
        if not isinstance(e, dict):
            continue
        scope = str(e.get("scope") or "user")
        number = re.sub(r"\D", "", str(e.get("number") or ""))
        if e.get("from_me") is True:
            _drop("from_me", number)
            continue
        if scope != "user":
            _drop(SCOPE_REASON.get(scope, scope), number if scope == "stale" else "")
            continue
        if not number:
            _drop("no_number", "", e.get("push", ""))
            continue
        person = _person(number)
        if person is None:
            _drop("not_in_list", number, e.get("push", ""))
            continue
        kind = re.sub(r"[^a-z]", "", str(e.get("kind") or "text"))[:12] or "text"
        text = _clean(e.get("text"), 2000) if (person["mode"] == "text" and kind == "text") else ""
        _add_message(person, {"kind": kind, "text": text}, now)
        took += 1
    return took


def _clean(s, limit: int) -> str:
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", str(s or ""))
    return re.sub(r"[ \t]{2,}", " ", s).strip()[:limit]


def _new_id(now: float) -> str:
    _S["seq"] += 1
    return f"w{int(now * 1000)}-{_S['seq']}"


def _add_message(person: dict, msg: dict, now: float) -> None:
    num = person["number"]
    h = _HELD.get(num)
    if h is not None and now - h["last_at"] <= GLUE_SEC:
        h["msgs"].append(msg)
        h["last_at"] = now
        return
    o = _OPEN.get(num)
    if o is not None and now - o["last_at"] <= GLUE_SEC and proactive.find_sec(o["sec"]) is not None:
        o["msgs"].append(msg)
        o["last_at"] = now
        _MSGS[o["sec"]] = o["msgs"]
        body, ask = compose_parts(o)
        proactive.amend_sec(o["sec"], body, ask, tg_text(o))
        print(f"secretary: ещё сообщение влилось в то же событие ({_who(num)})", flush=True)
        return
    _HELD[num] = {"sec": _new_id(now), "number": num, "name": display(person), "mode": person["mode"], "g": gender_of(person), "msgs": [msg],
                  "first_at": now, "last_at": now, "ready_at": now + GLUE_WAIT, "test": False}


# ---------- фразы ----------
_MEDIA = {"audio": "голосовое", "image": "фото", "video": "видео", "sticker": "стикер", "document": "файл", "location": "геопозицию", "contact": "контакт"}


def plural_msgs(n: int) -> str:
    if 11 <= n % 100 <= 14:
        w = "сообщений"
    elif n % 10 == 1:
        w = "сообщение"
    elif 2 <= n % 10 <= 4:
        w = "сообщения"
    else:
        w = "сообщений"
    return f"{n} {w}"


def _verbs(g: str) -> tuple:
    return ("написала", "накидала", "прислала") if g == "f" else ("написал", "накидал", "прислал")


def compose_parts(h: dict) -> tuple:
    """(что сказать, вопрос в конце) про одно событие."""
    wrote, tossed, sent = _verbs(h["g"])
    name, msgs = h["name"], h["msgs"]
    n = len(msgs)
    if h["mode"] == "name_only":
        if n == 1:
            return f"{name} {wrote}. Текст я не читаю", "Ответить?"
        return f"{name} {tossed} {plural_msgs(n)}. Текст я не читаю", "Ответить?"
    if n == 1:
        m = msgs[0]
        if m["kind"] != "text":
            return f"{name} {sent} {_MEDIA.get(m['kind'], 'что-то не текстом')}. Я такое не открываю", "Ответить?"
        t = m["text"]
        if len(t) > SPOKEN_TEXT_MAX:
            return f"{name} {wrote} длинное: «{_q(t[:SPOKEN_TEXT_MAX].rstrip())}…». Это начало", "Ответить?"
        return f"{name} {wrote}: «{_q(t)}»", "Ответить?"
    return f"{name} {tossed} {plural_msgs(n)}", "Прочитать?"


def _q(t: str) -> str:
    return t.replace("«", '"').replace("»", '"')


def tg_text(h: dict) -> str:
    """Что уходит в Телеграм, если вслух не прочитали. name_only текста не содержит физически."""
    name = h["name"]
    if h["mode"] == "text":
        texts = [m["text"] for m in h["msgs"] if m["text"]]
        if texts:
            return (f"WhatsApp, {name}: " + " | ".join(texts))[:900]
        return f"WhatsApp: {name} прислал(а) не текст"
    return f"WhatsApp: {name} написал(а) вам"


def _read_aloud(sec: str) -> str:
    msgs = _MSGS.get(sec)
    if not msgs:
        return "Не помню, что там было, открой WhatsApp."
    parts = []
    for m in msgs:
        if m["kind"] == "text" and m["text"]:
            parts.append("«" + _q(m["text"][:SPOKEN_TEXT_MAX]) + "»")
        else:
            parts.append(f"({_MEDIA.get(m['kind'], 'не текст')}, не открываю)")
    return (" ".join(parts))[:700] + ". Ответить?"


# ---------- решение и доставка ----------
def _kids() -> bool:
    import modules
    return modules.kids_on()


def _tg_reason(now: float, test: bool) -> str | None:
    if test:
        return None
    d = _data()
    if _kids():
        return "kids"
    if d["off"]:
        return "sec_off"
    if now < d["pause_until"]:
        return "sec_paused"
    return None


async def _deliver(h: dict, now: float) -> str:
    body, ask = compose_parts(h)
    d = _data()
    d["events"][h["sec"]] = {"number": h["number"], "name": h["name"], "at": now, "spoken_at": 0.0, "test": bool(h["test"])}
    _MSGS[h["sec"]] = h["msgs"]
    status = await proactive.deliver(proactive.WA_KIND, tg_text(h), item=body, calls=True, extra={"ask": ask, "sec": h["sec"], "test": bool(h["test"])},
                                     tg_reason=_tg_reason(now, h["test"]), force=bool(h["test"]))
    if status.startswith("tg:"):
        d["events"].pop(h["sec"], None)
        _MSGS.pop(h["sec"], None)
    else:
        _OPEN[h["number"]] = h
    _save()
    return status


async def tick(now: float | None = None) -> int:
    """Созревшие пачки -> в очередь звонков. Зовёт фоновый цикл раз в пару секунд (и ворота)."""
    now = _now() if now is None else now
    n = 0
    for num in [k for k, h in _HELD.items() if now >= h["ready_at"]]:
        h = _HELD.pop(num)
        await _deliver(h, now)
        n += 1
    for num in [k for k, o in _OPEN.items() if now - o["last_at"] > GLUE_SEC and proactive.find_sec(o["sec"]) is None]:
        _OPEN.pop(num, None)
    for k in [k for k in _MSGS if now - float(_data()["events"].get(k, {}).get("at") or 0) > REPLY_TTL * 2]:
        _MSGS.pop(k, None)
    return n


async def poll_once(now: float | None = None) -> int:
    """Забрать очередь у демона. -> сколько событий пришло. Нет модуля / списка / привязки / ответа демона -> 0, без исключений."""
    now = _now() if now is None else now
    d = _data()
    if not (module_on() and d["list"] and wa.enabled() and wa.linked()):
        return 0
    sync_flag()
    j = await wa.inbox(d["cursor"])
    if not j:
        return 0
    evs = [e for e in j["events"] if isinstance(e, dict) and isinstance(e.get("seq"), int)]
    if not evs:
        return 0
    ingest(evs, now)
    d["cursor"] = max([d["cursor"]] + [e["seq"] for e in evs])
    _save()
    return len(evs)


async def loop() -> None:
    """Фоновая задача сервера: опрос демона, склейка, уведомление о новом модуле. Любая ошибка проглатывается, цикл не умирает."""
    try:
        while True:
            try:
                sync_flag()                      # модуль выключили (рубильник) -> флаг для демона исчезает, демон перестаёт писать входящие
                if module_on():
                    await poll_once()
                    await tick()
                    await maybe_announce()
            except Exception as e:  # noqa: BLE001
                print(f"secretary: цикл споткнулся ({type(e).__name__})", flush=True)
            await asyncio.sleep(POLL_SEC)
    except asyncio.CancelledError:
        raise


# ---------- уведомление «Новое» ----------
async def maybe_announce(now: float | None = None) -> str:
    """Флаг secretary включился -> ОДИН раз пишем в Телеграм через очередь лицензий (ссылку-кнопку на /setup/sekretar ставит сервер лицензий).
    off (модуля нет) | done (уже было / отступились) | wait | skip (установка не активирована, попробуем позже) | sent | queued | retry."""
    now = _now() if now is None else now
    if not module_on():
        return "off"
    d = _data()
    if d["notified"] or d["notify_tries"] >= ANNOUNCE_TRIES:
        return "done"
    if d["notify_tries"] and now - d["notify_at"] < ANNOUNCE_GAP:
        return "wait"
    res = await notify.post("secretary_new", NEW_TEXT)
    if res == "skip":
        return "skip"
    d["notify_tries"] += 1
    d["notify_at"] = now
    if res in ("sent", "queued"):
        d["notified"] = True
        _save()
        print("secretary: уведомление о новом модуле ушло", flush=True)
        return res
    _save()
    return "retry"


# ---------- кого озвучили, кому можно ответить ----------
def after_claim(uid: str, claimed: list, now: float | None = None) -> bool:
    """main.handle вызывает после того, как proactive.claim() зачитал события. Запоминаем, кого озвучили (адресат для «ответь ей»), и готовим вопрос «Прочитать?» / «Ответить?».
    -> True, если вслух прозвучали чужие слова (модель должна знать, что это не команды)."""
    now = _now() if now is None else now
    d = _data()
    foreign = False
    spoken = []
    for c in claimed:
        if c.get("kind") != proactive.WA_KIND:
            continue
        foreign = True
        sec = c.get("sec") or ""
        ev = d["events"].get(sec)
        if c.get("test") or ev is None:
            d["events"].pop(sec, None)
            _MSGS.pop(sec, None)
            continue
        ev["spoken_at"] = now
        spoken.append((sec, c.get("ask") or ""))
    if spoken:
        _save()
        _OPEN.clear()
    if len(spoken) == 1:
        sec, ask = spoken[0]
        _PENDING[uid] = {"kind": "read" if ask == "Прочитать?" else "answer_ask", "sec": sec, "at": now}
    elif claimed:
        _PENDING.pop(uid, None)
    return foreign


def last_spoken(now: float | None = None) -> dict | None:
    now = _now() if now is None else now
    best = None
    for sec, ev in _data()["events"].items():
        sp = float(ev.get("spoken_at") or 0)
        if ev.get("test") or sp <= 0 or now - sp > REPLY_TTL:
            continue
        if best is None or sp >= best[1]:
            best = (sec, sp, ev)
    return None if best is None else {"sec": best[0], "number": best[2]["number"], "name": best[2]["name"], "at": best[1]}


# ---------- голосовые команды ----------
_WA = r"(?:вот?сап\w*|ватсап\w*|вацап\w*|воцап\w*|whatsapp\w*)"
_FILL = r"(?:(?:ну|слушай|кореш|алиса|пожалуйста|давай|так|а)\W+)*"
_TAIL = r"(?:\W+(?:пожалуйста|мне))*\W*"
OFF_TODAY_RE = re.compile(
    _FILL + r"(?:не\W+(?:читай|читайте|зачитывай|озвучивай)\W+(?:мне\W+)?" + _WA + r"(?:\W+мне)?(?:\W+сегодня)?"
    r"|сегодня\W+(?:мне\W+)?не\W+(?:читай|зачитывай|озвучивай)\W+(?:мне\W+)?" + _WA + r")" + _TAIL)
OFF_RE = re.compile(
    _FILL + r"(?:больше\W+не\W+(?:читай|зачитывай|озвучивай)\W+(?:мне\W+)?" + _WA +
    r"|(?:выключи|отключи)\W+(?:чтение\W+)?" + _WA + r"(?:\W+секретар\w+)?)" + _TAIL)
ON_RE = re.compile(
    _FILL + r"(?:(?:ты\W+)?(?:можешь|можно|начинай|продолжай)\W+(?:мне\W+)?(?:читать|зачитывать|озвучивать)(?:\W+(?:мне|" + _WA + r"|снова|опять|дальше))*"
    r"|давай\W+(?:читай|зачитывай|озвучивай)\W+(?:мне\W+)?" + _WA +
    r"|(?:читай|зачитывай|озвучивай)\W+(?:мне\W+)?" + _WA + r"\W+(?:снова|опять)"
    r"|(?:включи|верни)\W+(?:чтение\W+)?" + _WA + r"(?:\W+секретар\w+)?)" + _TAIL)


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower().replace("ё", "е"))


def command(low: str):
    """off_today | off | on | None. Только целой фразой, чтобы «прочитай вотсап жены» и «не читай вотсап от мамы» сюда не попали."""
    s = norm(low)
    if OFF_RE.fullmatch(s):
        return "off"
    if OFF_TODAY_RE.fullmatch(s):
        return "off_today"
    if ON_RE.fullmatch(s):
        return "on"
    return None


_MSGW = r"(?:(?:все\W+|новые\W+)?(?:сообщения|смс|письма)|" + _WA + r")"
_WHO = r"(?P<who>\S[^.!?]{0,60}?)"
ADD_RE = re.compile(_FILL + r"(?:читай|читайте|зачитывай|озвучивай)\W+(?:мне\W+)?" + _MSGW + r"\W+(?:(?:в|из)\W+" + _WA + r"\W+)?(?:(?:от|из)\W+|с\W+(?=номер))" + _WHO + _TAIL)
NAME_ONLY_RE = re.compile(_FILL + r"(?:читай|говори|сообщай|озвучивай)\W+(?:мне\W+)?только\W+(?:кто|когда)\W+(?:пишет|написал\w*|писал\w*)\W+(?:(?:мне|в\W+" + _WA + r")\W+)*(?:от\W+)?" + _WHO + _TAIL)
REMOVE_RE = re.compile(
    _FILL + r"(?:(?:перестань|хватит|прекрати)\W+(?:мне\W+)?(?:читать|зачитывать|озвучивать)\W+(?:мне\W+)?(?:" + _MSGW + r"\W+)*(?:(?:от|из)\W+)?" + _WHO
    + r"|(?:больше\W+)?не\W+(?:читай|зачитывай|озвучивай)\W+(?:мне\W+)?(?:больше\W+)?" + _MSGW + r"\W+(?:от|из)\W+" + _WHO.replace("<who>", "<who2>") + r")" + _TAIL)
LIST_RE = re.compile(_FILL + r"(?:(?:от\W+)?кого|чьи\W+сообщения)\W+(?:ты\W+)?(?:читаешь|зачитываешь|озвучиваешь)(?:\W+(?:вслух|мне|сообщения|" + _WA + r"))*" + _TAIL)
REPLY_PRON_RE = re.compile(_FILL + r"(?:ответь|ответьте|ответить)\W+(?:(?:ей|ему|им|в\W+ответ)\b\W*)(?:что\W+)?(?P<text>.*)", re.I)
REPLY_NAME_RE = re.compile(_FILL + r"(?:ответь|ответьте)\W+(?P<who>[^\W\d_]+(?:\W+[^\W\d_]+)?)\s*[:,—-]\s*(?P<text>.+)", re.I)
_PRON = ("ей", "ему", "им")
_KEYWORD = re.compile(r"сообщени|вот?сап|ватсап|вацап|воцап|whatsapp|смс")


def _tidy_who(w: str) -> str:
    w = norm(w)
    w = re.sub(r"(?:\W+(?:пожалуйста|мне|вслух))+$", "", w).strip(" ,.!?")
    return w


SAY = {
    "empty_list": "Пока никого не читаю вслух. Скажи «читай сообщения от жены», и я добавлю.",
    "not_linked": "WhatsApp у меня ещё не привязан. Привяжи его на странице настройки, потом возвращайся.",
    "no_contacts": "Я пока не вижу ни одного контакта WhatsApp. Подожди пару минут после привязки и спроси ещё раз.",
    "none": "Не нашёл такого в контактах WhatsApp. Назови, как человек записан у тебя в телефоне.",
    "number": "Номера голосом я не принимаю. Назови имя из контактов.",
    "bad_number": "Не расслышал номер. Скажи его целиком с кодом, например «читай сообщения с номера 8 707 101 99 36».",
    "cancel": "Хорошо, ничего не меняю.",
    "no_target": "Мне некому отвечать: за последние полчаса я никого не зачитывал.",
}


def _wa_ready() -> str:
    """'' | ключ SAY: можно ли вообще искать контакты."""
    if not wa.enabled() or not wa.linked():
        return "not_linked"
    if not wa.book():
        return "no_contacts"
    return ""


def _find_in_list(query: str) -> tuple:
    """Поиск по списку «читать»: имя, «как называть» и прозвища. ('one', person) | ('many', [имена]) | ('none',)."""
    toks = wa._words(query)
    if not toks:
        return ("none",)
    nicks = C.wa_nicks()
    exact, part = [], []
    for p in _data()["list"]:
        best = ""
        for target in [p["name"], p.get("as") or ""] + list(nicks.get(p["number"], [])):
            if not target:
                continue
            m = wa._match(toks, wa._words(target))
            if m == "exact":
                best = "exact"
                break
            if m == "part":
                best = best or "part"
        (exact if best == "exact" else part if best == "part" else []).append(p)
    hits = exact or part
    if len(hits) == 1:
        return ("one", hits[0])
    if len(hits) > 1:
        return ("many", sorted(display(x) for x in hits))
    return ("none",)


def _names_line(items: list) -> str:
    return ", ".join(items[:8])


def list_say() -> str:
    d = _data()
    if not d["list"]:
        return SAY["empty_list"]
    full = [display(p) for p in d["list"] if p["mode"] == "text"]
    only = [display(p) for p in d["list"] if p["mode"] == "name_only"]
    out = []
    if full:
        out.append("Читаю вслух сообщения от: " + _names_line(full) + ".")
    if only:
        out.append("Только говорю, что написал(а), без текста: " + _names_line(only) + ".")
    if d["off"]:
        out.append("Но чтение сейчас выключено: ты просил не читать.")
    elif _now() < d["pause_until"]:
        out.append("Но на сегодня ты просил не читать.")
    return " ".join(out)


def _midnight_after(ts: float) -> float:
    from datetime import timedelta
    d = proactive._local(ts)
    return (d + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def apply_command(cmd: str, now: float | None = None) -> str:
    now = _now() if now is None else now
    d = _data()
    if cmd == "off_today":
        d["pause_until"] = _midnight_after(now)
        _save()
        return "Понял, сегодня вотсап не читаю. Что прилетит, скину в Телеграм."
    if cmd == "off":
        d["off"] = True
        d["pause_until"] = 0.0
        _save()
        return "Понял, вотсап вслух больше не читаю, всё будет в Телеграме. Захочешь обратно, скажи «можешь читать вотсап»."
    d["off"] = False
    d["pause_until"] = 0.0
    _save()
    extra = ""
    if not d["list"]:
        extra = " Только список пока пуст: скажи «читай сообщения от жены», и я добавлю."
    elif not proactive.configured():
        extra = " Но сценарий-звонок у меня не настроен, без него я смогу только писать в Телеграм."
    return "Хорошо, читаю вотсап от своих. Но не ночью и не чаще раза в десять минут." + extra


async def _link_say() -> str:
    """Сценарий-звонок не настроен: честно говорим об этом и шлём ссылку на инструкцию в Телеграм. Навыку на ответ 4,5 с: ждём сервер недолго."""
    try:
        res = await asyncio.wait_for(notify.secretary_link(), timeout=2.5)
    except Exception:  # noqa: BLE001
        res = "fail"
    if res in ("queued", "again"):
        return " Но сценарий-звонок у меня не настроен, без него я смогу только писать в Телеграм. Ссылку на инструкцию скинул в Телеграм."
    return " Но сценарий-звонок у меня не настроен, без него я смогу только писать в Телеграм. Ссылку скинуть не вышло, она в боте на кнопке «⚙️ Мой Кореш»."


# ---------- разговор ----------
_PASS = object()          # переспрос снят, реплику дальше обрабатывает обычный путь


async def _apply_pending(uid: str, p: dict, phrase: str, s: str, now: float):
    kind = p["kind"]
    aff = wa.is_affirmation(phrase)
    neg = bool(wa.NEG_RE.search(s)) and not aff
    if kind in ("add", "remove"):
        if aff:
            _PENDING.pop(uid, None)
            if kind == "add":
                person = add_person(p["number"], p["name"], p["mode"])
                phone = is_phone_label(display(person))
                gen = display(person) if phone else genitive(display(person), gender_of(person) == 'f')
                if p["mode"] == "text":
                    msg = f"Готово, читаю вслух сообщения {'с номера' if phone else 'от'} {gen}."
                else:
                    msg = f"Готово, про {'номер ' if phone else ''}{gen} буду говорить только, что написал(а), без текста."
                if not proactive.configured():
                    msg += await _link_say()
                return msg
            cur = _person(p["number"])
            if cur is not None:
                remove_person(p["number"])
            return f"Готово, сообщения от {genitive(p['name'], p.get('fem', False))} больше не читаю."
        _PENDING.pop(uid, None)
        return "Хорошо, не трогаю." if neg else _PASS
    if kind == "read":
        if aff or re.fullmatch(r"(?:ну\W+)?(?:прочитай|читай|зачитай)(?:\W+(?:их|мне|пожалуйста))*", s):
            _PENDING[uid] = {"kind": "answer_ask", "sec": p["sec"], "at": now}
            return _read_aloud(p["sec"])
        _PENDING.pop(uid, None)
        return "Ладно, не читаю." if neg else _PASS
    if kind == "answer_ask":
        if aff or re.fullmatch(r"(?:ну\W+)?ответь(?:\W+(?:ей|ему|им))?", s):
            _PENDING[uid] = {"kind": "ask_text", "sec": p["sec"], "at": now}
            return "Что ответить?"
        _PENDING.pop(uid, None)
        return "Хорошо, не отвечаю." if neg else _PASS
    if kind == "ask_text":
        _PENDING.pop(uid, None)
        if neg or re.fullmatch(r"(?:ну\W+)?(?:ничего|не\W+надо|отмена|отбой)", s):
            return "Хорошо, не отвечаю."
        return await _reply_draft(uid, phrase, now)
    if kind == "reply":
        _PENDING.pop(uid, None)
        wa.note_turn(uid, phrase)
        if aff:
            res = await wa.tool_confirm(uid, phrase)
            if res.ok:
                return f"Отправил {p['name']}."
            return "Не отправил: " + re.sub(r"^error:\s*(?:NO_CONFIRM:\s*)?", "", res.text).split(". Ничего")[0].rstrip(".") + "."
        wa.clear_draft(uid)
        return "Хорошо, не отправляю." if neg else _PASS
    _PENDING.pop(uid, None)
    return _PASS


async def _reply_draft(uid: str, text: str, now: float) -> str:
    """Черновик ответа автору ПОСЛЕДНЕГО озвученного события: адресата выбирает сервер по id события. Дальше «да» -> wa.tool_confirm (лимиты, связь, ровно этот черновик)."""
    tgt = last_spoken(now)
    if tgt is None:
        return SAY["no_target"]
    body = wa.clean_text(text)
    body = re.sub(r"^(?:что\W+)", "", body, flags=re.I).strip()
    if not body:
        _PENDING[uid] = {"kind": "ask_text", "sec": tgt["sec"], "at": now}
        return "Что ответить?"
    if len(body) > wa.MAX_TEXT:
        return f"Длинновато: в WhatsApp уйдёт не больше {wa.MAX_TEXT} знаков, сократи."
    if not wa.enabled() or not wa.linked():
        return SAY["not_linked"]
    problem = await wa._connection_problem()
    if problem:
        return f"Не могу отправить: {problem}."
    lim = wa.limit_reason()
    if lim:
        return f"Не могу отправить: {lim}."
    wa.note_turn(uid, text)
    wa.DRAFTS[uid] = {"name": tgt["name"], "number": tgt["number"], "text": body, "ts": wa._now(), "turn": wa.TURNS.get(uid, 0)}      # возраст черновика считает wa по своим часам
    _PENDING[uid] = {"kind": "reply", "sec": tgt["sec"], "name": tgt["name"], "at": now}
    print(f"secretary: черновик ответа ({_who(tgt['number'])})", flush=True)
    return f"Пишу {tgt['name']}: «{body}». Отправить?"


def _resolve(who: str) -> tuple:
    """Контакт по словам хозяина. ('one', имя, номер) | ('many', [имена]) | ('none',) | ('number',) | ('err', ключ SAY)."""
    bad = _wa_ready()
    if bad:
        return ("err", bad)
    return wa.find_contact(who)


def _spoken_number(who: str):
    """None -- в словах хозяина нет номера; '' -- номер есть, но не годится; иначе цифры с кодом страны."""
    w = re.sub(r"\b(?:номер\w*|телефон\w*)\b", " ", who).strip()
    if not re.fullmatch(r"[+\d\s\-().]+", w) or not re.search(r"\d", w):
        return None
    return parse_phone(w)


def _confirm_add_number(uid: str, num: str, mode: str, now: float) -> str:
    if not wa.enabled() or not wa.linked():
        return SAY["not_linked"]
    cur = _person(num)
    if cur is not None and cur["mode"] == mode:
        return f"Номер {fmt_phone(num)} у меня уже в списке."
    contact = next((c for c in wa.book() if tail10(c["number"]) == tail10(num)), None)
    name = contact["name"] if contact else fmt_phone(num)
    _PENDING[uid] = {"kind": "add", "number": contact["number"] if contact else num, "name": name, "mode": mode, "at": now, "phone": contact is None}
    who = f"с номера {fmt_phone(num)}" if contact is None else f"от {genitive(name, _fem_name(name))}"
    if mode == "name_only":
        return f"Говорить только, что написал(а) человек {who}, без текста, правильно?" if contact is None else f"Говорить только, что написал(а) {genitive(name, _fem_name(name))}, без текста, правильно?"
    return f"Читать вслух сообщения {who}, правильно?"


def _confirm_add(uid: str, who: str, mode: str, now: float) -> str | None:
    num = _spoken_number(who)
    if num is not None:
        return _confirm_add_number(uid, num, mode, now) if num else SAY["bad_number"]
    found = _resolve(who)
    if found[0] == "err":
        return SAY[found[1]]
    if found[0] == "number":
        return SAY["number"]
    if found[0] == "none":
        return SAY["none"]
    if found[0] == "many":
        return "Подходит несколько: " + _names_line(found[1]) + ". Скажи полное имя."
    _, name, number = found
    cur = _person(number)
    if cur is not None and cur["mode"] == mode:
        return f"{display(cur)} у меня уже в списке."
    fem = _fem_name(name)
    gen = genitive(name, fem)
    _PENDING[uid] = {"kind": "add", "number": number, "name": name, "mode": mode, "at": now}
    if mode == "name_only":
        return f"Говорить только, что написал(а) {gen}, без текста, правильно?"
    return f"Читать вслух сообщения от {gen}, правильно?"


def _fem_name(name: str) -> bool:
    return gender_of({"name": name, "as": "", "g": "auto"}) == "f"


def _confirm_remove(uid: str, who: str, keyword: bool, now: float) -> str | None:
    hit = _find_in_list(who)
    if hit[0] == "one":
        p = hit[1]
        _PENDING[uid] = {"kind": "remove", "number": p["number"], "name": display(p), "fem": gender_of(p) == "f", "at": now}
        return f"Перестать читать сообщения от {genitive(display(p), gender_of(p) == 'f')}, правильно?"
    if hit[0] == "many":
        return "Подходит несколько: " + _names_line(hit[1]) + ". Скажи полное имя."
    book = wa.find_contact(who) if (wa.enabled() and wa.linked()) else ("none",)
    if book[0] == "one":
        return f"{book[1]} и так не в списке. {list_say()}"
    if keyword:
        return "Такого человека в списке нет. " + list_say()
    return None


async def voice(uid: str, phrase: str, now: float | None = None) -> str | None:
    """Голосовая часть секретаря. -> готовый ответ или None (реплика не про секретаря, дальше обычный путь). Модель не зовётся."""
    now = _now() if now is None else now
    s = norm(phrase)
    if not s:
        return None
    if module_on() and _kids():               # детский режим: ребёнок не должен менять список «кого читать» и отвечать в WhatsApp; реплика идёт дальше как обычно
        _PENDING.pop(uid, None)
        return None
    p = _PENDING.get(uid)
    if p is not None and now - p["at"] > PENDING_TTL:
        _PENDING.pop(uid, None)
        if p["kind"] == "reply":
            wa.clear_draft(uid)
        p = None
    if p is not None and module_on():
        r = await _apply_pending(uid, p, phrase, s, now)
        if r is not _PASS:
            return r
    elif p is not None:
        _PENDING.pop(uid, None)
    on = module_on()

    def gate(text: str) -> str:
        return text if on else OFF_SAY

    cmd = command(s)
    if cmd:
        return gate(apply_command(cmd, now) if on else "")
    if LIST_RE.fullmatch(s):
        return gate(list_say() if on else "")
    m = NAME_ONLY_RE.fullmatch(s) or None
    mode = "name_only"
    if m is None:
        m = ADD_RE.fullmatch(s)
        mode = "text"
    if m is not None:
        who = _tidy_who(m.group("who"))
        if not who:
            return None
        return gate(_confirm_add(uid, who, mode, now) or SAY["none"] if on else "")
    m = REMOVE_RE.fullmatch(s)
    if m is not None:
        who = _tidy_who(m.group("who") or m.group("who2") or "")
        kw = bool(_KEYWORD.search(s))
        if not on:
            return OFF_SAY if kw else None
        return _confirm_remove(uid, who, kw, now)
    m = REPLY_PRON_RE.fullmatch(norm_keep(phrase))
    if m is not None:
        return gate(await _reply_draft(uid, m.group("text"), now) if on else "")
    m = REPLY_NAME_RE.fullmatch(norm_keep(phrase))
    if m is not None and norm(m.group("who")) not in _PRON:
        who = _tidy_who(m.group("who"))
        found = wa.find_contact(who) if (wa.enabled() and wa.linked()) else ("none",)
        if found[0] in ("one", "many"):
            if not on:
                return OFF_SAY
            tgt = last_spoken(now)
            if tgt is None:
                return SAY["no_target"]
            if found[0] == "one" and tail10(found[2]) == tail10(tgt["number"]):
                return await _reply_draft(uid, m.group("text"), now)
            return f"Отвечать могу только тому, кто писал последним: {tgt['name']}. Скажи «ответь ей …»."
    return None


def norm_keep(phrase: str) -> str:
    """Для разбора «ответь ей: …» регистр текста сохраняем (в WhatsApp уйдёт как сказано), лишь пробелы схлопываем. Совпадение слов ведём без регистра (re.I)."""
    return re.sub(r"\s+", " ", (phrase or "").strip())


# ---------- кнопка «Проверить» ----------
async def simulate(now: float | None = None) -> dict:
    """Имитирует входящее от ПЕРВОГО человека списка: Станция должна заговорить. Ничего никому не отправляется; ответить на это событие нельзя.
    Тихие часы, паузы и лимиты не мешают (человек сам нажал), нужен настроенный звонок."""
    now = _now() if now is None else now
    if not module_on():
        return {"ok": False, "error": "no_module", "message": "Чтение WhatsApp вслух не подключено."}
    ppl = _data()["list"]
    if not ppl:
        return {"ok": False, "error": "empty", "message": "Сначала включи хотя бы одного человека."}
    if not proactive.configured():
        return {"ok": False, "error": "no_scenario", "message": "Сначала настрой сценарий-звонок: инструкция ниже на этой странице."}
    if now - _TEST["started"] < TEST_GAP:
        return {"ok": False, "error": "too_fast", "message": "Подожди несколько секунд и нажми ещё раз."}
    _TEST["started"] = now
    p = ppl[0]
    msg = {"kind": "text", "text": TEST_TEXT if p["mode"] == "text" else ""}
    h = {"sec": _new_id(now), "number": p["number"], "name": display(p), "mode": p["mode"], "g": gender_of(p), "msgs": [msg],
         "first_at": now, "last_at": now, "ready_at": now, "test": True}
    pd = proactive._data()
    pd["sec_test"] = {"status": "waiting", "started": now}
    proactive._save()
    status = await _deliver(h, now)
    if status.startswith("tg:"):
        pd["sec_test"] = {"status": "failed", "at": now, "message": "Сценарий не запустился."}
        proactive._save()
        return {"ok": False, "error": "call_failed", "message": "Сценарий не запустился: " + (proactive._data()["last"].get("error") or "ошибка") + "."}
    return {"ok": True}


def test_status(now: float | None = None) -> dict:
    now = _now() if now is None else now
    t = dict(proactive._data().get("sec_test") or {})
    st = t.get("status") or "none"
    if st == "waiting" and now - float(t.get("started") or 0) > proactive.FRESH_SEC:
        st = "no_answer"
    return {"status": st, "message": t.get("message", "")}


def status() -> dict:
    d = _data()
    now = _now()
    return {"module": module_on(), "count": len(d["list"]), "enabled": not d["off"], "paused": now < d["pause_until"]}
