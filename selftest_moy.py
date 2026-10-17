# Ворота ТЗ-34: «⚙️ Мой Кореш». Подключается из selftest.py (exec в его пространстве имён) после раздела 24, использует его помощников
# (_r24, _act24, say24, LIC24, IID24, RT24...). Что проверяем: голосовое «пришли ссылку на настройки» кладёт settings_link в очередь лицензий
# и честно говорит результат; адрес своего сервера уходит серверу лицензий; страница /setup/moduli пускает по тому же коду, не сбрасывает
# setup_done, отдаёт только свои ручки (остальные 403). Живых вызовов нет: модель не зовётся, сервер лицензий -- httpx.MockTransport.
DOM25 = "kor-moy-25.up.railway.app"
SET25 = "moy-setup-code-25"
KR25 = "KR-AAAA-BBBB-CCCC"
_ENV25 = ("PUBLIC_URL", "RAILWAY_PUBLIC_DOMAIN", "SETUP_CODE", "GENDER", "TZ_OFFSET", "KIDS_PHRASE")
_SAVED25 = {k: os.environ.get(k) for k in _ENV25}


class Lic25:
    """Сервер лицензий для ворот 25: пишет тела /v1/notify, отвечает status (queued | no_tg), знает один код KR для /v1/code/check."""

    def __init__(self):
        self.notes, self.refreshes, self.checks, self.status, self.down = [], [], [], "queued", False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("лежит")
        body = json.loads(request.content) if request.content else {}
        if request.url.path == "/v1/notify":
            if request.headers.get("authorization") != f"Bearer {RT24}" or body.get("install_id") != IID24:
                return httpx.Response(401, json={"ok": False})
            self.notes.append(body)
            return httpx.Response(200, json={"ok": True, "status": self.status})
        if request.url.path == "/v1/pack/refresh":
            self.refreshes.append(body)
            return httpx.Response(200, json={"ok": True, "status": "same", "pack_version": 1})
        if request.url.path == "/v1/code/check":
            self.checks.append(body)
            if body.get("code") == KR25:
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(404, json={"ok": False, "error": "bad_code"})
        return httpx.Response(404, json={"ok": False})


LIC25 = Lic25()


def _r25():
    _r24()
    activation._TRANSPORT = httpx.MockTransport(LIC25)
    LIC25.notes.clear()
    LIC25.refreshes.clear()
    LIC25.checks.clear()
    LIC25.status, LIC25.down = "queued", False
    notify._LAST_LINK["at"] = 0.0
    for k in ("PUBLIC_URL", "RAILWAY_PUBLIC_DOMAIN", "GENDER"):
        os.environ.pop(k, None)
    os.environ["RAILWAY_PUBLIC_DOMAIN"] = DOM25
    brain._create = _boom_create              # голосовая ссылка не должна звать мозг: если позовёт, ворота упадут


# ================= 25a. распознавание просьбы =================
SAY25 = ["пришли ссылку на настройки", "Пришли ссылку на мои настройки", "скинь ссылку на настройки", "кинь настройки в телеграм", "где мои настройки",
         "где настройки?", "ссылку на настройки", "дай ссылку на настройки пожалуйста", "ну скинь мне ссылку на настройки", "как зайти в настройки",
         "отправь ссылку на страницу настроек"]
NOT25 = ["где настройки звука на телевизоре", "включи свет", "настройки яркости лампы в спальне", "расскажи про настройки роутера подробно и по шагам",
         "сделай громче", "пришли привет маме", "что такое настройки"]
bad_yes = [p for p in SAY25 if not main.SETTINGS_RE.fullmatch(p.lower())]
bad_no = [p for p in NOT25 if main.SETTINGS_RE.fullmatch(p.lower())]
check("25a1. просьбы про ссылку на настройки узнаются (11 формулировок)", not bad_yes, str(bad_yes))
check("25a2. обычные фразы про настройки чего-то другого НЕ узнаются (7 фраз)", not bad_no, str(bad_no))

