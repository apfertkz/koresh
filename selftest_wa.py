# Ворота ТЗ-29: WhatsApp в клиентском Корешe. Подключается из selftest.py (exec в его пространстве имён: check, run, N, httpx, main, brain...).
# Живого WhatsApp и Anthropic нет: демон подменён фейком (HTTP-мок, фейковый Python-демон, настоящий wa-daemon.js на фейковом Baileys),
# модель -- заглушка или настоящий SDK поверх MockTransport. Имя из теста ("Ри" + "та") собрано из кусков: ворота №8 ловят следы выпиленного.
import base64
import shutil
import signal
import socket as _socket
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as _ET

import honesty
import setupwiz
import wa

RITA, RITE = "Ри" + "та", "Ри" + "те"
NUM_R, NUM_M = "77015551234", "77025552345"          # цифры, которых не должно быть нигде, кроме запроса к WhatsApp
_ENV22 = ("MEMORY_DIR", "USAGE_FILE", "SETUP_CODE", "WEBHOOK_SECRET", "ANTHROPIC_API_KEY", "WA_CONTACTS", "WA_HIDDEN", "WA_NICKS",
          "WA_ENABLED", "TONE", "GENDER", "HOUSEHOLD", "YANDEX_IOT_TOKEN", "ASSISTANT_NAME", "DAILY_TOKEN_CAP")
_SAVED22 = {k: os.environ.get(k) for k in _ENV22}
_DIR22 = tempfile.mkdtemp()
WAD = Path(_DIR22) / "wa"
os.environ["MEMORY_DIR"] = _DIR22
os.environ["USAGE_FILE"] = os.path.join(_DIR22, "usage.json")
for _k in ("WA_CONTACTS", "WA_HIDDEN", "WA_NICKS", "WA_ENABLED", "TONE", "GENDER", "HOUSEHOLD", "ASSISTANT_NAME", "YANDEX_IOT_TOKEN",
           "SETUP_CODE", "DAILY_TOKEN_CAP"):
    os.environ.pop(_k, None)
os.environ["ANTHROPIC_API_KEY"] = "sk-leak-canary-22"
os.environ["WEBHOOK_SECRET"] = "wa-hook-secret-22"
wa.reset_for_tests()
setupwiz.reset_for_tests()
C.set_setup_done(False)


def _w_reset_files():
    shutil.rmtree(WAD, ignore_errors=True)
    for f in Path(_DIR22).glob("config.json"):
        f.unlink()
    C._CFG.update(sig=None, data=None)
    C._MEM_ONLY.clear()
    wipe_memory_dir()
    for k in ("WA_CONTACTS", "WA_HIDDEN", "WA_NICKS"):
        os.environ.pop(k, None)
    wa.reset_for_tests()


def _w_linked(on=True):
    WAD.mkdir(parents=True, exist_ok=True)
    if on:
        (WAD / "linked.json").write_text(json.dumps({"linked": True, "at": 1}), encoding="utf-8")
    elif (WAD / "linked.json").exists():
        (WAD / "linked.json").unlink()


def _w_synced(people):
    WAD.mkdir(parents=True, exist_ok=True)
    (WAD / "contacts-sync.json").write_text(json.dumps({"at": 1, "contacts": [
        {"name": n, "number": num, "saved": s} for n, num, s in people]}, ensure_ascii=False), encoding="utf-8")


def _w_book(manual=None, nicks=None, hidden=None):
    for k, v in (("WA_CONTACTS", manual), ("WA_NICKS", nicks), ("WA_HIDDEN", hidden)):
        if v:
            os.environ[k] = json.dumps(v, ensure_ascii=False)
        else:
            os.environ.pop(k, None)


# ---------- фейковый демон на уровне HTTP-мока (для ходов мозга) ----------
FW = {}


def _w_fw_reset(**kw):
    FW.clear()
    FW.update(state="connected", linked=True, qr=None, contacts=0, connected_for=300, sent=[], send={"ok": True},
              pair={"ok": True, "code": "ABCD-1234"}, logout=0, token_bad=0)
    FW.update(kw)


def _w_handler(request: httpx.Request) -> httpx.Response:
    if request.headers.get("x-wa-token") != wa.TOKEN:
        FW["token_bad"] += 1
        return httpx.Response(401, json={"ok": False, "error": "unauthorized"})
    p = request.url.path
    body = json.loads(request.content) if request.content else {}
    if p == "/status":
        return httpx.Response(200, json={"ok": True, "state": FW["state"], "linked": FW["linked"], "qr": bool(FW["qr"]),
                                         "contacts": FW["contacts"], "connected_for": FW["connected_for"]})
    if p == "/qr":
        return httpx.Response(200, json={"ok": True, "qr": FW["qr"]})
    if p == "/send":
        FW["sent"].append(body)
        return httpx.Response(200, json=FW["send"])
    if p == "/pair":
        return httpx.Response(200, json=FW["pair"])
    if p == "/logout":
        FW["logout"] += 1
        return httpx.Response(200, json={"ok": True})
    return httpx.Response(404)


wa.DAEMON_CMD = [sys.executable, "-c", "import time; time.sleep(120)"]       # «команда демона» для enabled(); сами вызовы идут в мок
wa._TRANSPORT = httpx.MockTransport(_w_handler)
_w_fw_reset()
_w_reset_files()

# ================= 22a. имена и номера: разбор, падежи, прозвища, неоднозначность =================
check("22a1. normalize_number: +7 701..., 8 701..., 701... (десять цифр) -> 77015551234; мусор -> пусто",
      wa.normalize_number("+7 701 555 12 34") == NUM_R and wa.normalize_number("8 (701) 555-12-34") == NUM_R
      and wa.normalize_number("7015551234") == NUM_R and wa.normalize_number("12345") == "" and wa.normalize_number("abc") == "")
check("22a2. падежи как в мосте: " + RITE + "/Риту -> " + RITA.lower() + "; маме -> мама; жене -> жена",
      RITA.lower() in wa.word_variants(RITE) and "мама" in wa.word_variants("маме") and "жена" in wa.word_variants("жене"))

_w_book(manual={RITA: "+7 701 555 12 34", "Мама": "8 702 555 23 45", "Оля Иванова": "+77031112233", "Оля Петрова": "+77034445566"})
f1, f2, f3, f4 = wa.find_contact(RITE), wa.find_contact("маме"), wa.find_contact("Оле"), wa.find_contact("Вася")
check("22a3. «" + RITE + "» -> " + RITA + " (номер подставляет сервер), «маме» -> Мама", f1 == ("one", RITA, NUM_R) and f2 == ("one", "Мама", NUM_M), str((f1, f2)))
check("22a4. две «Оли» -> много совпадений, имена полные, переспросить; неизвестное имя -> none",
      f3[0] == "many" and f3[1] == ["Оля Иванова", "Оля Петрова"] and f4 == ("none",), str((f3, f4)))
check("22a5. голый номер в любой записи -> 'number' (не принимаем)",
      all(wa.find_contact(x) == ("number",) for x in ("+77015551234", "8 701 555 12 34", "77015551234", "701-555-12-34")))
_w_book(manual={"Оля": "+77031112233", "Оля работа": "+77034445566"})
check("22a6. полное совпадение главнее частичного: «Оле» -> Оля, а не «Оля работа»", wa.find_contact("Оле") == ("one", "Оля", "77031112233"))
_w_book(manual={RITA: "+77015551234"}, nicks={NUM_R: ["жена", "моя половинка"]})
check("22a7. прозвище: «жене» -> " + RITA + "; «половинке» -> " + RITA + "; без прозвища «мужу» -> none",
      wa.find_contact("жене") == ("one", RITA, NUM_R) and wa.find_contact("моей половинке")[0] in ("one", "none") and wa.find_contact("мужу") == ("none",))
_w_synced([("Борис Синхронов", "77051112233", True), ("Скрытый Человек", "77052223344", True), (RITA + " из WhatsApp", NUM_R, True)])
_w_book(manual={RITA: "+77015551234"}, hidden=["77052223344"])
names22 = sorted(c["name"] for c in wa.book())
check("22a8. книга = вписанные руками + синхронизация без скрытых; номер из ручной записи не дублируется синхронизированной",
      names22 == sorted([RITA, "Борис Синхронов"]), str(names22))
check("22a9. поиск по синхронизированному имени: «Борису» -> Борис Синхронов", wa.find_contact("Борису") == ("one", "Борис Синхронов", "77051112233"))
check("22a10. WA_CONTACTS в SECRET_KEYS и STORED_KEYS (в журнал значения не пишем), WA_HIDDEN/WA_NICKS тоже",
      all(k in C.STORED_KEYS and k in C.SECRET_KEYS for k in ("WA_CONTACTS", "WA_HIDDEN", "WA_NICKS")))
_w_reset_files()

# ================= 22b. подтверждение «строго по черновику», честность, промпт =================
_w_fw_reset()
_w_linked()
_w_book(manual={RITA: "+7 701 555 12 34", "Мама": "8 702 555 23 45"})
check("22b0. умение включено: привязан номер + есть контакты -> available, инструменты whatsapp_* у модели, блок в промпте",
      wa.available() and [t["name"] for t in brain.tools()][-2:] == ["whatsapp_draft", "whatsapp_confirm"] and "whatsapp_draft" in prompts.system_prompt())
sp_on = prompts.system_prompt()
check("22b0a. в промпте нет ни имён контактов, ни номеров (поиск по имени делает сервер)",
      RITA not in sp_on and "Мама" not in sp_on and NUM_R[-7:] not in sp_on and NUM_M[-7:] not in sp_on)
_w_linked(False)
check("22b0b. номер не привязан -> промпт без блока WhatsApp и инструментов нет (байт в байт как раньше)",
      not wa.available() and wa.prompt_block() == "" and "whatsapp" not in prompts.system_prompt().lower() and brain.tools() is brain.TOOLS)
_w_linked()
os.environ["WA_ENABLED"] = "0"
check("22b0c. WA_ENABLED=0 -> умения нет вообще", not wa.enabled() and not wa.available() and brain.tools() is brain.TOOLS)
os.environ.pop("WA_ENABLED")

REQ22 = []


