"""Страница настройки /setup: мастер на 7 шагов для того, кто ставит Кореша (мы на созвоне с клиентом).

Доступ: одноразовый SETUP_CODE из переменных -> сессия (httpOnly cookie, 2 часа). Нет SETUP_CODE -> /setup не существует (404).
После шага «Готово» страница закрывается; открыть снова можно только с кодом и явной кнопкой «перенастроить».
Настройки пишутся в MEMORY_DIR/config.json (config.save_values), переменные Railway их перекрывают.
Секреты (код, токены, ключи, кодовая фраза) в журнал не попадают: пишем только имена ключей и коды результатов."""
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

import activation
import brain
import config as C
import iot
import memory
import prompts

router = APIRouter()
PAGE = Path(__file__).resolve().parent / "setupwiz.html"

SESSION_TTL = 2 * 3600
MAX_FAILS = 5                  # неверных кодов подряд -> блок на LOCK_SECONDS (общий, не по адресу: адрес подделывается)
LOCK_SECONDS = 600
COOKIE = "koresh_setup"
SELFTEST_HEADER = "x-setup-selftest"
SELFTEST_NONCE = secrets.token_hex(16)      # знает только этот процесс: отличает «тест без Станции» от настоящего запроса Яндекса
SELFTEST_UID = "setup-selftest"
SELFTEST_RETRY_DELAY = 2.0
BRAIN_COOLDOWN = 3.0

_TRANSPORT = None              # ворота подставляют httpx.MockTransport (проверка токена умного дома и колбэк)
_sessions: dict[str, float] = {}
_fails = {"n": 0, "until": 0.0}
_heard = {"ts": 0.0, "text": "", "kind": ""}
_last_brain = {"ts": 0.0}


def _now() -> float:
    return time.time()


def reset_for_tests() -> None:
    _sessions.clear()
    _fails.update(n=0, until=0.0)
    _heard.update(ts=0.0, text="", kind="")
    _last_brain["ts"] = 0.0


def enabled() -> bool:
    return bool(C.env("SETUP_CODE"))


# ---------- сессии ----------
def _purge() -> None:
    now = _now()
    for sid in [s for s, exp in _sessions.items() if exp <= now]:
        _sessions.pop(sid, None)


def sessions_active() -> bool:
    _purge()
    return bool(_sessions)


def _session_ok(request: Request) -> bool:
    _purge()
    sid = request.cookies.get(COOKIE, "")
    return bool(sid) and sid in _sessions


def _code_ok(code: str) -> bool:
    expected = C.env("SETUP_CODE")
    a = hashlib.sha256(code.encode()).digest()
    b = hashlib.sha256(expected.encode()).digest()
    return hmac.compare_digest(a, b)


