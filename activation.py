"""Активация по коду: персональный пакет настроек Кореша.

Базовый характер публичный (prompts.py). Здесь только персональное: имя, тон, пол, хозяин, заметки по квизу.
Пакет выдаёт наш сервер лицензий ОДИН РАЗ при активации (код + вход Яндексом), дальше он живёт на Volume зашифрованным
(AES-GCM, ключ = HKDF(seed, yandex_id, install_id)) и в оперативке. Это защита от «открыл файл и прочитал», не от шелла.

Правила: пакет не попадает ни в переменные, ни в config.json, ни в журнал, ни в ответы /health и /setup/api/*.
Любой сбой тут тихий: нет пакета или он битый -> работает обычный публичный Кореш.
Yandex ID определяет только сервер лицензий по токену; клиент ему ничего про личность не присылает."""
import asyncio
import base64
import json
import os
import secrets
import time
import uuid

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

import config as C

# Адрес сервиса лицензий зашит в образ (без подмены переменной). [ПОДСТАВИТЬ] после создания сервиса в Railway.
# Только https://: по нему едут пакет и токен умного дома. Пустой или не https = шаг тихо недоступен.
LICENSE_URL = "https://license-production-4c25.up.railway.app"
STATE_NAME = "license.json"
PACK_NAME = "pack.enc"
MAGIC = b"KP1"
SUPPORTED_SCHEMA = 1
REFRESH_EVERY = 3600          # раз в час: рубильник модулей в админке должен сработать в пределах часа (ТЗ-32)
REFRESH_TIMEOUT = 3.0
FLOW_TTL = 600

_TRANSPORT = None              # ворота подставляют httpx.MockTransport
_FLOW = {"handle": "", "secret": "", "exp": 0.0}   # secret знает только этот сервер: без него обмен по handle не работает, в адресах его нет
_PENDING = {"has": False, "pack": {}}       # обновление пакета ждёт границы эпизода
_RAM_ID = {"id": ""}           # install_id, если диск недоступен

MSG_OFF = "Сервер активации сейчас не подключён к этому образу. Установку можно продолжить без кода: Кореш заработает обычным."
MSG_NO_DOMAIN = "Не удалось определить адрес сервера. Открой страницу настройки по адресу твоего Кореша в интернете (не localhost)."
MSG_NET_LOGIN = "Сервер активации не ответил. Попробуй ещё раз через минуту."
MSG_NET = "Сервер активации не ответил. Попробуй ещё раз через минуту; установку можно продолжить и без кода."


def configured() -> bool:
    return LICENSE_URL.startswith("https://")


def _dir() -> str:
    return C.memory_dir()


def _path(name: str) -> str:
    return os.path.join(_dir(), name)


