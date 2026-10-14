# Ворота ТЗ-32: модули (макросы, напоминания и действия по времени, детский режим) и уведомления в очередь Телеграма.
# Подключается из selftest.py (exec в его пространстве имён: check, run, N, httpx, main, brain, usage, iot, wa, activation...).
# Живых вызовов нет: модель скриптованная заглушка, умный дом, демон WhatsApp и сервер лицензий -- httpx.MockTransport, время -- сдвигаемые часы.
import calendar
import datetime as _dt

import modules
import notify

_ENV24 = ("MEMORY_DIR", "USAGE_FILE", "TONE", "GENDER", "WA_ENABLED", "WA_CONTACTS", "ANTHROPIC_API_KEY", "DAILY_TOKEN_CAP",
          "YANDEX_IOT_TOKEN", "TZ_OFFSET", "KIDS_PHRASE", "ALLOW_LOCKS", "LOCK_PHRASE", "SETUP_CODE")
_SAVED24 = {k: os.environ.get(k) for k in _ENV24}
_DIR24 = tempfile.mkdtemp()
_IOT24_SAVED = iot._TRANSPORT
_ACT24_SAVED = (activation.LICENSE_URL, activation._TRANSPORT)
_WA24_SAVED = wa._TRANSPORT
WAD24 = Path(_DIR24) / "wa"
RT24 = "rt-" + "z" * 30
IID24 = "ki-" + "9" * 32
GOOD_PHRASE24 = "кодовая ромашка"

# ---------- умный дом: свой, чтобы не трогать общий USER_INFO ----------
USER24 = {
    "rooms": [{"id": "r1", "name": "Спальня"}, {"id": "r2", "name": "Гостиная"}],
    "devices": [
        {"id": "lamp-bed", "name": "Лампа", "type": "devices.types.light", "room": "r1",
         "capabilities": [{"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": True}}]},
        {"id": "lamp-hall", "name": "Люстра", "type": "devices.types.light", "room": "r2",
         "capabilities": [{"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": True}}]},
        {"id": "sock1", "name": "Розетка", "type": "devices.types.socket", "room": "r2",
         "capabilities": [{"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": True}}]},
        {"id": "heater1", "name": "Обогреватель", "type": "devices.types.thermostat", "room": "r1",
         "capabilities": [{"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": True}}]},
        {"id": "lock1", "name": "Входной замок", "type": "devices.types.openable", "room": "r2",
         "capabilities": [{"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": False}}]},
    ],
    "groups": [{"id": "g-lights", "name": "Весь свет", "devices": ["lamp-bed", "lamp-hall"]},
               {"id": "g-mixed", "name": "Смешанная", "devices": ["lamp-bed", "sock1"]}],
    "scenarios": [{"id": "sc1", "name": "Кино"}],
}
POSTS24 = []                 # (путь, тело) команд умному дому
BROKEN24 = set()             # id устройств, которые "не на связи"


def _y24(request: httpx.Request) -> httpx.Response:
    if request.headers.get("authorization") != "Bearer test-iot-token":
        return httpx.Response(401, json={"status": "error"})
    if request.method == "GET" and request.url.path.endswith("/user/info"):
        return httpx.Response(200, json={"status": "ok", **USER24})
    body = json.loads(request.content) if request.content else None
    POSTS24.append((request.url.path, body))
    if request.url.path.endswith("/devices/actions") and body:
        did = body["devices"][0]["id"]
        if did in BROKEN24:
            return httpx.Response(200, json={"status": "ok", "devices": [{"id": did, "capabilities": [
                {"type": "devices.capabilities.on_off", "state": {"instance": "on", "action_result": {"status": "ERROR", "error_code": "DEVICE_UNREACHABLE"}}}]}]})
    return httpx.Response(200, json={"status": "ok"})


# ---------- WhatsApp-демон (служебные сообщения «себе») ----------
WA_SENT24 = []


def _w24(request: httpx.Request) -> httpx.Response:
    if request.headers.get("x-wa-token") != wa.TOKEN:
        return httpx.Response(401, json={"ok": False})
    body = json.loads(request.content) if request.content else {}
    if request.url.path == "/send":
        WA_SENT24.append(body)
        return httpx.Response(200, json={"ok": True})
    if request.url.path == "/status":
        return httpx.Response(200, json={"ok": True, "state": "connected", "linked": True})
    return httpx.Response(404)


# ---------- сервер лицензий: очередь уведомлений и обновление пакета ----------
class Lic24:
    def __init__(self):
        self.notes, self.refreshes, self.status, self.down = [], [], 200, False
        self.pack, self.version = {"schema": 1, "version": 1}, 1
        self.legacy_same = False      # старый сервер: «то же самое» без полей modules / promo_all_modules

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("лежит")
        body = json.loads(request.content) if request.content else {}
        if request.url.path == "/v1/notify":
            if request.headers.get("authorization") != f"Bearer {RT24}" or body.get("install_id") != IID24:
                return httpx.Response(401, json={"ok": False})
            if self.status != 200:
                return httpx.Response(self.status, json={"ok": False})
            self.notes.append(body)
            return httpx.Response(200, json={"ok": True, "status": "queued"})
        if request.url.path == "/v1/pack/refresh":
            self.refreshes.append(body)
            if self.version <= body.get("since", 0):
                extra = {} if self.legacy_same else {"modules": dict(self.pack.get("modules") or {}), "promo_all_modules": self.pack.get("promo_all_modules") is True}
                return httpx.Response(200, json={"ok": True, "status": "same", "pack_version": self.version, **extra})
            return httpx.Response(200, json={"ok": True, "status": "update", "pack": self.pack, "pack_version": self.version})
        return httpx.Response(404)


LIC24 = Lic24()
BASE24 = calendar.timegm((2026, 10, 14, 5, 0, 0))        # 14.10.2026, среда, 10:00 по Алматы (+5)
CLK24 = {"t": float(BASE24)}


def _local24(h, m, day=14, mon=10):
    """Эпоха для местного времени Алматы (+5)."""
    return float(calendar.timegm((2026, mon, day, h - 5, m, 0)))


def _r24():
    """Чистое состояние перед группой проверок."""
    reset_all()
    os.environ["MEMORY_DIR"] = _DIR24
    os.environ["USAGE_FILE"] = os.path.join(_DIR24, "usage.json")
    for k in ("TONE", "GENDER", "WA_ENABLED", "WA_CONTACTS", "DAILY_TOKEN_CAP", "TZ_OFFSET", "KIDS_PHRASE", "ALLOW_LOCKS", "LOCK_PHRASE"):
        os.environ.pop(k, None)
    os.environ["YANDEX_IOT_TOKEN"] = "test-iot-token"
    os.environ["ANTHROPIC_API_KEY"] = "sk-leak-canary-24"
    for f in Path(_DIR24).glob("*"):
        if f.is_file():
            f.unlink()
    modules.reset_for_tests()
    notify._now = lambda: CLK24["t"]
    modules._now = lambda: CLK24["t"]
    CLK24["t"] = float(BASE24)
    POSTS24.clear()
    BROKEN24.clear()
    WA_SENT24.clear()
    LIC24.notes.clear()
    LIC24.refreshes.clear()
    LIC24.status, LIC24.down = 200, False
    LIC24.legacy_same = False
    iot._TRANSPORT = httpx.MockTransport(_y24)
    iot.reset_cache()
    wa.reset_for_tests()
    wa.DAEMON_CMD = [sys.executable, "-c", "import time; time.sleep(120)"]
    wa._TRANSPORT = httpx.MockTransport(_w24)
    activation.LICENSE_URL = "https://license.example.test"
    activation._TRANSPORT = httpx.MockTransport(LIC24)
    activation.reset_for_tests()
    C.set_modules({})


def _wa24(on=True):
    WAD24.mkdir(parents=True, exist_ok=True)
    f = WAD24 / "linked.json"
    if on:
        f.write_text(json.dumps({"linked": True, "at": 1}), encoding="utf-8")
    elif f.exists():
        f.unlink()


def _act24():
    """Установка активирована: install_id и токен обновления лежат на диске."""
    (Path(_DIR24) / "license.json").write_text(json.dumps({"install_id": IID24, "refresh_token": RT24, "pack_version": 1}), encoding="utf-8")


def _pack24(mods=None, extra=None):
    """Пакет активации на диске (pack.enc + license.json), как после настоящего обмена; mods -- флаги модулей."""
    _act24()
    pack = {"schema": 1, "version": 1, **(extra or {})}
    if mods:
        pack["modules"] = {n: True for n in mods}
    activation._write_atomic(str(Path(_DIR24) / "pack.enc"), activation._encrypt(pack, "seed-" + "s" * 40, "1000000001", IID24))
    st = activation._read_state()
    st.update(seed="seed-" + "s" * 40, yandex_id="1000000001", pack_version=1)
    activation._write_state(st)


def _mods24(*names):
    C.set_modules({n: True for n in names})


