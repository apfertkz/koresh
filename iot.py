"""Умный дом Яндекса: список устройств и команды. Защита чувствительных устройств — здесь, в коде,
а не в промпте: модель можно уговорить, код — нет."""
import re
import time

import httpx

import config as C

IOT = "https://api.iot.yandex.net/v1.0"
IOT_TIMEOUT = 2.5
_TRANSPORT = None        # ворота подставляют httpx.MockTransport
_cache: dict = {"ts": 0.0, "data": None}
_index: dict = {"device": {}, "group": {}, "scenario": {}}

# Замок / ворота / сигнализация / охрана. Голос не аутентифицирует: гость скажет «открой замок».
SENSITIVE_TYPE = re.compile(r"lock|gate|garage|alarm|security|barrier|siren|intercom|openable$", re.I)
SENSITIVE_NAME = re.compile(r"замо[кч]|замк|ворот|калитк|сигнализац|охран|шлагбаум|домофон|гараж|\block\b|\bgate\b|alarm|garage", re.I)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=IOT_TIMEOUT, transport=_TRANSPORT)


def _auth() -> dict:
    return {"Authorization": f"Bearer {C.iot_token()}"}


def _name_sensitive(name: str) -> bool:
    n = (name or "").lower()
    return bool(SENSITIVE_NAME.search(n)) or any(w in n for w in C.extra_blocked_names())


def _device_sensitive(d: dict) -> bool:
    return bool(SENSITIVE_TYPE.search(d.get("type", ""))) or _name_sensitive(d.get("name", ""))


def compact_user_info(raw: dict) -> dict:
    """Сжимаем ответ Яндекса до нужного модели.

    Грабли: для умений без известного состояния (телевизор, видеопоток Станции) Яндекс присылает
    "state": null. get("state", {}) на таком ключе вернёт None, а не {} — отсюда AttributeError,
    роняющий ВЕСЬ список устройств из-за одного телевизора. Поэтому везде `or`, а не default в get.
    """
    rooms = {x["id"]: x["name"] for x in (raw.get("rooms") or [])}
    devices = []
    for d in (raw.get("devices") or []):
        item = {
            "id": d["id"], "name": d.get("name", ""), "type": d.get("type", ""),
            "room": rooms.get(d.get("room"), ""),
            "capabilities": [
                {"type": c.get("type"),
                 "instance": (c.get("state") or {}).get("instance"),
                 "value": (c.get("state") or {}).get("value")}
                for c in (d.get("capabilities") or [])
            ],
        }
        if _device_sensitive(item):
            item["protected"] = True
        devices.append(item)
    protected_ids = {d["id"] for d in devices if d.get("protected")}
    groups = []
    for g in (raw.get("groups") or []):
        item = {"id": g["id"], "name": g.get("name", ""), "_members": list(g.get("devices") or [])}
        if _name_sensitive(item["name"]) or protected_ids.intersection(item["_members"]):
            item["protected"] = True
        groups.append(item)
    scenarios = []
    for s in (raw.get("scenarios") or []):
        item = {"id": s["id"], "name": s.get("name", "")}
        if s.get("is_active") is False:
            item["inactive"] = True                  # ТЗ-35: неактивный сценарий запустить нельзя; ключ только когда Яндекс прямо сказал false
        if _name_sensitive(item["name"]):
            item["protected"] = True
        scenarios.append(item)
    return {"devices": devices, "groups": groups, "scenarios": scenarios}


def _remember(compact: dict):
    _index["device"] = {d["id"]: d for d in compact["devices"]}
    _index["group"] = {g["id"]: g for g in compact["groups"]}
    _index["scenario"] = {s["id"]: s for s in compact["scenarios"]}


def public_view(compact: dict) -> dict:
    """Что показываем модели: без служебного списка членов групп."""
    return {
        "devices": compact["devices"],
        "groups": [{k: v for k, v in g.items() if k != "_members"} for g in compact["groups"]],
        "scenarios": compact["scenarios"],
    }


