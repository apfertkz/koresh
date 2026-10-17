"""Кореш говорит первым (ТЗ-35): «звонок» на Станцию через сценарий Алисы.

Как это работает. У хозяина в «Доме с Алисой» есть сценарий, чьё действие для Станции: «Вопрос или команда Алисе» = `спроси у <имя> что нового` (так Алиса не говорит «Запускаю навык», первым отвечает Кореш).
Есть что сказать (напоминание подошло) -> образ запускает этот сценарий через API умного дома (iot.run_scenario) -> Станция сама открывает навык ->
первая реплика новой сессии (session.new) приходит в main.handle -> claim() отдаёт готовый текст события, МОДЕЛЬ НЕ ЗВОНИТСЯ (таймаут навыка 4,5 с),
дальше обычный разговор. Яндекс не даёт навыку писать первым, поэтому «позвонить» можно только так.

Правила, чтобы не бесить: тихие часы (по умолчанию 23:00-08:00) -> не звоним, уходит в Телеграм/WhatsApp, и утром старое не досыпаем; не чаще раза
в 10 минут и 6 в час; события, подошедшие вместе, -> один звонок, Кореш перечисляет; «не звони мне сегодня» / «больше не звони» / «можешь звонить»;
человек уже в сессии -> не звоним, говорим в его текущей сессии (со следующей реплики). Не дозвонились за 3 минуты (сессия не открылась, сценарий не
запустился, сценарий не настроен) -> старый путь: Телеграм и WhatsApp. Проактив -- часть модуля «Напоминания» (timers) и модуля secretary (WhatsApp-секретарь, ТЗ-36): без обоих флагов не звоним.

Очередь живёт на Volume (MEMORY_DIR/proactive.json) и переживает перезапуск. Время и часы можно подменять в воротах (_now)."""
import json
import re
import time
from datetime import datetime, timedelta

import config as C
import iot
import memory
import notify

FILE = "proactive.json"
FRESH_SEC = 180             # звонок живёт 3 минуты: открылся навык позже -> событие уже ушло в Телеграм
GAP_SEC = 600               # не чаще 1 звонка в 10 минут
HOUR_SEC = 3600
HOUR_MAX = 6                # и не больше 6 в скользящий час
ACTIVE_SEC = 120            # человек говорил с Корешем меньше двух минут назад и не прощался = он в сессии
HINT_EVERY = 24 * 3600      # честную подсказку «настрой сценарий-звонок» повторяем не чаще раза в сутки
DEFAULT_QUIET = (23, 8)
TEST_GAP = 15               # «Проверить звонок» не чаще раза в 15 секунд (кнопку не сломать мышью)
MAX_EVENTS = 30
SPOKEN_MAX = 900

ORD = ("Первое", "Второе", "Третье", "Четвёртое", "Пятое", "Шестое")

# ТЗ-36: события WhatsApp-секретаря идут тем же путём. Отличия: в Телеграм (а не в WhatsApp «себе»: сообщение и так в WhatsApp) и с причиной, почему не озвучено.
WA_KIND = "wa_message"
NO_SELF_KINDS = (WA_KIND,)                 # эти виды в WhatsApp «себе» не дублируем
REASON_KINDS = (WA_KIND,)
REASON_TEXT = {
    "quiet": "сейчас тихие часы, вслух не читал",
    "no_scenario": "сценарий-звонок не настроен, вслух не читал",
    "off": "ты просил не звонить первым, вслух не читал",
    "paused": "ты просил не звонить сегодня, вслух не читал",
    "sec_off": "ты просил не читать WhatsApp вслух",
    "sec_paused": "ты просил не читать WhatsApp сегодня",
    "limit_gap": "я и так недавно звонил, не стал дёргать",
    "limit_hour": "я и так уже много раз звонил за час",
    "call_failed": "звонок на Станцию не вышел",
    "timeout": "Станция не откликнулась за 3 минуты",
    "kids": "включён детский режим",
    "no_call": "вслух не читал",
}
ITEM_MAX = {WA_KIND: 420}


def _now() -> float:
    return time.time()


# ---------- хранилище ----------
_S = {"path": "", "data": None, "seq": 0}
_SESSION = {"at": 0.0, "ended": False}          # только в оперативке: кто и когда говорил с Корешем