def _json_err(status: int, error: str, message: str = "", **extra) -> JSONResponse:
    return JSONResponse({"ok": False, "error": error, "message": message, **extra}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def _json_ok(**data) -> JSONResponse:
    return JSONResponse({"ok": True, **data}, headers={"Cache-Control": "no-store"})


def _need_enabled() -> None:
    if not enabled():
        raise HTTPException(status_code=404)


def _need_auth(request: Request) -> None:
    _need_enabled()
    if not _session_ok(request):
        raise HTTPException(status_code=401)


async def _body(request: Request) -> dict:
    if "application/json" not in (request.headers.get("content-type") or "").lower():
        raise HTTPException(status_code=415)      # форма с чужого сайта JSON не отправит без предварительного запроса
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(status_code=400)
    if not isinstance(data, dict):
        raise HTTPException(status_code=400)
    return data


# ---------- запросы Яндекса: что «услышали» ----------
def note_request(phrase: str, selftest_header: str | None = None) -> None:
    """Зовёт вебхук на каждый разрешённый запрос. Запоминаем последнюю реплику, но только пока идёт настройка
    (есть живая сессия мастера): в остальное время разговоры нигде не копятся."""
    if not enabled() or not sessions_active():
        return
    own = bool(selftest_header) and hmac.compare_digest(str(selftest_header), SELFTEST_NONCE)
    text = (phrase or "").strip()
    kind = "self" if own else ("ping" if text.lower() == "ping" else "phrase")
    if kind == "self":
        return
    _heard.update(ts=_now(), text=text[:200], kind=kind)


# ---------- сборка данных для страницы ----------
TYPE_LABELS = (
    ("light", "свет"), ("socket", "розетка"), ("switch", "выключатель"), ("media_device.tv", "телевизор"),
    ("media_device", "медиа"), ("thermostat", "климат"), ("humidifier", "увлажнитель"), ("purifier", "очиститель"),
    ("vacuum", "пылесос"), ("sensor", "датчик"), ("openable.curtain", "шторы"), ("openable", "замок / открывашка"),
    ("camera", "камера"), ("cooking", "кухня"), ("washing", "стирка"), ("pet", "для питомца"),
)


def type_label(t: str) -> str:
    t = t or ""
    for key, label in TYPE_LABELS:
        if key in t:
            return label
    return t.rsplit(".", 1)[-1] or "устройство"


def locks_enabled() -> bool:
    return C.allow_locks() and bool(C.lock_phrase())


def devices_summary(view: dict) -> dict:
    devs = [{"name": d.get("name", ""), "type": type_label(d.get("type", "")), "room": d.get("room", ""),
             "locked": bool(d.get("protected"))} for d in (view.get("devices") or [])]
    scen = [{"name": s.get("name", ""), "locked": bool(s.get("protected"))} for s in (view.get("scenarios") or [])]
    rooms = sorted({d["room"] for d in devs if d["room"]})
    return {"count": len(devs), "devices": devs, "scenarios": scen, "rooms": rooms,
            "locked_count": sum(1 for d in devs if d["locked"]) + sum(1 for s in scen if s["locked"]),
            "locks_enabled": locks_enabled()}


def public_base(request: Request) -> str:
    base = C.env("PUBLIC_URL").rstrip("/")
    if base:
        return base
    dom = C.env("RAILWAY_PUBLIC_DOMAIN")
    if dom:
        return "https://" + dom
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    return f"{proto}://{host}"


def server_domain(request: Request) -> str:
    """Адрес ЭТОГО сервера (хост без схемы и пути): его покажет страница входа на сервере лицензий, чтобы человек подтвердил «это мой сервер»."""
    from urllib.parse import urlparse
    return (urlparse(public_base(request)).hostname or "").lower()


def webhook_url(request: Request) -> str:
    sec = C.webhook_secret()
    return f"{public_base(request)}/alice/{sec}" if sec else ""


def _field(name: str, value) -> dict:
    return {"value": value, "source": C.source(name)}


def build_state(request: Request) -> dict:
    h = C.household(with_pack=False)       # содержимое пакета (хозяин, комнаты) на страницу не отдаём
    blocked = C.extra_blocked_names(with_pack=False)
    return {
        "now": _now(),
        "version": C.VERSION,
        "model": C.model(),
        "brain_key_set": bool(C.anthropic_key()),
        "iot": {"set": bool(C.iot_token()), "source": C.source("YANDEX_IOT_TOKEN")},
        "yandex_client_id": C.env("YANDEX_OAUTH_CLIENT_ID"),
        "name": _field("ASSISTANT_NAME", C.assistant_name()),
        "tone": _field("TONE", C.tone()),
        "gender": _field("GENDER", C.gender()),
        "owner": h.get("owner") if isinstance(h.get("owner"), str) else "",
        "people": h.get("people") if isinstance(h.get("people"), dict) else {},
        "rooms": h.get("rooms") if isinstance(h.get("rooms"), dict) else {},
        "household_source": C.source("HOUSEHOLD"),
        "blocked": blocked,
        "blocked_source": C.source("BLOCKED_NAMES"),
        "allow_locks": _field("ALLOW_LOCKS", C.allow_locks()),
        "lock_phrase": {"set": bool(C.lock_phrase()), "source": C.source("LOCK_PHRASE")},     # саму фразу не отдаём никогда
        "webhook": {"url": webhook_url(request), "secret_set": bool(C.webhook_secret())},
        "memory": "disk" if memory.disk_ok() else "ram",
        "callback": bool(C.env("INSTALL_CALLBACK_URL") and C.env("INSTALL_TOKEN")),
        "tones": list(C.TONES),
        "license": {"configured": activation.configured(), "active": activation.active()},   # самого пакета здесь нет никогда
    }


# ---------- валидация ----------
def _clean_line(v, limit: int) -> str:
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", str(v or "")).strip()
    return re.sub(r"\s{2,}", " ", s)[:limit]


def clean_iot_token(raw: str) -> str:
    """Яндекс показывает токен голым, но оператор может вставить и «access_token=...&expires_in=...»."""
    raw = (raw or "").strip().strip("\"'")
    m = re.search(r"access_token=([^&\s#]+)", raw)
    tok = m.group(1) if m else raw
    return tok if re.fullmatch(r"[A-Za-z0-9_\-.~+/=]{10,400}", tok) else ""


# ---------- маршруты ----------
@router.get("/setup", response_class=HTMLResponse)
@router.get("/setup/", response_class=HTMLResponse, include_in_schema=False)
async def page():
    _need_enabled()
    headers = {
        "Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                   "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    }
    return HTMLResponse(PAGE.read_text(encoding="utf-8"), headers=headers)


@router.get("/setup/api/me")
async def me(request: Request):
    _need_enabled()
    return _json_ok(authed=_session_ok(request), closed=C.setup_done() and not _session_ok(request),
                    locked_for=max(0, int(_fails["until"] - _now())))


@router.post("/setup/api/login")
async def login(request: Request):
    _need_enabled()
    data = await _body(request)
    now = _now()
    if _fails["until"] > now:
        return _json_err(429, "locked", "Слишком много неверных кодов. Подожди и попробуй позже.",
                         retry_in=int(_fails["until"] - now) + 1)
    code = str(data.get("code") or "")
    if not code or not _code_ok(code):
        _fails["n"] += 1
        left = MAX_FAILS - _fails["n"]
        print(f"setup: неверный код ({_fails['n']}/{MAX_FAILS})", flush=True)
        if left <= 0:
            _fails.update(n=0, until=now + LOCK_SECONDS)
            return _json_err(429, "locked", "Слишком много неверных кодов. Вход закрыт на десять минут.",
                             retry_in=LOCK_SECONDS)
        return _json_err(401, "bad_code", "Код не подошёл.", left=left)
    _fails["n"] = 0
    reopen = bool(data.get("reopen"))
    if C.setup_done():
        if not reopen:
            return _json_err(403, "closed", "Настройка уже завершена. Чтобы начать заново, нажми «Перенастроить».")
        C.set_setup_done(False)
        print("setup: перенастройка открыта", flush=True)
    sid = secrets.token_urlsafe(32)
    _sessions[sid] = now + SESSION_TTL
    resp = _json_ok()
    secure = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip() == "https"
    resp.set_cookie(COOKIE, sid, max_age=SESSION_TTL, httponly=True, samesite="strict", secure=secure, path="/setup")
    return resp


@router.post("/setup/api/logout")
async def logout(request: Request):
    _need_enabled()
    _sessions.pop(request.cookies.get(COOKIE, ""), None)
    resp = _json_ok()
    resp.delete_cookie(COOKIE, path="/setup")
    return resp


@router.get("/setup/api/state")
async def state(request: Request):
    _need_auth(request)
    return _json_ok(**build_state(request))


@router.post("/setup/api/check-brain")
async def check_brain(request: Request):
    """Один крошечный платный вызов (десяток токенов) по нажатию оператора."""
    _need_auth(request)
    await _body(request)
    now = _now()
    if now - _last_brain["ts"] < BRAIN_COOLDOWN:
        return _json_err(429, "cooldown", "Не так быстро: подожди пару секунд.")
    _last_brain["ts"] = now
    if not C.anthropic_key():
        return _json_ok(status="nokey", message="Ключ ANTHROPIC_API_KEY не задан. Его вставляют в переменные Railway.")
    try:
        await brain._create(model=C.model(), max_tokens=16,
                            messages=[{"role": "user", "content": "Ответь одним словом: ок"}])
    except Exception as e:  # noqa: BLE001
        kind = "model" if getattr(e, "status_code", None) == 404 else brain.error_kind(e)
        print(f"setup: проверка мозга -> {kind}", flush=True)      # текст ошибки не печатаем: в нём может быть ключ
        return _json_ok(status=kind, message=BRAIN_MESSAGES.get(kind, BRAIN_MESSAGES["down"]))
    print("setup: проверка мозга -> ok", flush=True)
    return _json_ok(status="ok", message=BRAIN_MESSAGES["ok"])


BRAIN_MESSAGES = {
    "ok": "Ключ работает, мозг ответил.",
    "auth": "Ключ не принят: опечатка или он отозван. Создай новый в консоли Anthropic и обнови ANTHROPIC_API_KEY.",
    "billing": "Ключ настоящий, но денег нет: кредиты кончились или выбран лимит расходов. Пополни баланс или подними Spend limit.",
    "busy": "Anthropic сейчас перегружен. Подожди минуту и нажми ещё раз.",
    "bad": "Anthropic не принял запрос. Проверь переменные MODEL и EFFORT.",
    "model": "Такой модели нет. Проверь переменную MODEL.",
    "down": "Не получилось достучаться до Anthropic. Попробуй ещё раз через минуту.",
}


async def _accept_iot_token(tok: str):
    """Проверить токен умного дома у Яндекса и сохранить. Возвращает (None, ответ-ошибка) или ((summary, persisted), None)."""
    try:
        async with httpx.AsyncClient(timeout=8, transport=_TRANSPORT) as c:
            r = await c.get(f"{iot.IOT}/user/info", headers={"Authorization": f"Bearer {tok}"})
    except Exception as e:  # noqa: BLE001
        print(f"setup: токен умного дома: сеть {type(e).__name__}", flush=True)
        return None, _json_err(502, "net", "Яндекс не ответил. Попробуй ещё раз.")
    if r.status_code in (401, 403):
        print("setup: токен умного дома не принят", flush=True)
        return None, _json_err(400, "rejected", "Яндекс не принял токен: он выдан не под тем аккаунтом, без прав iot:view / iot:control, либо устарел.")
    if r.status_code != 200:
        print(f"setup: токен умного дома: Яндекс ответил {r.status_code}", flush=True)
        return None, _json_err(502, "net", "Яндекс ответил ошибкой. Попробуй ещё раз.")
    try:
        summary = devices_summary(iot.public_view(iot.compact_user_info(r.json())))
    except Exception as e:  # noqa: BLE001
        print(f"setup: токен умного дома: разбор ответа {type(e).__name__}", flush=True)
        return None, _json_err(502, "net", "Яндекс прислал странный ответ. Попробуй ещё раз.")
    persisted = C.save_values({"YANDEX_IOT_TOKEN": tok})
    iot.reset_cache()
    return (summary, persisted), None


@router.post("/setup/api/iot-token")
async def iot_token(request: Request):
    _need_auth(request)
    data = await _body(request)
    tok = clean_iot_token(str(data.get("token") or ""))
    if not tok:
        return _json_err(400, "bad_token", "Это не похоже на токен Яндекса. Нужна длинная строка, которую показала страница Яндекса.")
    ok, err = await _accept_iot_token(tok)
    if err is not None:
        return err
    summary, persisted = ok
    return _json_ok(devices=summary, persisted=persisted, shadowed_by_env=C.source("YANDEX_IOT_TOKEN") == "env")


# ---------- код активации: персональный пакет с сервера лицензий ----------
@router.post("/setup/api/license/start")
async def license_start(request: Request):
    """Код -> адрес входа Яндексом (центральный колбэк на сервере лицензий). Идентификатор входа остаётся у нас в оперативке."""
    _need_auth(request)
    d = await _body(request)
    code = re.sub(r"[^A-Za-z0-9\- ]", "", str(d.get("code") or ""))[:40]
    if not code:
        return _json_err(400, "bad_code", "Введи код активации.")
    r = await activation.start_flow(code, server_domain(request))
    if not r.get("ok"):
        return _json_err(400 if r.get("error") not in ("net", "not_configured") else 502, r.get("error", "server"), r.get("message", ""))
    return _json_ok(authorize_url=r["authorize_url"])


@router.get("/setup/api/license/status")
async def license_status(request: Request):
    """Опрос после того, как человек открыл вход Яндексом. Получили пакет -> он на Volume зашифрованным и в оперативке;
    токен умного дома проверяется и сохраняется так же, как при ручной вставке."""
    _need_auth(request)
    r = await activation.poll_flow()
    st = r.get("status")
    if st == "failed":
        return _json_ok(status="failed", error=r.get("error", ""), message=r.get("message", ""))
    if st != "ok":
        return _json_ok(status=st)
    out = {"status": "ok", "saved": bool(r.get("saved")), "iot": False}
    tok = clean_iot_token(r.get("iot_token") or "")
    if tok:
        ok, err = await _accept_iot_token(tok)
        if ok is not None:
            out.update(iot=True, devices=ok[0], persisted=ok[1])
    return _json_ok(**out)


@router.get("/setup/api/devices")
async def devices(request: Request):
    _need_auth(request)
    if not C.iot_token():
        return _json_err(400, "no_token", "Умный дом ещё не подключён: сначала шаг «Умный дом».")
    try:
        view = await iot.devices()
    except Exception as e:  # noqa: BLE001
        print(f"setup: список устройств: {type(e).__name__}", flush=True)
        return _json_err(502, "net", "Яндекс не ответил. Попробуй ещё раз.")
    if view.get("error"):
        return _json_err(400, "rejected", str(view["error"]))
    return _json_ok(devices=devices_summary(view))


@router.post("/setup/api/character")
async def character(request: Request):
    _need_auth(request)
    d = await _body(request)
    name = _clean_line(d.get("name"), 30)
    tone, gender = str(d.get("tone") or ""), str(d.get("gender") or "")
    if not name:
        return _json_err(400, "bad_name", "Нужно имя ассистента (до 30 символов).")
    if tone not in C.TONES:
        return _json_err(400, "bad_tone", "Неизвестный тон.")
    if gender not in C.GENDERS:
        return _json_err(400, "bad_gender", "Пол голоса: мужской или женский.")
    persisted = C.save_values({"ASSISTANT_NAME": name, "TONE": tone, "GENDER": gender})
    return _json_ok(persisted=persisted, overridden=[k for k in ("ASSISTANT_NAME", "TONE", "GENDER") if C.source(k) == "env"])


def _pairs(v, limit: int, key_len: int = 40, val_len: int = 60) -> dict:
    out = {}
    for item in (v or [])[:limit]:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            k, val = _clean_line(item[0], key_len), _clean_line(item[1], val_len)
            if k and val:
                out[k] = val
    return out


@router.post("/setup/api/home")
async def home(request: Request):
    _need_auth(request)
    d = await _body(request)
    owner = _clean_line(d.get("owner"), 40)
    people = _pairs(d.get("people"), 12)
    rooms = _pairs(d.get("rooms"), 30)
    blocked = []
    for w in (d.get("blocked") or [])[:30]:
        w = _clean_line(w, 40).lower().replace(",", " ")
        if w and w not in blocked:
            blocked.append(w)
    new_phrase = _clean_line(d.get("lock_phrase"), 80)
    allow = bool(d.get("allow_locks"))
    phrase_after = "" if d.get("clear_lock_phrase") else (new_phrase or C.lock_phrase())
    if allow and len(phrase_after) < 6:
        return _json_err(400, "need_phrase", "Чтобы включить замки, нужна кодовая фраза: минимум шесть символов, лучше длинная и не из обихода.")
    prev = {}
    try:
        prev = json.loads(C.stored("HOUSEHOLD") or "{}")
    except ValueError:
        pass
    hh = {"owner": owner, "people": people, "rooms": rooms}
    if isinstance(prev.get("notes"), str) and prev["notes"].strip():
        hh["notes"] = prev["notes"]
    upd = {"HOUSEHOLD": {k: v for k, v in hh.items() if v}, "BLOCKED_NAMES": ",".join(blocked),
           "ALLOW_LOCKS": "1" if allow else None}
    if d.get("clear_lock_phrase"):
        upd["LOCK_PHRASE"] = None
    elif new_phrase:
        upd["LOCK_PHRASE"] = new_phrase
    persisted = C.save_values(upd)
    return _json_ok(persisted=persisted, locks_enabled=locks_enabled(),
                    overridden=[k for k in ("HOUSEHOLD", "BLOCKED_NAMES", "ALLOW_LOCKS", "LOCK_PHRASE") if C.source(k) == "env"])


@router.get("/setup/api/heard")
async def heard(request: Request, since: float = 0.0):
    _need_auth(request)
    if _heard["ts"] > since:
        return _json_ok(now=_now(), heard=True, text=_heard["text"], kind=_heard["kind"])
    return _json_ok(now=_now(), heard=False)


@router.post("/setup/api/self-test")
async def self_test(request: Request):
    """Тест без Станции: ту же реплику шлём на собственный вебхук (через приложение, сеть не нужна). Один вызов мозга."""
    _need_auth(request)
    await _body(request)
    secret = C.webhook_secret()
    if not secret:
        return _json_err(400, "no_secret", "WEBHOOK_SECRET не задан: вебхук закрыт.")
    payload = {"version": "1.0", "session": {"new": False, "user": {"user_id": SELFTEST_UID}},
               "request": {"original_utterance": "Привет, ты тут?"}}
    waiting = {t for kinds in prompts.PHRASES.values() for k in ("hold", "still") for t in kinds[k]}
    text, status = "", 0
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=request.app), base_url="http://selftest",
                                     timeout=20) as c:
            for attempt in range(4):
                r = await c.post(f"/alice/{secret}", json=payload, headers={SELFTEST_HEADER: SELFTEST_NONCE})
                status = r.status_code
                text = ((r.json().get("response") or {}).get("text") or "") if status == 200 else ""
                if status != 200 or text not in waiting:
                    break
                payload["request"]["original_utterance"] = "ну что?"      # мозг ещё думает: любая реплика забирает ответ
                await asyncio.sleep(SELFTEST_RETRY_DELAY)
    except Exception as e:  # noqa: BLE001
        print(f"setup: тест без Станции: {type(e).__name__}", flush=True)
        return _json_err(500, "selftest", "Тест не прошёл: вебхук упал. Смотри журнал сервиса.")
    finally:
        _forget_selftest_user()
    if status != 200:
        return _json_err(500, "selftest", f"Вебхук ответил кодом {status}.")
    return _json_ok(text=text[:300])