def _write_atomic(path: str, data: bytes) -> bool:
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return True
    except OSError as e:
        print(f"activation: не удалось записать файл ({type(e).__name__})", flush=True)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def _read_state() -> dict:
    try:
        with open(_path(STATE_NAME), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(d: dict) -> bool:
    return _write_atomic(_path(STATE_NAME), json.dumps(d, ensure_ascii=False).encode())


def install_id() -> str:
    """Случайный идентификатор этой установки: создаётся при первом обращении и лежит на Volume."""
    iid = _read_state().get("install_id")
    if isinstance(iid, str) and 16 <= len(iid) <= 64:
        return iid
    iid = _RAM_ID["id"] or ("ki-" + uuid.uuid4().hex)
    _RAM_ID["id"] = iid
    st = _read_state()
    st["install_id"] = iid
    _write_state(st)
    return iid


def _key(seed: str, yandex_id: str, iid: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"koresh-pack-v1",
                info=f"{yandex_id}|{iid}".encode()).derive(seed.encode())


def _encrypt(pack: dict, seed: str, yandex_id: str, iid: str) -> bytes:
    nonce = secrets.token_bytes(12)
    ct = AESGCM(_key(seed, yandex_id, iid)).encrypt(nonce, json.dumps(pack, ensure_ascii=False).encode(), MAGIC)
    return MAGIC + nonce + ct


def _decrypt(blob: bytes, seed: str, yandex_id: str, iid: str) -> dict | None:
    try:
        if not blob.startswith(MAGIC) or len(blob) < 3 + 12 + 16:
            return None
        raw = AESGCM(_key(seed, yandex_id, iid)).decrypt(blob[3:15], blob[15:], MAGIC)
        d = json.loads(raw.decode())
        return d if isinstance(d, dict) else None
    except Exception:  # noqa: BLE001  любой сбой = пакета нет
        return None


# ---------- проверка пакета ----------
def _s(v, limit: int) -> str:
    return " ".join(str(v or "").replace("\x00", " ").split())[:limit]


def sanitize(raw) -> dict | None:
    """Оставляем только известные поля нужного типа. Незнакомая более новая схема -> None (работает публичный Кореш)."""
    if not isinstance(raw, dict):
        return None
    schema = raw.get("schema", SUPPORTED_SCHEMA)
    if not isinstance(schema, int) or schema > SUPPORTED_SCHEMA or schema < 1:
        return None
    p: dict = {"schema": schema, "version": raw.get("version") if isinstance(raw.get("version"), int) else 1}
    name = _s(raw.get("ASSISTANT_NAME"), 30)
    if name:
        p["ASSISTANT_NAME"] = name
    if raw.get("TONE") in C.TONES:
        p["TONE"] = raw["TONE"]
    if raw.get("GENDER") in C.GENDERS:
        p["GENDER"] = raw["GENDER"]
    owner = _s(raw.get("owner"), 40)
    if owner:
        p["owner"] = owner
    for key, lim in (("people", 12), ("rooms", 30)):
        v = raw.get(key)
        if isinstance(v, dict):
            d = {_s(k, 40): _s(x, 60) for k, x in list(v.items())[:lim]}
            d = {k: x for k, x in d.items() if k and x}
            if d:
                p[key] = d
    if isinstance(raw.get("blocked"), list):
        b = [_s(x, 40).lower() for x in raw["blocked"][:30]]
        b = [x for x in b if x and "," not in x]
        if b:
            p["blocked"] = b
    notes = _s(raw.get("notes"), 1500)
    if notes:
        p["notes"] = notes
    mods = raw.get("modules")                 # флаги модулей (ТЗ-32): только известные имена и только булевы значения
    if isinstance(mods, dict):
        m = {k: mods[k] for k in C.MODULE_NAMES if isinstance(mods.get(k), bool)}
        if m:
            p["modules"] = m
    if raw.get("promo_all_modules") is True:  # акция (ТЗ-33): все модули включены, явное false в modules перекрывает
        p["promo_all_modules"] = True
    return p


# ---------- хранение ----------
def store(resp: dict) -> bool:
    """Сохранить ответ обмена: пакет зашифрованно в pack.enc, seed и служебное в license.json. Значения в журнал не пишем."""
    pack = sanitize(resp.get("pack"))
    seed, yid, rt = resp.get("seed"), resp.get("yandex_id"), resp.get("refresh_token")
    if pack is None or not all(isinstance(x, str) and x for x in (seed, yid, rt)):
        return False
    iid = install_id()
    ok = _write_atomic(_path(PACK_NAME), _encrypt(pack, seed, str(yid), iid))
    st = _read_state()
    st.update(install_id=iid, seed=seed, yandex_id=str(yid), refresh_token=rt,
              pack_version=resp.get("pack_version") if isinstance(resp.get("pack_version"), int) else 1,
              activated_at=int(time.time()))
    ok = _write_state(st) and ok
    if ok:
        C.set_pack(pack)
    print("activation: пакет сохранён" if ok else "activation: пакет не сохранён (диск)", flush=True)
    return ok


def load() -> bool:
    """При старте: читаем pack.enc, расшифровываем в оперативку. Нет файла / битый / чужой ключ -> публичный Кореш."""
    try:
        st = _read_state()
        with open(_path(PACK_NAME), "rb") as f:
            blob = f.read()
        pack = sanitize(_decrypt(blob, str(st.get("seed") or ""), str(st.get("yandex_id") or ""), str(st.get("install_id") or "")))
    except OSError:
        pack = None
    if pack is None:
        C.clear_pack()
        return False
    C.set_pack(pack)
    return True


def forget(clear_ram: bool = True) -> None:
    """Стереть пакет с диска. clear_ram=False: из промпта он уйдёт на границе эпизода (apply_pending), не посреди разговора."""
    try:
        os.remove(_path(PACK_NAME))
    except OSError:
        pass
    st = _read_state()
    for k in ("seed", "refresh_token", "pack_version", "yandex_id"):
        st.pop(k, None)
    if st:
        _write_state(st)
    if clear_ram:
        C.clear_pack()


def active() -> bool:
    return C.pack_active()


def auth():
    """(install_id, токен обновления) этой установки или None, если активации нет. Нужно уведомлениям (notify.py)."""
    if not configured():
        return None
    st = _read_state()
    rt, iid = st.get("refresh_token"), st.get("install_id")
    if isinstance(rt, str) and rt and isinstance(iid, str) and iid:
        return iid, rt
    return None


def apply_pending() -> None:
    """Зовёт main в начале нового эпизода: новый пакет попадает в промпт только на границе, внутри эпизода он стабилен."""
    if _PENDING["has"]:
        pack = _PENDING["pack"]
        _PENDING.update(has=False, pack={})
        if pack:
            C.set_pack(pack)
        else:
            C.clear_pack()


def reset_for_tests() -> None:
    _FLOW.update(handle="", secret="", exp=0.0)
    _PENDING.update(has=False, pack={})
    _RAM_ID["id"] = ""
    C.clear_pack()


# ---------- разговор с сервером лицензий ----------
def _url(path: str) -> str:
    return LICENSE_URL.rstrip("/") + path


async def _post(path: str, body: dict, headers: dict | None = None, timeout: float = 10.0):
    async with httpx.AsyncClient(timeout=timeout, transport=_TRANSPORT, follow_redirects=False) as c:
        return await c.post(_url(path), json=body, headers=headers or {})


def _err(r) -> dict:
    try:
        j = r.json()
    except ValueError:
        j = {}
    return {"ok": False, "error": str(j.get("error") or "server")[:40], "message": str(j.get("message") or MSG_NET)[:300]}


async def check_code(code: str) -> dict:
    """Вход на /setup кодом KR-…: спрашиваем сервер лицензий, годится ли код ЭТОЙ установке (не отозван и либо ещё не активирован,
    либо активирован именно ею). Ничего не привязывает и не сохраняет. Отказ одинаковый на все причины: {"ok": False, "error": "bad_code"}.
    Сеть упала / сервер не ответил -> error "net" (это не неверная попытка ввода)."""
    if not configured():
        return {"ok": False, "error": "bad_code"}
    try:
        r = await _post("/v1/code/check", {"code": str(code or "")[:40], "install_id": install_id()}, timeout=8.0)
    except Exception as e:  # noqa: BLE001
        print(f"activation: проверка кода: сеть {type(e).__name__}", flush=True)
        return {"ok": False, "error": "net", "message": MSG_NET_LOGIN}
    if r.status_code == 200:
        try:
            if r.json().get("ok") is True:
                return {"ok": True}
        except ValueError:
            pass
        return {"ok": False, "error": "net", "message": MSG_NET_LOGIN}
    if r.status_code in (400, 404):
        return {"ok": False, "error": "bad_code"}
    print(f"activation: проверка кода: сервер ответил {r.status_code}", flush=True)
    return {"ok": False, "error": "net", "message": MSG_NET_LOGIN}


async def start_flow(code: str, domain: str = "") -> dict:
    """Шаг 1: код + адрес ЭТОГО сервера -> сервер лицензий отдаёт ссылку входа (она ведёт на сам сервер лицензий, не прямо на Яндекс).
    Идентификатор входа (handle) и секрет обмена хранятся только в оперативке. На странице входа человек увидит наш адрес
    и ответит «Это ваш сервер?»: так чужая ссылка входа от чужого сервера не сработает молча."""
    if not configured():
        return {"ok": False, "error": "not_configured", "message": MSG_OFF}
    domain = str(domain or "").strip()[:253]
    if not domain:
        return {"ok": False, "error": "bad_domain", "message": MSG_NO_DOMAIN}
    secret = secrets.token_urlsafe(32)
    try:
        r = await _post("/v1/install/start", {"code": str(code or "")[:40], "install_id": install_id(),
                                              "server_domain": domain, "exchange_secret": secret})
    except Exception as e:  # noqa: BLE001
        print(f"activation: старт: сеть {type(e).__name__}", flush=True)
        return {"ok": False, "error": "net", "message": MSG_NET}
    if r.status_code != 200:
        print(f"activation: старт отклонён {r.status_code}", flush=True)
        return _err(r)
    try:
        j = r.json()
        handle, url = str(j["handle"]), str(j["login_url"])
    except (ValueError, KeyError, TypeError):
        return {"ok": False, "error": "server", "message": MSG_NET}
    if not url.startswith(LICENSE_URL.rstrip("/") + "/yandex/start?handle="):
        return {"ok": False, "error": "server", "message": MSG_NET}
    _FLOW.update(handle=handle, secret=secret, exp=time.time() + FLOW_TTL)
    return {"ok": True, "authorize_url": url}


async def poll_flow() -> dict:
    """Шаг 2 (опрос с /setup): пока человек не вошёл — pending; вошёл — получаем пакет и IoT-токен ОДИН раз."""
    if not _FLOW["handle"]:
        return {"status": "none"}
    if time.time() > _FLOW["exp"]:
        _FLOW.update(handle="", secret="", exp=0.0)
        return {"status": "failed", "error": "expired", "message": "Время на вход вышло (10 минут). Введи код и начни заново."}
    try:
        r = await _post("/v1/install/exchange", {"handle": _FLOW["handle"], "install_id": install_id(), "exchange_secret": _FLOW["secret"]})
    except Exception as e:  # noqa: BLE001
        print(f"activation: обмен: сеть {type(e).__name__}", flush=True)
        return {"status": "pending", "net": True}
    if r.status_code == 202:
        return {"status": "pending"}
    if r.status_code != 200:
        e = _err(r)
        if r.status_code in (403, 404, 410):
            _FLOW.update(handle="", secret="", exp=0.0)
        print(f"activation: обмен отклонён {r.status_code}", flush=True)
        return {"status": "failed", "error": e["error"], "message": e["message"]}
    try:
        j = r.json()
    except ValueError:
        return {"status": "pending", "net": True}
    _FLOW.update(handle="", secret="", exp=0.0)
    saved = store(j)
    return {"status": "ok", "saved": saved, "iot_token": j.get("yandex_token") if isinstance(j.get("yandex_token"), str) else ""}


async def refresh_once() -> str:
    """same | update | revoked | skip | fail. Сбой тихий: у клиента остаётся то, что уже есть."""
    try:
        st = _read_state()
        rt, iid = st.get("refresh_token"), st.get("install_id")
        if not configured() or not rt or not iid or not os.path.exists(_path(PACK_NAME)):
            return "skip"
        r = await _post("/v1/pack/refresh", {"install_id": iid, "since": int(st.get("pack_version") or 0),
                                             "version": str(C.VERSION)[:40]},
                        headers={"Authorization": f"Bearer {rt}"}, timeout=REFRESH_TIMEOUT)
        if r.status_code == 410:
            forget(clear_ram=False)
            _PENDING.update(has=True, pack={})
            C.set_modules({}, False)           # отзыв: модули гаснут сразу, остальное на границе эпизода
            return "revoked"
        if r.status_code != 200:
            return "fail"
        j = r.json()
        if j.get("status") != "update":
            return "same"
        pack = sanitize(j.get("pack"))
        if pack is None:
            return "fail"
        ok = _write_atomic(_path(PACK_NAME), _encrypt(pack, str(st.get("seed")), str(st.get("yandex_id")), iid))
        st["pack_version"] = j.get("pack_version") if isinstance(j.get("pack_version"), int) else st.get("pack_version")
        _write_state(st)
        _PENDING.update(has=True, pack=pack)
        C.set_modules(pack.get("modules"), pack.get("promo_all_modules") is True)     # рубильник: модули включаются и гаснут сразу, не ждут границы эпизода
        return "update" if ok else "fail"
    except Exception:  # noqa: BLE001
        return "fail"


async def refresh_loop() -> None:
    """При старте и раз в час. Любая ошибка молча проглатывается. Заодно повторяем уведомления, которые не ушли."""
    try:
        await asyncio.sleep(5)
        while True:
            await refresh_once()
            try:
                import notify
                await notify.flush()
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(REFRESH_EVERY)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        return