def reset_for_tests() -> None:
    _S.update(path="", data=None, seq=0)
    _SESSION.update(at=0.0, ended=False)


def reload() -> None:
    """Забыть оперативную копию: следующее обращение читает диск (как после перезапуска)."""
    _S.update(path="", data=None)


def _path() -> str:
    import os
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
        evs = [e for e in (d.get("events") or []) if isinstance(e, dict) and e.get("id") and e.get("state") in ("calling", "session")]
        calls = [float(x) for x in (d.get("calls") or []) if isinstance(x, (int, float))]
        test = d.get("test") if isinstance(d.get("test"), dict) else {}
        last = d.get("last") if isinstance(d.get("last"), dict) else {}
        sec_test = d.get("sec_test") if isinstance(d.get("sec_test"), dict) else {}
        _S.update(path=p, data={"events": evs, "calls": calls, "off": d.get("off") is True,
                                "pause_until": float(d.get("pause_until") or 0), "hint_at": float(d.get("hint_at") or 0),
                                "test": test, "sec_test": sec_test, "last": last})
    return _S["data"]


def _save() -> bool:
    try:
        memory.atomic_write(_path(), json.dumps(_data(), ensure_ascii=False))
        return True
    except OSError as e:
        print(f"proactive: диск недоступен ({type(e).__name__})", flush=True)
        return False


# ---------- настройки ----------
def module_on() -> bool:
    """Звонок на Станцию нужен модулям «Напоминания» и «Чтение WhatsApp вслух» (ТЗ-36): хватает любого из двух."""
    return C.module_on("timers") or C.module_on("secretary")


def scenario_id() -> str:
    return C.env("CALL_SCENARIO")


def scenario_name() -> str:
    return C.env("CALL_SCENARIO_NAME")


def configured() -> bool:
    """Звонок возможен вообще: модуль «Напоминания» включён, умный дом подключён, сценарий выбран."""
    return module_on() and bool(C.iot_token()) and iot.scenario_id_ok(scenario_id())


def quiet_hours() -> tuple[int, int]:
    """(с, до) в часах местного времени. 'CALL_QUIET' = '23-8'. Мусор -> умолчание. Равные числа = тихих часов нет."""
    m = re.fullmatch(r"\s*(\d{1,2})\s*[-–:]\s*(\d{1,2})\s*", C.env("CALL_QUIET"))
    if m and int(m.group(1)) <= 23 and int(m.group(2)) <= 23:
        return int(m.group(1)), int(m.group(2))
    return DEFAULT_QUIET


def _local(ts: float) -> datetime:
    import modules
    return modules._local(ts)


def is_quiet(ts: float) -> bool:
    a, b = quiet_hours()
    if a == b:
        return False
    h = _local(ts).hour
    return (a <= h < b) if a < b else (h >= a or h < b)


