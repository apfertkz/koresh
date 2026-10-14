# Ворота ТЗ-31: деньги на мозги. Подключается из selftest.py (exec в его пространстве имён: check, run, N, httpx, main, brain, usage...).
# Что проверяем: заглушка при нуле на Anthropic в характере Кореша, служебное сообщение хозяину в WhatsApp (один раз, не чаще раза
# в сутки), счёт долларов по response.usage, предупреждение с 80%, «Я пополнил» на /setup, поле spent_usd в /health.
# Живого Anthropic и WhatsApp нет: модель заглушка, демон WhatsApp -- httpx.MockTransport.
from starlette.testclient import TestClient as _TC23

import setupwiz
import wa

_ENV23 = ("MEMORY_DIR", "USAGE_FILE", "SETUP_CODE", "TONE", "GENDER", "WA_ENABLED", "WA_CONTACTS", "ANTHROPIC_API_KEY", "DAILY_TOKEN_CAP")
_SAVED23 = {k: os.environ.get(k) for k in _ENV23}
_DIR23 = tempfile.mkdtemp()
os.environ["MEMORY_DIR"] = _DIR23
os.environ["USAGE_FILE"] = os.path.join(_DIR23, "usage.json")
for _k in ("TONE", "GENDER", "WA_ENABLED", "WA_CONTACTS", "DAILY_TOKEN_CAP"):
    os.environ.pop(_k, None)
os.environ["ANTHROPIC_API_KEY"] = "sk-leak-canary-23"
WAD23 = Path(_DIR23) / "wa"
SENT23 = []                                  # тела запросов /send к фейковому демону


def _b_handler(request: httpx.Request) -> httpx.Response:
    if request.headers.get("x-wa-token") != wa.TOKEN:
        return httpx.Response(401, json={"ok": False, "error": "unauthorized"})
    body = json.loads(request.content) if request.content else {}
    if request.url.path == "/send":
        SENT23.append(body)
        return httpx.Response(200, json={"ok": True})
    if request.url.path == "/status":
        return httpx.Response(200, json={"ok": True, "state": "connected", "linked": True})
    return httpx.Response(404)


def _b_link(on=True):
    WAD23.mkdir(parents=True, exist_ok=True)
    f = WAD23 / "linked.json"
    if on:
        f.write_text(json.dumps({"linked": True, "at": 1}), encoding="utf-8")
    elif f.exists():
        f.unlink()


def _b_reset():
    reset_all()
    os.environ["MEMORY_DIR"] = _DIR23
    os.environ["USAGE_FILE"] = os.path.join(_DIR23, "usage.json")
    SENT23.clear()
    wa.reset_for_tests()
    wa.DAEMON_CMD = [sys.executable, "-c", "import time; time.sleep(120)"]
    wa._TRANSPORT = httpx.MockTransport(_b_handler)
    usage._now = time.time
    _b_link(True)


class BUsage:
    def __init__(self, i=0, o=0, cw=0, cr=0):
        self.input_tokens, self.output_tokens = i, o
        self.cache_creation_input_tokens, self.cache_read_input_tokens = cw, cr


def _b_resp(text, u):
    r = Resp([Blk(type="text", text=text)], "end_turn")
    r.usage = u
    return r


async def _b_say(uid, text):
    """Реплика + дождаться фоновых задач (служебное сообщение хозяину уходит в фоне)."""
    out = (await main.handle(uid, text, False))["response"]["text"]
    await asyncio.sleep(0.05)
    await main.drain()
    return out


def _b_say_run(uid, text):
    return run(_b_say(uid, text))


def _b_money_err():
    return BadRequestError("Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing.")


async def _b_fail_money(**kw):
    raise _b_money_err()


async def _b_fail_429(**kw):
    raise RateLimitError("rate_limit_error: slow down")