# ================= 25b. голос -> очередь лицензий -> честный ответ =================
_r25()
_act24()
t25 = say24("m1", "пришли ссылку на настройки")
check("25b1. голос: «Скинул ссылку в Телеграм», мозг не звали", t25 == "Скинул ссылку в Телеграм.", t25)
n25 = LIC25.notes[-1] if LIC25.notes else {}
check("25b2. в очередь лицензий ушёл settings_link с install_id и адресом СВОЕГО сервера (бот по нему даст ссылки)",
      n25.get("kind") == "settings_link" and n25.get("install_id") == IID24 and n25.get("server_domain") == DOM25, str(n25))
check("25b3. токен обновления в тело не попал", RT24 not in json.dumps(n25))
t25 = say24("m1", "где мои настройки")
check("25b4. второй раз сразу: не спамим, говорим «только что скинул»; в очереди по-прежнему одно", "только что" in t25 and len(LIC25.notes) == 1, t25)
notify._now = lambda: CLK24["t"] + 61
t25 = say24("m1", "пришли ссылку на настройки")
check("25b5. через минуту можно снова: второе уведомление в очереди", len(LIC25.notes) == 2 and t25.startswith("Скинул"), t25)
notify._now = lambda: CLK24["t"]

_r25()
_act24()
os.environ["GENDER"] = "female"
t25 = say24("m2", "скинь ссылку на настройки")
check("25b6. женский род у женского ассистента: «Скинула»", t25 == "Скинула ссылку в Телеграм.", t25)

_r25()
_act24()
LIC25.status = "no_tg"
t25 = say24("m3", "пришли ссылку на настройки")
check("25b7. сервер лицензий не знает Телеграм клиента -> честно «не вышло», не «скинул»", "Скинул" not in t25 and "не знаю твой Телеграм" in t25, t25)
LIC25.status = "queued"
notify._LAST_LINK["at"] = 0.0

_r25()
(Path(_DIR24) / "license.json").unlink(missing_ok=True)
t25 = say24("m4", "пришли ссылку на настройки")
check("25b8. нет активации по коду -> честный отказ, в очередь ничего не шло", "Скинул" not in t25 and "не привязан" in t25 and not LIC25.notes, t25)

_r25()
_act24()
LIC25.down = True
t25 = say24("m5", "пришли ссылку на настройки")
check("25b9. сервер лицензий лёг -> «не достучался», не «скинул»", "Скинул" not in t25 and "не достучался" in t25, t25)
LIC25.down = False
t25 = say24("m5", "пришли ссылку на настройки")
check("25b10. после сбоя повторная просьба проходит (сбой не включает паузу)", t25.startswith("Скинул") and len(LIC25.notes) == 1, t25)

# ================= 25c. адрес сервера уходит серверу лицензий =================
_r25()
_pack24()
run(activation.refresh_once())
check("25c1. обновление пакета несёт server_domain (старые установки так «догоняют» адрес)", LIC25.refreshes and LIC25.refreshes[-1].get("server_domain") == DOM25, str(LIC25.refreshes))
os.environ.pop("RAILWAY_PUBLIC_DOMAIN")
os.environ["PUBLIC_URL"] = "https://Kor-Public.example.com/"
check("25c2. PUBLIC_URL главнее, схема и путь срезаются, регистр нормализуется", activation.server_domain() == "kor-public.example.com", activation.server_domain())
os.environ.pop("PUBLIC_URL")
run(activation.start_flow(KR25, "kor-flow-25.up.railway.app"))
check("25c3. адрес, с которого человек входил на /setup, запоминается и подходит, если переменных нет", activation.server_domain() == "kor-flow-25.up.railway.app", activation.server_domain())
run(activation.start_flow(KR25, "localhost"))
st25 = activation._read_state()
check("25c4. localhost и мусор адресом не становятся", activation._host("localhost") == "" and activation._host("http://evil/<x>") == "" and st25.get("server_domain") == "kor-flow-25.up.railway.app")

# ================= 25d. /setup/moduli =================
_r25()
setupwiz.reset_for_tests()
os.environ.pop("SETUP_CODE", None)
tcn = _TC(main.app)
check("25d0. без SETUP_CODE страницы /setup/moduli нет (404), как и /setup", tcn.get("/setup/moduli").status_code == 404 and tcn.get("/setup/api/moduli/me").status_code == 404)
os.environ["SETUP_CODE"] = SET25
setupwiz.reset_for_tests()
C.set_setup_done(True)                               # установка завершена: /setup закрыт, а страница модулей должна пускать
_mods24("timers", "kids")
tm = _TC(main.app)
pg = tm.get("/setup/moduli")
check("25d1. страница отдаётся: 200, CSP без внешних источников, не кэшируется", pg.status_code == 200 and "default-src 'none'" in pg.headers.get("content-security-policy", "")
      and pg.headers.get("cache-control") == "no-store" and "Модули и баланс" in pg.text)
