# Ворота ТЗ-35: «Кореш говорит первым» -- звонок на Станцию через сценарий Алисы. Подключается из selftest.py (exec в его пространстве имён) после раздела 25
# и использует помощников раздела 24 (_r24, _act24, _wa24, _mods24, say24, Script24, LIC24, WA_SENT24, CLK24, _local24...).
# Живых вызовов нет: умный дом Яндекса -- httpx.MockTransport (формат запуска сценария по документации: POST /v1.0/scenarios/{id}/actions без тела,
# 200 {"request_id","status":"ok"}), сервер лицензий -- фейк раздела 24, модель -- скриптованная заглушка, время -- сдвигаемые часы.
import contextlib
import io

import proactive

_ENV26 = ("MEMORY_DIR", "USAGE_FILE", "TONE", "GENDER", "WA_ENABLED", "WA_CONTACTS", "ANTHROPIC_API_KEY", "YANDEX_IOT_TOKEN", "TZ_OFFSET",
          "SETUP_CODE", "WEBHOOK_SECRET", "CALL_SCENARIO", "CALL_SCENARIO_NAME", "CALL_QUIET")
_SAVED26 = {k: os.environ.get(k) for k in _ENV26}
_IOT26_SAVED = iot._TRANSPORT
UID26 = "u35"

USER35 = {
    "rooms": [], "groups": [],
    "devices": [{"id": "lamp-bed", "name": "Лампа", "type": "devices.types.light", "room": None,
                 "capabilities": [{"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": True}}]}],
    "scenarios": [{"id": "sc-call", "name": "Кореш зайди", "is_active": True},
                  {"id": "sc-kino", "name": "Кино", "is_active": True},
                  {"id": "sc-gate", "name": "Открыть ворота", "is_active": True},
                  {"id": "sc-off", "name": "Кореш старый", "is_active": False}],
}
RUNS35 = []                    # (id сценария, тело пусто?, заголовок Authorization верный?)
SC35 = {"mode": "ok"}          # ok | 404 | 500 | 401 | net | errbody


def _y35(request: httpx.Request) -> httpx.Response:
    if request.headers.get("authorization") != "Bearer test-iot-token":
        return httpx.Response(401, json={"status": "error"})
    if request.method == "GET" and request.url.path == "/v1.0/user/info":
        return httpx.Response(200, json={"status": "ok", **USER35})
    if request.method == "POST" and request.url.path == "/v1.0/devices/actions":
        body = json.loads(request.content)
        POSTS24.append((request.url.path, body))
        did = body["devices"][0]["id"]
        if did in BROKEN24:
            return httpx.Response(200, json={"status": "ok", "devices": [{"id": did, "capabilities": [
                {"type": "devices.capabilities.on_off", "state": {"instance": "on", "action_result": {"status": "ERROR", "error_code": "DEVICE_UNREACHABLE"}}}]}]})
        return httpx.Response(200, json={"status": "ok"})
    m = re.fullmatch(r"/v1\.0/scenarios/([^/]+)/actions", request.url.path)
    if request.method == "POST" and m:
        RUNS35.append((m.group(1), request.content == b"", True))
        mode = SC35["mode"]
        if mode == "net":
            raise httpx.ConnectError("лежит")
        if mode == "404":
            return httpx.Response(404, json={"request_id": "r", "status": "error", "message": "Сценарий не найден"})
        if mode == "500":
            return httpx.Response(500, json={"request_id": "r", "status": "error", "message": "внутренняя ошибка"})
        if mode == "401":
            return httpx.Response(401, json={"status": "error"})
        if mode == "errbody":
            return httpx.Response(200, json={"request_id": "r", "status": "error", "message": "нельзя"})
        return httpx.Response(200, json={"request_id": "667b3e0f", "status": "ok"})
    return httpx.Response(404)


def _r35(conf=True, mods=("timers",), wa_on=True):
    """Чистое состояние: настроенный (или нет) сценарий-звонок, включённые модули, активация, WhatsApp «себе»."""
    _r24()
    os.environ.pop("CALL_SCENARIO", None)
    os.environ.pop("CALL_QUIET", None)
    iot._TRANSPORT = httpx.MockTransport(_y35)
    iot.reset_cache()
    proactive.reset_for_tests()
    proactive._now = lambda: CLK24["t"]
    proactive.GAP_SEC, proactive.HOUR_MAX = 600, 6
    RUNS35.clear()
    SC35["mode"] = "ok"
    _mods24(*mods)
    _act24()
    _wa24(wa_on)
    if conf:
        C.save_values({"CALL_SCENARIO": "sc-call", "CALL_SCENARIO_NAME": "Кореш зайди"})
    fresh(UID26)
    main.STATE.pop(UID26, None)


def add_rem35(text="забрать малого", at=None, **kw):
    modules._active_reminders().append({"id": "r" + str(len(modules._active_reminders()) + 1) + str(int(CLK24["t"])), "kind": "remind", "text": text,
                                        "action": None, "fire_at": CLK24["t"] if at is None else at, "at": None, "repeat": "none", "created": 1, **kw})
    modules._save()


def due35():
    return run(modules.run_due())


def sess35(text="", new=True, uid=UID26):
    return run(main.handle(uid, text, new))["response"]


def tg35():
    return [n for n in LIC24.notes if n.get("kind") == "reminder"]


def calls35():
    return [r for r in RUNS35]


# ================= 26a. API Яндекса: запуск и список сценариев =================
_r35()
r = run(iot.run_scenario("sc-call"))
check("26a1. запуск сценария по документации: POST /v1.0/scenarios/sc-call/actions, БЕЗ тела, с Bearer-токеном, ответ {status:ok} -> ok",
      r == {"ok": True} and RUNS35 == [("sc-call", True, True)], str((r, RUNS35)))
for mode, err in (("404", "not_found"), ("401", "auth"), ("500", "error"), ("net", "net"), ("errbody", "error")):
    SC35["mode"] = mode
    r = run(iot.run_scenario("sc-call"))
    check(f"26a2. Яндекс отвечает «{mode}» -> честная причина {err}, исключение не вылетает", r["ok"] is False and r["error"] == err, str(r))