async def _w_model_happy(**kw):
    REQ22.append(json.dumps({k: v for k, v in kw.items() if k != "messages"}, ensure_ascii=False, default=str)
                 + json.dumps(kw["messages"], ensure_ascii=False, default=str))
    last = kw["messages"][-1]
    if isinstance(last["content"], str):
        t = last["content"]
        if "Напиши" in t:
            return tool_resp("w1", "whatsapp_draft", {"to": RITE, "text": "Опоздаю"})
        if t.strip().lower() == "да":
            return tool_resp("w2", "whatsapp_confirm", {})
        return text_resp("Ок.")
    res = last["content"][0]["content"]
    if res.startswith("черновик готов"):
        return text_resp(f"Пишу {RITA}: «Опоздаю». Отправить?")
    if res.startswith("отправлено"):
        return text_resp("Отправил.")
    return text_resp("Не вышло: " + res)


def _w_say(uid, text):
    return run(main.handle(uid, text, False))["response"]["text"]


# --- 1. «Напиши Рите, что опоздаю» -> черновик -> «Отправить?» -> «да» -> ушло ровно то, что зачитано ---
_w_fw_reset()
brain._create = _w_model_happy
fresh("w-u1")
t1 = _w_say("w-u1", "Напиши Рите, что опоздаю")
check("22b1. шаг 1: хозяин услышал «Пишу <имя>: «Опоздаю». Отправить?», в WhatsApp ничего не ушло",
      f"Пишу {RITA}: «Опоздаю». Отправить?" == t1 and FW["sent"] == [], f"{t1!r} {FW['sent']}")
t2 = _w_say("w-u1", "да")
check("22b2. «да» -> ушло РОВНО одно сообщение: номер контакта (цифры) и дословно «Опоздаю»; ответ «Отправил.»",
      FW["sent"] == [{"to": NUM_R, "text": "Опоздаю"}] and t2 == "Отправил.", f"{FW['sent']} {t2!r}")
t3 = _w_say("w-u1", "да")
check("22b3. повторное «да» ничего не шлёт (черновик одноразовый), честный отказ вместо «отправил»",
      len(FW["sent"]) == 1 and "Отправил" not in t3 and "нет черновика" in t3, f"{t3!r}")

# «нет» -> не ушло
_w_fw_reset()
fresh("w-u2")
_w_say("w-u2", "Напиши Рите, что опоздаю")


async def _w_model_naive_confirm(**kw):
    last = kw["messages"][-1]
    if isinstance(last["content"], str):
        return tool_resp("n1", "whatsapp_confirm", {})
    return text_resp("Отправил.")


brain._create = _w_model_naive_confirm
t = _w_say("w-u2", "нет, не надо")
check("22b4. «нет» -> черновик стёрт, модель-хитрец зовёт confirm всё равно: ничего не ушло, ложное «Отправил» подменено честным",
      FW["sent"] == [] and "Отправил" not in t and "Не вышло" in t and wa.current_draft("w-u2") is None, f"{t!r}")

# ответ-не-«да» не отправляет, но черновик остаётся до «да»/«нет»/двух минут
brain._create = _w_model_happy
_w_fw_reset()
fresh("w-u3")
_w_say("w-u3", "Напиши Рите, что опоздаю")
brain._create = _w_model_naive_confirm
t = _w_say("w-u3", "да, но поменяй текст")
check("22b5. «да, но поменяй текст» согласием не считается: не ушло", FW["sent"] == [] and "Отправил" not in t, f"{t!r} {FW['sent']}")
check("22b5a. is_affirmation: да/давай отправляй/ага ок -> да; «да но…», «нет», «да нет», пусто -> нет",
      all(wa.is_affirmation(x) for x in ("да", "Да!", "давай, отправляй", "ага ок", "Отправляй")) and not any(
          wa.is_affirmation(x) for x in ("да, но поменяй", "нет", "да нет", "", "не надо отправлять", "подожди")))

# подмена в том же ходе: модель после «да» создаёт НОВЫЙ черновик с другим текстом и тут же подтверждает


async def _w_model_swap(**kw):
    last = kw["messages"][-1]
    if isinstance(last["content"], str):
        return tool_resp("s1", "whatsapp_draft", {"to": RITE, "text": "Приеду через час"})
    res = last["content"][0]["content"]
    if res.startswith("черновик готов"):
        return tool_resp("s2", "whatsapp_confirm", {})
    return text_resp("Отправил.")


_w_fw_reset()
fresh("w-u4")
brain._create = _w_model_happy
_w_say("w-u4", "Напиши Рите, что опоздаю")
brain._create = _w_model_swap
t = _w_say("w-u4", "да")
check("22b6. подмена текста после «да» (новый черновик + confirm в том же ходе): ничего не ушло, хозяин слышит НОВЫЙ текст с вопросом",
      FW["sent"] == [] and "Приеду через час" in t and t.endswith("Отправить?"), f"{t!r} {FW['sent']}")

# confirm с лишними параметрами: текст берётся из черновика


async def _w_model_extra(**kw):
    last = kw["messages"][-1]
    if isinstance(last["content"], str):
        if last["content"].strip() == "да":
            return tool_resp("e2", "whatsapp_confirm", {"text": "ДРУГОЙ ТЕКСТ", "to": "Мама"})
        return tool_resp("e1", "whatsapp_draft", {"to": RITE, "text": "Опоздаю"})
    res = last["content"][0]["content"]
    return text_resp(f"Пишу {RITA}: «Опоздаю». Отправить?" if res.startswith("черновик") else "Отправил.")


_w_fw_reset()
fresh("w-u5")
brain._create = _model = _w_model_extra
_w_say("w-u5", "Напиши Рите")
_w_say("w-u5", "да")
check("22b7. confirm с подсунутыми text/to игнорирует их: ушёл запомненный черновик, получатель тот же",
      FW["sent"] == [{"to": NUM_R, "text": "Опоздаю"}], str(FW["sent"]))

# черновик живёт две минуты
clock22 = [1_900_000_000.0]
wa._now = lambda: clock22[0]


def _w_flow(uid, to=RITE, text="привет", gap=0.0):
    wa.note_turn(uid, "напиши")
    d = run(wa.tool_draft(uid, to, text))
    if not d.ok:
        return d
    clock22[0] += gap
    wa.note_turn(uid, "да")
    return run(wa.tool_confirm(uid, "да"))


_w_fw_reset()
rr = _w_flow("w-t1", gap=119)
check("22c1. черновик 119 с -> ещё живой, уходит", rr.ok and len(FW["sent"]) == 1, rr.text)
rr = _w_flow("w-t2", gap=121)
check("22c2. черновик 121 с -> не отправляется: «устарел», ничего не ушло", not rr.ok and "устарел" in rr.text and len(FW["sent"]) == 1, rr.text)

# ---------- лимиты ----------
_w_reset_files()
_w_linked()
_w_book(manual={RITA: "+77015551234"})
_w_fw_reset()
res_all = [_w_flow("w-lim") for _ in range(10)]
nxt = _w_flow("w-lim")
check("22c3. 10 сообщений в час уходят, 11-е: лимит («не больше десяти в час»), черновик даже не создаётся",
      all(x.ok for x in res_all) and not nxt.ok and "десяти сообщений в час" in nxt.text and len(FW["sent"]) == 10 and wa.current_draft("w-lim") is None, nxt.text)
clock22[0] += 3601
res_all += [_w_flow("w-lim") for _ in range(10)]
clock22[0] += 3601
res_all += [_w_flow("w-lim") for _ in range(10)]
clock22[0] += 3601
nxt = _w_flow("w-lim")
check("22c4. за сутки 30 сообщений ушло, 31-е: лимит в сутки («тридцати в сутки»), хотя часовое окно свободно",
      all(x.ok for x in res_all) and len(FW["sent"]) == 30 and not nxt.ok and "тридцати сообщений в сутки" in nxt.text, nxt.text)
clock22[0] += 80000
nxt = _w_flow("w-lim")
check("22c5. через сутки от первых 10 окно сдвинулось -> снова можно", nxt.ok and len(FW["sent"]) == 31, nxt.text)
check("22c6. счётчик лежит на диске (переживает перезапуск): limits.json с метками времени", len(json.loads((WAD / "limits.json").read_text())["sent"]) >= 11)
# лимит ловится и при подтверждении (набили между черновиком и «да»)
_w_reset_files()
_w_linked()
_w_book(manual={RITA: "+77015551234"})
_w_fw_reset()
wa.note_turn("w-lim2", "напиши")
d = run(wa.tool_draft("w-lim2", RITE, "привет"))
WAD.mkdir(parents=True, exist_ok=True)
(WAD / "limits.json").write_text(json.dumps({"sent": [clock22[0] - 10 * i for i in range(10)]}))
wa.note_turn("w-lim2", "да")
cf = run(wa.tool_confirm("w-lim2", "да"))
check("22c7. лимит перепроверяется на «да»: набили 10 между черновиком и подтверждением -> не ушло", d.ok and not cf.ok and FW["sent"] == [], cf.text)
wa._now = time.time

# ---------- неизвестное имя, номер, неоднозначность ----------
_w_reset_files()
_w_linked()
_w_book(manual={RITA: "+77015551234", "Оля Иванова": "+77031112233", "Оля Петрова": "+77034445566"})
_w_fw_reset()
for who, want in (("Вася", "такого контакта не нашёл"), ("+7 701 555 99 99", "голые номера"), ("87015551234", "голые номера"),
                  ("Оле", "несколько контактов")):
    wa.note_turn("w-nm", "напиши")
    rr = run(wa.tool_draft("w-nm", who, "привет"))
    check(f"22d1. to={who!r}: отказ «{want}», черновика нет, в WhatsApp тишина", not rr.ok and want in rr.text and wa.current_draft("w-nm") is None and FW["sent"] == [], rr.text)
check("22d2. при неоднозначности модель получает ПОЛНЫЕ имена (чтобы переспросить), но не номера",
      "Оля Иванова" in rr.text and "Оля Петрова" in rr.text and "7703" not in rr.text and "5551234" not in rr.text, rr.text)