# ================= 23a. цены: одно место, четыре вида токенов =================
M = 1_000_000
check("23a1. цены sonnet-5-5 за 1 млн: вход $2, выход $10, чтение кэша $0,20, запись кэша $2,50",
      abs(usage.cost_usd(BUsage(i=M)) - 2.0) < 1e-9 and abs(usage.cost_usd(BUsage(o=M)) - 10.0) < 1e-9
      and abs(usage.cost_usd(BUsage(cr=M)) - 0.2) < 1e-9 and abs(usage.cost_usd(BUsage(cw=M)) - 2.5) < 1e-9)
check("23a2. живой замер 14.10: холодная реплика (434 вход, 156 выход, 2957 запись кэша) = $0,0098; тёплая (605/176/чтение 2957) = $0,0036",
      abs(usage.cost_usd(BUsage(434, 156, 2957, 0), "claude-sonnet-5-5") - 0.009820) < 1e-6
      and abs(usage.cost_usd(BUsage(605, 176, 0, 2957), "claude-sonnet-5-5") - 0.003561) < 5e-6)
check("23a3. haiku-5-5 (пересказ) считается по своей цене $0,10/$0,50, неизвестная модель -- по sonnet (счёт не занижаем)",
      abs(usage.cost_usd(BUsage(i=M, o=M), "claude-haiku-5-5") - 0.6) < 1e-9 and abs(usage.cost_usd(BUsage(i=M), "claude-future-9") - 2.0) < 1e-9)
check("23a4. usage без полей кэша (старый SDK) / dict / None не роняют счёт",
      abs(usage.cost_usd(Usage(1000, 100)) - 0.003) < 1e-9 and abs(usage.cost_usd({"input_tokens": 1000, "output_tokens": 100}) - 0.003) < 1e-9
      and usage.cost_usd(None) == 0.0)
check("23a5. цены лежат в одном месте (usage.PRICES); brain и summarize считают через usage.cost_of, своих чисел не держат",
      isinstance(usage.PRICES, dict) and "usage.cost_of(resp" in (HERE / "brain.py").read_text(encoding="utf-8")
      and "usage.cost_of(resp" in (HERE / "summarize.py").read_text(encoding="utf-8"))

# ================= 23b. траты копятся из ответов, переживают перезапуск, видны в /health =================
_b_reset()
n23 = {"n": 0}


async def _b_fake_ok(**kw):
    n23["n"] += 1
    return _b_resp("ответ", BUsage(1000, 100, 2000, 500))      # (1000*2 + 100*10 + 2000*2.5 + 500*0.2) / 1e6 = $0,0081

brain._create = _b_fake_ok
fresh("b1")
_b_say_run("b1", "привет")
check("23b1. одна реплика -> $0,0081 в счёте, токены в суточном счётчике как раньше", abs(usage.spent_usd() - 0.0081) < 1e-6 and usage.used() == 3600, f"{usage.spent_usd()} {usage.used()}")
usage._state.update(day="", used=0, spent=0.0, budget=5.0, daily={}, loaded=False)         # «перезапуск»: оперативная копия потеряна
check("23b2. после перезапуска счёт и сумма пополнения читаются с диска", abs(usage.spent_usd() - 0.0081) < 1e-6 and usage.budget_usd() == 5.0)
h23 = _TC23(main.app).get("/health").json()
check("23b3. /health отдаёт spent_usd и budget_usd", h23["spent_usd"] == 0.0081 and h23["budget_usd"] == 5.0, str(h23))
check("23b4. /health не светит ключ и лишних секретов", "sk-leak-canary-23" not in json.dumps(h23))
check("23b5. средний расход за окно: за один день с пополнения = траты этого дня", abs(usage.avg_daily_usd() - 0.0081) < 1e-6)

# сломанный/старый usage.json (только day и used, как до ТЗ-31) читается без падений
Path(os.environ["USAGE_FILE"]).write_text(json.dumps({"day": time.strftime("%Y-%m-%d", time.gmtime()), "used": 777}), encoding="utf-8")
usage._state.update(loaded=False)
check("23b6. старый usage.json (без долларов) читается: токены 777, потрачено $0, сумма по умолчанию $5", usage.used() == 777 and usage.spent_usd() == 0.0 and usage.budget_usd() == 5.0)
Path(os.environ["USAGE_FILE"]).write_text("{не json", encoding="utf-8")
usage._state.update(loaded=False, spent=0.0, budget=5.0)
check("23b7. битый usage.json -> считаем с нуля, не падаем", usage.spent_usd() == 0.0 and usage.budget_usd() == 5.0)