async def devices() -> dict:
    """Список устройств/групп/сценариев, кэш 10 минут."""
    if not C.iot_token():
        return {"error": "умный дом не подключён: нет YANDEX_IOT_TOKEN"}
    if _cache["data"] and time.time() - _cache["ts"] < 600:
        return public_view(_cache["data"])
    async with _client() as c:
        r = await c.get(f"{IOT}/user/info", headers=_auth())
    if r.status_code in (401, 403):
        return {"error": "токен умного дома не принят: истёк или отозван, нужен новый"}
    r.raise_for_status()
    compact = compact_user_info(r.json())
    _remember(compact)
    _cache.update(ts=time.time(), data=compact)
    return public_view(compact)


def reset_cache():
    _cache.update(ts=0.0, data=None)
    _index.update(device={}, group={}, scenario={})


def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-zа-я]+", " ", (s or "").lower().replace("ё", "е")).strip()


def _lock_gate(item: dict, user_text: str) -> str | None:
    """None — можно. Иначе строка-причина для модели."""
    if not item.get("protected"):
        return None
    if not C.allow_locks() or not C.lock_phrase():
        return "locks_disabled: управление замками, воротами и сигнализацией отключено в настройках"
    if _norm(C.lock_phrase()) not in _norm(user_text):
        return "need_phrase: для этого нужна кодовая фраза, попроси хозяина назвать её; сама фраза тебе неизвестна"
    return None


async def control(target: str, target_id: str, cap_type: str, instance: str, value, user_text: str = "") -> str:
    if not C.iot_token():
        return "умный дом не подключён: нет YANDEX_IOT_TOKEN"
    if target not in ("device", "group", "scenario"):
        return "error: target должен быть device, group или scenario"
    if not _index[target].get(target_id):
        await devices()          # индекс холодный — перечитываем, чтобы знать, что за id
    item = _index[target].get(target_id)
    if item is None:
        return "error: неизвестный id, сначала вызови list_devices"
    import modules                                   # лениво: modules сам импортирует iot
    kids = modules.kids_gate(target, item)           # детский режим: только свет (ТЗ-32)
    if kids:
        return kids
    refusal = _lock_gate(item, user_text)
    if refusal:
        return refusal
    action = {"type": cap_type, "state": {"instance": instance, "value": value}}
    if target == "device":
        url, body = f"{IOT}/devices/actions", {"devices": [{"id": target_id, "actions": [action]}]}
    elif target == "group":
        url, body = f"{IOT}/groups/{target_id}/actions", {"actions": [action]}
    else:
        url, body = f"{IOT}/scenarios/{target_id}/actions", None
    async with _client() as c:
        r = await c.post(url, headers=_auth(), json=body)
    _cache["ts"] = 0.0
    if r.status_code in (401, 403):
        return "error: токен умного дома не принят, нужен новый"
    if r.status_code != 200:
        return f"error {r.status_code}: {r.text[:200]}"
    try:
        for dev in (r.json().get("devices") or []):
            for cap in (dev.get("capabilities") or []):
                res = ((cap.get("state") or {}).get("action_result") or {})
                if res.get("status") == "ERROR":
                    return f"error: {res.get('error_code', 'устройство не выполнило команду')}"
    except ValueError:
        pass
    return "ok"


# ---------- сценарии как «звонок» (ТЗ-35) ----------
# Документация Яндекса (user API, «Управление запуском сценария»): POST /v1.0/scenarios/{id}/actions БЕЗ тела, заголовок Authorization: Bearer;
# 200 {"request_id", "status": "ok"}; ошибка: {"request_id", "status": "error", "message"} с кодом 404 и т.п.
# Список сценариев: GET /v1.0/user/info -> scenarios: [{id, name, is_active}]. Что внутри сценария (шаги, команда), API НЕ отдаёт.
SCENARIO_TIMEOUT = 6.0
_SCENARIO_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")
CALL_WORDS = ("зайди", "позвони", "звонок", "поговор", "скажи", "навык", "первым", "кореш")


def scenario_id_ok(sid) -> bool:
    return bool(_SCENARIO_ID.fullmatch(str(sid or "")))