def _forget_selftest_user() -> None:
    """Тестовый «пользователь» не должен оставлять память на диске и в оперативке."""
    st = memory.STORES.pop(SELFTEST_UID, None)
    try:
        import main
        main.STATE.pop(SELFTEST_UID, None)
    except ImportError:
        pass
    path = st.path if st is not None else memory.Store(SELFTEST_UID).path
    memory.STORES.pop(SELFTEST_UID, None)
    try:
        os.remove(path)
    except OSError:
        pass


def sign_callback(token: str, timestamp: str, raw_body: bytes) -> str:
    """sha256=<hex>: HMAC-SHA256, ключ INSTALL_TOKEN целиком, сообщение «<unix-секунды>.<сырое тело>» (как signCallback на нашей стороне)."""
    msg = timestamp.encode() + b"." + raw_body
    return "sha256=" + hmac.new(token.encode(), msg, hashlib.sha256).hexdigest()


async def send_install_callback() -> str:
    """off | sent | failed. В теле только токен установки, статус и версия; ошибка не мешает «Готово»."""
    url, tok = C.env("INSTALL_CALLBACK_URL"), C.env("INSTALL_TOKEN")
    if not url or not tok:
        return "off"
    if not (url.startswith("https://") or re.match(r"http://(localhost|127\.0\.0\.1)[:/]", url)):
        print("setup: колбэк установки: адрес должен начинаться с https://", flush=True)
        return "failed"
    public = tok.split(".", 1)[0]                     # INSTALL_TOKEN = <публичная часть>.<секрет>: наружу уходит только первая
    body = json.dumps({"token": public, "status": "installed", "version": C.VERSION},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ts = str(int(time.time()))
    try:
        async with httpx.AsyncClient(timeout=6, transport=_TRANSPORT, follow_redirects=False) as c:
            r = await c.post(url, content=body, headers={"Content-Type": "application/json", "X-Koresh-Timestamp": ts,
                                                         "X-Koresh-Signature": sign_callback(tok, ts, body)})
        ok = 200 <= r.status_code < 300
        print(f"setup: колбэк установки -> {r.status_code}", flush=True)
        return "sent" if ok else "failed"
    except Exception as e:  # noqa: BLE001
        print(f"setup: колбэк установки: {type(e).__name__}", flush=True)
        return "failed"


@router.post("/setup/api/finish")
async def finish(request: Request):
    _need_auth(request)
    await _body(request)
    C.set_setup_done(True)
    _sessions.clear()                      # страница закрывается для всех, включая эту сессию
    _heard.update(ts=0.0, text="", kind="")
    cb = await send_install_callback()
    print(f"setup: готово (колбэк: {cb})", flush=True)
    resp = _json_ok(callback=cb)
    resp.delete_cookie(COOKIE, path="/setup")
    return resp