check("25d2. вёрстка мобильная (viewport + медиазапрос), слов «Перенастроить» и вызова reopen нет",
      'name="viewport"' in pg.text and "@media (max-width:480px)" in pg.text and "Перенастроить" not in pg.text and "reopen" not in pg.text)
check("25d3. страница ходит только в /setup/api/moduli/*", set(re.findall(r"/setup/api/([a-z/-]+)", pg.text)) <= {"moduli/"}, str(set(re.findall(r"/setup/api/([a-z/-]+)", pg.text))))
check("25d4. без входа: me -> authed=False, state/settings/topup -> 401",
      tm.get("/setup/api/moduli/me").json()["authed"] is False and [tm.get("/setup/api/moduli/state").status_code,
      tm.post("/setup/api/moduli/settings", json={}).status_code, tm.post("/setup/api/moduli/topup", json={"amount": 5}).status_code] == [401, 401, 401])
rb = tm.post("/setup/api/moduli/login", json={"code": "неверный"})
check("25d5. неверный код -> 401 bad_code", rb.status_code == 401 and rb.json()["error"] == "bad_code", rb.text)
rl = tm.post("/setup/api/moduli/login", json={"code": SET25})
check("25d6. SETUP_CODE пускает, сессия «только модули»; setup_done НЕ сброшен", rl.status_code == 200 and rl.json()["scope"] == "moduli" and C.setup_done() is True, rl.text)
stm = tm.get("/setup/api/moduli/state").json()
check("25d7. state: четыре модуля с признаком on, пояс +5, фраза не задана, баланс есть; секретов нет",
      [m["id"] for m in stm["modules"]] == ["macros", "timers", "kids", "secretary"] and [m["on"] for m in stm["modules"]] == [False, True, True, False]
      and stm["tz_offset"]["value"] == 5.0 and stm["kids_phrase"]["set"] is False and "budget_usd" in stm["balance"]
      and not any(k in json.dumps(stm) for k in ("brain_key", "webhook", "yandex", "login_code", "iot")), str(stm)[:200])
rs = tm.post("/setup/api/moduli/settings", json={"tz_offset": "3", "kids_phrase": "синяя ромашка"})
check("25d8. пояс и фраза сохраняются и применяются сразу; сама фраза в ответ не возвращается",
      rs.status_code == 200 and C.tz_offset() == 3.0 and C.kids_phrase() == "синяя ромашка" and "ромашка" not in rs.text and C.setup_done() is True, rs.text)
check("25d9. кривой пояс и фраза из одного слова -> 400",
      [tm.post("/setup/api/moduli/settings", json={"tz_offset": "99"}).status_code, tm.post("/setup/api/moduli/settings", json={"kids_phrase": "односложная"}).status_code] == [400, 400])
rt = tm.post("/setup/api/moduli/topup", json={"amount": "7"})
check("25d10. «Я пополнил»: бюджет 7 $, траты с нуля", rt.status_code == 200 and rt.json()["balance"]["budget_usd"] == 7.0 and rt.json()["balance"]["spent_usd"] == 0.0, rt.text)
check("25d11. кривая сумма -> 400", tm.post("/setup/api/moduli/topup", json={"amount": "abc"}).status_code == 400)

FORBID25 = [("GET", "/setup/api/state"), ("POST", "/setup/api/check-brain"), ("POST", "/setup/api/topup"), ("POST", "/setup/api/modules"),
            ("POST", "/setup/api/iot-token"), ("POST", "/setup/api/license/start"), ("GET", "/setup/api/license/status"), ("GET", "/setup/api/devices"),
            ("POST", "/setup/api/character"), ("POST", "/setup/api/home"), ("GET", "/setup/api/heard"), ("POST", "/setup/api/self-test"),
            ("POST", "/setup/api/finish"), ("GET", "/setup/api/wa/state"), ("POST", "/setup/api/wa/consent"), ("GET", "/setup/api/wa/link"),
            ("POST", "/setup/api/wa/pair"), ("POST", "/setup/api/wa/logout"), ("GET", "/setup/api/wa/contacts"), ("POST", "/setup/api/wa/contacts")]