# ================= 23c. деньги кончились: заглушка в характере Кореша, без казёнщины и без мата =================
for _tone in ("derzkiy_mat", "derzkiy", "vezhlivyy"):
    for _g in ("male", "female"):
        os.environ["TONE"], os.environ["GENDER"] = _tone, _g
        _b_reset()
        os.environ["TONE"], os.environ["GENDER"] = _tone, _g
        wa.DAEMON_CMD = None
        _b_link(False)
        brain._create = _b_fail_money
        fresh("c1")
        t = run(main.handle("c1", "привет", False))["response"]["text"]
        low = t.lower()
        check(f"23c1. {_tone}/{_g}: на нуле -- «деньги на мозги», Billing, пять долларов, без мата и без старого казённого текста",
              "деньги на мозги" in low and "billing" in low and "пять долларов" in low and not MAT_RE.search(t)
              and "кредит" not in low and "spend limit" not in low and "мозг отказал" not in low, t)
        check(f"23c2. {_tone}/{_g}: заглушка нейтральна по роду (годится и Кореша, и корешки)",
              not self_forms(t, MASC_SELF) and not self_forms(t, FEM_SELF), str((self_forms(t, MASC_SELF), self_forms(t, FEM_SELF))))
os.environ["TONE"], os.environ["GENDER"] = "derzkiy", "male"
check("23c3. голосовой вариант без адреса сайта (Алиса его прочитает по буквам): ссылка только в WhatsApp",
      "console.anthropic" not in prompts.phrase("billing") and "https://console.anthropic.com" in prompts.phrase("billing_wa"))
check("23c4. «Spend limit» в озвучку не идёт (английское), в сообщении хозяину -- есть как подсказка",
      "spend limit" not in prompts.phrase("billing").lower() and "Spend limit" in prompts.phrase("billing_wa"))

# ================= 23d. служебное сообщение хозяину в WhatsApp: один раз, не чаще раза в сутки =================
_b_reset()
brain._create = _b_fail_money
fresh("d1")
t1 = _b_say_run("d1", "привет")
check("23d1. нуль -> хозяину ушло ровно одно сообщение «себе»: self=true, без номера, с ссылкой на консоль",
      len(SENT23) == 1 and SENT23[0].get("self") is True and "to" not in SENT23[0] and "console.anthropic.com" in SENT23[0]["text"]
      and "деньги на мозги" in SENT23[0]["text"], str(SENT23))
t2 = _b_say_run("d1", "ну давай")
t3 = _b_say_run("d1", "алло")
check("23d2. ещё две реплики на нуле -> Алиса говорит заглушку каждый раз, а WhatsApp -- без повтора (всё ещё одно сообщение)",
      "деньги на мозги" in t2.lower() and "деньги на мозги" in t3.lower() and len(SENT23) == 1, str(len(SENT23)))