SC35["mode"] = "ok"
RUNS35.clear()
r = run(iot.run_scenario("../devices/actions"))
check("26a3. кривой id (попытка выйти из пути) -> bad_id, в сеть не ходим", r["error"] == "bad_id" and not RUNS35, str(r))
os.environ.pop("YANDEX_IOT_TOKEN", None)
check("26a4. нет токена умного дома -> no_token, в сеть не ходим", run(iot.run_scenario("sc-call"))["error"] == "no_token" and not RUNS35)
os.environ["YANDEX_IOT_TOKEN"] = "test-iot-token"
lst = run(iot.scenarios_fresh())
by = {x["id"]: x for x in lst["scenarios"]}
check("26a5. свежий список сценариев: id, название, активен, «замок/ворота» помечен protected",
      lst["ok"] and len(by) == 4 and by["sc-call"]["active"] and not by["sc-off"]["active"] and by["sc-gate"]["protected"] and not by["sc-call"]["protected"], str(lst))
rk = iot.rank_call_scenarios(lst["scenarios"], "кореш дома", "Кореш")
check("26a6. «Найти мой сценарий»: неактивные и «ворота» не предлагаются, «Кореш зайди» первым, у «Кино» нулевой балл",
      [x["id"] for x in rk] == ["sc-call", "sc-kino"] and rk[0]["score"] > rk[1]["score"] == 0, str(rk))
cu = iot.compact_user_info(USER35)
check("26a7. модели сценарии показываются как раньше (только id и название); у выключенного добавлен признак inactive",
      {"id": "sc-call", "name": "Кореш зайди"} in cu["scenarios"] and {"id": "sc-off", "name": "Кореш старый", "inactive": True} in cu["scenarios"], str(cu["scenarios"]))

# ================= 26b. приёмка 1: напоминание в срок -> звонок -> первая реплика = текст напоминания =================
_r35()
S = Script24(text_resp("Забрать малого, понял. Что ещё?"))
brain._create = S
add_rem35()
n = due35()
ev = proactive._data()["events"]
check("26b1. напоминание подошло, сценарий настроен -> запущен сценарий-звонок (фейк Яндекса), Телеграм и WhatsApp молчат, событие ждёт открытия навыка",
      n == 1 and RUNS35 == [("sc-call", True, True)] and not LIC24.notes and not WA_SENT24 and len(ev) == 1 and ev[0]["state"] == "calling", str((RUNS35, LIC24.notes, ev)))
rsp = sess35("", True)
check("26b2. Станция открыла навык (новая сессия, пустая реплика) -> первая фраза = «Слушай, ты просил напомнить: забрать малого.», без вызова модели",
      rsp["text"] == "Слушай, ты просил напомнить: забрать малого." and not S.calls and rsp["end_session"] is False, rsp["text"])
check("26b3. событие закрыто (очереди нет), в Телеграм не ушло ни разу", proactive._data()["events"] == [] and not LIC24.notes and not WA_SENT24)
r2 = sess35("спасибо, ага", False)
msgs = S.calls[0]["messages"] if S.calls else []
ctx_msg = next((m for m in msgs if m["role"] == "user" and isinstance(m["content"], list) and "ты сам начал этот разговор" in str(m["content"])), None)
check("26b4. дальше обычный разговор: ответ даёт модель, и она знает, с чего начали (служебная вставка с тем, что сказали вслух, не репликой хозяина)",
      "Забрать малого, понял" in r2["text"] and ctx_msg is not None and "забрать малого" in str(ctx_msg["content"])
      and msgs.index(ctx_msg) + 2 < len(msgs) and msgs[msgs.index(ctx_msg) + 2]["content"] == "спасибо, ага", str(msgs)[:400])
check("26b5. второй раз открыть навык -> обычное приветствие, старое не повторяется", "малого" not in sess35("", True)["text"] and not S.calls[1:])
# через настоящий вебхук
_r35()
os.environ["WEBHOOK_SECRET"] = "s35secret"
add_rem35("выключить утюг")
due35()
tcw = TestClient(main.app)
bodyw = {"version": "1.0", "session": {"new": True, "user": {"user_id": UID26}}, "request": {"original_utterance": "", "command": ""}}
rw = tcw.post("/alice/s35secret", json=bodyw)
check("26b6. через настоящий вебхук /alice/{секрет}: session.new=true, пустая реплика -> «ты просил напомнить: выключить утюг»",
      rw.status_code == 200 and rw.json()["response"]["text"] == "Слушай, ты просил напомнить: выключить утюг." and rw.json()["response"]["tts"], rw.text)
# новая сессия с репликой сразу: сначала событие, потом ответ модели
_r35()
S = Script24(text_resp("Нормально, живём."))
brain._create = S
add_rem35("покормить кота")
due35()
rp = sess35("как дела", True)
check("26b7. хозяин в первой же реплике сказал дело: сперва то, с чего начал Кореш, потом ответ модели",
      rp["text"].startswith("Слушай, ты просил напомнить: покормить кота.") and rp["text"].endswith("Нормально, живём."), rp["text"])

# ================= 26c. приёмка 2: не настроено / не дозвонились -> Телеграм и WhatsApp =================
_r35(conf=False)
add_rem35()
due35()
check("26c1. сценарий не настроен -> звонка нет, напоминание как раньше: очередь Телеграма (reminder) и WhatsApp «себе»",
      not RUNS35 and len(tg35()) == 1 and "забрать малого" in tg35()[0]["text"] and len(WA_SENT24) == 1 and not proactive._data()["events"], str((RUNS35, LIC24.notes)))
for mode, err in (("500", "error"), ("404", "not_found"), ("401", "auth"), ("net", "net")):
    _r35()
    SC35["mode"] = mode
    add_rem35()
    due35()
    last = proactive.status()["last"]
    check(f"26c2. Яндекс «{mode}» при запуске сценария -> сразу Телеграм/WhatsApp, причина ({err}) запомнена для страницы, в очереди пусто",
          len(tg35()) == 1 and len(WA_SENT24) == 1 and last.get("ok") is False and last.get("error") == err and not proactive._data()["events"], str((last, LIC24.notes)))
_r35()
add_rem35()
due35()
CLK24["t"] += 179
run(proactive.tick())
check("26c3. через 179 секунд навык не открылся -> ещё ждём, ничего не отправлено", not LIC24.notes and len(proactive._data()["events"]) == 1)
CLK24["t"] += 2
n = run(proactive.tick())
check("26c4. через 181 секунду навык так и не открылся -> фолбэк: Телеграм и WhatsApp с тем же текстом, событие снято",
      n == 1 and len(tg35()) == 1 and "забрать малого" in tg35()[0]["text"] and len(WA_SENT24) == 1 and not proactive._data()["events"])
S = Script24()
brain._create = S
check("26c5. навык открыли ПОЗЖЕ -> старое напоминание не зачитывается (оно уже ушло в Телеграм), модель не зовётся",
      "малого" not in sess35("", True)["text"] and not S.calls)