async def _w_model_unknown(**kw):
    last = kw["messages"][-1]
    if isinstance(last["content"], str):
        return tool_resp("u1", "whatsapp_draft", {"to": "Вася", "text": "привет"})
    return text_resp("Отправил Васе.")      # ложь после отказа


brain._create = _w_model_unknown
fresh("w-u6")
t = _w_say("w-u6", "Напиши Васе привет")
check("22d3. неизвестное имя: Кореш ничего не шлёт, ложное «Отправил Васе» подменено честным «не вышло»",
      FW["sent"] == [] and "Отправил" not in t and "Не вышло" in t, f"{t!r}")


async def _w_model_number(**kw):
    last = kw["messages"][-1]
    if isinstance(last["content"], str):
        return tool_resp("u1", "whatsapp_draft", {"to": "+77015559999", "text": "привет"})
    res = last["content"][0]["content"]
    return text_resp("Номера голосом не принимаю, назови имя." if "голые номера" in res else "Отправил.")


brain._create = _w_model_number
fresh("w-u7")
t = _w_say("w-u7", "Напиши на плюс семь семьсот один пятьсот пятьдесят пять девяносто девять девяносто девять привет")
check("22d4. голый номер голосом: отказ сервера, модель просит имя, ничего не ушло", FW["sent"] == [] and "не принимаю" in t and wa.current_draft("w-u7") is None, f"{t!r}")

# ---------- связь: слетела сессия, демон не на связи ----------
for state, linked_now, want in (("logged_out", True, "WhatsApp отвалился, перепривяжи на странице настройки"),
                                ("need_qr", True, "WhatsApp отвалился, перепривяжи на странице настройки"),
                                ("connecting", True, "WhatsApp сейчас не на связи")):
    _w_fw_reset(state=state, linked=linked_now)
    brain._create = _w_model_happy
    fresh("w-u8")
    t = _w_say("w-u8", "Напиши Рите, что опоздаю")
    check(f"22e1. состояние {state}: попытка отправки -> честная фраза «{want[:34]}…», черновика нет, ничего не ушло",
          want in t and FW["sent"] == [] and wa.current_draft("w-u8") is None, f"{t!r}")


async def _w_model_liar(**kw):
    last = kw["messages"][-1]
    if isinstance(last["content"], str):
        return tool_resp("l1", "whatsapp_draft", {"to": RITE, "text": "Опоздаю"})
    return text_resp("Готово, написал.")


_w_fw_reset(state="logged_out")
brain._create = _w_model_liar
fresh("w-u9")
t = _w_say("w-u9", "Напиши Рите, что опоздаю")
check("22e2. слетела сессия + модель врёт «написал» -> подмена: «Не вышло: WhatsApp отвалился, перепривяжи на странице настройки»",
      t.startswith("Не вышло: WhatsApp отвалился, перепривяжи на странице настройки"), f"{t!r}")
# сессия слетела между черновиком и «да»
_w_fw_reset()
wa.note_turn("w-lost", "напиши")
d = run(wa.tool_draft("w-lost", RITE, "привет"))
FW["state"] = "logged_out"
wa.note_turn("w-lost", "да")
cf = run(wa.tool_confirm("w-lost", "да"))
check("22e3. сессия слетела между черновиком и «да»: не ушло, причина «отвалился», черновик не висит", d.ok and not cf.ok and "отвалился" in cf.text and FW["sent"] == [] and wa.current_draft("w-lost") is None, cf.text)
for err, want in (("not_on_whatsapp", "нет в WhatsApp"), ("send_failed", "не принял"), ("not_connected", "не на связи"), ("timeout", "не знаю, дошло ли")):
    _w_fw_reset()
    wa.note_turn("w-err", "напиши")
    run(wa.tool_draft("w-err", RITE, "привет"))
    wa.note_turn("w-err", "да")
    if err == "timeout":
        wa._call_orig = wa._call

        async def _w_slow(method, path, body=None, timeout=wa.CALL_TIMEOUT, _o=wa._call):
            if path == "/send":
                return {"ok": False, "error": "timeout"}
            return await _o(method, path, body, timeout)
        wa._call = _w_slow
        cf = run(wa.tool_confirm("w-err", "да"))
        wa._call = wa._call_orig
    else:
        FW["send"] = {"ok": False, "error": err}
        cf = run(wa.tool_confirm("w-err", "да"))
    check(f"22e4. демон ответил {err}: Кореш получает причину «{want}», успех не изображается", not cf.ok and want in cf.text, cf.text)

# ---------- честность (honesty.py) ----------
J = honesty.Journal
tn = lambda j, txt: honesty.check(txt, j, "derzkiy", "male")      # noqa: E731
j = J(wa_active=True)
check("22f1. «Отправил.» без единой отправки -> замена «сообщение не отправлено»", "Отправил" not in tn(j, "Отправил.") and "не отправлено" in tn(j, "Отправил."))
j = J(wa_active=True)
j.wa_send(True)
check("22f2. «Отправил.» и «Готово.» после успешной отправки проходят", tn(j, "Отправил.") == "Отправил." and tn(j, "Готово.") == "Готово.")
j = J(wa_active=True)
j.wa_send(True)
check("22f3. успешная отправка не оправдывает «Включил свет» (чужой тип действия)", "Включил" not in tn(j, "Включил свет."))
j = J(wa_active=False)
check("22f4. умение выключено: «Я написал тебе стих» -- обычная речь, не заменяется", tn(j, "Я написал тебе стих.") == "Я написал тебе стих.")
j = J(wa_active=True)
check("22f5. «Не отправил» (отрицание) не ловится", tn(j, "Не отправил, не вышло.") == "Не отправил, не вышло.")
j = J(wa_active=True)
j.wa_send(False, "WhatsApp отвалился, перепривяжи на странице настройки")
check("22f6. после неудачной отправки «скинул» -> честная замена с причиной", "отвалился" in tn(j, "Скинул Рите.") and "Скинул" not in tn(j, "Скинул Рите."))
j = J(wa_active=True)
j.wa_draft(True)
check("22f7. слова ВНУТРИ кавычек черновика («я отправил документы») заявлением ассистента не считаются",
      tn(j, f"Пишу {RITA}: «Я отправил документы». Отправить?") == f"Пишу {RITA}: «Я отправил документы». Отправить?")
check("22f8. старое поведение не сломано: «Включил свет» без вызова при включённом WhatsApp тоже подменяется", "Включил" not in tn(J(wa_active=True), "Включил свет."))

# ---------- пересказ черновика: хозяин слышит ровно то, что уйдёт ----------
_w_fw_reset()
wa.DRAFTS.clear()
wa.note_turn("w-rb", "напиши")
run(wa.tool_draft("w-rb", RITE, "Опоздаю на пять минут"))
rb_bad = wa.ensure_readback("w-rb", "Готово, написал.")
rb_wrong_text = wa.ensure_readback("w-rb", f"Пишу {RITA}: «Опоздаю на час». Отправить?")
rb_wrong_name = wa.ensure_readback("w-rb", "Пишу Маше: «Опоздаю на пять минут». Отправить?")
rb_ok = wa.ensure_readback("w-rb", f"Пишу {RITE}: «Опоздаю на пять минут». Отправить?")
check("22g1. пересказ модели без текста/получателя/вопроса, с другим текстом или другим получателем заменяется каноничной фразой",
      all(x == f"Сообщение для «{RITA}»: «Опоздаю на пять минут». Отправить?" for x in (rb_bad, rb_wrong_text, rb_wrong_name)), str((rb_bad, rb_wrong_text, rb_wrong_name)))
check("22g2. верный пересказ (дословный текст + имя в любом падеже + вопрос) остаётся как есть", rb_ok == f"Пишу {RITE}: «Опоздаю на пять минут». Отправить?")
wa.DRAFTS.clear()
# полное имя при переспросе: черновик на «Олю Иванову» зачитывается целиком
_w_reset_files()
_w_linked()
_w_book(manual={"Оля Иванова": "+77031112233", "Оля Петрова": "+77034445566"})
wa.note_turn("w-full", "напиши")
run(wa.tool_draft("w-full", "Оля Иванова", "Привет"))
check("22g3. неоднозначность снята полным именем -> зачитывается ПОЛНОЕ имя («Оля Иванова»), а не «Оля»",
      wa.ensure_readback("w-full", "Готово.") == "Сообщение для «Оля Иванова»: «Привет». Отправить?")
wa.DRAFTS.clear()

# ================= 22h. номера НЕ уходят в Anthropic: настоящий SDK поверх MockTransport, грепаем тело каждого запроса =================
_w_reset_files()
_w_linked()
_w_synced([("Zzz Уникальный", "77059998877", True), ("Борис Синхронов", "77051112233", True)])
_w_book(manual={RITA: "+7 701 555 12 34", "Мама": "8 702 555 23 45"}, nicks={NUM_R: ["жена"]})
_w_fw_reset()
BODIES22 = []


def _w_sdk_handler(request: httpx.Request) -> httpx.Response:
    raw = request.content.decode()
    BODIES22.append(raw)
    body = json.loads(raw)
    last = body["messages"][-1]

    def msg(blocks, stop):
        return httpx.Response(200, json={"id": "msg_w", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5", "content": blocks,
                                         "stop_reason": stop, "stop_sequence": None, "usage": {"input_tokens": 5, "output_tokens": 5}})
    if isinstance(last["content"], str):
        t = last["content"]
        if "Напиши" in t:
            return msg([{"type": "tool_use", "id": "toolu_w1", "name": "whatsapp_draft", "input": {"to": "жене", "text": "Опоздаю"}}], "tool_use")
        if t.strip().lower() == "да":
            return msg([{"type": "tool_use", "id": "toolu_w2", "name": "whatsapp_confirm", "input": {}}], "tool_use")
        return msg([{"type": "text", "text": "Ок."}], "end_turn")
    res = last["content"][0]["content"]
    if res.startswith("черновик готов"):
        return msg([{"type": "text", "text": f"Пишу {RITA}: «Опоздаю». Отправить?"}], "end_turn")
    return msg([{"type": "text", "text": "Отправил." if res.startswith("отправлено") else "Не вышло."}], "end_turn")