usage._now = lambda: time.time() + 25 * 3600
_b_say_run("d1", "через сутки")
check("23d3. прошли сутки, нуль держится -> напоминание хозяину раз в день (второе сообщение)", len(SENT23) == 2, str(len(SENT23)))
_b_say_run("d1", "и ещё раз")
check("23d4. и снова не чаще раза в сутки", len(SENT23) == 2, str(len(SENT23)))
usage._now = time.time
# WhatsApp не привязан -> молча, разговор не падает
_b_reset()
brain._create = _b_fail_money
_b_link(False)
fresh("d2")
t = _b_say_run("d2", "привет")
check("23d5. WhatsApp не привязан -> служебного сообщения нет, Алиса всё равно говорит заглушку", not SENT23 and "деньги на мозги" in t.lower(), str(SENT23))
# аварийный выключатель WhatsApp
_b_reset()
brain._create = _b_fail_money
os.environ["WA_ENABLED"] = "0"
fresh("d3")
_b_say_run("d3", "привет")
check("23d6. WA_ENABLED=0 -> хозяину ничего не шлём", not SENT23)
os.environ.pop("WA_ENABLED")
# «лимит расходов Anthropic» и 402 тоже считаются нулём; чужие ошибки (перегрузка) -- нет
_b_reset()
brain._create = _b_fail_429
fresh("d4")
t = _b_say_run("d4", "привет")
check("23d7. перегрузка (429) -- это не «кончились деньги»: заглушка про перегрузку, хозяину ничего", "перегружен" in t.lower() and not SENT23)
# отправка «себе» не съедает лимит сообщений контактам и не зависит от книги контактов
check("23d8. wa.notify_owner не требует контактов и не пишет в счётчик лимитов (десять в час остаются)",
      run(wa.notify_owner("проверка")) is True and wa.limit_reason() == "" and not (WAD23 / "limits.json").exists())

# ================= 23e. предупреждение с 80%: один раз, N дней по среднему расходу, плюс WhatsApp =================
_b_reset()
brain._create = _b_fake_ok
usage.topup(5)
usage.add(0, 3.9)                                  # 78% -- ещё молчим
fresh("e1")
t = _b_say_run("e1", "привет")
check("23e1. потрачено 78% -> Кореш молчит про деньги", "денег на мозги" not in t and not SENT23, t)
usage.add(0, 0.3)                                  # 4,2 + 0,0081 = 84%
t = _b_say_run("e1", "ещё раз")
check("23e2. потрачено >= 80% -> в начале ответа «Кстати, денег на мозги осталось примерно на N дней», дальше сам ответ",
      t.startswith("Кстати, денег на мозги осталось примерно на ") and t.endswith("ответ") and re.match(r"Кстати, денег на мозги осталось примерно на \d+ (день|дня|дней)\.", t), t)
check("23e3. хозяину ушёл тот же текст в WhatsApp (один раз), с ссылкой на Billing",
      len(SENT23) == 1 and "денег на мозги осталось примерно на" in SENT23[0]["text"] and "console.anthropic.com" in SENT23[0]["text"], str(SENT23))
t = _b_say_run("e1", "и ещё")
check("23e4. второй раз не повторяет: ни в голосе, ни в WhatsApp", "денег на мозги" not in t and len(SENT23) == 1, t)
usage.topup(5)
usage.add(0, 4.2)
t = _b_say_run("e1", "после пополнения")
check("23e5. после «Я пополнил» предупреждение снова разрешено", t.startswith("Кстати, денег на мозги"), t)
# N по среднему за 7 дней
_b_reset()
usage.topup(5)
today_s = time.time()
usage._state["since"] = today_s - 8 * 86400
usage._state["daily"] = {time.strftime("%Y-%m-%d", time.gmtime(today_s - i * 86400)): 0.5 for i in range(7)}
usage._state["spent"] = 4.0
check("23e6. N дней = остаток / средний расход за 7 дней: остаток $1, в среднем $0,50 в день -> 2 дня", usage.days_left() == 2, str(usage.days_left()))
usage._state["daily"] = {time.strftime("%Y-%m-%d", time.gmtime(today_s)): 4.0}
check("23e7. всё потрачено за сегодня (после пополнения 8 дней назад, окно 7 дней): среднее = 4/7, остаток $1 -> не меньше одного дня", usage.days_left() >= 1)
usage.topup(5)
usage.add(0, 4.5)
check("23e8. свежее пополнение, всё потрачено в первый день: окно из одного дня, остаток $0,5 при $4,5 в день -> 1 день (не 0)", usage.days_left() == 1, str(usage.days_left()))
check("23e9. склонение: 1 день, 2-4 дня, 5-20 дней, 21 день, 22 дня, 111 дней",
      [prompts.ru_days(n) for n in (1, 2, 4, 5, 11, 14, 20, 21, 22, 111)] == ["1 день", "2 дня", "4 дня", "5 дней", "11 дней", "14 дней", "20 дней", "21 день", "22 дня", "111 дней"])