async def run_scenario(scenario_id: str) -> dict:
    """Запустить сценарий пользователя по id. Только для «звонка» Кореша: id приходит из нашей же настройки, не от модели.
    -> {"ok": True} | {"ok": False, "error": no_token|bad_id|auth|not_found|net|error, "message": ...}. Токен и тело ответа в журнал не пишем."""
    if not C.iot_token():
        return {"ok": False, "error": "no_token", "message": "умный дом не подключён"}
    if not scenario_id_ok(scenario_id):
        return {"ok": False, "error": "bad_id", "message": "неверный id сценария"}
    try:
        async with httpx.AsyncClient(timeout=SCENARIO_TIMEOUT, transport=_TRANSPORT) as c:
            r = await c.post(f"{IOT}/scenarios/{scenario_id}/actions", headers=_auth())
    except Exception as e:  # noqa: BLE001
        print(f"iot: запуск сценария: сеть {type(e).__name__}", flush=True)
        return {"ok": False, "error": "net", "message": "Яндекс не ответил"}
    if r.status_code in (401, 403):
        return {"ok": False, "error": "auth", "message": "токен умного дома не принят"}
    if r.status_code == 404:
        return {"ok": False, "error": "not_found", "message": "сценарий не найден (удалён или выключен)"}
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code == 200 and (body or {}).get("status") == "ok":
        return {"ok": True}
    msg = str((body or {}).get("message") or "")[:120] if isinstance(body, dict) else ""
    print(f"iot: запуск сценария: Яндекс ответил {r.status_code}", flush=True)
    return {"ok": False, "error": "error", "message": msg or f"Яндекс ответил {r.status_code}"}


async def scenarios_fresh() -> dict:
    """Свежий список сценариев (мимо кэша): нужен странице настройки «Найти мой сценарий».
    -> {"ok": True, "scenarios": [{id, name, active, protected}]} | {"ok": False, "error", "message"}."""
    if not C.iot_token():
        return {"ok": False, "error": "no_token", "message": "Умный дом ещё не подключён: сначала токен."}
    try:
        async with httpx.AsyncClient(timeout=8.0, transport=_TRANSPORT) as c:
            r = await c.get(f"{IOT}/user/info", headers=_auth())
    except Exception as e:  # noqa: BLE001
        print(f"iot: список сценариев: сеть {type(e).__name__}", flush=True)
        return {"ok": False, "error": "net", "message": "Яндекс не ответил. Попробуй ещё раз."}
    if r.status_code in (401, 403):
        return {"ok": False, "error": "auth", "message": "Токен умного дома не принят: истёк или отозван."}
    if r.status_code != 200:
        return {"ok": False, "error": "net", "message": "Яндекс ответил ошибкой. Попробуй ещё раз."}
    try:
        raw = r.json()
        compact = compact_user_info(raw)
        out = []
        for s in (raw.get("scenarios") or []):
            name = str(s.get("name", ""))
            out.append({"id": s["id"], "name": name, "active": s.get("is_active") is not False, "protected": _name_sensitive(name)})
    except Exception as e:  # noqa: BLE001
        print(f"iot: список сценариев: разбор {type(e).__name__}", flush=True)
        return {"ok": False, "error": "net", "message": "Яндекс прислал странный ответ. Попробуй ещё раз."}
    _remember(compact)
    _cache.update(ts=time.time(), data=compact)
    return {"ok": True, "scenarios": out}


def rank_call_scenarios(scenarios: list, activation: str, assistant: str = "") -> list:
    """Угадываем «свой» сценарий по названию: API не показывает команду внутри. Кандидаты только активные и не «замки/ворота».
    -> [{id, name, score}] от лучшего к худшему."""
    words = {w for w in re.findall(r"[0-9a-zа-я]{3,}", f"{activation} {assistant}".lower().replace("ё", "е"))}
    act = re.sub(r"\s+", " ", (activation or "").lower().replace("ё", "е")).strip()
    res = []
    for s in scenarios:
        if not s.get("active") or s.get("protected"):
            continue
        n = re.sub(r"\s+", " ", s["name"].lower().replace("ё", "е"))
        score = 0
        if act and act in n:
            score += 4
        score += 2 * sum(1 for w in words if w in n)
        score += sum(1 for w in CALL_WORDS if w in n)
        res.append({"id": s["id"], "name": s["name"], "score": score})
    res.sort(key=lambda x: (-x["score"], x["name"].lower()))
    return res