codes25 = {}
for _m, _p in FORBID25:
    rr = tm.get(_p) if _m == "GET" else tm.post(_p, json={})
    codes25[f"{_m} {_p}"] = rr.status_code
check("25d12. сессия «только модули»: все 20 прочих ручек мастера отвечают 403", all(v == 403 for v in codes25.values()), str({k: v for k, v in codes25.items() if v != 403}))
check("25d13. после всего этого настройка по-прежнему «завершена» (setup_done=True), ничего не сброшено", C.setup_done() is True)
setupwiz.note_request("секретная реплика хозяина")
check("25d14. сессия модулей не включает режим «подслушивания» мастера: реплика не запомнена", setupwiz._heard["text"] == "")
rlo = tm.post("/setup/api/moduli/logout", json={})
check("25d15. выход: сессия погасла, state снова 401", rlo.status_code == 200 and tm.get("/setup/api/moduli/state").status_code == 401)

# вход кодом KR-… через сервер лицензий (как на /setup)
tk = _TC(main.app)
rk = tk.post("/setup/api/moduli/login", json={"code": "kr aaaa bbbb cccc"})
check("25d16. код KR-… (в любой записи) проверяет сервер лицензий и пускает; код не сохраняется", rk.status_code == 200 and LIC25.checks and LIC25.checks[-1]["code"] == KR25
      and KR25 not in json.dumps(setupwiz._session_codes), rk.text)
LIC25.down = True
tk2 = _TC(main.app)
rd = tk2.post("/setup/api/moduli/login", json={"code": "KR-DDDD-EEEE-FFFF"})
LIC25.down = False
check("25d17. сервер лицензий недоступен -> 502 net, это не неверная попытка (счётчик не растёт)", rd.status_code == 502 and setupwiz._fails["n"] == 0, rd.text)
# перебор: счётчик общий с мастером
tb = _TC(main.app)
setupwiz.reset_for_tests()
res = [tb.post("/setup/api/moduli/login", json={"code": f"nope{i}"}).status_code for i in range(5)]
check("25d18. пять неверных кодов подряд -> блок на десять минут (429), и мастеру /setup тоже: счётчик общий",
      res == [401, 401, 401, 401, 429] and tb.post("/setup/api/login", json={"code": SET25}).status_code == 429, str(res))
setupwiz.reset_for_tests()

# мастер и страница модулей рядом
tf = _TC(main.app)
tf.post("/setup/api/login", json={"code": SET25, "reopen": True})
check("25d19. полная сессия мастера видит и модули (state), и вход на страницу модулей её не понижает",
      tf.get("/setup/api/moduli/state").status_code == 200 and tf.post("/setup/api/moduli/login", json={"code": SET25}).json()["scope"] == "full"
      and tf.get("/setup/api/state").status_code == 200)
check("25d20. выход со страницы модулей полную сессию мастера не гасит", tf.post("/setup/api/moduli/logout", json={}).status_code == 200 and tf.get("/setup/api/state").status_code == 200)
C.set_setup_done(False)
setupwiz.reset_for_tests()

# закрытая страница мастера подсказывает, куда идти за модулями
html25 = (HERE / "setupwiz.html").read_text(encoding="utf-8")
check("25d21. закрытый /setup подсказывает ссылку на /setup/moduli (без перенастройки)", "href: '/setup/moduli'" in html25)
check("25d22. Dockerfile копирует setupmod.html (иначе в образе страницы нет)", "setupmod.html" in (HERE / "Dockerfile").read_text(encoding="utf-8"))

# ---------- хвост: вернуть окружение ----------
iot._TRANSPORT = _IOT24_SAVED
activation.LICENSE_URL, activation._TRANSPORT = _ACT24_SAVED
wa._TRANSPORT = _WA24_SAVED
activation.reset_for_tests()
modules.reset_for_tests()
notify._now = time.time
modules._now = time.time
notify._LAST_LINK["at"] = 0.0
brain._create = _boom_create
setupwiz.reset_for_tests()
C.set_setup_done(False)
for _k, _v in _SAVED25.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v
for _k, _v in _SAVED24.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v