_r35()
add_rem35()
due35()
CLK24["t"] += 200
t_late = sess35("", True)["text"]
check("26c6. сессия открылась на 200-й секунде, планировщик ещё не отработал -> не зачитываем протухшее; планировщик следом отправит в Телеграм",
      "малого" not in t_late and run(proactive.tick()) == 1 and len(tg35()) == 1, t_late)
_r35()
add_rem35()
due35()
proactive.reload()
CLK24["t"] += 190
run(proactive.tick())
check("26c7. звонок пережил перезапуск (очередь на диске), просроченное после рестарта всё равно уходит в Телеграм", len(tg35()) == 1)
_r35(mods=())
r = run(proactive.deliver("reminder", "Напоминание: тест", item="тест"))
check("26c8. флага модуля «Напоминания» нет -> не звонит никогда (старый путь), даже если сценарий выбран", r == "tg:no_scenario" and not RUNS35 and len(tg35()) == 1, r)
_r35()
add_rem35(at=CLK24["t"] - 3 * 3600)
due35()
check("26c9. напоминание проспали больше 2 часов -> звонить поздно: только сообщение «Пропустил напоминание»", not RUNS35 and len(tg35()) == 1 and "Пропустил" in tg35()[0]["text"])
_r35()
modules._active_reminders().append({"id": "a1", "kind": "action", "text": "", "action": {"target": "device", "target_id": "lamp-bed", "capability": "devices.capabilities.on_off",
                                    "instance": "on", "value": False, "label": "выключить свет в спальне"}, "fire_at": CLK24["t"], "at": None, "repeat": "none", "created": 1})
modules._save()
due35()
rt = sess35("", True)["text"]
check("26c10. действие по времени: свет выключен, итог «Сделал по расписанию…» тоже можно сказать голосом: «Слушай, сделал по расписанию: выключить свет в спальне.»",
      rt == "Слушай, сделал по расписанию: выключить свет в спальне." and any(p.endswith("/devices/actions") for p, b in POSTS24), rt)

# ================= 26d. тихие часы =================
_r35()
check("26d1. границы по умолчанию 23:00-08:00 (Алматы +5): 22:59 нет, 23:00 да, 00:00 да, 07:59 да, 08:00 нет",
      [proactive.is_quiet(_local24(*hm, day=d)) for hm, d in (((22, 59), 14), ((23, 0), 14), ((0, 0), 15), ((7, 59), 15), ((8, 0), 15))] == [False, True, True, True, False])
CLK24["t"] = _local24(23, 30)
add_rem35()
due35()
check("26d2. напоминание в 23:30 -> не звонит (сценарий не запускался), уходит в Телеграм и WhatsApp, в очереди звонков пусто",
      not RUNS35 and len(tg35()) == 1 and len(WA_SENT24) == 1 and not proactive._data()["events"])
CLK24["t"] = _local24(8, 5, day=15)
S = Script24()
brain._create = S
t_m = sess35("", True)["text"]
check("26d3. утром (08:05) навык открыли -> старое ночное НЕ досыпается: обычное приветствие, звонков нет", "малого" not in t_m and not RUNS35 and not S.calls, t_m)
_r35()
os.environ["CALL_QUIET"] = "22-6"
check("26d4. свои тихие часы 22-6: 22:30 тихо, 06:00 уже можно, 12:00 можно", proactive.is_quiet(_local24(22, 30)) and not proactive.is_quiet(_local24(6, 0, day=15)) and not proactive.is_quiet(_local24(12, 0)))
os.environ["CALL_QUIET"] = "9-9"
check("26d5. одинаковые числа = тихих часов нет", not proactive.is_quiet(_local24(3, 0)) and not proactive.is_quiet(_local24(23, 30)))
os.environ["CALL_QUIET"] = "мусор"
check("26d6. мусор в настройке -> умолчание 23-8", proactive.quiet_hours() == (23, 8))
os.environ.pop("CALL_QUIET", None)
CLK24["t"] = _local24(23, 30)
r = run(proactive.deliver("reminder", "Напоминание: тревога", item="тревога", priority="alarm"))
check("26d7. тревога тихие часы пробивает (источник появится в следующих ТЗ, правило уже работает)", r == "called" and len(RUNS35) == 1, r)
_r35()
os.environ["TZ_OFFSET"] = "3"
CLK24["t"] = float(BASE24)            # 05:00 UTC = 08:00 в поясе +3
check("26d8. часовой пояс клиента учитывается: 05:00 UTC при поясе +3 это 08:00 -> не тихо; при +0 это 05:00 -> тихо",
      not proactive.is_quiet(CLK24["t"]) and (os.environ.__setitem__("TZ_OFFSET", "0") or True) and proactive.is_quiet(CLK24["t"]))
os.environ.pop("TZ_OFFSET", None)

# ================= 26e. лимиты и пачки =================
_r35()
add_rem35("первое")
due35()
sess35("", True)
check("26e1. первый звонок прошёл и принят", len(RUNS35) == 1 and not proactive._data()["events"])
CLK24["t"] += 300
add_rem35("второе")
due35()
check("26e2. через 5 минут второе напоминание -> не чаще 1 звонка в 10 минут: ушло в Телеграм, сценарий второй раз не запускали",
      len(RUNS35) == 1 and len(tg35()) == 1 and "второе" in tg35()[0]["text"])
CLK24["t"] += 300
add_rem35("третье")
due35()
check("26e3. ровно через 10 минут после первого звонка -> снова можно звонить", len(RUNS35) == 2 and len(tg35()) == 1)
_r35()
proactive.GAP_SEC = 0
res = []
for i in range(6):
    r = run(proactive.deliver("reminder", f"Напоминание: дело {i}", item=f"дело {i}"))
    sess35("", True)
    res.append(r)
    CLK24["t"] += 130            # дольше окна «человек в сессии» (2 минуты), иначе следующее событие скажется в сессии, а не звонком
r7 = run(proactive.deliver("reminder", "Напоминание: седьмое", item="седьмое"))
check("26e4. не больше 6 звонков в час: шесть прошли, седьмой в тот же час -> Телеграм (limit_hour)", res == ["called"] * 6 and r7 == "tg:limit_hour" and len(RUNS35) == 6, str((res, r7)))
CLK24["t"] += 3600
r8 = run(proactive.deliver("reminder", "Напоминание: восьмое", item="восьмое"))
check("26e5. через час окно освободилось -> звонок снова проходит", r8 == "called" and len(RUNS35) == 7, r8)
proactive.GAP_SEC = 600
_r35()
for t in ("молоко", "хлеб", "яйца"):
    add_rem35(t)