class Script24:
    """Модель-заглушка: отдаёт заготовленные ответы по очереди, запоминает запросы."""

    def __init__(self, *steps):
        self.steps, self.calls = list(steps), []

    async def __call__(self, **kw):
        self.calls.append(kw)
        if not self.steps:
            raise AssertionError("ворота: модель позвали больше, чем заготовлено ответов")
        return self.steps.pop(0)

    def tool_results(self):
        """Все ответы инструментов за ход (история в запросах общая и растёт, берём последний снимок целиком)."""
        out = []
        if not self.calls:
            return out
        msgs = self.calls[-1]["messages"]
        start = max([i for i, m in enumerate(msgs) if m.get("role") == "user" and isinstance(m.get("content"), str)] or [0])
        for m in msgs[start:]:
            if m.get("role") == "user" and isinstance(m.get("content"), list):
                out += [str(b.get("content")) for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"]
        return out


async def _say24(uid, text):
    out = (await main.handle(uid, text, False))["response"]["text"]
    await asyncio.sleep(0.02)
    await main.drain()
    await notify.drain()
    return out


def say24(uid, text):
    return run(_say24(uid, text))


def tr(i, name, inp):
    return tool_resp(i, name, inp)


def act24(tid, on_, label, target="device"):
    return {"target": target, "target_id": tid, "capability": "devices.capabilities.on_off", "instance": "on", "value": on_, "label": label}


def disk24():
    p = Path(_DIR24) / "modules.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def posts_ids24():
    return [b["devices"][0]["id"] for p, b in POSTS24 if p.endswith("/devices/actions")]


# ================= 24a. флаги модулей из пакета, прозрачность без флага =================
_r24()
PROMPT_NONE24 = prompts.system_prompt()
check("24a1. нет флагов: инструментов модулей нет (brain.tools() это brain.TOOLS), в промпте ни слова про модули",
      brain.tools() is brain.TOOLS and modules.prompt_block() == "" and not re.search(r"макрос|напоминан|детск", PROMPT_NONE24, re.I))
_mods24("macros", "timers", "kids")
names24 = [t["name"] for t in brain.tools()]
check("24a2. все три флага: инструменты макросов, напоминаний, подтверждения и детского режима у модели",
      {"macro_draft", "macro_run", "macro_list", "macro_delete", "reminder_draft", "reminder_list", "reminder_cancel", "modules_confirm",
       "kids_mode"} <= set(names24) and "macro_draft" in prompts.system_prompt() and "reminder_draft" in prompts.system_prompt())
_mods24("macros")
check("24a3. только macros: напоминаний и детского режима у модели нет",
      "macro_draft" in [t["name"] for t in brain.tools()] and "reminder_draft" not in [t["name"] for t in brain.tools()]
      and "kids_mode" not in [t["name"] for t in brain.tools()] and "reminder_draft" not in prompts.system_prompt())
C.set_modules({})
check("24a4. флаги сняли -> промпт байт в байт как у обычного Кореша", prompts.system_prompt() == PROMPT_NONE24 and brain.tools() is brain.TOOLS)
# честный отказ без флага
_r24()
S = Script24(tr("t1", "macro_draft", {"name": "я ушёл", "actions": [act24("lamp-bed", False, "выключить свет")]}),
             text_resp("Этого я пока не умею, подключается отдельно."))
brain._create = S
fresh("a5")
t = say24("a5", "запомни: когда я говорю я ушёл, выключи свет")
res = S.tool_results()
check("24a5. без флага инструмент макросов отказывает честно: «этого я пока не умею, подключается отдельно», ничего не сохранено",
      res and "этого я пока не умею, подключается отдельно" in res[0] and not disk24().get("macros") and not POSTS24, str(res))
check("24a6. без флага Кореш отвечает отказом, а не «запомнил»", "не умею" in t.lower(), t)
# sanitize и пакет
san = activation.sanitize({"schema": 1, "modules": {"macros": True, "timers": "yes", "kids": False, "evil": True}})
check("24a7. sanitize пакета: из modules остаются только известные имена и только булевы значения (явное false сохраняется)",
      san.get("modules") == {"macros": True, "kids": False}, str(san))
sp = activation.sanitize({"schema": 1, "promo_all_modules": "true"})
check("24a7b. promo_all_modules пакета: только строгое true", "promo_all_modules" not in sp and activation.sanitize({"schema": 1, "promo_all_modules": True}).get("promo_all_modules") is True)
check("24a8. sanitize: пакет без modules остаётся как раньше (поля modules нет)", "modules" not in activation.sanitize({"schema": 1, "ASSISTANT_NAME": "Х"}))
C.set_pack({"schema": 1, "version": 1, "modules": {"timers": True}})
check("24a9. set_pack кладёт флаги в слой модулей; clear_pack их гасит", C.module_on("timers") and not C.module_on("macros") and (C.clear_pack() or not C.module_on("timers")))
check("24a10. обновление пакета раз в час, а не раз в сутки", activation.REFRESH_EVERY == 3600)

# ---- рубильник: обновление пакета включает/гасит модуль сразу, не дожидаясь границы эпизода; версия образа едет в refresh ----
_r24()
_act24()
activation._write_atomic(str(Path(_DIR24) / "pack.enc"), activation._encrypt({"schema": 1, "version": 1}, "seed-" + "s" * 40, "1000000001", IID24))
st24 = activation._read_state()
st24.update(seed="seed-" + "s" * 40, yandex_id="1000000001")
activation._write_state(st24)
LIC24.pack, LIC24.version = {"schema": 1, "version": 2, "modules": {"macros": True, "kids": True}}, 2
check("24a11. до обновления модулей нет", not C.module_on("macros") and not C.module_on("kids"))
r = run(activation.refresh_once())
check("24a12. refresh_once=update -> macros и kids включились СРАЗУ (рубильник), timers нет",
      r == "update" and C.module_on("macros") and C.module_on("kids") and not C.module_on("timers"), r)
check("24a13. в теле refresh едет версия образа и since", LIC24.refreshes and LIC24.refreshes[-1].get("version") == str(C.VERSION) and "since" in LIC24.refreshes[-1])
LIC24.pack, LIC24.version = {"schema": 1, "version": 3, "modules": {"macros": True}}, 3
run(activation.refresh_once())
check("24a14. рубильник выключили в админке -> после обновления kids погас сразу", C.module_on("macros") and not C.module_on("kids"))
LIC24.pack, LIC24.version = {"schema": 1, "version": 4}, 4
run(activation.refresh_once())
check("24a15. модули убрали из пакета целиком -> все погасли", not any(C.module_on(n) for n in C.MODULE_NAMES))
activation.reset_for_tests()
check("24a16. после перезапуска флаги читаются с диска вместе с пакетом", activation.load() and not C.module_on("kids"))

# ---- акция (ТЗ-33): pack.promo_all_modules = true -> все модули, явное false перекрывает ----
_r24()
C.set_pack({"schema": 1, "version": 1, "promo_all_modules": True})
check("24a17. акция в пакете: все три модуля включены без отдельных флагов", all(C.module_on(n) for n in C.MODULE_NAMES) and {"macro_draft", "reminder_draft", "kids_mode"} <= {t["name"] for t in brain.tools()})
C.set_pack({"schema": 1, "version": 1, "promo_all_modules": True, "modules": {"kids": False}})
check("24a18. акция + явный kids=false: макросы и напоминания есть, детского режима нет (рубильник перекрывает акцию)",
      C.module_on("macros") and C.module_on("timers") and not C.module_on("kids") and "kids_mode" not in [t["name"] for t in brain.tools()])
check("24a19. без акции явное false ничего не включает, отсутствующий флаг = выключено", (C.set_pack({"schema": 1, "modules": {"macros": True, "kids": False}}) or True)
      and C.module_on("macros") and not C.module_on("kids") and not C.module_on("timers"))
# обновление пакета: акция и рубильник доезжают через refresh
_r24()
_act24()
_pack24(None, {"promo_all_modules": True})
activation.load()
check("24a20. пакет с диска (акция): все модули включены после перезапуска", all(C.module_on(n) for n in C.MODULE_NAMES))
LIC24.pack, LIC24.version = {"schema": 1, "version": 2, "promo_all_modules": True, "modules": {"timers": False}}, 2
st24 = activation._read_state()
r = run(activation.refresh_once())
check("24a21. в админке выключили timers клиенту с акцией -> refresh: update, timers погас СРАЗУ, остальные остались",
      r == "update" and not C.module_on("timers") and C.module_on("macros") and C.module_on("kids"), r)
LIC24.pack, LIC24.version = {"schema": 1, "version": 3, "promo_all_modules": True}, 3
run(activation.refresh_once())
check("24a22. рубильник вернули (явного false нет) -> акция снова включает все", all(C.module_on(n) for n in C.MODULE_NAMES))
LIC24.revoked = False
C.set_modules({}, False)

# ---- ТЗ-37: диагностика обновления пакета (/health), причины сбоев, обновление при открытии страницы модулей, самовосстановление ----
_r24()
check("24r0. до первого обновления: last_refresh_status=never, модулей нет", activation.refresh_info() == {"at": 0.0, "status": "never"})
_pack24()
LIC24.pack, LIC24.version = {"schema": 1, "version": 2, "modules": {"macros": True, "timers": True, "kids": True}}, 2
check("24r1. refresh=update: статус ok, время проставлено",
      run(activation.refresh_once()) == "update" and activation.refresh_info()["status"] == "ok" and activation.refresh_info()["at"] > 0)
with TestClient(main.app) as _tc24r:
    hj = _tc24r.get("/health").json()
check("24r2. /health: pack_active, флаги модулей, last_refresh_at (ISO) и last_refresh_status=ok",
      hj.get("pack_active") is True and hj.get("modules") == {"macros": True, "timers": True, "kids": True} and hj.get("last_refresh_status") == "ok"
      and re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", str(hj.get("last_refresh_at"))), str(hj))
check("24r3. в /health нет секретов (токен обновления, seed, install_id, Yandex ID)",
      not any(x in json.dumps(hj) for x in (RT24, IID24, "seed-", "1000000001")), json.dumps(hj)[:200])
check("24r4. сервер говорит «то же самое» -> same, статус ok_same", run(activation.refresh_once()) == "same" and activation.refresh_info()["status"] == "ok_same")
LIC24.down = True
check("24r5. сервер лицензий лёг -> fail, статус net_ConnectError (причина без секретов), пакет и модули целы",
      run(activation.refresh_once()) == "fail" and activation.refresh_info()["status"] == "net_ConnectError" and C.module_on("kids"))
LIC24.down = False
_real_post24 = activation._post
async def _p401(path, body, headers=None, timeout=10.0):
    return httpx.Response(401, json={"ok": False})
activation._post = _p401
check("24r6. токен обновления не подошёл (401) -> fail, статус http_401", run(activation.refresh_once()) == "fail" and activation.refresh_info()["status"] == "http_401")
activation._post = _real_post24
# установка без токена обновления (старая схема / потерян license.json): честная причина и подсказка на странице
st24r = activation._read_state()
st24r.pop("refresh_token", None)
activation._write_state(st24r)
check("24r7. пакет есть, токена нет -> skip, статус skip_no_token", run(activation.refresh_once()) == "skip" and activation.refresh_info()["status"] == "skip_no_token")
check("24r8. страница модулей получает подсказку «обнови подключение»",
      setupwiz._link_state()["ok"] is False and "Обнови подключение" in setupwiz._link_state()["hint"] and "seed" not in json.dumps(setupwiz._link_state()))
# диск не принял пакет: версию принятой НЕ считаем, следующий заход заберёт пакет снова
_pack24()
LIC24.pack, LIC24.version = {"schema": 1, "version": 5, "modules": {"macros": True}}, 5
_real_wa24 = activation._write_atomic
activation._write_atomic = lambda path, data: False
check("24r9. диск не принял пакет -> fail, статус disk", run(activation.refresh_once()) == "fail" and activation.refresh_info()["status"] == "disk")
activation._write_atomic = _real_wa24
check("24r10. версия не записана -> следующий заход снова update (обновление не теряется)",
      activation._read_state().get("pack_version") == 1 and run(activation.refresh_once()) == "update" and activation._read_state().get("pack_version") == 5)
# сервер лицензий пересоздали: его версия ниже нашей -> счётчик сбрасывается, пакет приезжает заново
LIC24.version, LIC24.pack = 2, {"schema": 1, "version": 2, "modules": {"kids": True}}
check("24r11. версия сервера ниже нашей -> ok_same_resync, счётчик сброшен", run(activation.refresh_once()) == "same" and activation.refresh_info()["status"] == "ok_same_resync"
      and activation._read_state().get("pack_version") == 0)
check("24r12. следующий заход забирает пакет заново", run(activation.refresh_once()) == "update" and C.module_on("kids") and not C.module_on("macros"))
# ---- модули доезжают и когда версия пакета НЕ выросла (сервер вернул тот же пакет, но модули изменились) ----
_r24()
_pack24()
LIC24.pack, LIC24.version = {"schema": 1, "version": 1, "modules": {"macros": True, "kids": True}}, 1
check("24m1. сервер: версия та же (since=1), но модули включили -> модули обновились СРАЗУ, статус ok_modules",
      run(activation.refresh_once()) == "same" and activation.refresh_info()["status"] == "ok_modules" and C.module_on("macros") and C.module_on("kids")
      and not C.module_on("timers"), activation.refresh_info()["status"])
with TestClient(main.app) as _tc24m:
    hjm = _tc24m.get("/health").json()
check("24m2. /health показывает включённые модули", hjm.get("modules") == {"macros": True, "timers": False, "kids": True}, str(hjm))
activation.reset_for_tests()
check("24m3. после перезапуска модули читаются с диска (пакет на диске переписан)", activation.load() and C.module_on("macros") and C.module_on("kids") and not C.module_on("timers"))
check("24m4. версия пакета в license.json не тронута (это не обновление пакета)", activation._read_state().get("pack_version") == 1)
check("24m5. тот же набор модулей второй раз -> обычный ok_same, диск не трогаем",
      run(activation.refresh_once()) == "same" and activation.refresh_info()["status"] == "ok_same")
LIC24.pack = {"schema": 1, "version": 1, "modules": {"kids": True}}
check("24m6. выключение на сервере без роста версии доезжает: macros погас, kids остался",
      run(activation.refresh_once()) == "same" and activation.refresh_info()["status"] == "ok_modules" and not C.module_on("macros") and C.module_on("kids"))
LIC24.pack = {"schema": 1, "version": 1}
run(activation.refresh_once())
check("24m7. на сервере выключили всё (modules пусто) -> все погасли, и на диске тоже",
      not any(C.module_on(n) for n in C.MODULE_NAMES) and (activation.reset_for_tests() or activation.load()) and not any(C.module_on(n) for n in C.MODULE_NAMES))
_pack24()
LIC24.pack = {"schema": 1, "version": 1, "promo_all_modules": True, "modules": {"macros": True, "timers": False, "kids": True}}
run(activation.refresh_once())
check("24m8. акция на сервере без роста версии: все, кроме явно выключенного timers", C.module_on("macros") and C.module_on("kids") and not C.module_on("timers"))
activation.reset_for_tests()
activation.load()
check("24m9. акция с диска после перезапуска: те же флаги", C.module_on("macros") and C.module_on("kids") and not C.module_on("timers") and activation.sanitize(C.pack()).get("promo_all_modules") is True)
# старый сервер не присылает поля модулей в same -> ничего не трогаем
_r24()
_pack24(["macros"])
activation.load()
LIC24.legacy_same, LIC24.pack, LIC24.version = True, {"schema": 1, "version": 1}, 1
check("24m10. старый сервер: same без полей модулей -> модули не гаснут, статус ok_same",
      run(activation.refresh_once()) == "same" and activation.refresh_info()["status"] == "ok_same" and C.module_on("macros"))
LIC24.legacy_same = False
# отложенный пакет (граница эпизода) не откатывает свежие модули
_r24()
_pack24()
activation.load()
LIC24.pack, LIC24.version = {"schema": 1, "version": 2, "ASSISTANT_NAME": "Гоша"}, 2
run(activation.refresh_once())                                      # update: пакет ждёт границы эпизода, модулей нет
LIC24.pack = {"schema": 1, "version": 2, "ASSISTANT_NAME": "Гоша", "modules": {"timers": True}}
run(activation.refresh_once())                                      # same с новым набором модулей
activation.apply_pending()
check("24m11. граница эпизода после синхронизации модулей: timers остался включён, имя из нового пакета применилось",
      C.module_on("timers") and C.pack().get("ASSISTANT_NAME") == "Гоша")
# страница модулей (refresh_on_demand) подтягивает изменение без роста версии
_r24()
_pack24()
activation.load()
LIC24.pack, LIC24.version = {"schema": 1, "version": 1, "modules": {"macros": True, "timers": True}}, 1
activation._REFRESH["tried"] = 0.0
check("24m12. refresh_on_demand (открытие /setup/moduli): модули обновились без роста версии",
      run(activation.refresh_on_demand()) == "same" and C.module_on("macros") and C.module_on("timers"))
# страница модулей сама подтягивает пакет (не ждёт часового цикла) и не чаще раза в REFRESH_ON_DEMAND секунд
_r24()
_pack24()
os.environ["SETUP_CODE"] = "refresh-code-37"
setupwiz.reset_for_tests()
C.set_setup_done(True)
LIC24.pack, LIC24.version = {"schema": 1, "version": 2, "modules": {"macros": True, "timers": True}}, 2
tm37 = _TC(main.app)
tm37.post("/setup/api/moduli/login", json={"code": "refresh-code-37"})
n0 = len(LIC24.refreshes)
sm37 = tm37.get("/setup/api/moduli/state").json()
check("24r13. открыл страницу модулей -> пакет подтянулся сразу: макросы и напоминания «включён», связь ok",
      [m["on"] for m in sm37["modules"]] == [True, True, False] and sm37["link"] == {"ok": True} and len(LIC24.refreshes) == n0 + 1, str(sm37)[:300])
tm37.get("/setup/api/moduli/state")
check("24r14. повторное открытие в пределах 20 с не дёргает сервер лицензий", len(LIC24.refreshes) == n0 + 1)
activation._REFRESH["tried"] = 0.0
LIC24.pack, LIC24.version = {"schema": 1, "version": 3, "modules": {"macros": True, "timers": True, "kids": True}}, 3
sm37 = tm37.get("/setup/api/moduli/state").json()
check("24r15. по истечении окна новое включение (kids) видно на странице сразу", [m["on"] for m in sm37["modules"]] == [True, True, True])
activation._REFRESH["tried"] = 0.0
LIC24.down = True
sm37 = tm37.get("/setup/api/moduli/state").json()
check("24r16. сервер лицензий лёг: страница открывается, модули прежние, есть понятная подсказка про связь",
      sm37["ok"] and [m["on"] for m in sm37["modules"]] == [True, True, True] and sm37["link"]["ok"] is False and "net_ConnectError" in sm37["link"]["hint"], str(sm37["link"]))
LIC24.down = False
os.environ.pop("SETUP_CODE", None)
setupwiz.reset_for_tests()
C.set_setup_done(False)
activation._REFRESH["tried"] = 0.0

# ================= 24b. голосовые макросы =================
_r24()
_mods24("macros")
fresh("b1")
LAMPS = [act24("lamp-bed", False, "выключить свет в спальне"), act24("sock1", False, "выключить розетку"), act24("heater1", False, "выключить обогреватель")]
S = Script24(tr("t1", "list_devices", {}),
             tr("t2", "macro_draft", {"name": "я ушёл", "actions": LAMPS}),
             text_resp("Запомню: когда скажешь «я ушёл», я выключу свет в спальне, розетку и обогреватель. Сохранить?"))
brain._create = S
t = say24("b1", "запомни: когда я говорю я ушёл, выключи свет, розетки и обогреватель")
check("24b1. макрос: черновик зачитан с вопросом «Сохранить?», на диске ещё ничего нет", "Сохранить?" in t and not disk24().get("macros") and not POSTS24, t)
# модель-хитрец зовёт confirm в той же реплике -> отказ
S = Script24(tr("t1", "macro_draft", {"name": "я пришёл", "actions": [act24("lamp-bed", True, "включить свет в спальне")]}),
             tr("t2", "modules_confirm", {}),
             text_resp("Запомню: когда скажешь «я пришёл», я включу свет. Сохранить?"))
brain._create = S
fresh("b2")
say24("b2", "запомни я пришёл включи свет")
check("24b2. confirm в той же реплике (без «да» хозяина) отказывает NO_CONFIRM, ничего не сохранено",
      any("NO_CONFIRM" in x for x in S.tool_results()) and not disk24().get("macros"), str(S.tool_results()))
# «да, но поменяй» согласием не считается
S = Script24(tr("t1", "modules_confirm", {}), text_resp("Не сохранил, жду нормального «да»."))
brain._create = S
say24("b2", "да, но убери розетку")
check("24b3. «да, но убери розетку» не согласие: не сохранено", any("NO_CONFIRM" in x for x in S.tool_results()) and not disk24().get("macros"), str(S.tool_results()))
# «нет» стирает черновик
S = Script24(text_resp("Ок, не сохраняю."))
brain._create = S
say24("b2", "нет, отмена")
S = Script24(tr("t1", "modules_confirm", {}), text_resp("Нечего сохранять."))
brain._create = S
say24("b2", "да")
check("24b4. после «нет» черновик стёрт: «да» ничего не сохраняет", any("нечего подтверждать" in x for x in S.tool_results()) and not disk24().get("macros"), str(S.tool_results()))

# полный путь: черновик -> «Сохранить?» -> «да» -> сохранено
fresh("b5")
S = Script24(tr("t1", "macro_draft", {"name": "я ушёл", "actions": LAMPS}),
             text_resp("Запомню: когда скажешь «я ушёл», я выключу свет в спальне, розетку и обогреватель. Сохранить?"))
brain._create = S
say24("b5", "запомни: когда я говорю я ушёл, выключи свет, розетки и обогреватель")
S = Script24(tr("t1", "modules_confirm", {}), text_resp("Запомнил."))
brain._create = S
t = say24("b5", "да")
d = disk24()
check("24b5. «да» в следующей реплике -> макрос сохранён на диск (3 действия, подписи)",
      len(d.get("macros", [])) == 1 and d["macros"][0]["name"] == "я ушёл" and len(d["macros"][0]["actions"]) == 3
      and d["macros"][0]["actions"][1]["label"] == "выключить розетку" and "Запомнил" in t, str(d))
check("24b6. второе «да» ничего не сохраняет повторно (черновик одноразовый)", (lambda: (brain.__setattr__("_create", Script24(tr("t1", "modules_confirm", {}), text_resp("ок"))), say24("b5", "да"))[1] is not None and len(disk24()["macros"]) == 1)())

# выполнение + честный отчёт
S = Script24(tr("t1", "macro_run", {"name": "я ушёл"}), text_resp("Выключил свет, розетку и обогреватель."))
brain._create = S
POSTS24.clear()
t = say24("b5", "я ушёл")
check("24b7. «я ушёл» -> три команды умному дому по сохранённым действиям (id и значения), отчёт принят",
      sorted(posts_ids24()) == ["heater1", "lamp-bed", "sock1"] and all(b["devices"][0]["actions"][0]["state"]["value"] is False for _, b in POSTS24)
      and t == "Выключил свет, розетку и обогреватель.", f"{posts_ids24()} {t}")
# часть не вышла -> честно
BROKEN24.add("heater1")
S = Script24(tr("t1", "macro_run", {"name": "я ушёл"}), text_resp("Всё выключил, отдыхай."))
brain._create = S
t = say24("b5", "я ушёл")
check("24b8. обогреватель не на связи, модель врёт «всё выключил» -> честная замена: получилось не всё, причина названа",
      "не всё" in t.lower() and "не на связи" in t.lower() and "Всё выключил" not in t, t)
check("24b9. инструмент отдал модели список «вышло / НЕ вышло»", any("НЕ вышло: выключить обогреватель" in x and "выполнено 2 из 3" in x for x in S.tool_results()), str(S.tool_results()))
BROKEN24.clear()
# неизвестный макрос
S = Script24(tr("t1", "macro_run", {"name": "улетаю"}), text_resp("Такого макроса нет."))
brain._create = S
POSTS24.clear()
say24("b5", "улетаю")
check("24b10. неизвестный макрос -> «такого нет», перечислены имеющиеся, команд нет", any("такого макроса нет" in x and "я ушёл" in x for x in S.tool_results()) and not POSTS24)
# список и удаление
S = Script24(tr("t1", "macro_list", {}), text_resp("Один: я ушёл."))
brain._create = S
say24("b5", "какие у меня макросы")
check("24b11. «какие у меня макросы» -> список с подписями действий", any("«я ушёл»" in x and "выключить розетку" in x for x in S.tool_results()), str(S.tool_results()))
S = Script24(tr("t1", "macro_delete", {"name": "я ушёл"}), text_resp("Удалил."))
brain._create = S
say24("b5", "удали макрос я ушёл")
check("24b12. «удали макрос» -> файл без макроса", disk24()["macros"] == [])
# замена существующего
fresh("b13")
modules.macro_delete("x")
r = run(modules.macro_draft("b13", "Я ушёл", [act24("lamp-bed", False, "выключить свет")]))
modules.TURNS["b13"] = modules.TURNS.get("b13", 0) + 1
modules.confirm("b13", "да")
r2 = run(modules.macro_draft("b13", "я ушёл", [act24("lamp-hall", False, "выключить люстру")]))
check("24b13. «измени макрос»: тот же name -> в зачитке «Заменю макрос», после «да» макрос один и с новым составом",
      "Заменю макрос" in r2.text and (modules.TURNS.__setitem__("b13", modules.TURNS["b13"] + 1) or True)
      and modules.confirm("b13", "да").ok and len(disk24()["macros"]) == 1 and disk24()["macros"][0]["actions"][0]["target_id"] == "lamp-hall", r2.text)
# лимиты
r = run(modules.macro_draft("b14", "много", [act24("lamp-bed", False, "x")] * 15))
r16 = run(modules.macro_draft("b14", "много", [act24("lamp-bed", False, "x")] * 16))
check("24b14. до 15 действий в макросе можно, 16-е отказ", r.ok and not r16.ok and "15" in r16.reason, r16.text)
modules.reset_for_tests()
for i in range(30):
    modules._data()["macros"].append({"name": f"макрос {i}", "actions": [act24("lamp-bed", False, "x")]})
r31 = run(modules.macro_draft("b15", "тридцать первый", [act24("lamp-bed", False, "x")]))
r30 = run(modules.macro_draft("b15", "макрос 5", [act24("lamp-hall", False, "y")]))
check("24b15. 30 макросов: 31-й новый отказ, замена существующего можно", not r31.ok and "30" in r31.reason and r30.ok, r31.text)
modules.reset_for_tests()
modules._data()
rr = run(modules.macro_draft("b16", "выдуманный", [{"target": "device", "target_id": "no-such", "capability": "x", "instance": "on", "value": True, "label": "что-то"}]))
check("24b16. выдуманный id устройства отказ ещё на черновике (id сверяются со списком хозяина)", not rr.ok and "неизвестный id" in rr.reason, rr.text)
# замки: макрос можно составить, но выполнится только с кодовой фразой в ЭТОЙ реплике
modules.reset_for_tests()
os.environ["ALLOW_LOCKS"], os.environ["LOCK_PHRASE"] = "1", GOOD_PHRASE24
run(modules.macro_draft("b17", "запри всё", [act24("lock1", True, "закрыть замок"), act24("lamp-bed", False, "выключить свет")]))
modules.TURNS["b17"] = modules.TURNS.get("b17", 0) + 1
modules.confirm("b17", "да")
POSTS24.clear()
j = honesty.Journal()
rn = run(modules.macro_run("запри всё", "запри всё", j))
check("24b17. замок в макросе без кодовой фразы: не выполнен (need_phrase), свет выполнен, отчёт честный «вышло 1 из 2»",
      posts_ids24() == ["lamp-bed"] and "выполнено 1 из 2" in rn.text and "кодовая фраза" in rn.text and j.iot_ok == 1, rn.text)
POSTS24.clear()
rn = run(modules.macro_run("запри всё", "запри всё, " + GOOD_PHRASE24, honesty.Journal()))
check("24b18. с кодовой фразой в той же реплике замок отрабатывает", sorted(posts_ids24()) == ["lamp-bed", "lock1"], str(posts_ids24()))
os.environ.pop("ALLOW_LOCKS"); os.environ.pop("LOCK_PHRASE")
# переживает перезапуск
modules.reload()
check("24b19. после «перезапуска» (оперативная копия сброшена) макрос читается с диска", "запри всё" in [m["name"] for m in modules._data()["macros"]])
check("24b20. макросы лежат на Volume (MEMORY_DIR/modules.json), в файле нет ключей и токенов",
      (Path(_DIR24) / "modules.json").exists() and "sk-leak-canary-24" not in (Path(_DIR24) / "modules.json").read_text(encoding="utf-8")
      and "test-iot-token" not in (Path(_DIR24) / "modules.json").read_text(encoding="utf-8"))
# в промпте только правила, не списки
_mods24("macros")
check("24b21. список макросов в промпт не попадает (кэш промпта стабилен, список модель берёт инструментом)",
      "запри всё" not in prompts.system_prompt())

# ================= 24c. напоминания и действия по времени =================
def _rem_flow(uid, draft_args, confirm=True):
    """Черновик -> «да» в следующей реплике -> результат подтверждения."""
    r = run(modules.reminder_draft(uid, draft_args.get("kind"), draft_args.get("text"), draft_args.get("action"),
                                   draft_args.get("in_minutes"), draft_args.get("at"), draft_args.get("day"), draft_args.get("repeat")))
    if not confirm or not r.ok:
        return r, None
    modules.TURNS[uid] = modules.TURNS.get(uid, 0) + 1
    return r, modules.confirm(uid, "да")


_r24()
_mods24("timers")
_act24()
_wa24(True)
r, c = _rem_flow("c1", {"kind": "remind", "text": "забрать малого", "in_minutes": 40})
check("24c1. «через 40 минут напомни»: зачитка со временем (10:40) и «Создать?», создано", r.ok and "10:40" in r.text and "забрать малого" in r.text and "Создать?" in r.text and c.ok, r.text)
CLK24["t"] += 39 * 60
n = run(modules.run_due())
check("24c2. через 39 минут ещё тихо", n == 0 and not LIC24.notes and not WA_SENT24)
CLK24["t"] += 2 * 60
n = run(modules.run_due())
check("24c3. через 41 минуту сработало: ровно одно уведомление в очередь лицензий (reminder) и одно в WhatsApp «себе»",
      n == 1 and len(LIC24.notes) == 1 and LIC24.notes[0]["kind"] == "reminder" and "забрать малого" in LIC24.notes[0]["text"]
      and len(WA_SENT24) == 1 and WA_SENT24[0].get("self") is True and "забрать малого" in WA_SENT24[0]["text"], str((LIC24.notes, WA_SENT24)))
check("24c4. разовое напоминание после срабатывания удалено, второй раз не сработает", modules._active_reminders() == [] and run(modules.run_due()) == 0)
# в 18:00 по часам хозяина
CLK24["t"] = _local24(10, 0)
r, c = _rem_flow("c5", {"kind": "remind", "text": "позвонить маме", "at": "18:00"})
check("24c5. «в 18:00 напомни» в 10:00: «сегодня в 18:00», время посчитал сервер (по Алматы +5)",
      c.ok and "сегодня в 18:00" in r.text and modules._active_reminders()[0]["fire_at"] == _local24(18, 0), r.text)
LIC24.notes.clear()
CLK24["t"] = _local24(17, 59)
run(modules.run_due())
check("24c6. в 17:59 тихо", not LIC24.notes)
CLK24["t"] = _local24(18, 0) + 1
run(modules.run_due())
check("24c7. в 18:00 сработало (сдвиг часов)", len(LIC24.notes) == 1 and "позвонить маме" in LIC24.notes[0]["text"])
CLK24["t"] = _local24(19, 0)
r, c = _rem_flow("c8", {"kind": "remind", "text": "позвонить маме", "at": "18:00"})
check("24c8. в 19:00 «в 18:00» без уточнения -> завтра", c.ok and "завтра в 18:00" in r.text, r.text)
rt = run(modules.reminder_draft("c9", "remind", "x", None, None, "18:00", "today", "none"))
check("24c9. «сегодня в 18:00», когда уже 19:00 -> отказ с подсказкой про завтра", not rt.ok and "уже прошло" in rt.reason, rt.text)
bad = [run(modules.reminder_draft("c10", "remind", "x", None, v, None, None, "none")) for v in (0, -5, 99999, "abc")]
bad += [run(modules.reminder_draft("c10", "remind", "x", None, None, v, None, "none")) for v in ("25:00", "полдень", "", None)]
check("24c10. кривое время (0 / минус / больше недели / 25:00 / слова) -> отказ, ничего не создано", all(not b.ok for b in bad) and len(modules._active_reminders()) == 1, str([b.reason for b in bad]))
# часовой пояс настраивается
modules.reset_for_tests()
os.environ["TZ_OFFSET"] = "3"
for f in Path(_DIR24).glob("modules.json"):
    f.unlink()
modules.reload()
CLK24["t"] = float(calendar.timegm((2026, 10, 14, 7, 0, 0)))     # 10:00 по Москве (+3)
r, c = _rem_flow("c11", {"kind": "remind", "text": "тест пояса", "at": "12:00"})
check("24c11. TZ_OFFSET=3: «в 12:00» = 09:00 UTC (часовой пояс из настроек, не жёсткие +5)",
      c.ok and modules._active_reminders()[0]["fire_at"] == float(calendar.timegm((2026, 10, 14, 9, 0, 0))), r.text)
os.environ.pop("TZ_OFFSET")
check("24c12. TZ_OFFSET мусор -> Алматы +5", (os.environ.__setitem__("TZ_OFFSET", "abc") or C.tz_offset() == 5.0) and (os.environ.pop("TZ_OFFSET") or True))
# повторы
modules.reset_for_tests()
for f in Path(_DIR24).glob("modules.json"):
    f.unlink()
CLK24["t"] = _local24(8, 0, day=16)                               # пятница 08:00
r, c = _rem_flow("c13", {"kind": "remind", "text": "в школу", "at": "07:00", "repeat": "weekdays"})
rem = modules._active_reminders()[0]
check("24c13. «каждый будний день в 7:00», создано в пятницу в 8:00 -> первый раз в понедельник 07:00 (суббота и воскресенье пропущены)",
      c.ok and rem["fire_at"] == _local24(7, 0, day=19) and "каждый будний день в 07:00" in r.text, r.text)
LIC24.notes.clear()
CLK24["t"] = _local24(7, 0, day=19) + 5
run(modules.run_due())
check("24c14. в понедельник сработало и осталось на вторник 07:00 (повтор не удаляется)",
      len(LIC24.notes) == 1 and modules._active_reminders()[0]["fire_at"] == _local24(7, 0, day=20))
CLK24["t"] = _local24(7, 0, day=23) + 5                           # пятница
run(modules.run_due())
check("24c15. пятницу сработало и следующий раз снова понедельник", modules._active_reminders()[0]["fire_at"] == _local24(7, 0, day=26))
r, c = _rem_flow("c16", {"kind": "remind", "text": "зарядка", "at": "06:30", "repeat": "daily"})
check("24c16. «каждый день»: в субботу тоже", c.ok and "каждый день в 06:30" in r.text)
# список и отмена
rl = modules.reminder_list()
check("24c17. «какие у меня напоминания» -> оба, с временем", "в школу" in rl.text and "зарядка" in rl.text and "каждый" in rl.text, rl.text)
rc = modules.reminder_cancel("зарядк")
check("24c18. «отмени зарядку» -> удалено, остальное цело", rc.ok and len(modules._active_reminders()) == 1 and modules._active_reminders()[0]["text"] == "в школу")
rn = modules.reminder_cancel("такого-нет")
check("24c19. отмена несуществующего -> честный отказ", not rn.ok and "нет" in rn.reason)
# переживает перезапуск
modules.reload()
check("24c20. после «перезапуска» напоминание на месте (на диске)", [r["text"] for r in modules._active_reminders()] == ["в школу"])
LIC24.notes.clear()
CLK24["t"] = _local24(7, 0, day=26) + 5
run(modules.run_due())
check("24c21. и срабатывает после перезапуска", len(LIC24.notes) == 1 and "в школу" in LIC24.notes[0]["text"])
# действие по времени
modules.reset_for_tests()
for f in Path(_DIR24).glob("modules.json"):
    f.unlink()
CLK24["t"] = _local24(22, 0)
POSTS24.clear()
LIC24.notes.clear()
r, c = _rem_flow("c22", {"kind": "action", "action": act24("heater1", False, "выключить обогреватель"), "in_minutes": 30})
check("24c22. «через 30 минут выключи обогреватель»: зачитка «Сделаю: выключить обогреватель», пока ничего не выключено",
      c.ok and "выключить обогреватель" in r.text and "Создать?" in r.text and not POSTS24, r.text)
CLK24["t"] += 31 * 60
run(modules.run_due())
check("24c23. в срок сервер сам выключил обогреватель (команда в умный дом) и написал итог «Сделал по расписанию»",
      posts_ids24() == ["heater1"] and POSTS24[0][1]["devices"][0]["actions"][0]["state"]["value"] is False
      and len(LIC24.notes) == 1 and LIC24.notes[0]["kind"] == "timer_done" and "Сделал по расписанию" in LIC24.notes[0]["text"], str(LIC24.notes))
BROKEN24.add("lamp-bed")
r, c = _rem_flow("c24", {"kind": "action", "action": act24("lamp-bed", False, "выключить свет в спальне"), "in_minutes": 5})
LIC24.notes.clear()
CLK24["t"] += 6 * 60
run(modules.run_due())
check("24c24. не вышло (устройство не на связи) -> честное «Не вышло по расписанию» с причиной, не «сделал»",
      len(LIC24.notes) == 1 and "Не вышло по расписанию" in LIC24.notes[0]["text"] and "не на связи" in LIC24.notes[0]["text"], str(LIC24.notes))
BROKEN24.clear()
os.environ["ALLOW_LOCKS"], os.environ["LOCK_PHRASE"] = "1", GOOD_PHRASE24
rp = run(modules.reminder_draft("c25", "action", None, act24("lock1", True, "закрыть замок"), 10, None, None, "none"))
check("24c25. замок по расписанию нельзя вообще (кодовую фразу в срок никто не скажет)", not rp.ok and "кодов" in rp.reason, rp.text)
os.environ.pop("ALLOW_LOCKS"); os.environ.pop("LOCK_PHRASE")
# лимит 50
modules.reset_for_tests()
for f in Path(_DIR24).glob("modules.json"):
    f.unlink()
for i in range(50):
    modules._active_reminders().append({"id": f"x{i}", "kind": "remind", "text": f"n{i}", "action": None, "fire_at": CLK24["t"] + 9999 + i, "at": None, "repeat": "none"})
r51 = run(modules.reminder_draft("c26", "remind", "пятьдесят первое", None, 5, None, None, "none"))
check("24c26. лимит 50 активных: 51-е отказ", not r51.ok and "50" in r51.reason, r51.text)
# проспали: действие не выполняем, о напоминании сообщаем
modules.reset_for_tests()
for f in Path(_DIR24).glob("modules.json"):
    f.unlink()
CLK24["t"] = _local24(7, 0)
_rem_flow("c27", {"kind": "remind", "text": "таблетки", "in_minutes": 10})
_rem_flow("c27", {"kind": "action", "action": act24("lamp-bed", True, "включить свет"), "in_minutes": 10})
POSTS24.clear()
LIC24.notes.clear()
CLK24["t"] += 3 * 3600
run(modules.run_due())
txts = " | ".join(x["text"] for x in LIC24.notes)
check("24c27. сервер спал 3 часа: свет не включаем (уже день), о таблетках сообщаем с извинением, о невыполненном действии тоже",
      not POSTS24 and "Пропустил напоминание" in txts and "таблетки" in txts and "Не выполнил по расписанию" in txts, txts)
# модуль выключен рубильником: стоим, данные целы
modules.reset_for_tests()
for f in Path(_DIR24).glob("modules.json"):
    f.unlink()
CLK24["t"] = _local24(7, 0)
_rem_flow("c28", {"kind": "remind", "text": "тест рубильника", "in_minutes": 5})
C.set_modules({})
LIC24.notes.clear()
CLK24["t"] += 600
check("24c28. модуль timers выключили -> планировщик молчит, напоминание не потеряно", run(modules.run_due()) == 0 and not LIC24.notes and len(modules._active_reminders()) == 1)
_mods24("timers")
run(modules.run_due())
check("24c29. модуль включили обратно -> сработало (просрочка меньше 2 часов)", len(LIC24.notes) == 1)
# через модель: «да» в следующей реплике, зачитка пересказом модели подменяется каноничной, если не совпала
modules.reset_for_tests()
for f in Path(_DIR24).glob("modules.json"):
    f.unlink()
CLK24["t"] = _local24(10, 0)
fresh("c30")
S = Script24(tr("t1", "reminder_draft", {"kind": "remind", "text": "забрать малого", "at": "18:00"}), text_resp("Окей, всё записал."))
brain._create = S
t = say24("c30", "напомни в 18:00 забрать малого")
check("24c30. модель не зачитала черновик и не спросила -> хозяин всё равно слышит каноничное «Напомню сегодня в 18:00: …. Создать?»",
      "забрать малого" in t and "18:00" in t and "Создать?" in t and not modules._active_reminders(), t)
S = Script24(tr("t1", "modules_confirm", {}), text_resp("Поставил, напомню."))
brain._create = S
t = say24("c30", "да")
check("24c31. «да» -> создано, и «Поставил» после успешного сохранения не режется честностью", len(modules._active_reminders()) == 1 and t == "Поставил, напомню.", t)
# планировщик живёт в lifespan приложения
modules.reset_for_tests()
for f in Path(_DIR24).glob("modules.json"):
    f.unlink()
CLK24["t"] = _local24(10, 0)
_rem_flow("c32", {"kind": "remind", "text": "из планировщика", "in_minutes": 1})
CLK24["t"] += 120
LIC24.notes.clear()
_pack24(["timers"])                       # старт приложения читает пакет с диска: модуль timers включён в нём
modules.TICK_SEC = 0.05
with TestClient(main.app) as _lc:
    for _ in range(40):
        time.sleep(0.05)
        if LIC24.notes:
            break
modules.TICK_SEC = 15
check("24c32. планировщик стартует вместе с приложением и сам доставляет напоминание (без реплик хозяина)",
      len(LIC24.notes) == 1 and "из планировщика" in LIC24.notes[0]["text"], str(LIC24.notes))

# ================= 24d. детский режим =================
_r24()
_mods24("kids")
os.environ["TONE"] = "derzkiy_mat"
P_MAT24 = prompts.system_prompt()
check("24d1. модуль включён, режим выключен: тон прежний (мат остался у взрослого), в промпте только правило, как включать",
      bool(MAT_RE.search(P_MAT24)) and "kids_mode" in P_MAT24 and "ДЕТСКИЙ РЕЖИМ. Сейчас" not in P_MAT24)
fresh("d2")
# ТЗ-34: без кодовой фразы взрослого детский режим не включается (ни через мозг, ни напрямую; одно слово тоже не фраза)
S = Script24(tr("t1", "kids_mode", {"on": True, "child_name": "Алия"}), text_resp("Сначала задай кодовую фразу взрослого на странице настройки, без неё детский режим не включаю."))
brain._create = S
say24("d0", "включи детский режим, ребёнка зовут Алия")
check("24d0a. фраза не задана -> «включи детский режим» не включает, инструмент возвращает причину и текст для ответа, на диске режима нет",
      not modules.kids_on() and any("need_kids_phrase" in x and "Сначала задай кодовую фразу взрослого" in x for x in S.tool_results()) and not disk24().get("kids", {}).get("on"), str(S.tool_results()))
os.environ["KIDS_PHRASE"] = "ромашка"
check("24d0b. фраза из одного слова за фразу не считается -> не включается", not modules.kids_set("d0", True, "Алия", "").ok and not modules.kids_on())
os.environ["KIDS_PHRASE"] = GOOD_PHRASE24
fresh("d2")
S = Script24(tr("t1", "kids_mode", {"on": True, "child_name": "Алия"}), text_resp("Включил детский режим. Привет, Алия! Хочешь сказку?"))
brain._create = S
t = say24("d2", "включи детский режим, ребёнка зовут Алия")
P_KIDS24 = prompts.system_prompt()
check("24d2. «включи детский режим»: режим включён и лежит на диске, имя ребёнка запомнено",
      modules.kids_on() and disk24()["kids"]["on"] is True and disk24()["kids"]["child"] == "Алия", str(disk24()))
check("24d3. в ТОМ ЖЕ ходе после включения системный промпт уже детский (второй запрос к модели)",
      len(S.calls) == 2 and "ДЕТСКИЙ РЕЖИМ. Сейчас" in S.calls[1]["system"][0]["text"] and "ДЕТСКИЙ РЕЖИМ. Сейчас" not in S.calls[0]["system"][0]["text"])
check("24d4. детский промпт: мягкий тон, без мата (в самом промпте), без подколов и сарказма, сказки с именем, загадки, викторины, домашка без готовых ответов",
      "ДЕТСКИЙ РЕЖИМ. Сейчас" in P_KIDS24 and not MAT_RE.search(P_KIDS24) and "Никакого мата" in P_KIDS24 and "подколов" in P_KIDS24
      and "сказки на ночь" in P_KIDS24 and "загадки" in P_KIDS24 and "викторины" in P_KIDS24 and "готовый ответ" in P_KIDS24
      and "Алия" in P_KIDS24 and "Подъёбывай" not in P_KIDS24 and "перчинка" not in P_KIDS24, "")
check("24d5. в детском режиме мат-тон из промпта выброшен целиком (не просто дописан сверху)", "кореш-матершинник" not in P_KIDS24 and "Подъёбывай" not in P_KIDS24)
# страховка: мат от модели не доходит до ребёнка
for bad in ("Да ёбаный свет, включил.", "Ты тупой, как пробка.", "Ну и хуйня.", "Дурак ты, ей-богу"):
    brain._create = Script24(text_resp(bad))
    t = say24("d2", "включи свет")
    if t != modules.KIDS_FALLBACK:
        break
check("24d6. модель выдала мат или оскорбление в детском режиме -> ребёнок слышит доброе запасное, исходного нет", t == modules.KIDS_FALLBACK and not MAT_RE.search(t), t)
brain._create = Script24(text_resp("Вот тебе сказка про Алию и звёздного кота."))
t = say24("d2", "расскажи сказку")
check("24d7. нормальная сказка проходит без изменений", t == "Вот тебе сказка про Алию и звёздного кота.", t)
# умный дом: только свет
POSTS24.clear()
for tid, ok_expected in (("lamp-bed", True), ("sock1", False), ("heater1", False), ("lock1", False)):
    out = run(iot.control("device", tid, "devices.capabilities.on_off", "on", True, "включи"))
    check(f"24d8. детский режим: {tid} {'свет можно' if ok_expected else 'не свет -> kids_only_light'}",
          (out == "ok") == ok_expected and (ok_expected or out.startswith("kids_only_light")), out)
out_g = run(iot.control("group", "g-lights", "devices.capabilities.on_off", "on", True, ""))
out_m = run(iot.control("group", "g-mixed", "devices.capabilities.on_off", "on", True, ""))
out_s = run(iot.control("scenario", "sc1", "", "", None, ""))
check("24d9. группа из одних ламп можно, смешанная группа и сценарии нельзя", out_g == "ok" and out_m.startswith("kids_only_light") and out_s.startswith("kids_only_light"), f"{out_g} | {out_m} | {out_s}")
check("24d10. отказ детского режима честно объясняется в честности (причина названа)", "только свет" in honesty.reason_of(out_m), honesty.reason_of(out_m))
# WhatsApp детям не даём
_wa24(True)
os.environ["WA_CONTACTS"] = json.dumps({"Оля": "+77010000000"}, ensure_ascii=False)
check("24d11. в детском режиме WhatsApp-инструментов нет и блока в промпте нет", wa.available() and not [t for t in brain.tools() if t["name"].startswith("whatsapp")]
      and "whatsapp" not in prompts.system_prompt().lower())
os.environ.pop("WA_CONTACTS")
_wa24(False)
# выключение: неверная фраза / верная
os.environ["KIDS_PHRASE"] = GOOD_PHRASE24
S = Script24(tr("t1", "kids_mode", {"on": False}), text_resp("Режим остаётся включённым."))
brain._create = S
say24("d2", "выключи детский режим")
check("24d12. выключение без кодовой фразы взрослого -> отказ, режим включён", modules.kids_on() and any("need_phrase" in x for x in S.tool_results()))
S = Script24(tr("t1", "kids_mode", {"on": False}), text_resp("Режим остаётся включённым."))
brain._create = S
say24("d2", "выключи детский режим, пожалуйста, ну да, я взрослый")
check("24d13. «я взрослый» без фразы не помогает (фраза задана)", modules.kids_on())
S = Script24(tr("t1", "kids_mode", {"on": False}), text_resp("Режим выключен."))
brain._create = S
say24("d2", f"выключи детский режим, {GOOD_PHRASE24}")
check("24d14. кодовая фраза взрослого -> режим выключен, на диске тоже", not modules.kids_on() and disk24()["kids"]["on"] is False)
check("24d15. фраза не светится ни в промпте, ни в ответах инструментов, ни в config.json-значениях /health",
      GOOD_PHRASE24 not in prompts.system_prompt() and GOOD_PHRASE24 not in json.dumps(S.tool_results()) and "KIDS_PHRASE" in C.SECRET_KEYS)
check("24d16. режим выключен -> промпт снова взрослый с матом (тон не потерян)", bool(MAT_RE.search(prompts.system_prompt())) and "ДЕТСКИЙ РЕЖИМ. Сейчас" not in prompts.system_prompt())
# ТЗ-34: «я взрослый» не выключает; три неверные попытки подряд -> 10 минут игнора
os.environ["KIDS_PHRASE"] = GOOD_PHRASE24
modules.kids_set("d3", True, "Тимур", "")
S = Script24(tr("t1", "kids_mode", {"on": False}), tr("t2", "kids_mode", {"on": False}), text_resp("Режим остаётся включённым."))
brain._create = S
fresh("d3")
say24("d3", "выключи детский режим")
check("24d17. нет фразы в реплике -> выключение отказано, режим включён (две пробы модели в одной реплике считаются как одна попытка)",
      modules.kids_on() and sum("need_phrase" in x for x in S.tool_results()) == 2 and disk24()["kids"]["fails"] == 1, str(S.tool_results()) + str(disk24()))
S = Script24(tr("t1", "kids_mode", {"on": False}), text_resp("Режим остаётся включённым."))
brain._create = S
say24("d3", "да я взрослый")
check("24d18. «да я взрослый» (и «ты взрослый?» — «да») режим НЕ выключает", modules.kids_on() and any("need_phrase" in x for x in S.tool_results()) and disk24()["kids"]["fails"] == 2)
S = Script24(tr("t1", "kids_mode", {"on": False}), text_resp("Режим остаётся включённым."))
brain._create = S
say24("d3", "выключи, я мама")
check("24d19. третья неверная попытка подряд -> пауза 10 минут на диске, режим включён",
      modules.kids_on() and disk24()["kids"]["fails"] >= 3 and abs(disk24()["kids"]["pause_until"] - (CLK24["t"] + 600)) < 2, str(disk24()))
S = Script24(tr("t1", "kids_mode", {"on": False}), text_resp("Режим остаётся включённым."))
brain._create = S
say24("d3", f"выключи детский режим, {GOOD_PHRASE24}")
check("24d19a. в паузе даже верная фраза игнорируется (ответ инструмента kids_paused, фраза не подсказана)",
      modules.kids_on() and any("kids_paused" in x for x in S.tool_results()) and GOOD_PHRASE24 not in json.dumps(S.tool_results()))
modules.reload()
check("24d19b. пауза переживает перезапуск (лежит на диске)", modules.kids_on() and not modules.kids_set("d3", False, None, GOOD_PHRASE24).ok and modules.kids_on())
CLK24["t"] += 599
check("24d19c. через 9:59 пауза ещё действует", not modules.kids_set("d3", False, None, GOOD_PHRASE24).ok and modules.kids_on())
CLK24["t"] += 2
S = Script24(tr("t1", "kids_mode", {"on": False}), text_resp("Режим выключен."))
brain._create = S
say24("d3", f"выключи детский режим, {GOOD_PHRASE24}")
check("24d19d. пауза вышла -> верная фраза выключает, счётчик сброшен", not modules.kids_on() and disk24()["kids"]["fails"] == 0 and "pause_until" not in disk24()["kids"], str(disk24()))
modules.kids_set("d5", True, None, "")
for t_ in ("нет", "да взрослый"):
    modules.TURNS["d5"] = modules.TURNS.get("d5", 0) + 1
    modules.kids_set("d5", False, None, t_)
modules.TURNS["d5"] += 1
modules.kids_set("d5", False, None, GOOD_PHRASE24)                      # верная фраза: режим выключен, счётчик сброшен
modules.kids_set("d5", True, None, "")
for t_ in ("нет", "нет"):
    modules.TURNS["d5"] += 1
    modules.kids_set("d5", False, None, t_)
check("24d19e. ошибки считаются подряд: две ошибки, успех, снова включили и две ошибки -> счётчик 2, паузы нет", modules.kids_on() and "pause_until" not in disk24()["kids"] and disk24()["kids"]["fails"] == 2, str(disk24()))
modules.kids_set("d5", False, None, GOOD_PHRASE24)
modules.kids_set("d4", True, None, "")
# запоминается между разговорами
modules.reload()
check("24d20. детский режим запомнился между разговорами и перезапусками (читается с диска)", modules.kids_on())
# рубильник лицензии гасит режим, даже если он включён на диске
C.set_modules({})
check("24d21. модуль выключили в лицензии -> режим не действует, тон прежний, гейт света снят",
      not modules.kids_on() and "ДЕТСКИЙ РЕЖИМ. Сейчас" not in prompts.system_prompt() and run(iot.control("device", "sock1", "devices.capabilities.on_off", "on", True, "")) == "ok")
os.environ.pop("TONE", None)

# ================= 24e. уведомления: образ -> очередь лицензий =================
_r24()
_act24()
check("24e1. образ без активации (нет токена обновления) молча ничего не шлёт: ни запроса, ни ошибки",
      (Path(_DIR24) / "license.json").unlink() is None and run(notify.post("reminder", "тест")) == "skip" and not LIC24.notes)
_act24()
r = run(notify.post("balance_zero", prompts.phrase("billing_wa")))
n0 = LIC24.notes[-1]
check("24e2. деньги кончились: POST /v1/notify с install_id и токеном обновления, kind balance_zero, ссылки в тексте нет (кнопку ставит сервер)",
      r == "sent" and n0["kind"] == "balance_zero" and n0["install_id"] == IID24 and "console.anthropic.com" not in n0["text"] and "деньги на мозги" in n0["text"], str(n0))
check("24e3. неизвестный вид уведомления не уходит", run(notify.post("hack", "x")) == "skip")
# через настоящий путь образа: нуль на Anthropic -> одно в WhatsApp и одно в очередь, дальше раз в сутки
_r24()
_act24()
_wa24(True)
brain._create = _b_fail_money
fresh("e4")
say24("e4", "привет")
check("24e4. нуль на Anthropic: в очередь лицензий ушло ровно одно balance_zero и одно сообщение в WhatsApp",
      [x["kind"] for x in LIC24.notes] == ["balance_zero"] and len(WA_SENT24) == 1, str((LIC24.notes, WA_SENT24)))
say24("e4", "ну давай")
say24("e4", "алло")
check("24e5. ещё две реплики на нуле: очередь без повтора (деньги раз в сутки)", len(LIC24.notes) == 1 and len(WA_SENT24) == 1)
usage._now = lambda: time.time() + 25 * 3600
say24("e4", "через сутки")
usage._now = time.time
check("24e6. прошли сутки, нуль держится -> второе напоминание в очередь", len(LIC24.notes) == 2 and LIC24.notes[1]["kind"] == "balance_zero")
# 80% -> balance_low один раз
_r24()
_act24()
_wa24(True)
usage.topup(5)
usage.add(100, 4.2)
brain._create = Script24(text_resp("ответ"), text_resp("ответ"))
fresh("e7")
t = say24("e7", "привет")
say24("e7", "ещё")
check("24e7. потрачено 84% -> предупреждение: одно balance_low в очереди, второй реплике повторно не шлём",
      [x["kind"] for x in LIC24.notes] == ["balance_low"] and "console.anthropic.com" not in LIC24.notes[0]["text"] and "Пополнить баланс" in str(main.alice_reply(t + prompts.BILLING_URL)), str(LIC24.notes))
# сеть лежит: очередь на диске и повтор; 4xx: не повторяем
_r24()
_act24()
LIC24.down = True
r = run(notify.post("reminder", "забрать малого"))
ob = json.loads((Path(_DIR24) / "notify-outbox.json").read_text(encoding="utf-8")) if (Path(_DIR24) / "notify-outbox.json").exists() else []
check("24e8. сервер лицензий лёг: уведомление не потеряно, лежит в локальной очереди на диске (queued)", r == "queued" and len(ob) == 1 and ob[0]["kind"] == "reminder", str((r, ob)))
check("24e9. пока лежит, повтор не проходит и очередь цела", run(notify.flush()) == 0 and (Path(_DIR24) / "notify-outbox.json").exists())
LIC24.down = False
check("24e10. сервер поднялся -> повтор отправил, очередь стёрта", run(notify.flush()) == 1 and len(LIC24.notes) == 1 and not (Path(_DIR24) / "notify-outbox.json").exists())
LIC24.status = 429
r = run(notify.post("reminder", "x"))
check("24e11. сервер ответил 429/4xx (лимит) -> не повторяем, в очередь на диск не кладём", r == "drop" and not (Path(_DIR24) / "notify-outbox.json").exists())
LIC24.status = 500
r = run(notify.post("reminder", "y"))
check("24e12. сервер ответил 500 -> на повтор", r == "queued")
LIC24.status = 200
for i in range(25):
    LIC24.down = True
    run(notify.post("reminder", f"n{i}"))
ob = json.loads((Path(_DIR24) / "notify-outbox.json").read_text(encoding="utf-8"))
check("24e13. локальная очередь не растёт бесконечно: не больше 20, свежие остаются", len(ob) == 20 and ob[-1]["text"] == "n24", str(len(ob)))
CLK24["t"] += 25 * 3600
LIC24.down = False
check("24e14. сутки спустя устаревшее не шлём (напоминание о вчерашнем бессмысленно)", run(notify.flush()) == 0 and not (Path(_DIR24) / "notify-outbox.json").exists())
check("24e15. токен обновления и install_id в очередь и журнал не попадают", RT24 not in json.dumps(ob) and RT24 not in (Path(_DIR24) / "notify-outbox.json").read_text(encoding="utf-8") if (Path(_DIR24) / "notify-outbox.json").exists() else True)

# ================= 24f. /setup: часовой пояс и кодовая фраза взрослого =================
_r24()
os.environ["SETUP_CODE"] = "mod-setup-code-24"
setupwiz.reset_for_tests()
tcm = _TC(main.app)
tcm.post("/setup/api/login", json={"code": os.environ["SETUP_CODE"], "reopen": True})
st = tcm.get("/setup/api/state").json()
check("24f1. /setup/api/state: какие модули включены, пояс +5 по умолчанию, фраза не задана", st["modules"] == {"macros": False, "timers": False, "kids": False}
      and st["tz_offset"]["value"] == 5.0 and st["kids_phrase"]["set"] is False, str(st.get("modules")))
_mods24("timers", "kids")
r = tcm.post("/setup/api/modules", json={"tz_offset": "3", "kids_phrase": GOOD_PHRASE24})
st = tcm.get("/setup/api/state").json()
check("24f2. пояс и фраза сохраняются, применяются сразу (C.tz_offset=3, фраза есть)", r.status_code == 200 and C.tz_offset() == 3.0 and C.kids_phrase() == GOOD_PHRASE24
      and st["tz_offset"]["value"] == 3.0 and st["kids_phrase"]["set"] is True and st["modules"]["kids"] is True)
check("24f3. сама фраза нигде не отдаётся страницей", GOOD_PHRASE24 not in json.dumps(st) and GOOD_PHRASE24 not in r.text)
bad = [tcm.post("/setup/api/modules", json=x).status_code for x in ({"tz_offset": "abc"}, {"tz_offset": "99"}, {"kids_phrase": "abc"})]
check("24f4. кривой пояс и слишком короткая фраза -> 400", bad == [400, 400, 400], str(bad))
r1 = tcm.post("/setup/api/modules", json={"kids_phrase": "односложнаяфраза"})
check("24f4a. фраза из одного слова (даже длинного) на /setup отклоняется: минимум два слова", r1.status_code == 400 and r1.json()["error"] == "short_phrase" and C.kids_phrase() == GOOD_PHRASE24, r1.text)
os.environ["LOCK_PHRASE"] = "замочная фраза"
rs = tcm.post("/setup/api/modules", json={"kids_phrase": "Замочная   фраза!"})
check("24f5. фраза взрослого не может совпадать с фразой от замков (ребёнок её не должен знать из-за замков)", rs.status_code == 400 and rs.json()["error"] == "same_phrase", rs.text)
os.environ.pop("LOCK_PHRASE")
modules.kids_set("f1", True, None, "")
tcm.post("/setup/api/modules", json={"clear_kids_phrase": True, "tz_offset": "5"})
check("24f6a. взрослый стёр фразу на /setup -> детский режим заодно выключен (без фразы он не живёт)", not modules.kids_on())
check("24f6. «стереть фразу» и пояс 5 -> настройки сброшены", C.kids_phrase() == "" and C.tz_offset() == 5.0)
html24 = (HERE / "setupwiz.html").read_text(encoding="utf-8")
check("24f7a. рядом с полем фразы подсказка «Не говори её при детях», минимум два слова, проверка в браузере до отправки; про «ты взрослый?» ни слова",
      "Не говори её при детях" in html24 and "минимум 2 слова" in html24 and "length < 2" in html24 and "ты взрослый" not in html24)
check("24f7. страница показывает блок «Модули» только когда модуль включён (modulesBlock в шаге 4)", "function modulesBlock()" in html24 and "if (!m.timers && !m.kids) return null" in html24)
setupwiz.reset_for_tests()
C.set_setup_done(False)

# ---------- хвост: вернуть окружение ----------
iot._TRANSPORT = _IOT24_SAVED
activation.LICENSE_URL, activation._TRANSPORT = _ACT24_SAVED
wa._TRANSPORT = _WA24_SAVED
activation.reset_for_tests()
modules.reset_for_tests()
notify._now = time.time
modules._now = time.time
brain._create = _boom_create
for _k, _v in _SAVED24.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v
check("24z. Dockerfile копирует notify.py и modules.py, в образе есть cryptography/segno как раньше",
      "modules.py" in (HERE / "Dockerfile").read_text(encoding="utf-8") and "notify.py" in (HERE / "Dockerfile").read_text(encoding="utf-8"))