# сумма 0 = слежения нет (автопополнение)
_b_reset()
brain._create = _b_fake_ok
usage.topup(0)
usage.add(0, 50)
fresh("e2")
t = _b_say_run("e2", "привет")
check("23e10. сумма 0 (включено автопополнение) -> предупреждений нет вообще", "денег на мозги" not in t and not SENT23 and usage.days_left() is None, t)
# вежливый тон
os.environ["TONE"] = "vezhlivyy"
check("23e11. вежливый тон: предупреждение без «закинь», на «вы»",
      "Пополните" in prompts.phrase("lowbalance", days=3) and "3 дня" in prompts.phrase("lowbalance", days=3) and not MAT_RE.search(prompts.phrase("lowbalance_wa", days=3)))
os.environ["TONE"] = "derzkiy"

# ================= 23f. /setup: «Сколько ты закинул» и «Я пополнил» =================
_b_reset()
brain._create = _b_fake_ok
SC23 = "balance-setup-code-23"
os.environ["SETUP_CODE"] = SC23
setupwiz.reset_for_tests()
C.set_setup_done(False)
usage.topup(5)
usage.add(0, 4.1)
with _TC23(main.app) as tc23:
    check("23f0. без входа ручка пополнения закрыта (401)", tc23.post("/setup/api/topup", json={"amount": 5}).status_code == 401)
    check("23f1. вход кодом", tc23.post("/setup/api/login", json={"code": SC23}).status_code == 200)
    st23 = tc23.get("/setup/api/state").json()
    check("23f2. состояние страницы: сумма $5, потрачено $4,1, остаток $0,9", st23["balance"]["budget_usd"] == 5.0 and st23["balance"]["spent_usd"] == 4.1
          and abs(st23["balance"]["remaining_usd"] - 0.9) < 1e-6, str(st23["balance"]))
    r = tc23.post("/setup/api/topup", json={"amount": "10"})
    check("23f3. «Я пополнил» на $10 -> сумма 10, счётчик с нуля, предупреждение снова разрешено",
          r.status_code == 200 and r.json()["balance"]["budget_usd"] == 10.0 and r.json()["balance"]["spent_usd"] == 0.0 and usage.take_low_warning() is None, r.text)
    r = tc23.post("/setup/api/topup", json={"amount": "7,5 $"})
    check("23f4. сумма с запятой и значком доллара понимается (7,5)", r.status_code == 200 and r.json()["balance"]["budget_usd"] == 7.5, r.text)
    bad = [tc23.post("/setup/api/topup", json={"amount": x}).status_code for x in ("abc", "-3", "999999", "nan", "")]
    check("23f5. мусор, минус, космос, nan, пусто -> 400, сумма не меняется", bad == [400] * 5 and usage.budget_usd() == 7.5, str(bad))
    check("23f6. ручка принимает только JSON (415 на форму)", tc23.post("/setup/api/topup", data={"amount": "5"}).status_code == 415)
    r = tc23.post("/setup/api/topup", json={"amount": 0})
    check("23f7. сумма 0 допустима (слежение выключено)", r.status_code == 200 and usage.budget_usd() == 0.0)
    page = tc23.get("/setup").text
    check("23f8. на странице есть поле «Сколько ты закинул на Anthropic ($)» и кнопка «Я пополнил»; текст про автопополнение",
          "Сколько ты закинул на Anthropic" in page and "Я пополнил" in page and "Auto-reload" in page)
os.environ.pop("SETUP_CODE", None)
setupwiz.reset_for_tests()
C.set_setup_done(False)