due35()
check("26e6. три напоминания подошли вместе -> ОДИН звонок (сценарий запущен один раз)", len(RUNS35) == 1 and len(proactive._data()["events"]) == 3 and not LIC24.notes, str(RUNS35))
tt = sess35("", True)["text"]
check("26e7. Кореш перечисляет всё одной фразой: «у меня для тебя несколько вещей. Первое: молоко. Второе: хлеб. Третье: яйца.»; очередь пуста",
      tt == "Слушай, у меня для тебя несколько вещей. Первое: молоко. Второе: хлеб. Третье: яйца." and not proactive._data()["events"], tt)
_r35()
add_rem35("A")
due35()
CLK24["t"] += 40
add_rem35("B")
due35()
check("26e8. второе событие подошло, пока первый звонок ещё ждёт открытия навыка -> присоединяется к нему, второго звонка нет", len(RUNS35) == 1 and len(proactive._data()["events"]) == 2)
check("26e9. присоединённое говорится в той же фразе", "Первое: A. Второе: B." in sess35("", True)["text"])

# ================= 26f. голосовые «не звони» / «можешь звонить» =================
OFFT = ["не звони мне сегодня", "Не звони сегодня", "кореш не звони мне сегодня пожалуйста", "сегодня не звони", "не звони мне до завтра", "не звони весь день"]
OFFF = ["больше не звони", "Больше не звони мне", "больше не звони первым", "не звони мне больше", "не звони первым", "не начинай разговор сам", "выключи звонки", "ну больше не звони пожалуйста"]
ONF = ["можешь звонить", "ты можешь мне звонить", "можешь звонить первым", "давай звони первым", "звони мне первым", "включи звонки", "начинай звонить снова"]
NOTF = ["позвони маме", "не звони маме", "можешь позвонить маме", "не звони никому", "звони маме сегодня", "включи свет", "что такое звонок", "не звони",
        "пожалуйста позвони мне сегодня вечером", "больше не звони маме"]
check("26f1. фразы «не звони мне сегодня» узнаются", all(proactive.command(x.lower()) == "off_today" for x in OFFT), str([x for x in OFFT if proactive.command(x.lower()) != "off_today"]))
check("26f2. фразы «больше не звони (первым)» узнаются", all(proactive.command(x.lower()) == "off" for x in OFFF), str([x for x in OFFF if proactive.command(x.lower()) != "off"]))
check("26f3. фразы «можешь звонить» узнаются", all(proactive.command(x.lower()) == "on" for x in ONF), str([x for x in ONF if proactive.command(x.lower()) != "on"]))
check("26f4. похожие фразы («позвони маме», «не звони маме», «можешь позвонить маме»…) НЕ узнаются", all(proactive.command(x.lower()) is None for x in NOTF), str([x for x in NOTF if proactive.command(x.lower())]))
_r35()
S = Script24()
brain._create = S
t1 = sess35("не звони мне сегодня", False)["text"]
st = proactive.status()
add_rem35()
due35()
check("26f5. «не звони мне сегодня»: честный ответ голосом, модель не звали, пауза до конца местных суток; напоминание сегодня уходит в Телеграм без звонка",
      "сегодня не звоню" in t1 and not S.calls and st["paused"] and not RUNS35 and len(tg35()) == 1, t1)
CLK24["t"] = _local24(9, 0, day=15)
add_rem35("завтрашнее")
due35()
check("26f6. наступил следующий день -> пауза кончилась сама, звонок проходит", len(RUNS35) == 1 and not proactive.status()["paused"])
sess35("", True)
LIC24.notes.clear()
t2 = sess35("больше не звони первым", False)["text"]
proactive.reload()
CLK24["t"] += 3600 * 2
add_rem35("после отказа")
due35()
check("26f7. «больше не звони первым»: выключает насовсем (переживает перезапуск), напоминание идёт в Телеграм, сценарий не запускают",
      "сам больше не звоню" in t2 and proactive.status()["enabled"] is False and len(RUNS35) == 1 and len(tg35()) == 1 and "после отказа" in tg35()[0]["text"], t2)
t3 = sess35("можешь звонить", False)["text"]
CLK24["t"] += 3600
add_rem35("снова можно")
due35()
check("26f8. «можешь звонить» включает обратно: ответ голосом, следующее напоминание снова звонком", "буду звонить сам" in t3 and proactive.status()["enabled"] and len(RUNS35) == 2, t3)
_r35(conf=False)
t4 = sess35("можешь звонить", False)["text"]
check("26f9. «можешь звонить», а сценарий не настроен -> честно: «сценарий-звонок ещё не настроен, ссылка в боте»", "сценарий-звонок" in t4 and "не настроен" in t4, t4)
_r35(mods=())
t5, t6 = sess35("больше не звони", False)["text"], sess35("можешь звонить", False)["text"]
check("26f10. модуля «Напоминания» нет -> честно: «и так не звоню, функция не подключена»; «можешь звонить» -> «подключается отдельно»",
      "не подключена" in t5 and "подключается отдельно" in t6, f"{t5} | {t6}")

# ================= 26g. человек уже в сессии =================
_r35()
S = Script24(text_resp("Привет, слушаю."), text_resp("Хорошо, слушаю дальше."))
brain._create = S
sess35("привет", True)
CLK24["t"] += 30
add_rem35("позвонить маме")
due35()
check("26g1. человек в сессии (говорил 30 секунд назад) -> НЕ звоним: сценарий не запускали, в Телеграм пока тоже нет, событие ждёт реплики",
      not RUNS35 and not LIC24.notes and len(proactive._data()["events"]) == 1 and proactive._data()["events"][0]["state"] == "session")
rp = sess35("а ты как", False)["text"]
check("26g2. со следующей реплики Кореш говорит в текущей сессии: «Кстати, ты просил напомнить: позвонить маме.» и дальше ответ на реплику",
      rp.startswith("Кстати, ты просил напомнить: позвонить маме.") and rp.endswith("Хорошо, слушаю дальше.") and not proactive._data()["events"] and not LIC24.notes, rp)
