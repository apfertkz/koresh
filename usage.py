"""Суточный счётчик токенов (UTC-сутки). Хранится в памяти и в маленьком файле на диске памяти
(MEMORY_DIR), чтобы обновление образа не обнуляло потолок посреди дня."""
import json
import os
import time

import config as C
import memory

_state = {"day": "", "used": 0, "loaded": False}


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def reset_for_tests():
    _state.update(day="", used=0, loaded=False)
    try:
        os.remove(C.usage_file())
    except OSError:
        pass


def _load():
    if _state["loaded"]:
        return
    _state["loaded"] = True
    try:
        with open(C.usage_file(), encoding="utf-8") as f:
            d = json.load(f)
        _state.update(day=str(d.get("day", "")), used=int(d.get("used", 0)))
    except Exception:  # noqa: BLE001  нет файла / битый файл — считаем с нуля
        pass


def _roll():
    _load()
    if _state["day"] != _today():
        _state.update(day=_today(), used=0)


def used() -> int:
    _roll()
    return _state["used"]


def add(tokens: int):
    _roll()
    _state["used"] += max(0, int(tokens))
    try:
        memory.atomic_write(C.usage_file(), json.dumps({"day": _state["day"], "used": _state["used"]}))
    except OSError as e:  # диск недоступен -- считаем в памяти процесса и не роняем разговор
        memory._log_disk("disk", False, type(e).__name__)


def exceeded() -> bool:
    cap = C.daily_token_cap()
    return cap > 0 and used() >= cap