# ================= 23h. прямая ссылка на пополнение: WhatsApp текстом, Диалоги кнопкой (голосом не читаем) =================
_BU = prompts.BILLING_URL
os.environ["TONE"], os.environ["GENDER"] = "derzkiy", "male"
check("23h1. BILLING_URL одна константа в prompts, ровно консольная страница Billing",
      _BU == "https://console.anthropic.com/settings/billing")
for _tone in ("derzkiy_mat", "derzkiy", "vezhlivyy"):
    check(f"23h2. {_tone}: billing_wa и lowbalance_wa содержат BILLING_URL отдельной строкой; голосовой billing/lowbalance -- без ссылки",
          all(("\n" + _BU) in prompts.phrase(k, tone=_tone, days=3) and prompts.phrase(k, tone=_tone, days=3).endswith(_BU) for k in ("billing_wa", "lowbalance_wa"))
          and all("http" not in prompts.phrase(k, tone=_tone, days=3) for k in ("billing", "lowbalance")))
# ноль: в WhatsApp ушла ссылка, навыку -- кнопка с url, tts без ссылки
_b_reset()
brain._create = _b_fail_money
fresh("h1")
rh = run(main.handle("h1", "привет", False))["response"]
run(main.drain())
check("23h3. нуль -> в WhatsApp хозяину ссылка BILLING_URL отдельной строкой", len(SENT23) == 1 and SENT23[0]["text"].endswith("\n" + _BU), str(SENT23))
check("23h4. нуль -> в ответе навыку кнопка «Пополнить баланс» с url=BILLING_URL, hide=false",
      rh.get("buttons") == [{"title": "Пополнить баланс", "url": _BU, "hide": False}], str(rh.get("buttons")))
check("23h5. нуль -> в text ссылка видна на экране, в tts нет «http», нет «console», нет слова «ссылка»",
      _BU in rh["text"] and "http" not in rh["tts"] and "console" not in rh["tts"] and "ссылка" not in rh["tts"], rh["tts"])
check("23h6. нуль -> tts читается как обычная заглушка (деньги на мозги, Billing)", "деньги на мозги" in rh["tts"].lower(), rh["tts"])
# 80%: то же самое
_b_reset()
brain._create = _b_fake_ok
usage.topup(5)
usage.add(0, 4.2)
fresh("h2")
rh = run(main.handle("h2", "привет", False))["response"]
run(main.drain())
check("23h7. 80% -> в WhatsApp ссылка BILLING_URL отдельной строкой", len(SENT23) == 1 and SENT23[0]["text"].endswith("\n" + _BU), str(SENT23))
check("23h8. 80% -> кнопка «Пополнить баланс» с url в ответе навыку",
      rh.get("buttons") == [{"title": "Пополнить баланс", "url": _BU, "hide": False}], str(rh.get("buttons")))
check("23h9. 80% -> tts без «http» и без «console», сам ответ модели в tts на месте; в text ссылка видна",
      "http" not in rh["tts"] and "console" not in rh["tts"] and "ответ" in rh["tts"] and _BU in rh["text"], rh["tts"])
rh = run(main.handle("h2", "ещё раз", False))["response"]
check("23h10. обычный ответ (предупреждение уже было) -- без кнопок и ссылок", "buttons" not in rh and "http" not in rh["text"], str(rh))
check("23h11. alice_reply без ссылки кнопок не добавляет (ping, помощь)", "buttons" not in main.alice_reply("pong")["response"])

# ================= 23g. следы и сборка =================
src23 = "".join((HERE / f).read_text(encoding="utf-8") for f in ("main.py", "usage.py", "wa.py", "setupwiz.py"))
check("23g1. ключ Anthropic и тексты ошибок в журнал не попадают: в новом коде нет print с str(e)/ключом",
      "print(f\"notify" in src23 and "str(e)" not in (HERE / "usage.py").read_text(encoding="utf-8"))

# --- уборка раздела 23 ---
brain._create = _boom_create
wa._TRANSPORT = None
wa.DAEMON_CMD = None
wa.reset_for_tests()
usage._now = time.time
setupwiz.reset_for_tests()
for _k, _v in _SAVED23.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v