_r35()
brain._create = Script24(text_resp("Привет."))
sess35("привет", True)
CLK24["t"] += 30
add_rem35()
due35()
CLK24["t"] += 200
run(proactive.tick())
check("26g3. в сессии, но человек молчит 3 минуты -> напоминание не теряется: фолбэк в Телеграм", len(tg35()) == 1 and not RUNS35)
_r35()
brain._create = Script24(text_resp("Привет."))
sess35("привет", True)
sess35("пока", False)
CLK24["t"] += 20
add_rem35()
due35()
check("26g4. человек попрощался («пока») -> он уже не в сессии, звоним", len(RUNS35) == 1)
_r35()
brain._create = Script24(text_resp("Привет."))
sess35("привет", True)
CLK24["t"] += 300
add_rem35()
due35()
check("26g5. последний раз говорили 5 минут назад -> сессия давно кончилась, звоним", len(RUNS35) == 1)
_r35()
add_rem35()
due35()
rsl = run(main.handle(setupwiz.SELFTEST_UID, "", True))["response"]["text"]
check("26g6. проверка мастера /setup (служебный пользователь) чужой звонок не забирает: событие осталось ждать настоящую Станцию",
      "малого" not in rsl and len(proactive._data()["events"]) == 1)
check("26g7. настоящая Станция следом забирает", "малого" in sess35("", True)["text"])

# ================= 26h. честность при настройке напоминания =================
_r35(conf=False)
r1 = run(modules.reminder_draft(UID26, "remind", "забрать малого", None, 40, None, None, "none"))
check("26h1. сценарий-звонок не настроен -> при настройке напоминания Кореш зачитывает: «в Телеграм… настрой сценарий-звонок, ссылка в боте»",
      r1.ok and "в Телеграм" in r1.text and "сценарий-звонок" in r1.text and "ссылка в боте" in r1.text and "забрать малого" in r1.text and "Создать?" in r1.text, r1.text)
r2 = run(modules.reminder_draft(UID26, "remind", "купить хлеб", None, 40, None, None, "none"))
check("26h2. подсказка не долбит: второй раз в те же сутки её нет", "сценарий-звонок" not in r2.text and "купить хлеб" in r2.text, r2.text)
CLK24["t"] += 25 * 3600
r3 = run(modules.reminder_draft(UID26, "remind", "купить воду", None, 40, None, None, "none"))
check("26h3. через сутки подсказка возвращается", "сценарий-звонок" in r3.text)
rb = modules.ensure_readback(UID26, "Напомню через 40 минут купить воду. Создать?")
check("26h4. модель выкинула подсказку из зачитки -> хозяин всё равно слышит каноничную фразу с ней", "сценарий-звонок" in rb and "купить воду" in rb, rb)
_r35()
r4 = run(modules.reminder_draft(UID26, "remind", "забрать малого", None, 40, None, None, "none"))
check("26h5. сценарий настроен -> подсказки нет, зачитка как раньше", "сценарий-звонок" not in r4.text and "Телеграм" not in r4.text and "Создать?" in r4.text, r4.text)
_r35(conf=False)
r5 = run(modules.reminder_draft(UID26, "action", None, act24("lamp-bed", False, "выключить свет"), 40, None, None, "none"))
check("26h6. подсказка только про напоминания (действие по времени её не получает)", "сценарий-звонок" not in r5.text and "Создать?" in r5.text)

# ================= 26i. серверные ручки страницы =================
_r35()
os.environ["SETUP_CODE"] = SET25
setupwiz.reset_for_tests()
C.set_setup_done(True)
tc = TestClient(main.app)
EP26 = [("get", "state"), ("post", "find"), ("post", "select"), ("post", "test"), ("get", "test-status"), ("post", "settings")]
anon = {f"{m} {p}": getattr(tc, m)(f"/setup/api/moduli/call/{p}", **({"json": {}} if m == "post" else {})).status_code for m, p in EP26}
check("26i1. без входа все шесть ручек отвечают 401", set(anon.values()) == {401}, str(anon))
rlg = tc.post("/setup/api/moduli/login", json={"code": SET25})
check("26i2. вход по тому же коду, что и в модули (сессия «только модули»)", rlg.status_code == 200 and rlg.json()["scope"] == "moduli", rlg.text)
cs = tc.get("/setup/api/moduli/call/state").json()
check("26i3. state: модуль включён, сценарий «Кореш зайди», звонить можно, тихие часы 23-8, активационное имя «кореш дома», команда «спроси у кореш дома что нового»; токена нет",
      cs["ok"] and cs["module"] and cs["configured"] and cs["scenario"]["name"] == "Кореш зайди" and cs["enabled"] and cs["quiet"] == {"from": 23, "to": 8}
      and cs["activation"] == "кореш дома" and cs["command"] == "спроси у кореш дома что нового" and "test-iot-token" not in json.dumps(cs), str(cs))
fnd = tc.post("/setup/api/moduli/call/find", json={"activation": "кореш дома"}).json()
check("26i4. «Найти мой сценарий»: кандидаты «Кореш зайди» и «Кино» (выключенный и «ворота» спрятаны), лучший отмечен, спрятано 2",
      fnd["ok"] and [c["id"] for c in fnd["candidates"]] == ["sc-call", "sc-kino"] and fnd["suggested"] == "sc-call" and fnd["hidden"] == 2 and fnd["total"] == 4, str(fnd))
_save_user = list(USER35["scenarios"])
USER35["scenarios"] = [{"id": "sc-a", "name": "Кореш зайди", "is_active": True}, {"id": "sc-b", "name": "Кореш позвони", "is_active": True}]
fnd2 = tc.post("/setup/api/moduli/call/find", json={}).json()
USER35["scenarios"] = _save_user
check("26i5. два одинаково похожих сценария -> лучшего не навязываем (suggested пусто), выбирает человек", fnd2["ok"] and len(fnd2["candidates"]) == 2 and fnd2["suggested"] == "", str(fnd2))
C.save_values({"CALL_SCENARIO": None, "CALL_SCENARIO_NAME": None})
bad = {k: tc.post("/setup/api/moduli/call/select", json={"id": k}) for k in ("sc-gate", "sc-off", "sc-none", "../x")}
check("26i6. выбрать нельзя: «ворота» (400 protected), выключенный (400 inactive), несуществующий (404), кривой id (400)",
      bad["sc-gate"].status_code == 400 and bad["sc-gate"].json()["error"] == "protected" and bad["sc-off"].json()["error"] == "inactive"
      and bad["sc-none"].status_code == 404 and bad["../x"].status_code == 400 and not C.env("CALL_SCENARIO"), str({k: v.text for k, v in bad.items()}))
ok = tc.post("/setup/api/moduli/call/select", json={"id": "sc-call"})
check("26i7. «Кореш зайди» выбирается: записано в config.json, в state видно имя, звонок теперь настроен",
      ok.status_code == 200 and ok.json()["scenario"] == {"id": "sc-call", "name": "Кореш зайди"} and C.stored("CALL_SCENARIO") == "sc-call" and proactive.configured(), ok.text)
