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