def _local_midnight_after(ts: float) -> float:
    d = _local(ts)
    nxt = (d + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt.timestamp()


def in_session(now: float | None = None) -> bool:
    now = _now() if now is None else now
    return (not _SESSION["ended"]) and _SESSION["at"] > 0 and now - _SESSION["at"] < ACTIVE_SEC


def note_activity(now: float | None = None) -> None:
    _SESSION.update(at=_now() if now is None else now, ended=False)


def note_end() -> None:
    _SESSION["ended"] = True


# ---------- голосовые команды ----------
_FILL = r"(?:(?:ну|слушай|кореш|алиса|пожалуйста|давай|так|а)\W+)*"
_TAIL = r"(?:\W+(?:пожалуйста|мне))*\W*"
OFF_TODAY_RE = re.compile(
    _FILL + r"(?:не\W+(?:звони|звоните)\W+(?:мне\W+)?(?:сегодня|до\W+завтра|весь\W+день)|сегодня\W+(?:мне\W+)?не\W+звони(?:те)?)" + _TAIL)
OFF_RE = re.compile(
    _FILL + r"(?:больше\W+не\W+звони(?:те)?(?:\W+мне)?(?:\W+(?:первым|первой|сам|сама))?"
    r"|не\W+звони(?:те)?(?:\W+мне)?\W+(?:больше|первым|первой|сам|сама)"
    r"|не\W+начинай\W+(?:разговор\W+)?(?:первым|первой|сам|сама)"
    r"|(?:выключи|отключи)\W+(?:звонки|звонок\W+первым))" + _TAIL)
ON_RE = re.compile(
    _FILL + r"(?:(?:ты\W+)?(?:можешь|можно|давай|начинай|продолжай)\W+(?:мне\W+)?звонить(?:\W+(?:мне|первым|первой|сам|сама|снова|опять|дальше))*"
    r"|звони\W+(?:мне\W+)?(?:первым|первой|сам|сама|снова|опять)"
    r"|(?:включи|верни)\W+звонки)" + _TAIL)


def command(low: str) -> str | None:
    """off_today | off | on | None. Только целиком фразой: «позвони маме» и «не звони маме» сюда не попадают."""
    s = (low or "").strip().lower().replace("ё", "е")
    if OFF_TODAY_RE.fullmatch(s):
        return "off_today"
    if OFF_RE.fullmatch(s):
        return "off"
    if ON_RE.fullmatch(s):
        return "on"
    return None


def apply_command(cmd: str, now: float | None = None) -> str:
    """Выполнить команду и вернуть честный ответ голосом."""
    now = _now() if now is None else now
    d = _data()
    if not module_on():
        if cmd == "on":
            return "Этого я пока не умею, подключается отдельно."
        return "Я тебе и так сам не звоню: эта функция у меня не подключена."
    if cmd == "off_today":
        d["pause_until"] = _local_midnight_after(now)
        _save()
        return "Понял, сегодня не звоню. Если что, напишу в Телеграм."
    if cmd == "off":
        d["off"] = True
        d["pause_until"] = 0
        _save()
        return "Понял, сам больше не звоню. Всё будет приходить в Телеграм. Захочешь обратно, скажи «можешь звонить»."
    d["off"] = False
    d["pause_until"] = 0
    _save()
    if not configured():
        return "Хорошо. Но сценарий-звонок у меня ещё не настроен, ссылка на настройки в боте: без него я позвонить не смогу."
    return "Хорошо, буду звонить сам, когда есть что сказать. Но не ночью и не чаще раза в десять минут."


def enabled() -> bool:
    d = _data()
    return not d["off"]


def paused(now: float | None = None) -> bool:
    now = _now() if now is None else now
    return now < _data()["pause_until"]


def set_enabled(on: bool) -> None:
    d = _data()
    d["off"] = not on
    if on:
        d["pause_until"] = 0
    _save()


# ---------- решение: звонить или нет ----------
def _inflight(now: float):
    for e in _data()["events"]:
        if e["state"] == "calling" and e.get("kind") != "test" and now - float(e.get("call_at") or 0) < FRESH_SEC:
            return e
    return None


def _limit_hit(now: float) -> str:
    calls = [c for c in _data()["calls"] if now - c < HOUR_SEC]
    if calls and now - max(calls) < GAP_SEC:
        return "gap"
    if len(calls) >= HOUR_MAX:
        return "hour"
    return ""


def decide(now: float, priority: str = "normal", force: bool = False) -> tuple[str, str]:
    """('call'|'join'|'session', '') или ('tg', причина). force (кнопка «Проверить» секретаря): тихие часы, паузы и лимиты не мешают, нужен только настроенный звонок."""
    if not configured():
        return "tg", "no_scenario"
    if not force:
        if _data()["off"]:
            return "tg", "off"
        if paused(now):
            return "tg", "paused"
        if priority != "alarm" and is_quiet(now):
            return "tg", "quiet"
    if _inflight(now) is not None:
        return "join", ""
    if in_session(now):
        return "session", ""
    if not force:
        lim = _limit_hit(now)
        if lim:
            return "tg", "limit_" + lim
    return "call", ""


def _new_event(kind: str, text: str, item: str, priority: str, now: float, state: str, call_at: float = 0.0, extra: dict | None = None) -> dict:
    _S["seq"] += 1
    ev = {"id": f"e{int(now * 1000)}-{_S['seq']}", "kind": kind, "text": text, "item": item, "priority": priority,
          "created": now, "state": state, "call_at": call_at, "expires": now + FRESH_SEC if state == "session" else call_at + FRESH_SEC}
    if extra:
        ev.update({k: v for k, v in extra.items() if k in ("ask", "sec", "test")})
    return ev


def _item_of(text: str, item: str | None, kind: str = "") -> str:
    s = re.sub(r"\s+", " ", str(item if item is not None else text or "")).strip().rstrip(".")
    return s[:ITEM_MAX.get(kind, 200)]


def _tg_text(kind: str, text: str, why: str) -> str:
    """Текст для Телеграма: у событий секретаря в конце причина, почему не озвучили (ТЗ-36)."""
    if kind in REASON_KINDS and why in REASON_TEXT:
        return f"{text} ({REASON_TEXT[why]})"
    return text


async def _send_tg(kind: str, text: str, why: str = "") -> None:
    await notify.send_owner(kind, _tg_text(kind, text, why), wa_too=kind not in NO_SELF_KINDS)


def find_sec(sec_id: str):
    """Событие секретаря по его id, если оно ещё ждёт озвучки (звонок идёт или ждёт слова в сессии)."""
    now = _now()
    for e in _data()["events"]:
        if e.get("sec") == sec_id and e["state"] in ("calling", "session") and now <= e["expires"]:
            return e
    return None


def amend_sec(sec_id: str, item: str, ask: str, text: str) -> bool:
    """Склейка: в уже поставленное в очередь событие секретаря влилось ещё сообщение -- обновляем фразу и текст для Телеграма. False -> событие уже озвучено/ушло."""
    e = find_sec(sec_id)
    if e is None:
        return False
    e["item"], e["ask"], e["text"] = _item_of(text, item, WA_KIND), ask, text
    _save()
    return True


async def deliver(kind: str, text: str, item: str | None = None, priority: str = "normal", calls: bool = True,
                  extra: dict | None = None, tg_reason: str | None = None, force: bool = False) -> str:
    """Доставить событие хозяину. -> 'called' (запущен звонок, ждём навык) | 'session' (скажем в текущей сессии) | 'tg:<причина>' (ушло в Телеграм/WhatsApp).
    text -- для Телеграма (как раньше), item -- короткое «что сказать голосом». calls=False: заведомо только Телеграм (например, проспали напоминание).
    extra (ТЗ-36): ask -- вопрос в конце фразы, sec -- id события секретаря, test -- проверка кнопкой. tg_reason: причина, по которой звонить нельзя уже решил вызывающий.
    force: «Проверить» секретаря."""
    now = _now()
    if tg_reason:
        mode, why = "tg", tg_reason
    else:
        mode, why = decide(now, priority, force) if calls else ("tg", "no_call")
    if mode == "tg":
        print(f"proactive: {kind} -> Телеграм ({why})", flush=True)
        await _send_tg(kind, text, why)
        return "tg:" + why
    d = _data()
    it = _item_of(text, item, kind)
    if mode == "session":
        ev = _new_event(kind, text, it, priority, now, "session", extra=extra)
        d["events"] = (d["events"] + [ev])[-MAX_EVENTS:]
        _save()
        print(f"proactive: {kind} -> скажу в текущей сессии", flush=True)
        return "session"
    if mode == "join":
        base = _inflight(now)
        ev = _new_event(kind, text, it, priority, now, "calling", float(base["call_at"]), extra=extra)
        d["events"] = (d["events"] + [ev])[-MAX_EVENTS:]
        _save()
        print(f"proactive: {kind} -> в тот же звонок", flush=True)
        return "called"
    ev = _new_event(kind, text, it, priority, now, "calling", now, extra=extra)       # call_at ставим ДО запуска: параллельные события присоединятся
    d["events"] = (d["events"] + [ev])[-MAX_EVENTS:]
    _save()
    res = await iot.run_scenario(scenario_id())
    if res["ok"]:
        if not force:
            d["calls"] = [c for c in d["calls"] if now - c < HOUR_SEC] + [now]
        d["last"] = {"at": now, "ok": True, "error": ""}
        _save()
        print(f"proactive: {kind} -> звонок (сценарий запущен)", flush=True)
        return "called"
    d["last"] = {"at": now, "ok": False, "error": res["error"]}
    failed = [e for e in d["events"] if e["state"] == "calling" and e.get("call_at") == now]
    d["events"] = [e for e in d["events"] if e not in failed]
    _save()
    print(f"proactive: звонок не вышел ({res['error']}) -> Телеграм", flush=True)
    for e in failed:
        if e.get("test"):
            continue
        await _send_tg(e["kind"], e["text"], "call_failed")
    return "tg:call_failed"


# ---------- первая реплика ----------
def _compose(events: list, mid: bool) -> str:
    items = [e for e in events if e["item"]]
    if not items:
        return ""
    if len(events) == 1:
        e = events[0]
        if e["kind"] == WA_KIND:                     # ТЗ-36: «<имя> написала: «…». Ответить?» -- без «Слушай»; посреди разговора «Кстати, …»
            body = f"{e['item']}. {e.get('ask') or 'Ответить?'}"
            return ("Кстати, " + body) if mid else body
        op = "Кстати," if mid else "Слушай,"
        it = e["item"]
        if e["kind"] == "reminder":
            return f"{op} ты просил напомнить: {it}."
        return f"{op} {it[:1].lower() + it[1:]}."
    op = "Кстати," if mid else "Слушай,"
    parts = [f"{ORD[i] if i < len(ORD) else str(i + 1)}: {e['item']}." for i, e in enumerate(items[:6])]
    tail = " Кому-то ответить?" if any(e["kind"] == WA_KIND for e in items[:6]) else ""
    return f"{op} у меня для тебя несколько вещей. " + " ".join(parts) + tail


_CLAIMED: list = []            # что забрал последний claim(): main.handle отдаёт это секретарю (запомнить, кого озвучили; задать вопрос «Ответить?»)


def claimed() -> list:
    """События, зачитанные последним claim(), и сразу забыть: [{kind, sec, test, ask}]."""
    out = list(_CLAIMED)
    _CLAIMED.clear()
    return out


def claim(new_session: bool, now: float | None = None) -> str | None:
    """Вызов от main.handle на каждой живой реплике. Есть чем начать -> готовый текст (событие закрыто), иначе None.
    new_session=True: Станция открыла навык; берём события звонка не старше 3 минут. Реплика внутри сессии: берём события, дождавшиеся «слова в сессии»."""
    now = _now() if now is None else now
    d = _data()
    take, tests = [], []
    for e in d["events"]:
        if e["state"] == "session" and now <= e["expires"]:
            take.append(e)
        elif new_session and e["state"] == "calling" and now - float(e.get("call_at") or 0) <= FRESH_SEC:
            (tests if e["kind"] == "test" else take).append(e)
    if not take and not tests:
        return None
    mid = bool(take) and all(e["state"] == "session" for e in take)
    parts = []
    if tests:
        parts.append("Это проверка звонка. Я на связи: раз ты это слышишь, звонок работает.")
        d["test"] = {"status": "answered", "at": now}
    take.sort(key=lambda e: e["created"])
    text = _compose(take, mid) if take else ""
    if text:
        parts.append(text)
    if any(e.get("test") for e in take):
        d["sec_test"] = {"status": "answered", "at": now}
    _CLAIMED[:] = [{"kind": e["kind"], "sec": e.get("sec", ""), "test": bool(e.get("test")), "ask": e.get("ask", "")} for e in take]
    d["events"] = [e for e in d["events"] if e not in take and e not in tests]
    _save()
    out = " ".join(parts)[:SPOKEN_MAX]
    print(f"proactive: событий сказано голосом: {len(take) + len(tests)}", flush=True)
    return out or None


async def tick(now: float | None = None) -> int:
    """Просроченное (не дозвонились за 3 минуты / не дождались реплики в сессии) уходит старым путём: Телеграм и WhatsApp. Зовёт планировщик."""
    now = _now() if now is None else now
    d = _data()
    late = [e for e in d["events"] if now > e["expires"]]
    if not late:
        return 0
    d["events"] = [e for e in d["events"] if e not in late]
    for e in late:
        if e["kind"] == "test":
            d["test"] = {"status": "no_answer", "at": now}
        if e.get("test"):
            d["sec_test"] = {"status": "no_answer", "at": now}
    _save()
    n = 0
    for e in late:
        if e["kind"] == "test" or e.get("test"):
            continue
        n += 1
        print(f"proactive: {e['kind']}: навык не открылся за 3 минуты -> Телеграм", flush=True)
        await _send_tg(e["kind"], e["text"], "timeout")
    return n


# ---------- проверка звонка из настроек ----------
async def test_call(now: float | None = None) -> dict:
    """Кнопка «Проверить звонок»: запускает выбранный сценарий сейчас. Тихие часы и лимиты не мешают (человек сам нажал); в лимиты не засчитывается."""
    now = _now() if now is None else now
    if not module_on():
        return {"ok": False, "error": "no_module", "message": "Модуль «Напоминания» не подключён."}
    if not C.iot_token():
        return {"ok": False, "error": "no_token", "message": "Сначала подключи умный дом."}
    if not iot.scenario_id_ok(scenario_id()):
        return {"ok": False, "error": "no_scenario", "message": "Сначала выбери сценарий."}
    d = _data()
    if now - float((d["test"] or {}).get("started") or 0) < TEST_GAP:
        return {"ok": False, "error": "too_fast", "message": "Подожди несколько секунд и нажми ещё раз."}
    d["events"] = [e for e in d["events"] if e["kind"] != "test"]
    d["events"].append(_new_event("test", "", "", "normal", now, "calling", now))
    d["test"] = {"status": "waiting", "started": now}
    _save()
    res = await iot.run_scenario(scenario_id())
    if not res["ok"]:
        d["events"] = [e for e in d["events"] if e["kind"] != "test"]
        d["test"] = {"status": "failed", "error": res["error"], "message": res["message"], "at": now}
        _save()
        return {"ok": False, "error": res["error"], "message": res["message"]}
    return {"ok": True}


def test_status(now: float | None = None) -> dict:
    now = _now() if now is None else now
    t = dict(_data()["test"] or {})
    st = t.get("status") or "none"
    if st == "waiting" and now - float(t.get("started") or 0) > FRESH_SEC:
        st = "no_answer"
    return {"status": st, "message": t.get("message", "")}


# ---------- для страниц и разговора ----------
def status(now: float | None = None) -> dict:
    now = _now() if now is None else now
    d = _data()
    a, b = quiet_hours()
    pu = d["pause_until"]
    return {"module": module_on(), "iot": bool(C.iot_token()), "configured": configured(),
            "scenario": {"id": scenario_id(), "name": scenario_name()} if iot.scenario_id_ok(scenario_id()) else None,
            "enabled": not d["off"], "paused": now < pu, "quiet": {"from": a, "to": b},
            "last": d["last"], "test": test_status(now), "source": C.source("CALL_SCENARIO")}


def hint(now: float | None = None) -> str:
    """Честность при настройке напоминания: сценарий-звонок не настроен -> зачитать, что напомним только в Телеграм. Не чаще раза в сутки."""
    now = _now() if now is None else now
    if not module_on() or configured():
        return ""
    d = _data()
    if now - d["hint_at"] < HINT_EVERY:
        return ""
    d["hint_at"] = now
    _save()
    return "Хочешь, чтобы я говорил сам на колонке, настрой сценарий-звонок, ссылка в боте."


def skill_activation() -> str:
    """Активационное имя по умолчанию -- как в мастере: имя ассистента + «дома» (латиница -> «Кореш»)."""
    nm = (C.assistant_name() or "Кореш").strip()
    if re.search(r"[A-Za-z]", nm):
        nm = "Кореш"
    low = nm.lower()
    return low if re.search(r"\s", low) else low + " дома"


def parse_quiet(frm, to) -> tuple[str | None, str]:
    """-> (значение для CALL_QUIET или None = умолчание, ошибка)."""
    try:
        a, b = int(str(frm).strip()), int(str(to).strip())
    except (TypeError, ValueError):
        return None, "Тихие часы: два числа от 0 до 23, например с 23 до 8."
    if not (0 <= a <= 23 and 0 <= b <= 23):
        return None, "Тихие часы: часы от 0 до 23."
    return (None if (a, b) == DEFAULT_QUIET else f"{a}-{b}"), ""