RUNS35.clear()
tt1 = tc.post("/setup/api/moduli/call/test", json={})
st0 = tc.get("/setup/api/moduli/call/test-status").json()
check("26i8. «Проверить звонок»: запущен выбранный сценарий, статус «ждём», в лимиты звонков это не засчитано", tt1.status_code == 200 and RUNS35 == [("sc-call", True, True)]
      and st0["status"] == "waiting" and not proactive._data()["calls"], tt1.text)
tt2 = tc.post("/setup/api/moduli/call/test", json={})
check("26i9. кнопку не спамят: повтор раньше чем через 15 секунд -> 429", tt2.status_code == 429 and len(RUNS35) == 1, tt2.text)
S = Script24()
brain._create = S
rtest = sess35("", True)["text"]
st1 = tc.get("/setup/api/moduli/call/test-status").json()
check("26i10. Станция открыла навык -> «Это проверка звонка. Я на связи», модель не звали, статус «дозвонился» (answered)", "проверка звонка" in rtest and not S.calls and st1["status"] == "answered", rtest)
CLK24["t"] += 20
tc.post("/setup/api/moduli/call/test", json={})
CLK24["t"] += 200
st2 = tc.get("/setup/api/moduli/call/test-status").json()
run(proactive.tick())
check("26i11. навык не открылся за 3 минуты -> статус «не ответил» (no_answer), в Телеграм проверка не уходит", st2["status"] == "no_answer" and not LIC24.notes, str(st2))
SC35["mode"] = "404"
CLK24["t"] += 20
tf = tc.post("/setup/api/moduli/call/test", json={})
stf = tc.get("/setup/api/moduli/call/test-status").json()
SC35["mode"] = "ok"
check("26i12. сценария уже нет у Яндекса -> «Проверить звонок» говорит правду (400 not_found), статус failed", tf.status_code == 400 and tf.json()["error"] == "not_found" and stf["status"] == "failed", tf.text)
CLK24["t"] = _local24(23, 40, day=14)
RUNS35.clear()
proactive._data()["test"] = {}
tq = tc.post("/setup/api/moduli/call/test", json={})
check("26i13. «Проверить звонок» ночью работает (человек сам нажал, тихие часы не мешают)", tq.status_code == 200 and len(RUNS35) == 1)
sg = tc.post("/setup/api/moduli/call/settings", json={"quiet_from": "22", "quiet_to": "7", "enabled": True})
check("26i14. тихие часы 22-7 сохраняются и применяются сразу", sg.status_code == 200 and sg.json()["quiet"] == {"from": 22, "to": 7} and proactive.quiet_hours() == (22, 7), sg.text)
sg2 = tc.post("/setup/api/moduli/call/settings", json={"quiet_from": "25", "quiet_to": "x"})
check("26i15. кривые часы -> 400, прежние остались", sg2.status_code == 400 and proactive.quiet_hours() == (22, 7))
sg3 = tc.post("/setup/api/moduli/call/settings", json={"quiet_from": "23", "quiet_to": "8"})
check("26i16. 23-8 это умолчание: в файл не пишем (строка убрана)", sg3.status_code == 200 and not C.stored("CALL_QUIET") and proactive.quiet_hours() == (23, 8))
tc.post("/setup/api/moduli/call/settings", json={"enabled": False})
CLK24["t"] = _local24(12, 0, day=14)
add_rem35("после выключателя")
RUNS35.clear()
due35()
check("26i17. выключатель «звонить первым» на странице -> звонков нет, напоминание в Телеграм", not RUNS35 and len(tg35()) == 1 and tc.get("/setup/api/moduli/call/state").json()["enabled"] is False)
tc.post("/setup/api/moduli/call/settings", json={"enabled": True})
rm = tc.post("/setup/api/moduli/call/select", json={"id": ""})
check("26i18. «Убрать сценарий»: настройка очищена, звонок снова не настроен", rm.status_code == 200 and rm.json()["scenario"] is None and not proactive.configured())
_mods24()
off_mod = {p: getattr(tc, m)(f"/setup/api/moduli/call/{p}", **({"json": {}} if m == "post" else {})).status_code for m, p in EP26 if p not in ("state", "test-status")}
check("26i19. модуль «Напоминания» не подключён -> find/select/test/settings отвечают 403 no_module, state честно говорит module=false",
      set(off_mod.values()) == {403} and tc.get("/setup/api/moduli/call/state").json()["module"] is False, str(off_mod))
_mods24("timers")
tw = TestClient(main.app)
tw.post("/setup/api/login", json={"code": SET25, "reopen": True})
check("26i20. полная сессия мастера /setup те же ручки видит (блок есть и на /setup)", tw.get("/setup/api/moduli/call/state").status_code == 200)
setupwiz.reset_for_tests()
C.set_setup_done(False)
os.environ.pop("SETUP_CODE", None)

# ================= 26j. страницы /setup и /setup/moduli =================
mod26 = (HERE / "setupmod.html").read_text(encoding="utf-8")
wiz26 = (HERE / "setupwiz.html").read_text(encoding="utf-8")
check("26j1. в обеих страницах есть блок «Кореш может начинать разговор сам», кнопки «Найти мой сценарий» и «Проверить звонок», все ручки под moduli/call",
      all(("Кореш может начинать разговор сам" in x and "Найти мой сценарий" in x and "Проверить звонок" in x and "CALLP + 'find'" in x) for x in (mod26, wiz26)))
check("26j2. страница модулей по-прежнему ходит только в /setup/api/moduli/*", set(re.findall(r"/setup/api/([a-z/-]+)", mod26)) <= {"moduli/"})
check("26j3. Dockerfile копирует proactive.py (иначе в образе звонка нет)", "proactive.py" in (HERE / "Dockerfile").read_text(encoding="utf-8"))
if not shutil.which("node"):
    print("SKIP  26j4-26j12. node не найден: проверки отрисовки страниц пропущены (в CI node есть)")