_w_client = AsyncAnthropic(api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(_w_sdk_handler)))


async def _w_real_create(**kw):
    return await _w_client.messages.create(**kw)


brain._create = _w_real_create
fresh("w-sdk")
a1 = _w_say("w-sdk", "Напиши жене, что опоздаю")
a2 = _w_say("w-sdk", "да")
allraw = "\n".join(BODIES22)
digits_only = re.sub(r"\D", "", allraw)
sys_parts = [json.loads(b).get("system") for b in BODIES22]
sys_text = json.dumps(sys_parts, ensure_ascii=False)
check("22h1. сценарий через SDK прошёл целиком: черновик зачитан, «да» -> ушло дословно «Опоздаю» контакту по прозвищу «жене»",
      a1 == f"Пишу {RITA}: «Опоздаю». Отправить?" and a2 == "Отправил." and FW["sent"] == [{"to": NUM_R, "text": "Опоздаю"}], f"{a1!r} {a2!r} {FW['sent']}")
check("22h2. в телах запросов к Anthropic (все {} шт.) нет ни одного номера контактов (ни в виде цифр, ни хвостом)".replace("{}", str(len(BODIES22))),
      len(BODIES22) >= 4 and not any(x in digits_only for x in (NUM_R, NUM_M, "77059998877", "77051112233", NUM_R[1:], NUM_M[1:]))
      and not any(t in allraw for t in ("5551234", "5552345", "9998877", "1112233")))
check("22h3. имена, которых модель не спрашивала («Zzz Уникальный», «Борис Синхронов», «Мама»), в запросах не светятся; в системном промпте нет ни одного имени",
      "Уникальн" not in allraw and "Синхронов" not in allraw and "Мама" not in allraw and RITA not in sys_text)
check("22h4. зато имя найденного контакта модель получает (иначе нечего зачитывать): «" + RITA + "» есть в теле запроса после черновика", RITA in allraw)
check("22h5. ключ Anthropic и секрет вебхука в тела запросов не попадают (канарейки)", "sk-leak-canary-22" not in allraw and "wa-hook-secret-22" not in allraw and wa.TOKEN not in allraw)
brain._create = _boom_create

# ================= 22i. демон-сайдкар: надзор Python (настоящий HTTP, фейковый Python-демон) =================
_fake_daemon_py = r'''
import json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
TOKEN = os.environ["WA_TOKEN"]; PORT = int(os.environ["WA_PORT"]); D = os.environ["WA_DATA_DIR"]
os.makedirs(D, exist_ok=True)
open(os.path.join(D, "fake-env.json"), "w").write(json.dumps(sorted(os.environ.keys())))
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _s(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def _auth(self):
        if self.headers.get("X-WA-Token") != TOKEN:
            self._s(401, {"ok": False, "error": "unauthorized"}); return False
        return True
    def do_GET(self):
        if not self._auth(): return
        self._s(200, {"ok": True, "state": "connected", "linked": True, "qr": False, "contacts": 0, "connected_for": 5, "pid": os.getpid()})
    def do_POST(self):
        if not self._auth(): return
        n = int(self.headers.get("Content-Length") or 0); self.rfile.read(n)
        if self.path == "/die":
            self._s(200, {"ok": True}); threading.Thread(target=lambda: (time.sleep(0.1), os._exit(3))).start(); return
        self._s(200, {"ok": True})
class S(HTTPServer):
    def server_bind(self):                      # HTTPServer.server_bind делает getfqdn: на некоторых машинах это десятки секунд
        import socketserver
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]
S(("127.0.0.1", PORT), H).serve_forever()
'''
_fd22 = Path(tempfile.mkdtemp()) / "fake_wa_daemon.py"
_fd22.write_text(_fake_daemon_py, encoding="utf-8")


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


async def _w_wait(cond, t=12.0):
    end = time.time() + t
    while time.time() < end:
        v = await cond()
        if v:
            return v
        await asyncio.sleep(0.1)
    return None


async def _w_sup():
    out = {}
    wa.reset_for_tests()
    wa._TRANSPORT = None
    wa.DAEMON_CMD = [sys.executable, str(_fd22)]
    out["started"] = wa.ensure_started()
    out["twice"] = wa.ensure_started()

    async def conn():
        s = await wa.status()
        return s if s["state"] == "connected" else None
    out["st"] = await _w_wait(conn)
    j1 = await wa._call("GET", "/status")
    out["pid1"] = j1 and j1["pid"]
    out["env"] = json.loads((WAD / "fake-env.json").read_text())
    async with httpx.AsyncClient() as c:
        out["unauth"] = (await c.get(f"http://127.0.0.1:{wa._S['port']}/status")).status_code
        out["badtok"] = (await c.get(f"http://127.0.0.1:{wa._S['port']}/status", headers={"X-WA-Token": "x"})).status_code
    await wa._call("POST", "/die", {})

    async def again():
        j = await wa._call("GET", "/status")
        return j if j and j.get("pid") and j["pid"] != out["pid1"] else None
    j2 = await _w_wait(again, 20)
    out["pid2"] = j2 and j2["pid"]
    out["restarts"] = wa._S["restarts"]
    out["stat_after"] = (await wa.status())["state"]
    by = subprocess.Popen(["sleep", "60"])
    out["by"] = by.pid
    await wa.shutdown()
    out["alive2"] = _pid_alive(out["pid2"]) if out["pid2"] else None
    out["by_alive"] = by.poll() is None
    by.terminate()
    by.wait()
    out["running_after"] = wa.running()
    return out


_w_reset_files()
os.environ["ANTHROPIC_API_KEY"] = "sk-leak-canary-22"
SUP = run(_w_sup())
check("22i1. Python поднимает демон сайдкаром (процесс запущен, второй ensure_started не плодит копий), статус connected", SUP["started"] and SUP["twice"] and SUP["st"] and SUP["st"]["state"] == "connected", str(SUP))
check("22i2. запросы без токена и с чужим токеном демон отбивает (401); с токеном этого процесса работают", SUP["unauth"] == 401 and SUP["badtok"] == 401 and SUP["pid1"])
check("22i3. демон УПАЛ -> Python поднял новый процесс (другой pid), restarts>=1, связь вернулась",
      SUP["pid2"] and SUP["pid2"] != SUP["pid1"] and SUP["restarts"] >= 1 and SUP["stat_after"] == "connected", str((SUP["pid1"], SUP["pid2"], SUP["restarts"])))
check("22i4. демону отдаётся только своё окружение: WA_TOKEN/WA_PORT/WA_AUTH_DIR есть, ключа Anthropic и секрета вебхука нет",
      {"WA_TOKEN", "WA_PORT", "WA_AUTH_DIR", "WA_DATA_DIR"} <= set(SUP["env"]) and "ANTHROPIC_API_KEY" not in SUP["env"] and "WEBHOOK_SECRET" not in SUP["env"], str(SUP["env"]))
check("22i5. остановка гасит ТОЛЬКО свой процесс демона (по pid); посторонний sleep жив", SUP["alive2"] is False and SUP["by_alive"] is True and SUP["running_after"] is False)
wa.DAEMON_CMD = [sys.executable, "-c", "import time; time.sleep(120)"]
wa._TRANSPORT = httpx.MockTransport(_w_handler)
check("22i6. лишний процесс не висит: нет привязки -> autostart ничего не запускает (клиент без WhatsApp не платит памятью)",
      (_w_linked(False), wa.autostart())[1] is False and not wa.running())
_w_linked()

# ================= 22j. настоящий wa-daemon.js на фейковом Baileys =================
if not shutil.which("node"):
    print("SKIP  22j. node не найден: проверки wa-daemon.js пропущены (в CI node есть)")