else:
    DOM35 = DOM_JS.replace("stepIot, stepAlice, stepCheck, renderLogin, stepWa, renderWaOnly}", "stepIot, stepAlice, stepCheck, renderLogin, stepWa, renderWaOnly, callBlock}")
    DOM35M = DOM_JS.replace("stepIot, stepAlice, stepCheck, renderLogin, stepWa, renderWaOnly}", "renderMain, callBlock}").replace(
        "Element, console, Promise", "Element, console, location: {origin: 'https://k.test'}, Promise")
    ST_BASE = {"ok": True, "module": True, "iot": True, "configured": False, "scenario": None, "enabled": True, "paused": False, "quiet": {"from": 23, "to": 8},
               "last": {}, "test": {"status": "none"}, "activation": "кореш дома", "command": "спроси у кореш дома что нового"}
    ST_SEL = dict(ST_BASE, configured=True, scenario={"id": "sc-call", "name": "Кореш зайди"})
    FIND = {"ok": True, "candidates": [{"id": "sc-call", "name": "Кореш зайди", "score": 4}, {"id": "sc-kino", "name": "Кино", "score": 0}], "suggested": "sc-call", "total": 4, "hidden": 2}
    scen26 = [
        {"fn": "callBlock", "S": _S(), "fetch": {"moduli/call/state": ST_BASE, "moduli/call/find": FIND, "moduli/call/select": dict(ST_SEL, ok=True)},
         "seq": [{"click": "Найти мой сценарий"}, {"click": "Выбрать этот сценарий"}]},                                                         # 0
        {"fn": "callBlock", "S": _S(), "fetch": {"moduli/call/state": ST_SEL, "moduli/call/test": {"ok": True, "started": True},
                                                "moduli/call/test-status": {"ok": True, "status": "answered", "message": ""}}, "seq": [{"click": "Проверить звонок"}]},   # 1
        {"fn": "callBlock", "S": _S(), "fetch": {"moduli/call/state": ST_SEL, "moduli/call/test": {"ok": True, "started": True},
                                                "moduli/call/test-status": {"ok": True, "status": "no_answer", "message": ""}}, "seq": [{"click": "Проверить звонок"}]},   # 2
        {"fn": "callBlock", "S": _S(), "fetch": {"moduli/call/state": dict(ST_BASE, iot=False)}},                                              # 3
        {"fn": "callBlock", "S": _S(), "fetch": {"moduli/call/state": dict(ST_SEL, last={"ok": False, "error": "not_found"}, enabled=False, paused=True)}},   # 4
        {"fn": "callBlock", "S": _S(), "fetch": {"moduli/call/state": ST_BASE, "moduli/call/find": dict(FIND, candidates=[], suggested="")}, "seq": [{"click": "Найти мой сценарий"}]},   # 5
        {"fn": "callBlock", "S": _S(), "fetch": {"moduli/call/state": ST_SEL, "moduli/call/settings": dict(ST_SEL, ok=True, persisted=True)}, "seq": [{"click": "Сохранить"}]},   # 6
        {"fn": "stepCheck", "S": dict(_S(), modules={"timers": True}), "fetch": {"heard": {"ok": True, "heard": False, "now": 1}, "moduli/call/state": ST_BASE}},    # 7
        {"fn": "stepCheck", "S": dict(_S(), modules={"macros": True}), "fetch": {"heard": {"ok": True, "heard": False, "now": 1}}},                              # 8
        {"fn": "callBlock", "S": _S(), "fetch": {"moduli/call/state": ST_BASE, "moduli/call/find": FIND}, "seq": [{"click": "Найти мой сценарий"}]},               # 9
    ]
    _j26 = Path(tempfile.mkdtemp()) / "dom.js"
    _j26.write_text(DOM35, encoding="utf-8")
    _j26m = Path(tempfile.mkdtemp()) / "dom.js"
    _j26m.write_text(DOM35M, encoding="utf-8")
    p26 = subprocess.run(["node", str(_j26), str(HERE / "setupwiz.html"), json.dumps(scen26, ensure_ascii=False)], capture_output=True, text=True, timeout=90)
    ok26 = p26.returncode == 0 and p26.stdout.strip().startswith("[")
    check("26j4. блок отрисовывается в настоящем JS мастера /setup (10 сценариев)", ok26, (p26.stderr or p26.stdout)[:400])
    if ok26:
        R26 = json.loads(p26.stdout.strip().splitlines()[-1])
        T = [_text(x["tree"]) if x["tree"] else "" for x in R26]
        fl = lambda i: [(c["k"], c["body"]) for c in R26[i]["calls"]]       # noqa: E731
        check("26j5. инструкция по шагам: Дом с Алисой -> Сценарии -> «+», условие фраза, действие «Вопрос или команда Алисе», команда «спроси у кореш дома что нового»",
              all(x in T[3] for x in ("Дом с Алисой", "Сценарии", "Вопрос или команда Алисе", "спроси у кореш дома что нового", "кореш зайди", "Найти мой сценарий")), T[3][:300])
        check("26j6. «Найти мой сценарий» шлёт активационное имя, лучший кандидат уже отмечен; «Выбрать» шлёт его id; сценарий выбран, видно «Проверить звонок»",
              ("moduli/call/find", {"activation": "кореш дома"}) in fl(0) and ("moduli/call/select", {"id": "sc-call"}) in fl(0), str(fl(0)))
        check("26j7. после «Найти» честно сказано: Яндекс не показывает содержимое сценария, выбор по названию; лучший помечен «похоже на твой»",
              "Яндекс не показывает, что внутри сценария" in T[9] and "Кореш зайди (похоже на твой)" in T[9] and "Кино" in T[9], T[9][-300:])
        check("26j8. «Проверить звонок» -> запуск, опрос статуса, при успехе «Дозвонился…»; при тишине «навык не открылся»",
              "Дозвонился" in T[1] and ("moduli/call/test", {}) in fl(1) and "навык не открылся" in T[2], T[1][-200:] + " || " + T[2][-200:])
        check("26j9. умный дом не подключён -> жёлтое «Сначала подключи умный дом»", "Сначала подключи умный дом" in T[3])
        check("26j10. прошлый звонок не вышел, звонки выключены и пауза на сегодня -> всё это написано человеческими словами",
              "Последний звонок не вышел" in T[4] and "сценарий не найден" in T[4] and "Звонить первым выключено" in T[4] and "Сегодня не звоню" in T[4], T[4][-400:])
        check("26j11. нет подходящих сценариев -> подсказка создать по шагам, а не пустота", "Подходящих сценариев не вижу" in T[5])
        check("26j12. сохранение шлёт тихие часы и выключатель", ("moduli/call/settings", {"quiet_from": "23", "quiet_to": "8", "enabled": True}) in fl(6), str(fl(6)))
        check("26j13. на шаге «Проверка» блок есть только при включённых «Напоминаниях» (без флага ручки не дёргаются)",
              ("moduli/call/state", None) in fl(7) and "Кореш может начинать разговор сам" in T[7] and "Кореш может начинать разговор сам" not in T[8]
              and not [c for c in fl(8) if c[0].startswith("moduli/call")], str((fl(7), fl(8))))
    SM = {"version": "dev", "name": "Кореш", "tz_offset": {"value": 5, "source": "default"}, "kids_phrase": {"set": False, "source": "default"},
          "balance": {"budget_usd": 5, "spent_usd": 0, "remaining_usd": 5, "days_left": None},
          "modules": [{"id": "macros", "title": "Макросы", "about": "", "on": False}, {"id": "timers", "title": "Напоминания", "about": "", "on": True},
                      {"id": "kids", "title": "Детский режим", "about": "", "on": False}]}
    SM_OFF = dict(SM, modules=[dict(m, on=False) for m in SM["modules"]])
    scenm = [{"fn": "callBlock", "S": SM, "fetch": {"moduli/call/state": ST_SEL}},        # холостой: даёт странице дорисовать собственный экран входа, чтобы он не затёр следующие
             {"fn": "renderMain", "S": SM, "fetch": {"moduli/call/state": ST_SEL}}, {"fn": "renderMain", "S": SM_OFF, "fetch": {}}]
    pm26 = subprocess.run(["node", str(_j26m), str(HERE / "setupmod.html"), json.dumps(scenm, ensure_ascii=False)], capture_output=True, text=True, timeout=60)
    okm26 = pm26.returncode == 0 and pm26.stdout.strip().startswith("[")
    check("26j14. страница /setup/moduli отрисовывается в настоящем JS", okm26, (pm26.stderr or pm26.stdout)[:400])
    if okm26:
        RM26 = json.loads(pm26.stdout.strip().splitlines()[-1])
        tm1, tm2 = _text(RM26[1]["app"]), _text(RM26[2]["app"])
        check("26j15. /setup/moduli: блок звонка виден при включённых «Напоминаниях» (с выбранным сценарием), без них его нет и ручки не дёргаются",
              "Кореш может начинать разговор сам" in tm1 and "Выбран сценарий: «Кореш зайди»" in tm1 and "Проверить звонок" in tm1
              and "Кореш может начинать разговор сам" not in tm2 and not [c for c in RM26[2]["calls"] if c["k"].startswith("moduli/call")], tm1[-300:])

# ================= 26m. честность: болтовня с «сделал» не подменяется (баг с живого Кореша: «поматерись» -> «команда до умного дома не дошла») =================
_r35()
CHAT26 = [("поматерись", "Хуясе, ну ты даёшь. Ладно, давай, сделал по-твоему: хуй с ним."), ("расскажи анекдот", "Ладно, давай. Сделал тебе анекдот: колобок повесился."),
          ("ты как", "Нормально. Вчера столько всего сделал, что сам охуел."), ("а прикинь", "Ну ты сказанул. Всё, поставил точку.")]
for i, (q, a) in enumerate(CHAT26):
    brain._create = Script24(text_resp(a))
    fresh("h" + str(i))
    got = say24("h" + str(i), q)
    check(f"26m{i + 1}. болтовня «{q}»: ответ с «сделал/поставил», но действия никто не просил и инструмент не звали -> ответ остаётся как есть", got == a, got)
brain._create = Script24(text_resp("Включил."))
fresh("h5")
g5 = say24("h5", "включи свет")
check("26m5. «включи свет» без успешного вызова инструмента -> по-прежнему подменяется честной фразой", "Включил." not in g5 and "не дошла" in g5, g5)
brain._create = Script24(tr("t1", "list_devices", {}), tr("t2", "control_device", {"target": "device", "target_id": "lamp-bed", "capability": "devices.capabilities.on_off", "instance": "on", "value": False}),
                         text_resp("Выключил."))
BROKEN24.add("lamp-bed")
fresh("h6")
g6 = say24("h6", "а теперь так")
BROKEN24.discard("lamp-bed")
check("26m6. реплика без явной просьбы, но модель пыталась управлять домом и не вышло -> её «Выключил.» всё равно подменяется", "Выключил." not in g6 and "не на связи" in g6, g6)
check("26m7. намерение распознаётся: «включи свет», «выключи розетку», «запусти сценарий кино», «напомни в шесть», «сделай ярче» -- да; «поматерись», «расскажи анекдот», «как дела» -- нет",
      all(honesty.wants_action(x) for x in ("включи свет", "выключи розетку", "запусти сценарий кино", "напомни в шесть", "сделай ярче", "Закрой шторы")) and
      not any(honesty.wants_action(x) for x in ("поматерись", "расскажи анекдот", "как дела", "ладно давай")) and honesty.wants_action(None))
# код ответа Яндекса в журнале (401 это или 429), без токена
def _y429(request):
    return httpx.Response(429, json={"status": "error"}) if request.url.path.endswith("/user/info") else httpx.Response(200, json={})
iot._TRANSPORT = httpx.MockTransport(_y429)
iot.reset_cache()
brain._create = Script24(tr("t1", "list_devices", {}), text_resp("Не смог посмотреть."))
fresh("h8")
b8 = io.StringIO()
with contextlib.redirect_stdout(b8):
    say24("h8", "что у меня включено")
check("26m8. сбой списка устройств пишет в журнал код ответа: «tool error [list_devices]: HTTPStatusError 429», токена в журнале нет",
      "tool error [list_devices]: HTTPStatusError 429" in b8.getvalue() and "test-iot-token" not in b8.getvalue(), b8.getvalue()[-200:])
iot._TRANSPORT = httpx.MockTransport(_y35)
iot.reset_cache()

# ================= 26k. секреты =================
_r35()
buf26 = io.StringIO()
with contextlib.redirect_stdout(buf26):
    add_rem35("секретное дело")
    due35()
    sess35("", True)
    SC35["mode"] = "500"
    add_rem35("второе секретное")
    CLK24["t"] += 700
    due35()
    SC35["mode"] = "ok"
out26 = buf26.getvalue()
check("26k1. в журнале нет токена умного дома, токена обновления и текста напоминаний; события пишутся без содержания",
      "test-iot-token" not in out26 and RT24 not in out26 and "секретное" not in out26 and "proactive:" in out26, out26[-400:])
check("26k2. очередь звонков на диске не хранит токенов", "test-iot-token" not in (Path(_DIR24) / "proactive.json").read_text(encoding="utf-8") if (Path(_DIR24) / "proactive.json").exists() else True)

# ---------- хвост: вернуть окружение ----------
iot._TRANSPORT = _IOT26_SAVED
proactive._now = time.time
proactive.GAP_SEC, proactive.HOUR_MAX = 600, 6
proactive.reset_for_tests()
modules.reset_for_tests()
notify._now = time.time
modules._now = time.time
C.set_modules({})
brain._create = _boom_create
setupwiz.reset_for_tests()
C.set_setup_done(False)
for _k, _v in _SAVED26.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v