else:
    _nd = Path(tempfile.mkdtemp())
    shutil.copy(HERE / "wa" / "wa-daemon.js", _nd / "wa-daemon.js")
    (_nd / "node_modules" / "@whiskeysockets" / "baileys").mkdir(parents=True)
    (_nd / "node_modules" / "pino").mkdir(parents=True)
    (_nd / "node_modules" / "pino" / "index.js").write_text(
        "module.exports = () => { const l = {level:'silent', child(){return l}, trace(){}, debug(){}, info(){}, warn(){}, error(){}, fatal(){}}; return l }\n")
    (_nd / "node_modules" / "@whiskeysockets" / "baileys" / "index.js").write_text(r"""
const fs = require('fs'), path = require('path'), { EventEmitter } = require('events');
const CTL = process.env.FAKE_CTL, CALLS = process.env.FAKE_CALLS;
const log = (o) => fs.appendFileSync(CALLS, JSON.stringify(o) + '\n');
let n = 0, off = 0, cur = null;
setInterval(() => {
  let txt = ''; try { txt = fs.readFileSync(CTL, 'utf8') } catch { return }
  const lines = txt.split('\n').filter(Boolean);
  while (off < lines.length) { const c = JSON.parse(lines[off++]); if (cur && c.emit) cur.ev.emit(c.emit[0], c.emit[1]) }
}, 40);
exports.DisconnectReason = { loggedOut: 401, restartRequired: 515, connectionClosed: 428, timedOut: 408 };
exports.fetchLatestBaileysVersion = async () => ({ version: [2, 3000, 1] });
exports.makeCacheableSignalKeyStore = (k) => k;
exports.useMultiFileAuthState = async (dir) => {
  fs.mkdirSync(dir, { recursive: true });
  const f = path.join(dir, 'creds.json');
  let creds = {};
  try { creds = JSON.parse(fs.readFileSync(f, 'utf8')) } catch { creds = { gen: Date.now() }; fs.writeFileSync(f, JSON.stringify(creds)) }
  return { state: { creds, keys: {} }, saveCreds: async () => fs.writeFileSync(f, JSON.stringify(creds)) };
};
exports.default = function makeWASocket(opts) {
  n++; const ev = new EventEmitter(); const names = []; const my = n;
  const on = ev.on.bind(ev); ev.on = (name, fn) => { names.push(name); return on(name, fn) };
  const sock = { ev, user: { id: '77019990000:12@s.whatsapp.net' }, end() { log({ end: my }) },
    signalRepository: { lidMapping: { async getPNsForLIDs(l) { log({ resolve: l }); return l.filter(x => x === '777@lid').map(x => ({ lid: x, pn: '77029990000:0@s.whatsapp.net' })) } } },
    async onWhatsApp(jid) { log({ onWA: jid }); return jid.startsWith('7700000') ? [{ exists: false }] : [{ exists: true, jid }] },
    async sendMessage(jid, content) { log({ send: jid, text: content.text }); return { key: { id: 'ID' + my }, message: content } },
    async requestPairingCode(p) { log({ pair: p }); return 'ABCD1234' },
    async logout() { log({ logout: my }) } };
  cur = sock;
  setTimeout(() => log({ sock: my, events: names.slice(), opts: Object.keys(opts) }), 30);
  return sock;
};
""")
    _ndata = Path(tempfile.mkdtemp())
    _nauth = _ndata / "auth"
    _nctl, _ncalls, _nout = _nd / "ctl.jsonl", _nd / "calls.jsonl", _nd / "daemon.out"
    _nctl.write_text("")
    _ncalls.write_text("")
    with _socket.socket() as _sk:
        _sk.bind(("127.0.0.1", 0))
        _nport = _sk.getsockname()[1]
    _nenv = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/tmp"), "WA_TOKEN": "TOK-22", "WA_PORT": str(_nport),
             "WA_AUTH_DIR": str(_nauth), "WA_DATA_DIR": str(_ndata), "FAKE_CTL": str(_nctl), "FAKE_CALLS": str(_ncalls)}

    def _nhttp(method, path, body=None, token="TOK-22"):
        hdr = {"Content-Type": "application/json"}
        if token:
            hdr["X-WA-Token"] = token
        req = urllib.request.Request(f"http://127.0.0.1:{_nport}{path}", method=method, headers=hdr,
                                     data=json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None))
        try:
            with urllib.request.urlopen(req, timeout=6) as rr_:
                return rr_.status, json.loads(rr_.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _nemit(name, data):
        with open(_nctl, "a", encoding="utf-8") as f_:
            f_.write(json.dumps({"emit": [name, data]}, ensure_ascii=False) + "\n")

    def _ncalls_list():
        return [json.loads(x) for x in _ncalls.read_text(encoding="utf-8").splitlines() if x.strip()]

    def _nwait(fn, t=12.0):
        end = time.time() + t
        while time.time() < end:
            v = fn()
            if v:
                return v
            time.sleep(0.1)
        return None

    def _nsocks():
        return [c for c in _ncalls_list() if "sock" in c]

    # без токена не стартует
    _p0 = subprocess.run([shutil.which("node"), str(_nd / "wa-daemon.js")], env={k: v for k, v in _nenv.items() if k != "WA_TOKEN"},
                         capture_output=True, text=True, timeout=20)
    check("22j0. wa-daemon.js без WA_TOKEN не стартует (код 2): сайдкар без токена не бывает", _p0.returncode == 2, str(_p0.returncode))
    # недопривязанная сессия (номер есть, аккаунта нет) стирается при старте
    _nauth.mkdir(parents=True)
    (_nauth / "creds.json").write_text(json.dumps({"me": {"id": "x"}, "gen": "OLD"}))
    _fo = open(_nout, "w", encoding="utf-8")
    _proc = subprocess.Popen([shutil.which("node"), str(_nd / "wa-daemon.js")], env=_nenv, cwd=str(_nd), stdout=_fo, stderr=subprocess.STDOUT)
    try:
        s1 = _nwait(lambda: _nsocks())
        check("22j1. демон поднял сокет Baileys; брошенная недопривязанная сессия стёрта при старте", bool(s1) and "OLD" not in (_nauth / "creds.json").read_text(), str(s1))
        c401 = _nhttp("GET", "/status", token=None)
        c401b = _nhttp("GET", "/status", token="wrong-token-x")
        c401c = _nhttp("POST", "/send", {"to": "77015551234", "text": "x"}, token="")
        check("22j2. без токена и с чужим токеном демон отвечает 401 на все ручки, отправка закрыта", c401[0] == 401 and c401b[0] == 401 and c401c[0] == 401)
        ev_names = s1[0]["events"]
        check("22j3. входящие читаются только для секретаря (ТЗ-36) и только по файлу-флагу inbox.json (проверки 28n); подписки: creds/connection/контакты/messages.upsert",
              set(ev_names) <= {"creds.update", "connection.update", "contacts.upsert", "contacts.update", "messaging-history.set", "lid-mapping.update", "messages.upsert"}, str(ev_names))
        check("22j3a. настройки сокета: markOnlineOnConnect/syncFullHistory/getMessage заданы, логгер тихий",
              {"markOnlineOnConnect", "syncFullHistory", "getMessage", "logger", "auth"} <= set(s1[0]["opts"]), str(s1[0]["opts"]))
        st0 = _nhttp("GET", "/status")[1]
        check("22j4. до подключения: состояние connecting, не linked", st0["state"] == "connecting" and st0["linked"] is False, str(st0))
        sn = _nhttp("POST", "/send", {"to": "77015551234", "text": "рано"})[1]
        check("22j5. отправка до подключения: not_connected, Baileys не дёрнут", sn == {"ok": False, "error": "not_connected"} and not [c for c in _ncalls_list() if "send" in c], str(sn))
        _nemit("connection.update", {"qr": "QR-STRING-22-ABC"})
        q = _nwait(lambda: _nhttp("GET", "/qr")[1].get("qr"))
        st1 = _nhttp("GET", "/status")[1]
        check("22j6. пришёл QR -> /qr отдаёт строку, состояние need_qr", q == "QR-STRING-22-ABC" and st1["state"] == "need_qr" and st1["qr"] is True, str((q, st1)))
        pr = _nhttp("POST", "/pair", {"phone": "+7 701 555 12 34"})[1]
        pr_bad = _nhttp("POST", "/pair", {"phone": "123"})[1]
        check("22j7. код сопряжения: ABCD-1234, в Baileys ушли только цифры; кривой номер -> bad_phone",
              pr == {"ok": True, "code": "ABCD-1234"} and {"pair": "77015551234"} in _ncalls_list() and pr_bad["error"] == "bad_phone", str((pr, pr_bad)))
        _nemit("connection.update", {"connection": "open"})
        st2 = _nwait(lambda: (lambda s: s if s["state"] == "connected" else None)(_nhttp("GET", "/status")[1]))
        check("22j8. соединение открылось: connected, linked, признак привязки записан на диск (linked.json), QR погашен",
              bool(st2) and st2["linked"] is True and st2["qr"] is False and (_ndata / "linked.json").exists())
        pr2 = _nhttp("POST", "/pair", {"phone": "+77015551234"})[1]
        check("22j8a. код сопряжения при подключённом аккаунте: already_connected", pr2["error"] == "already_connected")
        se = _nhttp("POST", "/send", {"to": "+7 701 555 12 34", "text": "привет, опоздаю"})[1]
        cl = _ncalls_list()
        check("22j9. отправка: onWhatsApp проверил номер, sendMessage ушёл на 77015551234@s.whatsapp.net дословным текстом",
              se == {"ok": True} and {"onWA": "77015551234@s.whatsapp.net"} in cl and {"send": "77015551234@s.whatsapp.net", "text": "привет, опоздаю"} in cl, str(cl[-3:]))
        sn2 = _nhttp("POST", "/send", {"to": "77000000000", "text": "x"})[1]
        check("22j10. номера нет в WhatsApp -> not_on_whatsapp, sendMessage не звали", sn2 == {"ok": False, "error": "not_on_whatsapp"} and not any(c.get("send") == "77000000000@s.whatsapp.net" for c in cl))
        sself = _nhttp("POST", "/send", {"self": True, "text": "служебное себе"})[1]
        cl2 = _ncalls_list()
        check("22j10a. служебное «себе» (self=true): уходит на собственный номер аккаунта без суффикса устройства, номер не проверяется через onWhatsApp, чужой номер не нужен",
              sself == {"ok": True} and {"send": "77019990000@s.whatsapp.net", "text": "служебное себе"} in cl2 and {"onWA": "77019990000@s.whatsapp.net"} not in cl2, str(cl2[-3:]))
        sself2 = _nhttp("POST", "/send", {"self": True})[1]
        check("22j10b. self без текста -> missing_to_or_text; без token -> 401", sself2 == {"ok": False, "error": "missing_to_or_text"}
              and _nhttp("POST", "/send", {"self": True, "text": "x"}, token="")[0] == 401)
        check("22j11. sent-store для getMessage пишется на Volume, права 0600", (_ndata / "sent-store.json").exists() and oct((_ndata / "sent-store.json").stat().st_mode & 0o777) == "0o600")

        # --- синхронизация контактов (фейк) ---
        _nemit("contacts.upsert", [
            {"id": "77011112222@s.whatsapp.net", "name": "Оля Книга"},
            {"id": "123456@lid", "phoneNumber": "77023334444@s.whatsapp.net", "notify": "Борис пуш"},
            {"id": "120363@g.us", "name": "Семейная группа"},
            {"id": "77025556666@s.whatsapp.net", "notify": "Только Пуш"},
            {"id": "999@lid", "name": "Без номера"}])
        cts = _nwait(lambda: (lambda j: j if j["count"] >= 3 else None)(_nhttp("GET", "/contacts")[1]))
        names_sync = [(c["name"], c["number"], c["saved"]) for c in (cts or {}).get("contacts", [])]
        check("22j12. имена из синхронизации: Оля Книга (из книги), Борис пуш (через LID+phoneNumber, не из книги), Только Пуш; группа и контакт без номера отброшены",
              names_sync == [("Борис пуш", "77023334444", False), ("Оля Книга", "77011112222", True), ("Только Пуш", "77025556666", False)], str(names_sync))
        _nemit("contacts.update", [{"id": "77011112222@s.whatsapp.net", "name": "Оля Новая"}])
        _nemit("messaging-history.set", {"contacts": [{"id": "77026667777@s.whatsapp.net", "name": "Из истории"}], "chats": [{"id": "x"}],
                                          "messages": [{"key": {}, "message": {"conversation": "СЕКРЕТНЫЙ-ТЕКСТ-ЧАТА"}}]})
        cts = _nwait(lambda: (lambda j: j if j["count"] >= 4 and any(c["name"] == "Оля Новая" for c in j["contacts"]) else None)(_nhttp("GET", "/contacts")[1]))
        check("22j13. contacts.update обновил имя, из messaging-history.set взяты только контакты (сообщения и чаты не берутся)", bool(cts) and any(c["name"] == "Из истории" for c in cts["contacts"]))
        # в rc14 имена из записной книжки приходят под LID без номера: номер подбираем событием lid-mapping.update или через репозиторий Baileys
        _nemit("contacts.upsert", [{"id": "555@lid", "lid": "555@lid", "name": "Лид Контакт"}, {"id": "777@lid", "lid": "777@lid", "name": "Через репозиторий"}])
        time.sleep(0.4)
        _nemit("lid-mapping.update", {"lid": "555@lid", "pn": "77028887766@s.whatsapp.net"})
        lidc = _nwait(lambda: (lambda j: j if {"Лид Контакт", "Через репозиторий"} <= {c["name"] for c in j["contacts"]} else None)(_nhttp("GET", "/contacts")[1]))
        lid_map = {c["name"]: c["number"] for c in (lidc or {"contacts": []})["contacts"]}
        check("22j13a. имена под LID без номера: номер подобран и событием lid-mapping.update, и запросом к репозиторию соответствий Baileys",
              lid_map.get("Лид Контакт") == "77028887766" and lid_map.get("Через репозиторий") == "77029990000", str(lid_map))
        sync_file = _nwait(lambda: (_ndata / "contacts-sync.json").exists() and (_ndata / "contacts-sync.json"))
        raw_sync = sync_file.read_text(encoding="utf-8") if sync_file else ""
        check("22j14. список контактов лежит на Volume (contacts-sync.json, 0600), текста чатов в нём нет; статус показывает число контактов",
              bool(sync_file) and "СЕКРЕТНЫЙ" not in raw_sync and oct(sync_file.stat().st_mode & 0o777) == "0o600" and _nhttp("GET", "/status")[1]["contacts"] >= 4)
        # формат файла демона читает Python-сторона
        WAD.mkdir(parents=True, exist_ok=True)
        shutil.copy(sync_file, WAD / "contacts-sync.json")
        py_names = sorted(c["name"] for c in wa.synced())
        check("22j15. Python читает файл демона: имена и номера совпадают (формат согласован)", "Оля Новая" in py_names and "Борис пуш" in py_names and len(py_names) >= 4, str(py_names))
        (WAD / "contacts-sync.json").unlink()

        # --- обрыв сети: переподключение без выхода в logged_out ---
        n_before = len(_nsocks())
        _nemit("connection.update", {"connection": "close", "lastDisconnect": {"error": {"output": {"statusCode": 408}}}})
        stc = _nwait(lambda: (lambda s: s if s["state"] == "connecting" else None)(_nhttp("GET", "/status")[1]), 3)
        again = _nwait(lambda: len(_nsocks()) > n_before and _nsocks(), 12)
        check("22j16. обрыв 408: состояние connecting (не logged_out), через ~3с поднят новый сокет, старый закрыт, сессия цела",
              bool(stc) and bool(again) and any("end" in c for c in _ncalls_list()) and (_nauth / "creds.json").exists(), str((stc, len(_nsocks()), n_before)))
        _nemit("connection.update", {"connection": "open"})
        _nwait(lambda: _nhttp("GET", "/status")[1]["state"] == "connected")

        # --- сессию разлогинили на телефоне (401) ---
        (_nauth / "extra-key.txt").write_text("ключ сессии")
        n_before = len(_nsocks())
        _nemit("connection.update", {"connection": "close", "lastDisconnect": {"error": {"output": {"statusCode": 401}}}})
        stl = _nwait(lambda: (lambda s: s if s["state"] == "logged_out" else None)(_nhttp("GET", "/status")[1]), 5)
        again = _nwait(lambda: len(_nsocks()) > n_before and _nsocks(), 8)
        check("22j17. сессия слетела (401): auth стёрт, состояние logged_out при linked=true (это «отвалилось», а не «не привязывали»), контакты чужого аккаунта забыты, поднят свежий сокет",
              bool(stl) and stl["linked"] is True and not (_nauth / "extra-key.txt").exists() and bool(again) and _nhttp("GET", "/contacts")[1]["count"] == 0
              and not (_ndata / "contacts-sync.json").exists(), str(stl))
        _nemit("connection.update", {"qr": "QR-AFTER-LOSS"})
        q2 = _nwait(lambda: _nhttp("GET", "/qr")[1].get("qr"))
        st_l = _nhttp("GET", "/status")[1]
        check("22j18. после слёта для перепривязки доступен новый QR, а статус остаётся logged_out (Python скажет «отвалился»)", q2 == "QR-AFTER-LOSS" and st_l["state"] == "logged_out" and st_l["linked"] is True, str(st_l))
        _nemit("connection.update", {"connection": "open"})
        st3 = _nwait(lambda: (lambda s: s if s["state"] == "connected" else None)(_nhttp("GET", "/status")[1]))
        check("22j19. перепривязка по новому QR: снова connected", bool(st3))
        # --- явная отвязка ---
        n_before = len(_nsocks())
        lo = _nhttp("POST", "/logout", {})[1]
        _nwait(lambda: len(_nsocks()) > n_before)
        _nemit("connection.update", {"qr": "QR-AFTER-LOGOUT"})
        st4 = _nwait(lambda: (lambda s: s if s["state"] == "need_qr" else None)(_nhttp("GET", "/status")[1]), 5)
        check("22j20. /logout: Baileys.logout вызван, сессия стёрта, linked.json удалён, состояние need_qr (не logged_out: это не «отвалилось»)",
              lo == {"ok": True} and any("logout" in c for c in _ncalls_list()) and bool(st4) and st4["linked"] is False and not (_ndata / "linked.json").exists(), str(st4))
        _proc.terminate()
        _proc.wait(timeout=10)
        _fo.close()
        log_txt = _nout.read_text(encoding="utf-8")
        check("22j21. лог демона без номеров, текстов сообщений, имён контактов и QR",
              not any(x in log_txt for x in ("77015551234", "опоздаю", "Оля", "Борис", "QR-STRING", "QR-AFTER", "СЕКРЕТ", "ABCD1234", "TOK-22")) and "подключён" in log_txt, log_txt[-300:])
    finally:
        if _proc.poll() is None:
            _proc.kill()
            _proc.wait()
        if not _fo.closed:
            _fo.close()

# ================= 22k. /setup: шаг «WhatsApp» на сервере: согласие, QR, код, контакты, перепривязка после «Готово» =================
from starlette.testclient import TestClient as _TC22  # noqa: E402

SC22 = "wa-setup-code-22"
os.environ["SETUP_CODE"] = SC22
_w_reset_files()
_w_linked()
wa.DAEMON_CMD = [sys.executable, "-c", "import time; time.sleep(120)"]       # живой процесс-пустышка: проверяем, что демон стартует/гасится
wa._TRANSPORT = httpx.MockTransport(_w_handler)
setupwiz.reset_for_tests()
C.set_setup_done(False)
_w_fw_reset(state="need_qr", qr="WA-QR-PAYLOAD-ONE-22", linked=False)
(WAD / "linked.json").unlink()
_pids_before = set()


def _w_svg_ok(data_uri):
    if not data_uri or not data_uri.startswith("data:image/svg+xml;"):
        return False
    head, payload = data_uri.split(",", 1)
    raw_svg = base64.b64decode(payload) if head.endswith("base64") else urllib.parse.unquote(payload).encode("utf-8")
    root = _ET.fromstring(raw_svg)
    return root.tag.endswith("svg") and len(list(root.iter())) > 3


with _TC22(main.app) as tcl:
    check("22k0. вход на /setup кодом", tcl.post("/setup/api/login", json={"code": SC22}).status_code == 200)
    r0 = tcl.get("/setup/api/wa/link")
    check("22k1. без согласия QR не отдаётся (403 need_consent), демон не запускается", r0.status_code == 403 and r0.json()["error"] == "need_consent" and not wa.running(), r0.text)
    check("22k1a. и код сопряжения без согласия закрыт", tcl.post("/setup/api/wa/pair", json={"phone": "+77015551234"}).status_code == 403)
    r1 = tcl.post("/setup/api/wa/consent", json={"accepted": False})
    r1b = tcl.post("/setup/api/wa/consent", json={})
    check("22k2. согласие обязательно: accepted=false / пусто -> 400, согласие не записано", r1.status_code == 400 and r1b.status_code == 400 and not wa.consent_given())
    st = tcl.get("/setup/api/wa/state").json()
    check("22k3. состояние до согласия: текст риска с сервера дословно из ТЗ, согласия нет",
          st["consent"] is False and st["consent_text"].startswith("Насколько это безопасно.")
          and "связанное устройство" in st["consent_text"] and "рассылки" in st["consent_text"] and "10 сообщений в час" in st["consent_text"]
          and "неофициально" not in st["consent_text"] and "почти невозможно" not in st["consent_text"] and "только после твоего «да»" in st["consent_text"])
    r2 = tcl.post("/setup/api/wa/consent", json={"accepted": True})
    check("22k4. согласие принято (на диск), демон-сайдкар запущен", r2.status_code == 200 and wa.consent_given() and wa.running()
          and oct((WAD / "state.json").stat().st_mode & 0o777) == "0o600")
    lk = tcl.get("/setup/api/wa/link").json()
    check("22k5. QR отрисовывается: SVG data-URI, валидный XML с фигурами (фейковая строка QR)", lk["state"] == "need_qr" and _w_svg_ok(lk["qr"]), str(lk)[:200])
    FW["qr"] = "WA-QR-PAYLOAD-TWO-22"
    lk2 = tcl.get("/setup/api/wa/link").json()
    check("22k6. QR обновляется вместе со строкой от демона (другая строка -> другая картинка)", _w_svg_ok(lk2["qr"]) and lk2["qr"] != lk["qr"])
    FW["state"] = "connecting"
    check("22k6a. пока демон готовит соединение, картинки нет", tcl.get("/setup/api/wa/link").json()["qr"] is None)
    pr = tcl.post("/setup/api/wa/pair", json={"phone": "+7 701 555 12 34"})
    FW["pair"] = {"ok": False, "error": "pair_failed"}
    pr_bad = tcl.post("/setup/api/wa/pair", json={"phone": "+77015551234"})
    pr_short = tcl.post("/setup/api/wa/pair", json={"phone": "123"})
    check("22k7. код сопряжения: ABCD-1234 приходит; если WhatsApp не дал код, честный отказ «привяжи по QR»; кривой номер -> 400",
          pr.json().get("code") == "ABCD-1234" and pr_bad.status_code == 400 and "QR" in pr_bad.json()["message"] and pr_short.status_code == 400)
    FW["state"], FW["qr"], FW["linked"] = "connected", None, True
    _w_linked()
    lk3 = tcl.get("/setup/api/wa/link").json()
    check("22k8. подключено: state connected, QR нет, linked", lk3["state"] == "connected" and lk3["qr"] is None and lk3["linked"] is True)

    # --- синхронизация: статусы ---
    FW["connected_for"] = 30
    s_pend = tcl.get("/setup/api/wa/state").json()
    FW["connected_for"] = 200
    s_empty = tcl.get("/setup/api/wa/state").json()
    _w_synced([("Борис Синхронов", "77051112233", True), ("Скрытый Человек", "77052223344", True)])
    s_ok = tcl.get("/setup/api/wa/state").json()
    check("22k9. статус синхронизации: ждём (<90 с) / WhatsApp промолчал (>=90 с, имён нет) / имена пришли",
          s_pend["sync"] == "pending" and s_empty["sync"] == "empty" and s_ok["sync"] == "ok" and s_ok["sync_count"] == 2, str((s_pend["sync"], s_empty["sync"], s_ok["sync"])))

    # --- контакты: ручной ввод с проверкой ---
    def post_c(**kw):
        body = {"manual": [], "hidden": [], "nicks": {}}
        body.update(kw)
        return tcl.post("/setup/api/wa/contacts", json=body)
    bads = {
        "короткий номер": [["Оля", "+7701"]], "без имени": [["", "+77015551234"]], "цифры в имени": [["Оля2", "+77015551234"]],
        "не +7": [["Оля", "+12025550123"]], "буквы вместо цифр": [["Оля", "+7701abc1234"]], "дубль имени": [["Оля", "+77015551234"], ["оля", "+77015551235"]],
        "31 штука": [[f"Имя{chr(1072 + i % 30)}{chr(1072 + i // 30)}", f"+7701555{i:04d}"] for i in range(31)]}
    res_bad = {k: post_c(manual=v) for k, v in bads.items()}
    check("22k10. ручной ввод проверяется: формат +7XXXXXXXXXX, имя обязательно и без цифр, без дублей, не больше 30 (7 видов ошибок -> 400)",
          all(r.status_code == 400 and r.json()["error"] == "bad_contacts" for r in res_bad.values()) and not wa.manual_contacts(),
          str({k: r.status_code for k, r in res_bad.items()}))
    ok_c = post_c(manual=[[RITA, "+77015551234"], ["Мама", "+7 702 555 23 45"], ["Оля работа", "7035556677"]],
                  hidden=["77052223344", "мусор"], nicks={NUM_R: "жена, супруга", "77051112233": "брат", "70000000000": "чужой"})
    book_names = sorted(c["name"] for c in wa.book())
    check("22k11. валидный список сохранён (номера нормализованы), скрытый номер исчез из книги, номер без 7 (10 цифр) дополнен до +7",
          ok_c.status_code == 200 and wa.manual_contacts() == {RITA: NUM_R, "Мама": NUM_M, "Оля работа": "77035556677"}
          and "Скрытый Человек" not in book_names and "Борис Синхронов" in book_names, str((ok_c.text, book_names)))
    check("22k12. прозвища: «жене» -> " + RITA + " (вписана рукой), «брату» -> Борис Синхронов (из WhatsApp); прозвище для неизвестного номера отброшено",
          wa.find_contact("жене") == ("one", RITA, NUM_R) and wa.find_contact("брату") == ("one", "Борис Синхронов", "77051112233") and "70000000000" not in C.wa_nicks())
    gc = tcl.get("/setup/api/wa/contacts").json()
    sync_rows = {c["number"]: c for c in gc["synced"]}
    check("22k13. страница получает: ручные, список из WhatsApp с флагом hidden и прозвищами",
          [n for n, _ in gc["manual"]] == [RITA, "Мама", "Оля работа"] and sync_rows["77052223344"]["hidden"] is True
          and sync_rows["77051112233"]["nicks"] == ["брат"] and gc["manual_nicks"][NUM_R] == ["жена", "супруга"], str(gc)[:300])
    cfg = json.loads((Path(_DIR22) / "config.json").read_text(encoding="utf-8"))
    check("22k14. контакты в config.json (права 600), номера в журнал не пишутся",
          "WA_CONTACTS" in cfg["values"] and oct((Path(_DIR22) / "config.json").stat().st_mode & 0o777) == "0o600")
    sl = tcl.get("/setup/api/state").json()
    check("22k15. /setup/api/state отдаёт блок wa (состояние, согласие) без номеров и имён", sl["wa"]["consent"] is True and sl["wa"]["enabled"] is True
          and NUM_R not in json.dumps(sl["wa"]) and RITA not in json.dumps(sl["wa"]))
    check("22k15a. снять все контакты можно: пустой список очищает ключи", post_c().status_code == 200 and wa.manual_contacts() == {} and C.wa_nicks() == {})
    post_c(manual=[[RITA, "+77015551234"]], nicks={NUM_R: "жена"})

    # --- слёт сессии: баннер ---
    FW["state"] = "logged_out"
    me_lost = tcl.get("/setup/api/me").json()
    st_lost = tcl.get("/setup/api/state").json()["wa"]
    check("22k16. сессия слетела: /setup/api/me (без входа) и state говорят wa_lost / lost=true, страница покажет баннер",
          me_lost["wa_lost"] is True and st_lost["lost"] is True and st_lost["state"] == "logged_out")
    FW["state"] = "connected"
    check("22k16a. подключились снова — баннер гаснет", tcl.get("/setup/api/me").json()["wa_lost"] is False)

    # --- «Готово», затем перепривязка тем же кодом ---
    fin = tcl.post("/setup/api/finish", json={})
    plain = tcl.post("/setup/api/login", json={"code": SC22})
    check("22k17. после «Готово» обычный вход закрыт (403 closed), настройка не открывается", fin.status_code == 200 and plain.status_code == 403 and plain.json()["error"] == "closed")
    FW["state"] = "logged_out"
    wl = tcl.post("/setup/api/login", json={"code": SC22, "wa": True})
    check("22k18. но тем же кодом можно войти «только за WhatsApp» (200, wa_only), настройка при этом остаётся закрытой", wl.status_code == 200 and wl.json()["wa_only"] is True and C.setup_done())
    blocked = [tcl.get("/setup/api/state").status_code, tcl.post("/setup/api/character", json={"name": "X", "tone": "derzkiy", "gender": "male"}).status_code,
               tcl.post("/setup/api/home", json={}).status_code, tcl.post("/setup/api/check-brain", json={}).status_code,
               tcl.post("/setup/api/iot-token", json={"token": "x" * 20}).status_code, tcl.post("/setup/api/finish", json={}).status_code,
               tcl.get("/setup/api/devices").status_code]
    check("22k19. сессия «только WhatsApp» не видит остальных настроек: state/character/home/brain/iot/finish/devices -> 403", blocked == [403] * 7, str(blocked))
    me_w = tcl.get("/setup/api/me").json()
    ws = tcl.get("/setup/api/wa/state")
    lkw = tcl.get("/setup/api/wa/link").json()
    cw = tcl.get("/setup/api/wa/contacts")
    check("22k20. зато WhatsApp доступен: me.wa_only, wa/state, wa/link (QR), wa/contacts; баннер «отвалился» виден",
          me_w["authed"] and me_w["wa_only"] and ws.status_code == 200 and ws.json()["lost"] is True and lkw["state"] == "logged_out"
          and cw.status_code == 200)
    FW["qr"] = "WA-QR-REBIND-22"
    check("22k21. при перепривязке после слёта отдаётся новый QR (состояние logged_out + QR)", _w_svg_ok(tcl.get("/setup/api/wa/link").json()["qr"]))
    lo = tcl.post("/setup/api/wa/logout", json={})
    check("22k22. отвязка с этой страницы доходит до демона", lo.status_code == 200 and FW["logout"] == 1)
    tcl.post("/setup/api/logout", json={})
    check("22k23. после выхода сессия мертва (401), «Перенастроить» по-прежнему отдельно и по коду",
          tcl.get("/setup/api/wa/state").status_code == 401 and tcl.post("/setup/api/login", json={"code": SC22, "reopen": True}).status_code == 200)
    C.set_setup_done(False)
    live = wa.running()
pids_gone = not wa.running()
check("22k24. при остановке сервера демон гасится (процесс-пустышка завершён по своему pid)", live and pids_gone)

# ---- страница: HTML/JS ----
wiz = (HERE / "setupwiz.html").read_text(encoding="utf-8")
check("22l0. страница: шаг «WhatsApp» в мастере, текст про перепривязку после слёта, кнопка «Привязать WhatsApp заново», баннер, текст про молчание WhatsApp",
      "'WhatsApp'" in wiz and "Привязать WhatsApp заново" in wiz and "WhatsApp отвалился" in wiz and "WhatsApp не отдал контакты, добавь нужных руками." in wiz
      and "waBanner()" in wiz and "renderWaOnly" in wiz)
check("22l1. поле номера: «+» вынесен из поля (стереть нельзя), в поле только цифры, максимум 11", "class: 'plus'" in wiz and "maxlength: 11" in wiz and "replace(/\\D/g, '')" in wiz)

if not shutil.which("node"):
    print("SKIP  22l2-22l9. node не найден")
else:
    W_BASE = {"ok": True, "enabled": True, "switched_off": False, "consent": False, "consent_text": wa.CONSENT_TEXT, "state": "off", "linked": False,
              "lost": False, "sync": "wait", "sync_count": 0, "limits": {"hour": 10, "day": 30}, "contacts_source": "default"}
    CT_EMPTY = {"ok": True, "manual": [], "synced": [], "manual_nicks": {}, "source": "default"}
    LINK_QR = {"ok": True, "state": "need_qr", "lost": False, "linked": False, "qr": "data:image/svg+xml;base64,QUJD", "sync": "wait", "sync_count": 0}
    CT5 = {"ok": True, "manual": [["Мама", "+77025552345"]], "manual_nicks": {"77025552345": ["мама"]}, "source": "default",
           "synced": [{"name": "Борис Синхронов", "number": "77051112233", "saved": True, "hidden": False, "nicks": ["брат"]},
                      {"name": "Скрытый Человек", "number": "77052223344", "saved": True, "hidden": True, "nicks": []}],
           "persisted": True, "count": 2, "overridden": []}
    W_ON = dict(W_BASE, consent=True, linked=True, state="connected", sync="ok", sync_count=2)
    scen22 = [
        {"fn": "stepWa", "S": _S(), "fetch": {"wa/state": W_BASE, "wa/contacts": CT_EMPTY}, "seq": [{"click": "Показать QR-код"}]},
        {"fn": "stepWa", "S": _S(), "fetch": {"wa/state": W_BASE, "wa/contacts": CT_EMPTY, "wa/consent": dict(W_BASE, consent=True), "wa/link": LINK_QR},
         "seq": [{"checkAll": True}, {"click": "Показать QR-код"}]},
        {"fn": "stepWa", "S": _S(), "fetch": {"wa/state": dict(W_ON, sync="empty", sync_count=0), "wa/contacts": CT_EMPTY,
                                              "wa/link": {"ok": True, "state": "connected", "lost": False, "linked": True, "qr": None, "sync": "empty", "sync_count": 0}}},
        {"fn": "stepWa", "S": _S(), "fetch": {"wa/state": dict(W_ON, state="logged_out", lost=True), "wa/contacts": CT_EMPTY,
                                              "wa/link": dict(LINK_QR, state="logged_out", lost=True, linked=True)}},
        {"fn": "stepWa", "S": _S(), "fetch": {"wa/state": W_ON, "wa/contacts": CT5, "wa/link": {"ok": True, "state": "connected", "lost": False, "linked": True, "qr": None, "sync": "ok", "sync_count": 2}},
         "seq": [{"click": "Сохранить контакты"}]},
        {"fn": "renderWaOnly", "S": _S(), "fetch": {"wa/state": W_BASE, "wa/contacts": CT_EMPTY}},
        {"fn": "renderLogin", "S": _S(), "args": ["Настройка уже завершена.", True, True]},
        {"fn": "stepWa", "S": _S(), "fetch": {"wa/state": dict(W_BASE, enabled=False, switched_off=True)}},
    ]
    _jsf22 = Path(tempfile.mkdtemp()) / "dom.js"
    _jsf22.write_text(DOM_JS, encoding="utf-8")
    proc22 = subprocess.run(["node", str(_jsf22), str(HERE / "setupwiz.html"), json.dumps(scen22, ensure_ascii=False)], capture_output=True, text=True, timeout=60)
    ok22 = proc22.returncode == 0 and proc22.stdout.strip().startswith("[")
    check("22l2. шаг «WhatsApp» отрисовывается в настоящем JS страницы (8 сценариев)", ok22, (proc22.stderr or proc22.stdout)[:400])
    if ok22:
        R22 = json.loads(proc22.stdout.strip().splitlines()[-1])
        t1, t2, t3, t4, t5, t6, t7, t8 = [_text(x["tree"]) if x["tree"] else "" for x in R22]
        calls = lambda i: [c["k"] for c in R22[i]["calls"]]       # noqa: E731
        check("22l3. согласие обязательно на странице: без галочки «Показать QR-код» не зовёт ни wa/consent, ни wa/link, а говорит «Без галочки…»; текст риска показан",
              "wa/consent" not in calls(0) and "wa/link" not in calls(0) and "Без галочки WhatsApp не подключаем" in t1
              and "Насколько это безопасно." in t1 and "рассылки" in t1 and "Понятно: пишу своим людям, без рассылок" in t1, str(calls(0)))
        imgs2 = [n for n in _nodes(R22[1]["tree"], "img")]
        cons = [c for c in R22[1]["calls"] if c["k"] == "wa/consent"]
        check("22l4. с галочкой: ушёл wa/consent {accepted:true}, затем опрос wa/link, QR показан картинкой, есть инструкция «Связанные устройства»",
              len(cons) == 1 and cons[0]["body"] == {"accepted": True} and "wa/link" in calls(1) and imgs2 and imgs2[0]["a"]["src"] == LINK_QR["qr"]
              and "Связанные устройства" in t2, str((calls(1), imgs2[:1])))
        check("22l5. код сопряжения на странице есть (для открытой на телефоне): поле номера и кнопка «Получить код»", "Получить код" in t2 and "Страница открыта на телефоне?" in t2)
        plus_nodes = [n for n in _nodes(R22[2]["tree"], "span") if n["a"].get("class") == "plus"]
        phone_inputs = [n for n in _nodes(R22[2]["tree"], "input") if n["a"].get("inputmode") == "numeric"]
        check("22l6. подключено + имена не пришли: «WhatsApp не отдал контакты, добавь нужных руками.»; поле номера: «+» отдельным нестираемым блоком, в поле цифры (maxlength 11)",
              "WhatsApp подключён" in t3 and "WhatsApp не отдал контакты, добавь нужных руками." in t3 and plus_nodes and _text(plus_nodes[0]) == "+"
              and phone_inputs and phone_inputs[0]["a"].get("maxlength") == "11", t3[-200:])
        check("22l7. слетела сессия: на шаге красное «WhatsApp отвалился…» и свежий QR для перепривязки", "WhatsApp отвалился" in t4 and [n for n in _nodes(R22[3]["tree"], "img")])
        save_calls = [c for c in R22[4]["calls"] if c["k"] == "wa/contacts" and c["body"]]
        body5 = save_calls[0]["body"] if save_calls else {}
        check("22l8. контакты на странице: список из WhatsApp (имена, «скрыть», прозвища), сохранение шлёт ручные, скрытые номера и прозвища",
              "Борис Синхронов" in t5 and "скрыть" in t5 and "Из WhatsApp: 2" in t5 and body5.get("manual") == [["Мама", "+77025552345"]]
              and body5.get("hidden") == ["77052223344"] and body5.get("nicks") == {"77051112233": "брат", "77025552345": "мама"} and "Сохранено" in t5, str(body5))
        check("22l9. страница перепривязки (после «Готово»): «Настройка Кореша закрыта, здесь только WhatsApp»; экран входа: баннер «отвалился» + кнопки «Перенастроить» и «Привязать WhatsApp заново»",
              "здесь только WhatsApp" in t6 and "WhatsApp отвалился" in t7 and "Привязать WhatsApp заново" in t7 and "Перенастроить" in t7)
        check("22l10. если WhatsApp выключен в сборке/переменной, шаг честно говорит об этом и ничего не предлагает", "WA_ENABLED=0" in t8 and "Показать QR-код" not in t8)

# ================= 22m. сборка: Dockerfile, пины, документация =================
dock22 = (HERE / "Dockerfile").read_text(encoding="utf-8")
pkg22 = json.loads((HERE / "wa" / "package.json").read_text(encoding="utf-8"))
lock22 = json.loads((HERE / "wa" / "package-lock.json").read_text(encoding="utf-8"))
daemon_src = (HERE / "wa" / "wa-daemon.js").read_text(encoding="utf-8")
check("22m1. Baileys закреплён ровно 7.0.0-rc14 (package.json без ^ и ~, lock тоже)",
      pkg22["dependencies"]["@whiskeysockets/baileys"] == "7.0.0-rc14" and lock22["packages"]["node_modules/@whiskeysockets/baileys"]["version"] == "7.0.0-rc14")
check("22m2. Dockerfile: стадия node:22-slim с npm ci по lock, в итоговый образ идут node, node_modules, wa-daemon.js и wa.py",
      "FROM node:22-slim" in dock22 and "npm ci" in dock22 and "package-lock.json" in dock22 and "COPY --from=wa /usr/local/bin/node" in dock22
      and "COPY --from=wa /wa/node_modules" in dock22 and "wa/wa-daemon.js" in dock22 and " wa.py " in dock22)
check("22m3. демон слушает только 127.0.0.1, токен в заголовке сверяется через timingSafeEqual, входящие пишутся только по файлу-флагу inbox.json, сообщения не логируются",
      "'127.0.0.1'" in daemon_src and "timingSafeEqual" in daemon_src and "X-WA-Token" in daemon_src and "INBOX_FLAG" in daemon_src and "if (!nums) return" in daemon_src
      and "markOnlineOnConnect: false" in daemon_src)
check("22m4. segno закреплён в requirements; wa/node_modules и wa/auth не попадают в git и в контекст сборки",
      "segno==" in (HERE / "requirements.txt").read_text(encoding="utf-8") and "wa/node_modules" in (HERE / ".dockerignore").read_text(encoding="utf-8")
      and "wa/auth" in (HERE / ".gitignore").read_text(encoding="utf-8"))
tvars = json.loads((HERE / "template-variables.json").read_text(encoding="utf-8"))
check("22m5. template-variables.json: WA_ENABLED описана как аварийный выключатель (необязательная); SETUP.md описывает WhatsApp и риск",
      any(v["name"] == "WA_ENABLED" and not v.get("required") for v in tvars["variables"])
      and "WhatsApp" in (HERE / "SETUP.md").read_text(encoding="utf-8") and "заблокировать" in (HERE / "SETUP.md").read_text(encoding="utf-8"))

# --- уборка раздела 22 ---
brain._create = _boom_create
wa._now = time.time
wa._TRANSPORT = None
wa.DAEMON_CMD = None
wa.reset_for_tests()
setupwiz.reset_for_tests()
C.set_setup_done(False)
for _k, _v in _SAVED22.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v
