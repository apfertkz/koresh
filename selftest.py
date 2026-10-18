"""Ворота. Запуск: python selftest.py. Ключи и сеть не нужны: Anthropic и Яндекс замоканы,
любое настоящее обращение наружу роняет ворота."""
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

for k in ("ANTHROPIC_API_KEY", "YANDEX_IOT_TOKEN", "WEBHOOK_SECRET", "TONE", "HOUSEHOLD", "ALLOW_LOCKS",
          "LOCK_PHRASE", "DAILY_TOKEN_CAP", "MODEL", "ASSISTANT_NAME", "HOLD_TIMEOUT", "BLOCKED_NAMES", "GENDER"):
    os.environ.pop(k, None)
MEMDIR = tempfile.mkdtemp()
os.environ["MEMORY_DIR"] = MEMDIR            # свой каталог памяти: ворота не трогают /data
os.environ["USAGE_FILE"] = os.path.join(tempfile.mkdtemp(), "usage.json")

import httpx  # noqa: E402

import brain  # noqa: E402
import config as C  # noqa: E402
import iot  # noqa: E402
import main  # noqa: E402
import memory  # noqa: E402
import prompts  # noqa: E402
import usage  # noqa: E402

FAILED = []
N = 0


def check(name, cond, detail=""):
    global N
    N += 1
    print(f"{'OK  ' if cond else 'FAIL'}  {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        main.cancel_background()                  # недоделанное фоновое закрытие эпизода не переезжает в следующую проверку
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()


# ---------- запрет настоящей сети ----------
CALLS = []          # (метод, путь, тело) запросов к «Яндексу»


def _no_net(*a, **k):
    raise AssertionError("ворота: попытка настоящего сетевого вызова")


async def _boom_create(**kw):
    raise AssertionError("ворота: настоящий вызов Anthropic")

brain._create = _boom_create

# ---------- замоканный умный дом ----------
USER_INFO = {
    "rooms": [{"id": "r1", "name": "Спальня"}, {"id": "r2", "name": "Гостиная"}],
    "devices": [
        {"id": "lamp-bed", "name": "Лампа", "type": "devices.types.light", "room": "r1",
         "capabilities": [{"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": False}}]},
        {"id": "tv", "name": "Телевизор", "type": "devices.types.media_device.tv", "room": "r2",
         "capabilities": [{"type": "devices.capabilities.video_stream", "state": None},
                          {"type": "devices.capabilities.on_off", "state": None}]},
        {"id": "lock1", "name": "Входной замок", "type": "devices.types.openable", "room": "r2",
         "capabilities": [{"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": False}}]},
        {"id": "gate1", "name": "Ворота во дворе", "type": "devices.types.other", "room": "r2",
         "capabilities": [{"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": False}}]},
    ],
    "groups": [{"id": "g-all", "name": "Весь свет", "devices": ["lamp-bed"]},
               {"id": "g-sec", "name": "Прочее", "devices": ["lock1", "lamp-bed"]}],
    "scenarios": [{"id": "sc1", "name": "Кино"}, {"id": "sc2", "name": "Открыть ворота"}],
}


def yandex_handler(request: httpx.Request) -> httpx.Response:
    if request.headers.get("authorization") != "Bearer test-iot-token":
        return httpx.Response(401, json={"status": "error"})
    path = request.url.path
    if request.method == "GET" and path.endswith("/user/info"):
        return httpx.Response(200, json={"status": "ok", **USER_INFO})
    body = json.loads(request.content) if request.content else None
    CALLS.append((request.method, path, body))
    return httpx.Response(200, json={"status": "ok"})


iot._TRANSPORT = httpx.MockTransport(yandex_handler)
os.environ["YANDEX_IOT_TOKEN"] = "test-iot-token"


def post_calls():
    return [c for c in CALLS if c[0] == "POST"]


# ---------- заглушка Claude ----------
class Blk:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self):
        return dict(self.__dict__)


class Usage:
    def __init__(self, i, o):
        self.input_tokens, self.output_tokens = i, o


class Resp:
    def __init__(self, blocks, stop, u=(10, 10)):
        self.content, self.stop_reason, self.usage = blocks, stop, Usage(*u)


def text_resp(t, u=(10, 10)):
    return Resp([Blk(type="text", text=t)], "end_turn", u)


def tool_resp(tid, name, inp, u=(10, 10)):
    return Resp([Blk(type="tool_use", id=tid, name=name, input=inp)], "tool_use", u)


MAT_RE = re.compile(r"хуй|хуя|хуе|хуё|пизд|бля|ебан|ебат|ебал|ёб|сука|охуе|нихуя|мудак|залуп", re.I)


def wipe_memory_dir():
    memory.reset_cache()
    for f in Path(os.environ["MEMORY_DIR"]).glob("*"):
        if f.is_file():
            f.unlink()


def fresh(uid):
    main.STATE.pop(uid, None)
    memory.STORES.pop(uid, None)
    p = Path(os.environ["MEMORY_DIR"]) / ("mem-" + __import__("hashlib").sha256(uid.encode()).hexdigest()[:24] + ".json")
    if p.exists():
        p.unlink()


def reset_all():
    for k in ("TONE", "GENDER", "ALLOW_LOCKS", "LOCK_PHRASE", "DAILY_TOKEN_CAP", "HOLD_TIMEOUT", "HOUSEHOLD", "BLOCKED_NAMES"):
        os.environ.pop(k, None)
    usage.reset_for_tests()
    wipe_memory_dir()
    iot.reset_cache()
    CALLS.clear()
    brain._create = _boom_create


# ================= 1. три тона =================
reset_all()
prm = {}
for t in C.TONES:
    os.environ["TONE"] = t
    prm[t] = prompts.system_prompt()
check("1a. три TONE -> три разных системных промпта", len(set(prm.values())) == 3)
check("1b. vezhlivyy: в промпте нет мата", not MAT_RE.search(prm["vezhlivyy"]))
check("1c. derzkiy: в промпте нет мата", not MAT_RE.search(prm["derzkiy"]))
check("1d. derzkiy_mat: мат есть", bool(MAT_RE.search(prm["derzkiy_mat"])))
check("1e. vezhlivyy: во фразах-заглушках нет мата",
      not any(MAT_RE.search(x if isinstance(x, str) else " ".join(x)) for x in prompts.PHRASES["vezhlivyy"].values()))
check("1f. derzkiy: во фразах-заглушках нет мата",
      not any(MAT_RE.search(x if isinstance(x, str) else " ".join(x)) for x in prompts.PHRASES["derzkiy"].values()))
os.environ["TONE"] = "garbage"
check("1g. кривой TONE -> безопасный derzkiy, а не мат", C.tone() == "derzkiy")
os.environ["TONE"] = "derzkiy"
os.environ["ASSISTANT_NAME"] = "Маша"
os.environ["HOUSEHOLD"] = json.dumps({"owner": "Ержан", "people": {"Айгуль": "жена"}, "rooms": {"детская": "Комната Али"}}, ensure_ascii=False)
p = prompts.system_prompt()
check("1h. ASSISTANT_NAME и HOUSEHOLD попадают в промпт", "Маша" in p and "Ержан" in p and "Айгуль" in p and "Комната Али" in p)
os.environ["HOUSEHOLD"] = "{битый json"
check("1i. битый HOUSEHOLD не роняет промпт", "Маша" in prompts.system_prompt())
os.environ.pop("ASSISTANT_NAME")
os.environ["LOCK_PHRASE"] = "секретная ромашка"
check("1k. LOCK_PHRASE не попадает в промпт", "ромашка" not in prompts.system_prompt())

# ================= 2. «включи свет в спальне» =================
reset_all()
os.environ["TONE"] = "derzkiy"
seen_tool_results = []


async def fake_light(**kw):
    msgs = kw["messages"]
    last = msgs[-1]
    if isinstance(last["content"], str):
        return tool_resp("t1", "list_devices", {})
    res = last["content"][0]
    seen_tool_results.append(res["content"])
    if res["tool_use_id"] == "t1":
        devs = json.loads(res["content"])["devices"]
        lamp = next(d for d in devs if d["room"] == "Спальня" and d["type"] == "devices.types.light")
        return tool_resp("t2", "control_device", {
            "target": "device", "target_id": lamp["id"],
            "capability": "devices.capabilities.on_off", "instance": "on", "value": True})
    return text_resp("Включил свет в спальне.")

brain._create = fake_light
fresh("a")
r = run(main.handle("a", "включи свет в спальне", False))
pc = post_calls()
check("2a. ответ Алисе валиден", r["version"] == "1.0" and r["response"]["text"] == "Включил свет в спальне.", repr(r))
check("2b. ровно один POST к IoT API на нужное устройство",
      len(pc) == 1 and pc[0][1].endswith("/devices/actions")
      and pc[0][2] == {"devices": [{"id": "lamp-bed", "actions": [
          {"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": True}}]}]}, repr(pc))
check("2c. телевизор со state:null не уронил список устройств",
      '"tv"' in seen_tool_results[0] if seen_tool_results else False)

# ================= 3. замки =================
reset_all()
os.environ["YANDEX_IOT_TOKEN"] = "test-iot-token"
OPEN = ("device", "lock1", "devices.capabilities.on_off", "on", True)
r1 = run(iot.control(*OPEN, user_text="открой замок"))
check("3a. замок без ALLOW_LOCKS -> отказ, POST не было", r1.startswith("locks_disabled") and not post_calls(), r1)
r1b = run(iot.control("device", "gate1", "devices.capabilities.on_off", "on", True, user_text="открой ворота"))
check("3b. ворота (по названию) без ALLOW_LOCKS -> отказ", r1b.startswith("locks_disabled") and not post_calls(), r1b)
r1c = run(iot.control("group", "g-sec", "devices.capabilities.on_off", "on", True, user_text="включи всё"))
check("3c. группа с замком внутри -> отказ", r1c.startswith("locks_disabled") and not post_calls(), r1c)
r1d = run(iot.control("scenario", "sc2", "", "", None, user_text="запусти сценарий"))
check("3d. сценарий «Открыть ворота» -> отказ", r1d.startswith("locks_disabled") and not post_calls(), r1d)
os.environ["ALLOW_LOCKS"] = "1"
r2 = run(iot.control(*OPEN, user_text="открой замок"))
check("3e. ALLOW_LOCKS без LOCK_PHRASE в настройках -> всё равно отказ", r2.startswith("locks_disabled") and not post_calls(), r2)
os.environ["LOCK_PHRASE"] = "Секретная ромашка"
r3 = run(iot.control(*OPEN, user_text="открой замок"))
check("3f. ALLOW_LOCKS без фразы от хозяина -> отказ", r3.startswith("need_phrase") and not post_calls(), r3)
r4 = run(iot.control(*OPEN, user_text="открой замок секретная РОМАШКА!"))
check("3g. ALLOW_LOCKS + верная фраза -> ок, POST ушёл", r4 == "ok" and len(post_calls()) == 1, r4)
CALLS.clear()
r5 = run(iot.control("device", "lamp-bed", "devices.capabilities.on_off", "on", True, user_text="включи лампу"))
check("3h. обычная лампа не требует фразы", r5 == "ok" and len(post_calls()) == 1, r5)
os.environ.pop("ALLOW_LOCKS"); CALLS.clear()
r6 = run(iot.control("device", "nope", "devices.capabilities.on_off", "on", True, user_text="x"))
check("3i. выдуманный id -> ошибка, POST не было", r6.startswith("error") and not post_calls(), r6)

# через мозг: модель «подсмотрела» фразу в ответе пользователя, которого не было
reset_all()
os.environ.update(ALLOW_LOCKS="1", LOCK_PHRASE="ромашка")
steps = {"n": 0}


async def fake_lock(**kw):
    steps["n"] += 1
    last = kw["messages"][-1]
    if steps["n"] == 1:
        return tool_resp("k1", "control_device", {"target": "device", "target_id": "lock1",
                                                  "capability": "devices.capabilities.on_off", "instance": "on", "value": True})
    return text_resp("Не открою: " + last["content"][0]["content"][:12])

brain._create = fake_lock
fresh("b")
r = run(main.handle("b", "открой замок, ромашка ромашка", False))   # фраза от хозяина есть
check("3j. через мозг: фраза хозяина в реплике -> команда ушла", len(post_calls()) == 1, repr(post_calls()))
reset_all(); os.environ.update(ALLOW_LOCKS="1", LOCK_PHRASE="ромашка", YANDEX_IOT_TOKEN="test-iot-token")
steps["n"] = 0
brain._create = fake_lock
fresh("b2")
run(main.handle("b2", "открой замок", False))
check("3k. через мозг: без фразы в реплике -> команды нет", not post_calls(), repr(post_calls()))

# ================= 4. секрет вебхука =================
from starlette.testclient import TestClient  # noqa: E402

reset_all()
os.environ["YANDEX_IOT_TOKEN"] = "test-iot-token"
tc = TestClient(main.app)
BODY = {"version": "1.0", "session": {"new": True, "session_id": "s", "user": {"user_id": "u9"}},
        "request": {"command": "", "original_utterance": ""}}
r_unset = tc.post("/alice/anything", json=BODY)
check("4a. WEBHOOK_SECRET не задан -> отказ любому", r_unset.status_code == 403, r_unset.status_code)
os.environ["WEBHOOK_SECRET"] = "правильный-секрет-123"
r_bad = tc.post("/alice/wrong", json=BODY)
check("4b. неверный секрет -> 403", r_bad.status_code == 403, r_bad.status_code)
r_ok = tc.post("/alice/%D0%BF%D1%80%D0%B0%D0%B2%D0%B8%D0%BB%D1%8C%D0%BD%D1%8B%D0%B9-%D1%81%D0%B5%D0%BA%D1%80%D0%B5%D1%82-123", json=BODY)
check("4c. верный секрет -> 200 и приветствие", r_ok.status_code == 200 and r_ok.json()["response"]["text"], r_ok.text[:100])
os.environ["WEBHOOK_SECRET"] = "abc123"
r_ping = tc.post("/alice/abc123", json={**BODY, "request": {"command": "ping", "original_utterance": "ping"}})
check("4d. ping от Яндекса отвечает без мозга", r_ping.status_code == 200 and r_ping.json()["response"]["text"] == "pong")
r_help = tc.post("/alice/abc123", json={**BODY, "request": {"command": "помощь", "original_utterance": "Помощь"}})
check("4e. «Помощь» отвечает без мозга (требование модерации)", r_help.status_code == 200 and "Кореш" in r_help.json()["response"]["text"], r_help.text[:100])
check("4f. /health не светит секреты",
      "abc123" not in tc.get("/health").text and "test-iot-token" not in tc.get("/health").text)

# ================= 5. суточный потолок =================
reset_all()
os.environ["DAILY_TOKEN_CAP"] = "100"
os.environ["WEBHOOK_SECRET"] = "abc123"
called = {"n": 0}


async def counting(**kw):
    called["n"] += 1
    return text_resp("Привет, это мозг.", u=(60, 50))

brain._create = counting
fresh("c")
r1 = run(main.handle("c", "расскажи что-нибудь", False))
check("5a. под потолком мозг отвечает", r1["response"]["text"] == "Привет, это мозг." and called["n"] == 1, repr(r1))
check("5b. токены посчитаны (110)", usage.used() == 110, usage.used())
r2 = run(main.handle("c", "ещё", False))
check("5c. потолок превышен -> вежливый ответ про лимит", "лимит" in r2["response"]["text"].lower(), repr(r2))
check("5d. потолок превышен -> мозг НЕ вызван", called["n"] == 1, called["n"])
os.environ["DAILY_TOKEN_CAP"] = "0"
r3 = run(main.handle("c", "и ещё", False))
check("5e. DAILY_TOKEN_CAP=0 снимает потолок", called["n"] == 2, called["n"])

# ================= 6. медленный мозг =================
reset_all()
os.environ["TONE"] = "derzkiy"
os.environ["HOLD_TIMEOUT"] = "0.4"


async def slow(**kw):
    await asyncio.sleep(0.9)
    return text_resp("В спальне тихо, жду команд.")

brain._create = slow
fresh("d")


async def scenario_slow():
    t0 = time.monotonic()
    first = await main.handle("d", "включи свет в спальне", False)
    t_first = time.monotonic() - t0
    t0 = time.monotonic()
    second = await main.handle("d", "ну что там", False)       # ждёт ещё 0.4: 0.9 не прошло
    t_second = time.monotonic() - t0
    third = await main.handle("d", "ну?", False)               # к этому моменту мозг доехал
    return first, t_first, second, t_second, third

first, t1, second, t2, third = run(scenario_slow())
check("6a. заглушка отдана в бюджет (HOLD_TIMEOUT + запас)", t1 < 0.4 + 0.35, f"{t1:.2f}с")
check("6b. заглушка из набора фраз", first["response"]["text"] in prompts.PHRASES["derzkiy"]["hold"], repr(first))
check("6c. первый переспрос тоже укладывается и не теряет мысль",
      t2 < 0.4 + 0.35 and second["response"]["text"] in prompts.PHRASES["derzkiy"]["still"] + ["В спальне тихо, жду команд."], f"{t2:.2f}с {second}")
check("6d. переспрос отдаёт готовый ответ", "В спальне тихо, жду команд." in (second["response"]["text"], third["response"]["text"]))
check("6e. дефолтный HOLD_TIMEOUT укладывается в 3 секунды",
      (os.environ.pop("HOLD_TIMEOUT"), C.hold_timeout())[1] <= 4.0)

# ================= 7. state: null =================
reset_all()
os.environ["YANDEX_IOT_TOKEN"] = "test-iot-token"
try:
    out = run(iot.devices())
    ids = {d["id"] for d in out["devices"]}
    tv = next(d for d in out["devices"] if d["id"] == "tv")
    ok = {"lamp-bed", "tv"} <= ids and all(c["value"] is None for c in tv["capabilities"])
except Exception as e:  # noqa: BLE001
    ok, out = False, repr(e)
check("7a. state:null в user/info не роняет список устройств", ok, str(out)[:200])
raw = {"devices": [{"id": "x", "name": "ТВ", "type": "t", "capabilities": None, "room": None}], "rooms": None,
       "groups": None, "scenarios": None}
try:
    ok = iot.compact_user_info(raw)["devices"][0]["capabilities"] == []
except Exception as e:  # noqa: BLE001
    ok = False
check("7b. null вместо списков (capabilities/rooms/groups) тоже переживаем", ok)
os.environ["YANDEX_IOT_TOKEN"] = "wrong"
iot.reset_cache()
out = run(iot.devices())
check("7c. отозванный токен -> понятная ошибка, не исключение", "error" in out and "токен" in out["error"], str(out))
os.environ["YANDEX_IOT_TOKEN"] = "test-iot-token"

# ================= доп.: озвучка и подготовка ответа =================
reset_all()
rep = main.alice_reply("Ну пиздец, блядь, хуясе.")
check("9a. мат в tts обходит словарь маркером «+», в text остаётся чистым",
      "+" in rep["response"]["tts"] and "+" not in rep["response"]["text"], repr(rep))
check("9b. ошибка мозга -> фраза, а не 500",
      "ключ" in main.brain_error_text(type("AuthenticationError", (Exception,), {})("sk-ant-SECRET")).lower()
      and "sk-ant" not in main.brain_error_text(type("AuthenticationError", (Exception,), {})("sk-ant-SECRET")))


async def boom(**kw):
    raise RuntimeError("401 invalid x-api-key sk-ant-SECRET")

brain._create = boom
fresh("e")
r = run(main.handle("e", "привет", False))
check("9c. исключение в мозге -> валидный ответ Алисе без утечки ключа",
      r["version"] == "1.0" and "sk-ant" not in json.dumps(r) and r["response"]["text"], repr(r))

# ================= 10. sonnet-5-5: запрос, история, страховка, тексты ошибок =================
import hashlib  # noqa: E402


def _canon(m):
    c = m["content"]
    if not isinstance(c, str):
        c = [b if isinstance(b, dict) else b.model_dump() for b in c]
    return json.dumps({"role": m["role"], "content": c}, ensure_ascii=False, sort_keys=True, default=str)


class BadRequestError(Exception):
    status_code = 400


class AuthenticationError(Exception):
    status_code = 401


class RateLimitError(Exception):
    status_code = 429


class APIStatusError(Exception):      # SDK так отдаёт нестандартные коды (402)
    status_code = 402


class OverloadedError(Exception):
    status_code = 529


FORBIDDEN_KW = ("temperature", "top_p", "top_k", "thinking", "tool_choice")


class FakeApi:
    """Эмулятор проверки привязки thinking-блоков (аккаунты с 31.08.2026): блок помнит всё, что было до него;
    если при повторе префикс другой -> 400 как у настоящего API. Каждый ответ содержит thinking-блок."""

    def __init__(self):
        self.n = 0
        self.prefix = {}          # signature -> хэш префикса (system + tools + все прошлые сообщения)
        self.log = []             # [{canon: [...], kw: {...}}]
        self.errors = 0
        self.script = None        # одноразовая подмена: функция(kw) -> Resp | исключение

    @staticmethod
    def _h(kw, canon):
        return hashlib.sha256(json.dumps([kw.get("system"), [t["name"] for t in kw.get("tools", [])], canon],
                                         ensure_ascii=False, default=str).encode()).hexdigest()

    async def create(self, **kw):
        msgs = kw["messages"]
        canon = [_canon(m) for m in msgs]
        self.log.append({"canon": canon, "kw": {k: v for k, v in kw.items() if k != "messages"}, "chars": sum(map(len, canon))})
        if self.script:
            f, self.script = self.script, None
            r = f(kw)
            if isinstance(r, Exception):
                raise r
            return r
        for i, m in enumerate(msgs):
            if m["role"] == "assistant" and not isinstance(m["content"], str):
                for b in m["content"]:
                    if (b.get("type") if isinstance(b, dict) else b.type) == "thinking":
                        sig = b["signature"] if isinstance(b, dict) else b.signature
                        if self.prefix.get(sig) != self._h(kw, canon[:i]):
                            self.errors += 1
                            raise BadRequestError(f"messages.{i}.content.0: Invalid `signature` in `thinking` block. "
                                                  "The block is bound to a different conversation.")
        self.n += 1
        sig = f"sig{self.n}"
        self.prefix[sig] = self._h(kw, canon)
        think_blk = Blk(type="thinking", thinking="", signature=sig)
        last = msgs[-1]
        if isinstance(last["content"], str) and "свет" in last["content"]:
            return Resp([think_blk, Blk(type="tool_use", id=f"tu{self.n}", name="list_devices", input={})], "tool_use")
        if isinstance(last["content"], str):
            return Resp([think_blk, Blk(type="text", text=f"ответ на: {last['content'][:40]}")], "end_turn")
        return Resp([think_blk, Blk(type="text", text="Свет проверил.")], "end_turn")


def new_api():
    reset_all()
    os.environ["YANDEX_IOT_TOKEN"] = "test-iot-token"
    api = FakeApi()
    brain._create = api.create
    return api


# ---- 10a. параметры запроса по умолчанию ----
api = new_api()
fresh("p1")
r = run(main.handle("p1", "привет", False))
kw = api.log[-1]["kw"]
check("10a. по умолчанию model=claude-sonnet-5-5", kw["model"] == "claude-sonnet-5-5", kw["model"])
check("10b. по умолчанию effort=low (output_config.effort)", kw.get("extra_body") == {"output_config": {"effort": "low"}}, repr(kw.get("extra_body")))
check("10c. в запросе нет temperature/top_p/top_k/thinking/tool_choice", not any(k in kw for k in FORBIDDEN_KW), repr(list(kw)))
check("10d. нет prefill: последнее сообщение запроса от user", api.log[-1]["canon"][-1].startswith('{"content": "привет"') and json.loads(api.log[-1]["canon"][-1])["role"] == "user")
check("10e. обычный ответ дошёл до Алисы", r["response"]["text"] == "ответ на: привет", repr(r))
check("10f. max_tokens на low = 2048 (запас под размышления; ответ Алисе всё равно <= 1000 симв.)", kw["max_tokens"] == 2048, kw["max_tokens"])
for raw, want in (("мусор", "low"), ("", "low"), ("HIGH", "high"), ("xhigh", "xhigh"), ("max", "max"), ("medium", "medium"), ("ultra", "low")):
    os.environ["EFFORT"] = raw
    ok = C.effort() == want and brain.request_params()["extra_body"]["output_config"]["effort"] == want
    check(f"10g. EFFORT={raw!r} -> {want}", ok, f"{C.effort()}")
os.environ["EFFORT"] = "high"
check("10h. max_tokens растёт с effort (high = 4096 > low)", brain.request_params()["max_tokens"] == 4096 > C.MAX_TOKENS["low"])
os.environ.pop("EFFORT")
os.environ["MODEL"] = "claude-haiku-4-5-20251001"
check("10i. haiku-4-5 не знает effort -> output_config не шлём", "extra_body" not in brain.request_params())
os.environ["MODEL"] = "claude-haiku-5-5"
check("10j. haiku-5-5 effort понимает", "extra_body" in brain.request_params())
os.environ.pop("MODEL")
check("10k. /health показывает model и effort",
      (lambda j: j["model"] == "claude-sonnet-5-5" and j["effort"] == "low")(TestClient(main.app).get("/health").json()))

# ---- 10l. реальный SDK: тело запроса и круг thinking-блока через сериализацию ----
from anthropic import AsyncAnthropic  # noqa: E402

BODIES = []


def _sdk_handler(request: httpx.Request) -> httpx.Response:
    BODIES.append(json.loads(request.content))
    return httpx.Response(200, json={
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
        "content": [{"type": "thinking", "thinking": "", "signature": "SIG-ABC"}, {"type": "text", "text": "ок"}],
        "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}})


async def sdk_roundtrip():
    client = AsyncAnthropic(api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(_sdk_handler)))
    hist = [{"role": "user", "content": "привет"}]
    r1 = await client.messages.create(system=[{"type": "text", "text": "s"}], tools=brain.TOOLS, messages=hist,
                                      **brain.request_params())
    hist.append({"role": "assistant", "content": r1.content})
    hist.append({"role": "user", "content": "ещё"})
    await client.messages.create(system=[{"type": "text", "text": "s"}], tools=brain.TOOLS, messages=hist,
                                 **brain.request_params())

run(sdk_roundtrip())
b1, b2 = BODIES
check("10l. реальный SDK: в теле model/max_tokens/output_config.effort, без temperature/thinking/tool_choice",
      b1["model"] == "claude-sonnet-5-5" and b1["max_tokens"] == 2048 and b1["output_config"] == {"effort": "low"}
      and not any(k in b1 for k in FORBIDDEN_KW), repr({k: v for k, v in b1.items() if k != "tools"}))
asst = b2["messages"][1]["content"]
check("10m. реальный SDK: thinking-блок вернулся в историю без изменений (signature цела)",
      asst[0] == {"type": "thinking", "thinking": "", "signature": "SIG-ABC"} and asst[1]["text"] == "ок", repr(asst))

# ---- 10n. контроль эмулятора: старая нарезка головы ловит 400 ----
api = new_api()


def legacy_strip(d):
    out = d[-20:]
    while out and (out[0]["role"] != "user" or not isinstance(out[0]["content"], str)):
        out.pop(0)
    return out


async def legacy_run():
    d = []
    for i in range(30):
        d = legacy_strip(d)
        d.append({"role": "user", "content": f"реплика {i}"})
        await brain.think(d)

legacy_dead = False
try:
    run(legacy_run())
except BadRequestError:
    legacy_dead = True
check("10n. контроль: прежняя нарезка головы эмулятор валит 400-й (значит эмулятор честный)", legacy_dead and api.errors >= 1)

# ---- 10o. 200 реплик подряд: append-only, ограниченный размер, без 400 ----
api = new_api()
os.environ["LOCK_PHRASE"] = "секретная ромашка"
fresh("long")
replies, bad_replies = [], []


async def long_run():
    for i in range(200):
        text = f"реплика {i}" + (" включи свет" if i % 4 == 0 else "") + (" секретная ромашка" if i == 1 else "")
        r = await main.handle("long", text, False)
        replies.append(r["response"]["text"])

run(long_run())
snaps = api.log
episodes, violations, first_msgs = 1, [], []
for prev, cur in zip(snaps, snaps[1:]):
    if cur["canon"][0] == prev["canon"][0]:
        if cur["canon"][:len(prev["canon"])] != prev["canon"]:
            violations.append(len(cur["canon"]))
    else:
        episodes += 1
        first = json.loads(cur["canon"][0])
        first_msgs.append(cur["canon"])
        if not (isinstance(first["content"], list) and "Служебный пересказ" in first["content"][0]["text"]):
            violations.append(-1)
check("10o. 200 реплик: ни один запрос не правит начало истории (append-only внутри эпизода)", not violations, str(violations[:5]))
check("10p. 200 реплик: эмулятор привязки thinking-блоков не выдал ни одного 400", api.errors == 0, api.errors)
check("10q. 200 реплик: все ответы нормальные (не тексты ошибок)", all(t.startswith("ответ на:") or t == "Свет проверил." for t in replies), str([t for t in replies if not (t.startswith("ответ на:") or t == "Свет проверил.")][:3]))
check("10r. 200 реплик: эпизодов >= 4 (сжатие реально срабатывало)", episodes >= 4, episodes)
check("10s. 200 реплик: размер запроса ограничен (сообщений <= 70, символов <= 200k)",
      max(len(s["canon"]) for s in snaps) <= 70 and max(s["chars"] for s in snaps) <= 200_000,
      f"{max(len(s['canon']) for s in snaps)} / {max(s['chars'] for s in snaps)}")
check("10t. новый эпизод стартует без thinking/tool-блоков старой истории",
      all('"type": "thinking"' not in "".join(c[:-1]) and '"tool_use"' not in "".join(c[:-1]) for c in first_msgs))
check("10u. после сжатия пересказ помнит недавнее (в нём есть реплика перед сжатием)",
      any("реплика" in json.loads(c[0])["content"][0]["text"] for c in first_msgs))
check("10v. кодовая фраза замков не переезжает в пересказ",
      all("ромашка" not in "".join(c) for c in first_msgs))
check("10w. все запросы без temperature/top_p/thinking/tool_choice, последнее сообщение от user",
      all(not any(k in s["kw"] for k in FORBIDDEN_KW) and json.loads(s["canon"][-1])["role"] == "user" for s in snaps))
check("10x. пересказ не попадает в проверку кодовой фразы (читаются только строки хозяина)",
      "ромашка" not in brain._recent_user_text(main.state_for("long").dialog, 50))
os.environ.pop("LOCK_PHRASE")

# ---- 10y. ограничение по размеру: огромные реплики сжимаются раньше счётчика сообщений ----
api = new_api()
fresh("big")


async def big_run():
    for i in range(40):
        await main.handle("big", f"реплика {i} " + "ы" * 6000, False)

run(big_run())
check("10y. 40 реплик по 6000 символов: запрос не превышает ~EPISODE_MAX_CHARS и без 400",
      max(s["chars"] for s in api.log) <= main.history.EPISODE_MAX_CHARS + 20_000 and api.errors == 0,
      f"{max(s['chars'] for s in api.log)} err={api.errors}")
check("10z. ...и без единой ошибки эмулятора", api.errors == 0)

# ---- 11. страховка: 400 про историю -> один повтор с чистым началом ----
api = new_api()
fresh("heal")


async def heal_run():
    out = []
    for t in ("первая", "вторая", "третья"):
        out.append((await main.handle("heal", t, False))["response"]["text"])
    st = main.state_for("heal")
    st.dialog = st.dialog[2:]                      # имитация старого бага: голову истории отрезали
    n_before = len(api.log)
    out.append((await main.handle("heal", "четвёртая", False))["response"]["text"])
    calls_for_fix = len(api.log) - n_before
    retry_snap = api.log[n_before + 1]
    out.append((await main.handle("heal", "пятая", False))["response"]["text"])
    return out, calls_for_fix, st, retry_snap

out, calls_fix, st, retry_snap = run(heal_run())
check("11a. сломанная история: первый запрос -> 400, один повтор, ответ нормальный",
      api.errors == 1 and calls_fix == 2 and out[3] == "ответ на: четвёртая", f"err={api.errors} calls={calls_fix} {out[3]}")
check("11b. следующая реплика после починки тоже нормальная (Кореш не умер)", out[4] == "ответ на: пятая" and api.errors == 1, out[4])
check("11c. состояние пользователя сброшено на чистое начало (пересказ помнит то, что осталось от разговора)",
      "вторая" in st.recap and json.loads(_canon(st.dialog[0]))["content"][0]["text"].startswith("[Служебный"), st.recap[:80])
check("11d. повторный запрос чистый: ни одного thinking-блока и ровно [пересказ, ответ, реплика]",
      '"type": "thinking"' not in "".join(retry_snap["canon"]) and len(retry_snap["canon"]) == 3, str(len(retry_snap["canon"])))

# не-историческая 400 повтор не вызывает и не врёт про баланс
api = new_api()
fresh("bad")
api.script = lambda kw: BadRequestError("output_config.effort: Input should be 'low', 'medium' or 'high'")
r = run(main.handle("bad", "привет", False))
check("11e. чужая 400 (не про историю): повтора нет, про баланс не врём",
      len(api.log) == 1 and "баланс" not in r["response"]["text"].lower() and "кредит" not in r["response"]["text"].lower(), r["response"]["text"])

# ---- 12. refusal ----
api = new_api()
fresh("ref")
api.script = lambda kw: Resp([], "refusal")
r = run(main.handle("ref", "сделай что-то запретное", False))
check("12a. refusal -> непустая живая фраза из набора",
      r["response"]["text"] in prompts.PHRASES["derzkiy"]["refusal"], repr(r["response"]["text"]))
dlg = main.state_for("ref").dialog
check("12b. refusal не кладёт пустой ход ассистента в историю", dlg and dlg[-1]["role"] == "user")
r = run(main.handle("ref", "ладно, привет", False))
check("12c. после refusal следующая реплика работает", r["response"]["text"].startswith("ответ на:") and api.errors == 0, repr(r["response"]["text"]))
check("12d. фразы refusal/cut во всех тонах без мата там, где его быть не должно",
      not any(MAT_RE.search(" ".join(prompts.PHRASES[t][k])) for t in ("derzkiy", "vezhlivyy") for k in ("refusal", "cut")))

def _refusal_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={
        "id": "m", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5", "content": [],
        "stop_reason": "refusal", "stop_sequence": None, "stop_details": {"category": "general_harms"},
        "usage": {"input_tokens": 1, "output_tokens": 0}})


async def sdk_refusal():
    client = AsyncAnthropic(api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(_refusal_handler)))
    return await client.messages.create(messages=[{"role": "user", "content": "a"}], **brain.request_params())

_rr = run(sdk_refusal())
check("12g. реальный SDK 0.69 разбирает stop_reason=refusal со stop_details",
      _rr.stop_reason == "refusal" and brain._refusal_category(_rr) == "general_harms")

# max_tokens без текста
api = new_api()
fresh("cut")
api.script = lambda kw: Resp([Blk(type="thinking", thinking="", signature="x")], "max_tokens")
r = run(main.handle("cut", "привет", False))
check("12e. max_tokens и ни одного слова -> фраза «не успел», а не «Готово.»",
      r["response"]["text"] in prompts.PHRASES["derzkiy"]["cut"], repr(r["response"]["text"]))
check("12f. ответ Алисе по-прежнему режется до 1000 символов", len(main.alice_reply("я" * 5000)["response"]["text"]) == 1000)

# ---- 13. тексты ошибок ----
def err_text(exc):
    api = new_api()
    fresh("err")
    api.script = lambda kw: exc
    return run(main.handle("err", "привет", False))["response"]["text"]

t401 = err_text(AuthenticationError("invalid x-api-key sk-ant-SECRET"))
t402 = err_text(APIStatusError("payment required"))
t429 = err_text(RateLimitError("rate_limit_error: slow down"))
t529 = err_text(OverloadedError("overloaded_error"))
t400b = err_text(BadRequestError("Your credit balance is too low to access the Anthropic API."))
t400x = err_text(BadRequestError("messages: roles must alternate"))
check("13a. 401 -> про ключ", "ключ" in t401.lower() and "sk-ant" not in t401, t401)
check("13b. 402 -> про деньги на мозги (в характере Кореша)", "деньги на мозги" in t402.lower() and "billing" in t402.lower(), t402)
check("13c. 400 «credit balance» -> про деньги на мозги", "деньги на мозги" in t400b.lower() and "billing" in t400b.lower(), t400b)
check("13d. 429 -> «перегружен», а не «баланс»", "перегружен" in t429.lower() and "деньги" not in t429.lower(), t429)
check("13e. 529 -> «перегружен»", "перегружен" in t529.lower() and "деньги" not in t529.lower(), t529)
check("13f. прочая 400 -> не «деньги»", "деньги" not in t400x.lower(), t400x)
check("13g. все шесть текстов разные", len({t401, t402, t429, t400x, main.ERROR_TEXTS["history"], main.ERROR_TEXTS["down"]}) == 6)
check("13h. ключ нигде не утёк", not any("sk-ant" in x for x in (t401, t402, t429, t529, t400b, t400x)))


# ================= 15. долгая память (ТЗ-7): диск, перезапуск, пересказ haiku, секреты, лимиты =================
import contextlib  # noqa: E402
import io  # noqa: E402
import subprocess  # noqa: E402

HAIKU = "claude-haiku-5-5"


def mem_file(uid):
    return Path(os.environ["MEMORY_DIR"]) / ("mem-" + hashlib.sha256(uid.encode()).hexdigest()[:24] + ".json")


def mem_json(uid):
    return json.loads(mem_file(uid).read_text(encoding="utf-8"))


def restart():
    """Новый процесс: оперативка пуста, память читается с диска."""
    main.cancel_background()
    main.STATE.clear()
    memory.reset_cache()


class MemApi:
    """Главная модель по плану инструментов + отдельный счёт вызовов пересказа (haiku)."""

    def __init__(self):
        self.log, self.haiku = [], []
        self.plan = []                         # [(имя инструмента, вход)] -- по одному вызову на запрос
        self.haiku_text = "Говорили про кино и ремонт.\nОбещал вернуться к выбору дивана."
        self.haiku_fail = False
        self.results = []                      # что вернули инструменты (tool_result)
        self.n = 0

    async def create(self, **kw):
        if kw["model"] == HAIKU:
            self.haiku.append(kw)
            if self.haiku_fail:
                raise RuntimeError("haiku упал sk-ant-SECRET")
            return text_resp(self.haiku_text, (50, 60))
        msgs = kw["messages"]
        self.log.append({"msgs": [_canon(m) for m in msgs], "system": json.dumps(kw["system"], ensure_ascii=False),
                         "tools": [t["name"] for t in kw["tools"]], "kw": kw})
        last = msgs[-1]
        if not isinstance(last["content"], str):
            self.results.append(last["content"][0]["content"])
        self.n += 1
        if self.plan:
            name, inp = self.plan.pop(0)
            return tool_resp(f"m{self.n}", name, inp)
        return text_resp("ответ")


def mem_api():
    reset_all()
    os.environ["MEMORY_DIR"] = MEMDIR
    api = MemApi()
    brain._create = api.create
    return api


async def say(uid, text):
    return (await main.handle(uid, text, False))["response"]["text"]


def first_block(api_entry):
    m0 = json.loads(api_entry["msgs"][0])
    return m0["content"][0]["text"] if isinstance(m0["content"], list) else None


# ---- 15a. remember -> диск -> «перезапуск» -> заметка в начале следующего эпизода ----
api = mem_api()
fresh("m1")
api.plan = [("remember", {"fact": "не ест лук"}), ("remember", {"fact": "жену зовут Айгуль"})]
run(say("m1", "я не ем лук, жена Айгуль"))
j = mem_json("m1")
check("15a. remember пишет на диск (два факта, файл на пользователя)", [n["t"] for n in j["notes"]] == ["не ест лук", "жену зовут Айгуль"], str(j["notes"]))
check("15b. имя файла не содержит user_id в открытом виде", "m1" not in mem_file("m1").name.replace("mem-", "").replace(".json", "") and mem_file("m1").name.startswith("mem-"))
blk_now = first_block(api.log[-1])
check("15c. заметка, сделанная посреди эпизода, в начало ЭТОГО эпизода не подмешивается (кэш и append-only)",
      blk_now is None, str(blk_now)[:80])
restart()
run(say("m1", "привет, помнишь меня?"))
blk = first_block(api.log[-1])
check("15d. после перезапуска заметки в начале нового эпизода (первое сообщение, блок-список)",
      blk is not None and blk.startswith("[Служебный пересказ") and "не ест лук" in blk and "Айгуль" in blk, str(blk)[:120])
check("15e. после блока идёт ответ ассистента и только потом реплика хозяина (чередование ролей)",
      [json.loads(c)["role"] for c in api.log[-1]["msgs"]] == ["user", "assistant", "user"])
check("15f. блок памяти не попадает в проверку кодовой фразы (читаются только строки хозяина)",
      "Айгуль" not in brain._recent_user_text(main.state_for("m1").dialog, 50).replace("привет, помнишь меня?", ""))

# ---- 15g. внутри эпизода: append-only и system/tools байт-в-байт ----
async def episode_run():
    for i in range(6):
        await say("m1", f"болтаем {i}")
run(episode_run())
ep = [e for e in api.log if first_block(e) is not None]
ok_prefix = all(b["msgs"][:len(a["msgs"])] == a["msgs"] for a, b in zip(ep, ep[1:]))
check("15g. внутри эпизода каждый запрос начинается с предыдущего (append-only), блок памяти не менялся", ok_prefix and len(ep) >= 6)
check("15h. system и список tools одинаковы байт-в-байт во всех запросах (до и после перезапуска)",
      len({e["system"] for e in api.log}) == 1 and len({tuple(e["tools"]) for e in api.log}) == 1
      and api.log[0]["tools"] == ["list_devices", "control_device", "remember", "forget"])
check("15i. ни один запрос главной модели не содержит temperature/top_p/thinking/tool_choice",
      all(not any(k in e["kw"] for k in FORBIDDEN_KW) for e in api.log))

# ---- 15j. forget(fact) и «забудь всё» стирают с диска ----
api.plan = [("forget", {"fact": "не ест лук"})]
run(say("m1", "забудь, что я не ем лук"))
j = mem_json("m1")
check("15j. forget(fact) убрал заметку с диска, вторая осталась", [n["t"] for n in j["notes"]] == ["жену зовут Айгуль"], str(j["notes"]))
check("15k. модели сказано не упоминать забытое в этом разговоре", "не упоминай" in api.results[-1], api.results[-1])
api.plan = [("forget", {"fact": "про космос"})]
run(say("m1", "забудь про космос"))
check("15l. forget несуществующего -> not_found и список того, что помним (модель может повторить точнее)",
      api.results[-1].startswith("not_found") and "Айгуль" in api.results[-1], api.results[-1])
api.plan = [("forget", {"all": True})]
run(say("m1", "забудь всё"))
j = mem_json("m1")
check("15m. «забудь всё»: на диске нет ни заметок, ни пересказов", j["notes"] == [] and j["episodes"] == [] and j["archive"] == "" and j["open"] == "", str(j)[:200])
run(say("m1", "так о чём мы"))
check("15n. после «забудь всё» следующий запрос идёт с чистого листа: старого блока и истории нет",
      first_block(api.log[-1]) is None and len(api.log[-1]["msgs"]) == 1, str(len(api.log[-1]["msgs"])))
restart()
run(say("m1", "ты кто"))
blk = first_block(api.log[-1]) or ""
check("15o. после перезапуска забытое не возвращается (ни заметки, ни разговор до «забудь всё»)",
      not any(x in blk for x in ("лук", "Айгуль", "болтаем", "помнишь")), blk[:200])

# ---- 15p. пересказ закрытого эпизода: haiku вне ответа, после ответа ----
api = mem_api()
fresh("m2")
api.plan = [("remember", {"fact": "болеет за Кайрат"})]


async def to_overflow(uid):
    i = 0
    while not main.history.needs_compaction(main.state_for(uid).dialog):
        await say(uid, f"реплика {i}")
        i += 1
    return i

run_state = {}


async def overflow_flow():
    await to_overflow("m2")
    run_state["haiku_before_drain"] = len(api.haiku)           # ответ Алисе уже отдан, пересказ ещё не делался
    run_state["dialog_before"] = len(main.state_for("m2").dialog)
    await main.drain()
    run_state["haiku_after"] = len(api.haiku)

run(overflow_flow())
check("15p. вызов пересказа идёт ПОСЛЕ ответа Алисе: к моменту ответа вызовов 0, после фона ровно 1",
      run_state["haiku_before_drain"] == 0 and run_state["haiku_after"] == 1, str(run_state))
hk = api.haiku[0]
check("15q. пересказ: claude-haiku-5-5, effort low, без инструментов, без temperature/thinking/tool_choice",
      hk["model"] == HAIKU and hk.get("extra_body") == {"output_config": {"effort": "low"}} and "tools" not in hk
      and not any(k in hk for k in FORBIDDEN_KW), repr({k: v for k, v in hk.items() if k != "messages"}))
j = mem_json("m2")
check("15r. пересказ haiku сохранён на диск", len(j["episodes"]) == 1 and "диван" in j["episodes"][0]["text"], str(j["episodes"])[:200])
st2 = main.state_for("m2")
check("15s. эпизод закрыт: новый начинается с блока памяти (пересказ внутри), без старых блоков",
      len(st2.dialog) == 2 and "диван" in st2.dialog[0]["content"][0]["text"]
      and '"thinking"' not in json.dumps([_canon(m) for m in st2.dialog]), str(len(st2.dialog)))
restart()
run(say("m2", "я вернулся"))
check("15t. после перезапуска пересказ и заметка видны в начале нового эпизода", "диван" in (first_block(api.log[-1]) or "") and "Кайрат" in (first_block(api.log[-1]) or ""))
check("15u. счёт токенов учёл вызов пересказа (50+60)", usage.used() >= 110, usage.used())

# ---- 15v. haiku упал -> локальный пересказ ----
api = mem_api()
fresh("m3")
api.haiku_fail = True
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    async def fail_flow():
        await to_overflow("m3")
        await main.drain()
    run(fail_flow())
j = mem_json("m3")
check("15v. haiku упал -> локальный пересказ из последних реплик сохранён, разговор не сломан",
      len(api.haiku) == 1 and len(j["episodes"]) == 1 and "Хозяин: реплика" in j["episodes"][0]["text"], str(j["episodes"])[:200])
check("15w. в логе про сбой haiku есть строка без текста ошибки (ключ не утёк)",
      "haiku не ответил" in buf.getvalue() and "sk-ant" not in buf.getvalue())
api = mem_api()
os.environ["DAILY_TOKEN_CAP"] = "10"
usage.add(50)
import summarize  # noqa: E402
_r = run(summarize.summarize([{"role": "user", "content": "привет"}, {"role": "assistant", "content": "здорово"}]))
check("15x. потолок токенов исчерпан -> пересказ к API не обращается (локальный путь)", _r == "" and not api.haiku)
os.environ.pop("DAILY_TOKEN_CAP")

# ---- 15y. перезапуск посреди эпизода: снимок становится пересказом ----
api = mem_api()
fresh("m4")
run(say("m4", "давай обсудим ремонт кухни"))
run(say("m4", "хочу синие фасады"))
restart()
run(say("m4", "продолжим"))
blk = first_block(api.log[-1]) or ""
check("15y. процесс умер посреди эпизода -> разговор не потерян (локальный снимок в начале следующего)",
      "ремонт кухни" in blk and "синие фасады" in blk, blk[:200])

# ---- 15z. секреты не попадают ни в заметки, ни в пересказы ----
api = mem_api()
os.environ["LOCK_PHRASE"] = "секретная ромашка"
fresh("m5")
tries = ["кодовая фраза замков: секретная ромашка", "СЕКРЕТНАЯ, ромашка!", "пароль от вайфая 1234", "пин-код 4321",
         "карта 4111 1111 1111 1111", "номер карты 5500-0000-0000-0004", "код из смс 558899", "api key sk-ant-abc",
         "CVV 123", "токен abcdef0123456789abcdef0123456789"]
st5 = memory.store_for("m5")
res = [st5.remember(t)[0] for t in tries]
check("15z1. секреты/пароли/карты/коды отказ при записи заметки", res == ["refused"] * len(tries), str(list(zip(tries, res))))
check("15z2. нормальные факты рядом с секретами пишутся", st5.remember("любит чай без сахара")[0] == "ok" and st5.remember("работает в банке")[0] == "ok")
api.plan = [("remember", {"fact": "пароль от банка 1234"})]
run(say("m5", "запомни мой пароль 1234"))
check("15z3. через инструмент: «запомни пароль» -> refused, модели велено отшутиться",
      api.results[-1].startswith("refused") and "шутк" in api.results[-1] and not any("1234" in n["t"] for n in memory.store_for("m5").notes))
api.haiku_text = "Хозяин назвал пароль 1234.\nГоворили про кино.\nКодовая фраза секретная ромашка.\nКарта 4111 1111 1111 1111.\nОбещал вернуться к дивану."
api.plan = []


def pad_to_60(st):
    while len(st.dialog) < 60:
        st.dialog.extend([{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}])


async def secret_flow():
    st = main.state_for("m5")
    for t_ in ("мой пароль 1234", "карта 4111 1111 1111 1111", "секретная ромашка", "про кино"):
        await say("m5", t_)
    pad_to_60(st)
    await say("m5", "ещё реплика")
    await main.drain()
run(secret_flow())
alltext = mem_file("m5").read_text(encoding="utf-8") + memory.store_for("m5").render_block()
check("15z4. в пересказе haiku нет пароля, карты, кодовой фразы (строки выброшены), остальное сохранено",
      not any(x in alltext for x in ("1234", "4111", "ромашка")) and "кино" in alltext and "диван" in alltext, alltext[:300])
api.haiku_fail = True
st5 = memory.store_for("m5")


async def secret_flow2():
    st = main.state_for("m5")
    pad_to_60(st)
    await say("m5", "пин-код 4321")
    await main.drain()
    for t_ in ("СЕКРЕТНАЯ ромашка", "люблю зиму"):
        await say("m5", t_)
run(secret_flow2())
alltext = mem_file("m5").read_text(encoding="utf-8") + memory.store_for("m5").render_block()
check("15z5. локальный пересказ (haiku упал) тоже без пин-кода и кодовой фразы",
      not any(x in alltext for x in ("4321", "ромашка")) and "люблю зиму" in alltext, alltext[-300:])
check("15z6. снимок открытого эпизода и блок памяти без кодовой фразы",
      "ромашка" not in memory.store_for("m5").open and "ромашка" not in memory.store_for("m5").render_block())
os.environ.pop("LOCK_PHRASE")

# ---- 15aa. лимиты: 100 заметок, 200 символов, без дублей ----
reset_all()
os.environ["MEMORY_DIR"] = MEMDIR
st6 = memory.store_for("m6")
statuses = [st6.remember(f"факт номер {i} про человека")[0] for i in range(105)]
check("15aa. лимит 100 заметок: сотая принята, 101-я отклонена (full), на диске ровно 100",
      statuses[:100] == ["ok"] * 100 and statuses[100:] == ["full"] * 5 and len(mem_json("m6")["notes"]) == 100, str(statuses[98:103]))
st7 = memory.store_for("m7")
st7.remember("я" * 500)
check("15ab. заметка обрезается до 200 символов", len(st7.notes[0]["t"]) == 200)
st7.remember("Любит ЧАЙ, без сахара!")
r1 = st7.remember("любит чай без сахара")
st7.remember("любит чай")
check("15ac. дубли не плодятся (регистр/знаки/вложенность)", r1[0] == "dup" and len(st7.notes) == 2, str([n["t"] for n in st7.notes]))
st7.remember("любит чай без сахара и лимона")
check("15ad. более полная формулировка обновляет старую заметку, а не добавляет рядом",
      len(st7.notes) == 2 and any("лимона" in n["t"] for n in st7.notes))
check("15ae. вложенные эпизоды: хранится не больше 10 пересказов, старые склеены в archive",
      (lambda s: ([s.add_episode(f"тема {i}") for i in range(14)], len(s.episodes) == 10 and "тема 0" in s.archive and "тема 13" not in s.archive)[1])(memory.store_for("m8")))
check("15af. блок памяти: пересказы в хронологии, бюджет ограничен",
      (lambda s: len(s.render_summaries()) <= memory.BLOCK_SUMMARY_BUDGET + memory.ARCHIVE_CHARS + 200)(memory.store_for("m8")))

# ---- 15aa2. forget пишет на диск сразу (а не «когда-нибудь потом») ----
st11 = memory.store_for("m11")
st11.remember("боится пауков")
st11.remember("водит синюю машину")
st11.forget("боится пауков")
d1 = mem_json("m11")
check("15aa2. forget(fact) сразу на диске: заметки нет, соседняя осталась", [n["t"] for n in d1["notes"]] == ["водит синюю машину"], str(d1["notes"]))
st11.add_episode("говорили про пауков и синюю машину\nобсуждали отпуск")
st11.forget("пауков")
d1 = mem_json("m11")
check("15aa3. forget чистит и строки пересказов, где факт упоминался (на диске)",
      all("паук" not in e["text"] for e in d1["episodes"]) and any("отпуск" in e["text"] for e in d1["episodes"]), str(d1["episodes"]))
st11.forget(everything=True)
d1 = mem_json("m11")
check("15aa4. forget(all) сразу очищает файл", d1["notes"] == [] and d1["episodes"] == [])

# ---- 15ag. MEMORY_DIR недоступен: работает в памяти, не падает, логирует один раз ----
reset_all()
bad_parent = Path(tempfile.mkdtemp()) / "file"
bad_parent.write_text("x")
saved_uf = os.environ.pop("USAGE_FILE")
usage._state.update(day="", used=0, loaded=False)
os.environ["MEMORY_DIR"] = str(bad_parent / "sub")          # родитель -- обычный файл: ни создать, ни писать
api = MemApi()
brain._create = api.create
api.plan = [("remember", {"fact": "любит футбол"}), ("remember", {"fact": "ездит на велике"})]
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    r = run(main.handle("m9", "запомни", False))
    run(say("m9", "ещё"))
    usage.add(100)
    h = TestClient(main.app).get("/health").json()
out = buf.getvalue()
check("15ag. каталог памяти недоступен: ответ нормальный, не 500", r["response"]["text"] == "ответ", repr(r))
check("15ah. заметки живут в оперативке и видны модели в рамках процесса", [n["t"] for n in memory.store_for("m9").notes] == ["любит футбол", "ездит на велике"])
check("15ai. честная строка в лог: каталог недоступен, работаю в памяти; не спамит на каждую запись",
      out.count("недоступен") == 1 and "памяти процесса" in out, out)
check("15aj. /health показывает memory=ram", h["memory"] == "ram", str(h))
check("15ak. суточный счётчик при недоступном диске считает в памяти", usage.used() >= 100)
os.environ["USAGE_FILE"] = saved_uf
usage._state.update(day="", used=0, loaded=False)
os.environ["MEMORY_DIR"] = MEMDIR
reset_all()

# ---- 15al. суточный лимит переживает перезапуск ----
saved_usage_file = os.environ.pop("USAGE_FILE")
os.environ["MEMORY_DIR"] = tempfile.mkdtemp()
usage._state.update(day="", used=0, loaded=False)
usage.add(1234)
usage._state.update(day="", used=0, loaded=False)           # «перезапуск»: оперативная копия потеряна
check("15al. счётчик токенов лежит в MEMORY_DIR и переживает перезапуск",
      usage.used() == 1234 and Path(os.environ["MEMORY_DIR"], "usage.json").exists(), str(usage.used()))
os.environ["USAGE_FILE"] = saved_usage_file
os.environ["MEMORY_DIR"] = MEMDIR
usage._state.update(day="", used=0, loaded=False)
reset_all()

# ---- 15am. атомарная запись и битый файл ----
d = tempfile.mkdtemp()
memory.atomic_write(os.path.join(d, "a.json"), "{}")
check("15am. атомарная запись не оставляет tmp-файлов", sorted(os.listdir(d)) == ["a.json"], str(os.listdir(d)))
os.environ["MEMORY_DIR"] = d
mem_file_bad = Path(d) / ("mem-" + hashlib.sha256(b"m10").hexdigest()[:24] + ".json")
mem_file_bad.write_text("{битый", encoding="utf-8")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    s10 = memory.store_for("m10")
check("15an. битый файл памяти не роняет: откладывается в .corrupt, старт с чистого", s10.notes == [] and Path(str(mem_file_bad) + ".corrupt").exists())
os.environ["MEMORY_DIR"] = MEMDIR
reset_all()

# ---- 15ao. сборка: Dockerfile, шаблон, .env, документация ----
dock = (HERE / "Dockerfile").read_text(encoding="utf-8")
check("15ao. Dockerfile копирует memory.py и summarize.py и создаёт /data для пользователя app",
      "memory.py" in dock and "summarize.py" in dock and "mkdir -p /data" in dock and "chown app" in dock and "MEMORY_DIR=/data" in dock)
tv = json.loads((HERE / "template-variables.json").read_text(encoding="utf-8"))
names = {v["name"] for v in tv["variables"]}
check("15ap. template-variables.json: MEMORY_DIR, RAILWAY_RUN_UID и Volume на /data описаны",
      {"MEMORY_DIR", "RAILWAY_RUN_UID", "MEMORY_MODEL"} <= names and "/data" in json.dumps(tv, ensure_ascii=False) and "volume" in json.dumps(tv).lower())
envx = (HERE / ".env.example").read_text(encoding="utf-8")
check("15aq. .env.example: MEMORY_DIR и MEMORY_MODEL", "MEMORY_DIR=" in envx and "MEMORY_MODEL=" in envx)
setup = (HERE / "SETUP.md").read_text(encoding="utf-8")
check("15ar. SETUP.md: раздел «Память», Volume /data, RAILWAY_RUN_UID=0, как стереть", "## Память" in setup and "/data" in setup and "RAILWAY_RUN_UID=0" in setup and "забудь всё" in setup.lower())
check("15as. railway.json без multiRegionConfig (реплики с Volume несовместимы по доке Railway)", "multiRegionConfig" not in (HERE / "railway.json").read_text(encoding="utf-8"))
sp = prompts.system_prompt()
check("15at. в промпте правило памяти: remember/forget, секреты не запоминать, забытое не упоминать",
      "remember" in sp and "forget" in sp and "запомни пароль" in sp and "не упоминай забытое" in sp)

# запуск без Volume / с недоступным каталогом не ломает старт сервиса (отдельный процесс)
env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY",)}
env["MEMORY_DIR"] = str(bad_parent / "sub")
env.pop("USAGE_FILE", None)
proc = subprocess.run([sys.executable, "-c",
                       "import main, usage; from fastapi.testclient import TestClient; "
                       "r = TestClient(main.app).get('/health').json(); usage.add(5); print('HEALTH', r['ok'], r['memory'])"],
                      cwd=str(HERE), env=env, capture_output=True, text=True, timeout=60)
check("15au. процесс стартует и отвечает /health при недоступном MEMORY_DIR (как контейнер без Volume и без прав)",
      proc.returncode == 0 and "HEALTH True ram" in proc.stdout, (proc.stdout + proc.stderr)[-300:])


# ================= 16. род ассистента (ТЗ-11): GENDER male | female =================
MASC_SELF = re.compile(r"\b[а-яё]+л\b|\b[а-яё]+лся\b|\bзавис\b", re.I)
FEM_SELF = re.compile(r"\b[а-яё]+ла\b|\b[а-яё]+лась\b", re.I)
# подлежащее не сам ассистент: «ответ оборвался» (м.р.), «мысль оборвалась» (ж.р.) одинаковы в обоих родах
NOT_SELF = {"оборвался", "оборвалась", "сбилась", "отказал", "принял", "журнал", "хотел"}   # подлежащее: мозг, память, ответ, хозяин; журнал -- существительное


def self_forms(text, rx):
    return sorted(w for w in rx.findall(text) if w.lower() not in NOT_SELF)


def flat(table):
    out = []
    for tone, kinds in table.items():
        for kind, v in kinds.items():
            for x in (v if isinstance(v, list) else [v]):
                out.append((tone, kind, x))
    return out


reset_all()
os.environ.pop("GENDER", None)
check("16a. GENDER не задан -> male", C.gender() == "male")
os.environ["GENDER"] = "female"
check("16b. GENDER=female -> female (регистр не важен)", C.gender() == "female")
os.environ["GENDER"] = "FEMALE "
check("16b2. GENDER=FEMALE с пробелом -> female", C.gender() == "female")
for junk in ("мужской", "", "1", "ж", "other"):
    os.environ["GENDER"] = junk
    check(f"16c. мусор GENDER={junk!r} -> male", C.gender() == "male")

# системный промпт: правильный род в каждом из трёх тонов
MALE_PROMPT_BAD = re.compile(r"\b(?:вырубила|видела|поняла|Поправила|Не успела|привыкла|придумала|Узнала|Не поняла|сделала)\b")
FEM_PROMPT_BAD = re.compile(r"\b(?:вырубил|видел|понял|Поправил|Не успел|привык|придумал|Узнал|Не понял|сделал)\b")
for t in C.TONES:
    os.environ["TONE"] = t
    for g, bad, rule_must, rule_must_not in (
            ("male", MALE_PROMPT_BAD, "ты мужского рода", "женского рода"),
            ("female", FEM_PROMPT_BAD, "ты женского рода", "мужского рода")):
        os.environ["GENDER"] = g
        sp_g = prompts.system_prompt()
        body = sp_g.replace(prompts.GENDER_FORMS[g]["gender_rule"], "")      # само правило называет оба рода
        check(f"16d. {t}/{g}: правило рода в промпте, чужих форм о себе нет",
              rule_must in sp_g and rule_must_not not in body and not bad.search(body), str(bad.findall(body)))
    os.environ["GENDER"] = "junk"
    check(f"16e. {t}: мусор GENDER -> промпт как у male", prompts.system_prompt() == prompts.system_prompt(gender="male"))
    os.environ.pop("GENDER")
    check(f"16f. {t}: без GENDER промпт как у male, а male и female различаются",
          prompts.system_prompt() == prompts.system_prompt(gender="male") != prompts.system_prompt(gender="female"))
os.environ["TONE"] = "derzkiy"

# готовые фразы: обе таблицы полные, форма рода верная
PM, PF = prompts.PHRASES_BY_GENDER["male"], prompts.PHRASES_BY_GENDER["female"]
check("16g. таблицы male/female: те же тоны, те же виды фраз, то же число вариантов",
      PM.keys() == PF.keys() and all(PM[t].keys() == PF[t].keys() for t in PM)
      and all(len(PM[t][k]) == len(PF[t][k]) for t in PM for k in PM[t] if isinstance(PM[t][k], list)))
check("16h. male: в готовых фразах нет женских форм о себе",
      not [(t, k, self_forms(x, FEM_SELF)) for t, k, x in flat(PM) if self_forms(x, FEM_SELF)])
check("16i. female: в готовых фразах нет мужских форм о себе",
      not [(t, k, self_forms(x, MASC_SELF)) for t, k, x in flat(PF) if self_forms(x, MASC_SELF)],
      str([(t, k, self_forms(x, MASC_SELF)) for t, k, x in flat(PF) if self_forms(x, MASC_SELF)]))
DIFF = [(t, k) for (t, k, a), (_, _, b) in zip(flat(PM), flat(PF)) if a != b]
check("16j. фразы про «не успел/завис/выговорился» (cut, limit) в двух родах, остальные нейтральны и общие",
      {k for _, k in DIFF} == {"cut", "limit"} and len(DIFF) == 7, str(DIFF))
check("16k. каждое различие female содержит женскую форму о себе, male мужскую",
      all(self_forms(b, FEM_SELF) and self_forms(a, MASC_SELF)
          for (t, k, a), (_, _, b) in zip(flat(PM), flat(PF)) if a != b))
SPOKEN = list(main.ERROR_TEXTS.values()) + ["Ну? Говори.", "Готово.", "Что-то пошло не так, повтори."]
check("16l. ошибки и служебные реплики нейтральны по роду (годятся обоим)",
      not [x for x in SPOKEN if self_forms(x, MASC_SELF) or self_forms(x, FEM_SELF)])
check("16l2. служебные реплики из кода на месте", all(lit in (HERE / "main.py").read_text(encoding="utf-8") + (HERE / "brain.py").read_text(encoding="utf-8")
                                                      for lit in ("Ну? Говори.", "Готово.", "Что-то пошло не так, повтори.")))

# phrase() выбирает таблицу по GENDER в окружении
for g, forms_rx, other_rx in (("male", MASC_SELF, FEM_SELF), ("female", FEM_SELF, MASC_SELF)):
    os.environ["GENDER"] = g
    for t in C.TONES:
        os.environ["TONE"] = t
        seen = {k: {prompts.phrase(k) for _ in range(60)} for k in ("cut", "limit")}
        txt = " ".join(" ".join(v) for v in seen.values())
        check(f"16m. phrase() {t}/{g}: cut и limit в нужном роде",
              not self_forms(txt, other_rx) and (t == "vezhlivyy" and g == "male" or bool(self_forms(txt, forms_rx))), txt[:120])
    allp = " ".join(prompts.phrase(k) for _ in range(40) for k in ("hold", "still", "greet", "bye", "refusal", "help"))
    check(f"16n. phrase() {g}: hold/still/greet/bye/refusal/help без чужих форм", not self_forms(allp, other_rx))
os.environ["TONE"] = "derzkiy"
os.environ["GENDER"] = "female"
check("16o. phrase(): явный gender важнее окружения", prompts.phrase("limit", "derzkiy", "male") == PM["derzkiy"]["limit"]
      and prompts.phrase("limit", "derzkiy", "female") == PF["derzkiy"]["limit"])

# system байт-в-байт стабилен между запросами развёртывания (кэш) в обоих родах
for g in ("male", "female"):
    api = mem_api()
    os.environ["GENDER"] = g
    os.environ["TONE"] = "derzkiy_mat"
    fresh("g1")
    api.plan = [("list_devices", {})]
    run(say("g1", "привет"))
    for i in range(4):
        run(say("g1", f"болтаем {i}"))
    systems = {e["system"] for e in api.log}
    check(f"16p. {g}: system одинаков байт-в-байт во всех {len(api.log)} запросах и содержит правило рода",
          len(systems) == 1 and len(api.log) >= 5 and ("ты мужского рода" if g == "male" else "ты женского рода") in next(iter(systems)))
reset_all()
os.environ["GENDER"] = "female"
r = main.app.router  # noqa: F841
from fastapi.testclient import TestClient  # noqa: E402
check("16q. /health показывает gender", TestClient(main.app).get("/health").json().get("gender") == "female")

# сборка: переменная описана везде
envx2 = (HERE / ".env.example").read_text(encoding="utf-8")
tv2 = json.loads((HERE / "template-variables.json").read_text(encoding="utf-8"))
gv = [v for v in tv2["variables"] if v["name"] == "GENDER"]
setup2 = (HERE / "SETUP.md").read_text(encoding="utf-8")
check("16r. .env.example и template-variables.json: GENDER (male по умолчанию, не обязательная)",
      "GENDER=male" in envx2 and len(gv) == 1 and gv[0]["default"] == "male" and gv[0]["required"] is False)
check("16s. SETUP.md: шаг про голос навыка, соответствие GENDER, пометка [ПРОВЕРИТЬ] со ссылкой на список голосов",
      "GENDER" in setup2 and "голос Алисы" in setup2 and "female" in setup2 and "male" in setup2
      and "[ПРОВЕРИТЬ]" in setup2 and "https://yandex.ru/dev/dialogs/alice/doc/ru/voices" in setup2)
reset_all()

# ================= 17. страница настройки /setup (ТЗ-15) =================
import contextlib  # noqa: E402
import hashlib  # noqa: E402
import hmac as _hmac  # noqa: E402
import io  # noqa: E402
import stat as _stat  # noqa: E402

import setupwiz  # noqa: E402
from fastapi.testclient import TestClient as _TC  # noqa: E402


class _Tee:
    """Всё, что печатает программа за время раздела 17, копится тут: потом ищем в журнале секреты."""
    def __init__(self, real):
        self.real, self.buf = real, io.StringIO()

    def write(self, s):
        self.buf.write(s)
        return self.real.write(s)

    def flush(self):
        self.real.flush()


_tee = _Tee(sys.stdout)
sys.stdout = _tee

reset_all()
SDIR = tempfile.mkdtemp()                       # свой каталог памяти: config.json не мешает другим разделам
os.environ["MEMORY_DIR"] = SDIR
SC = "test-setup-code-777"
KEYV = "sk-ant-test-KEY-0123456789"
SECRET = "whsec-0123456789abcdef"
LOCKPH = "Мой Тайный Пароль Для Замков 42"
INSTALL_PUB = "0123456789abcdef"
INSTALL_TOK = INSTALL_PUB + ".SECRETpart-XYZ_987"
T17 = [1_000_000.0]
setupwiz._now = lambda: T17[0]
setupwiz.reset_for_tests()
for k in ("SETUP_CODE", "YANDEX_OAUTH_CLIENT_ID", "INSTALL_CALLBACK_URL", "INSTALL_TOKEN", "PUBLIC_URL", "RAILWAY_PUBLIC_DOMAIN",
          "YANDEX_IOT_TOKEN"):
    os.environ.pop(k, None)
os.environ["WEBHOOK_SECRET"] = SECRET
tc = _TC(main.app)


def lg(c, code=SC, reopen=False):
    return c.post("/setup/api/login", json={"code": code, "reopen": reopen})


# --- 17a. без SETUP_CODE страницы нет ---
check("17a. без SETUP_CODE: /setup, state, login = 404",
      tc.get("/setup").status_code == 404 and tc.get("/setup/api/state").status_code == 404
      and tc.post("/setup/api/login", json={"code": "x"}).status_code == 404 and tc.get("/setup/api/me").status_code == 404)

os.environ["SETUP_CODE"] = SC
r = tc.get("/setup")
html17 = r.text
check("17b. /setup отдаёт страницу, no-store, CSP, без внешних скриптов и CDN",
      r.status_code == 200 and "no-store" in r.headers.get("cache-control", "") and "default-src 'none'" in r.headers.get("content-security-policy", "")
      and not re.search(r"<script[^>]+src=|<link[^>]+href=|@import|https?://[^\s\"']*(cdn|googleapis|jsdelivr|unpkg|cloudflare)", html17, re.I)
      and "innerHTML" not in html17 and "eval(" not in html17 and "document.write" not in html17)
check("17c. страница в стиле бренда: paper #f4f0e8, ink #20201e, orange #ff4d2b, yellow #f4cb70",
      all(c in html17 for c in ("#f4f0e8", "#20201e", "#ff4d2b", "#f4cb70")))
check("17d. API без сессии = 401",
      all(tc.get(p).status_code == 401 for p in ("/setup/api/state", "/setup/api/devices", "/setup/api/heard"))
      and all(tc.post(p, json={}).status_code == 401 for p in ("/setup/api/character", "/setup/api/home", "/setup/api/finish",
                                                              "/setup/api/check-brain", "/setup/api/iot-token", "/setup/api/self-test")))
check("17e. login не JSON -> 415 (чужая форма не пройдёт)",
      tc.post("/setup/api/login", content='{"code":"x"}', headers={"content-type": "text/plain"}).status_code == 415)
check("17f. неверный код -> 401 и без кук", lg(tc, "wrong").status_code == 401 and not tc.cookies.get("koresh_setup"))

# --- лимит попыток ---
setupwiz.reset_for_tests()
codes = [lg(tc, f"bad{i}").status_code for i in range(5)]
check("17g. неверный код x5: 401 x4, на пятой блок 429", codes == [401, 401, 401, 401, 429], str(codes))
check("17h. во время блока даже верный код не пускает (429)", lg(tc).status_code == 429)
T17[0] += 601
r = lg(tc)
sc = r.headers.get("set-cookie", "")
check("17i. после паузы верный код -> сессия: httpOnly, SameSite=Strict, 2 часа, path=/setup",
      r.status_code == 200 and "httponly" in sc.lower() and "samesite=strict" in sc.lower() and "max-age=7200" in sc.lower()
      and "path=/setup" in sc.lower(), sc)
check("17j. с сессией state открыт", tc.get("/setup/api/state").status_code == 200)
T17[0] += 7199
ok1 = tc.get("/setup/api/state").status_code == 200
T17[0] += 3
check("17k. сессия живёт 2 часа и ни секундой дольше", ok1 and tc.get("/setup/api/state").status_code == 401)
check("17l. сравнение кода timing-safe (compare_digest по хэшам) и не падает на длинном вводе",
      "compare_digest" in Path(setupwiz.__file__).read_text(encoding="utf-8") and lg(tc, "я" * 5000).status_code == 401)
setupwiz.reset_for_tests()
tc = _TC(main.app)
check("17m. нет cookie у второго клиента -> 401", tc.get("/setup/api/state").status_code == 401)
check("17n. вход после сброса счётчика ок", lg(tc).status_code == 200)

# --- config.json ---
CP = Path(SDIR) / "config.json"
r = tc.post("/setup/api/character", json={"name": "Бро", "tone": "derzkiy_mat", "gender": "female"})
st_mode = _stat.S_IMODE(os.stat(CP).st_mode) if CP.exists() else None
cfg = json.loads(CP.read_text(encoding="utf-8")) if CP.exists() else {}
check("17o. сохранение характера: config.json создан, права 600, нет временных файлов",
      r.status_code == 200 and st_mode == 0o600 and cfg["values"]["ASSISTANT_NAME"] == "Бро"
      and not list(Path(SDIR).glob("*.tmp")), f"{r.status_code} {oct(st_mode or 0)}")
check("17p. применяется сразу, без рестарта: имя/тон/пол в config и в промпте",
      C.assistant_name() == "Бро" and C.tone() == "derzkiy_mat" and C.gender() == "female"
      and "Тебя зовут Бро" in prompts.system_prompt() and "женского рода" in prompts.system_prompt())
os.environ["TONE"] = "vezhlivyy"
check("17q. характер: выбор на /setup главнее переменной (стартовая TONE=derzkiy не глушит «С матом»)", C.tone() == "derzkiy_mat" and C.source("TONE") == "setup")
sj = tc.get("/setup/api/state").json()
check("17r. state: тон и имя из /setup, источник setup", sj["tone"]["source"] == "setup" and sj["name"]["source"] == "setup")
os.environ.pop("TONE")
check("17s. убрали переменную -> снова значение из config", C.tone() == "derzkiy_mat" and C.source("TONE") == "setup")
check("17t. умолчание, если нигде не задано", C.source("BLOCKED_NAMES") == "default" and C.extra_blocked_names() == [])

before = CP.read_text(encoding="utf-8")
_real_replace = os.replace


def _bad_replace(a, b):
    raise OSError("диск умер")


os.replace = _bad_replace
try:
    ok_w = C.save_values({"ASSISTANT_NAME": "Сломанный"})
finally:
    os.replace = _real_replace
check("17u. запись атомарна: сбой на rename -> старый файл цел, мусора нет, значения живут в оперативке",
      ok_w is False and CP.read_text(encoding="utf-8") == before and not list(Path(SDIR).glob("*.tmp"))
      and C.assistant_name() == "Бро")
C._MEM_ONLY.clear()
CP.write_text("{битый json", encoding="utf-8")
check("17v. битый config.json не роняет сервис: настройки по умолчанию", C.assistant_name() == "Кореш" and C.tone() == "derzkiy")
CP.write_text(before, encoding="utf-8")
check("17w. починенный файл читается снова", C.assistant_name() == "Бро")
os.environ["ALLOW_LOCKS"] = "0"
C.save_values({"ALLOW_LOCKS": "1", "LOCK_PHRASE": "какая-то длинная фраза"})
check("17x. ALLOW_LOCKS=0 в переменных главнее ALLOW_LOCKS=1 из config", C.allow_locks() is False)
os.environ.pop("ALLOW_LOCKS")
C.save_values({"ALLOW_LOCKS": None, "LOCK_PHRASE": None})
try:
    C.save_values({"ANTHROPIC_API_KEY": "x"})
    _rej = False
except KeyError:
    _rej = True
check("17y. ключ Anthropic и секрет вебхука в config.json писать нельзя", _rej)

# --- валидация характера ---
check("17z1. плохой тон/пол/пустое имя -> 400",
      tc.post("/setup/api/character", json={"name": "Бро", "tone": "x", "gender": "male"}).status_code == 400
      and tc.post("/setup/api/character", json={"name": "Бро", "tone": "derzkiy", "gender": "x"}).status_code == 400
      and tc.post("/setup/api/character", json={"name": "   ", "tone": "derzkiy", "gender": "male"}).status_code == 400)
tc.post("/setup/api/character", json={"name": "Очень длинное имя\nс переносом и ещё много букв сверху", "tone": "derzkiy", "gender": "male"})
check("17z2. имя режется до 30 символов и без переносов строк", len(C.assistant_name()) <= 30 and "\n" not in C.assistant_name())
tc.post("/setup/api/character", json={"name": "Кореш", "tone": "derzkiy", "gender": "male"})

# --- умный дом ---
setupwiz._TRANSPORT = httpx.MockTransport(yandex_handler)
check("17z3. кривой токен -> 400", tc.post("/setup/api/iot-token", json={"token": "коротко"}).status_code == 400)
r = tc.post("/setup/api/iot-token", json={"token": "wrong-token-1234567"})
check("17z4. токен, который Яндекс не принял -> 400 и в config НЕ записан", r.status_code == 400 and C.stored("YANDEX_IOT_TOKEN") == "")
r = tc.post("/setup/api/iot-token", json={"token": "access_token=test-iot-token&expires_in=31536000"})
d = r.json().get("devices", {})
check("17z5. токен из /setup -> мок /user/info -> «вижу 4 устройства», комнаты и сценарии",
      r.status_code == 200 and d.get("count") == 4 and set(d.get("rooms", [])) == {"Спальня", "Гостиная"}
      and len(d.get("scenarios", [])) == 2, str(r.text)[:200])
lockd = {x["name"]: x["locked"] for x in d.get("devices", [])}
check("17z6. замок и ворота помечены заблокированными, лампа и телевизор нет",
      lockd.get("Входной замок") is True and lockd.get("Ворота во дворе") is True and lockd.get("Лампа") is False
      and lockd.get("Телевизор") is False and {x["name"]: x["locked"] for x in d["scenarios"]}["Открыть ворота"] is True)
check("17z7. токен сохранён и применяется без рестарта; ответ токен не содержит",
      C.iot_token() == "test-iot-token" and "test-iot-token" not in r.text and C.source("YANDEX_IOT_TOKEN") == "setup")
vw = asyncio.run(iot.devices())
check("17z8. iot работает с токеном ТОЛЬКО из config.json (4 устройства)", len(vw.get("devices", [])) == 4, str(vw)[:120])
r = tc.get("/setup/api/devices")
check("17z9. GET /devices повторяет список", r.status_code == 200 and r.json()["devices"]["count"] == 4)
os.environ["YANDEX_IOT_TOKEN"] = "env-token-should-win"
check("17za. токен из переменной главнее config", C.iot_token() == "env-token-should-win")
os.environ.pop("YANDEX_IOT_TOKEN")

# --- дом: люди, комнаты, не трогать, замки ---
r = tc.post("/setup/api/home", json={"owner": "Рустем", "people": [["Айгуль", "жена"], ["", "пусто"]],
                                      "rooms": [["детская", "Спальня"], ["зал", "Гостиная"]], "blocked": ["Телевизор", "телевизор", " "],
                                      "allow_locks": False})
sp = prompts.system_prompt()
check("17zb. дом сохранён: хозяин, люди, комнаты попадают в промпт, мусорные строки отброшены",
      r.status_code == 200 and "Хозяина зовут Рустем" in sp and "Айгуль — жена" in sp and "«детская» → «Спальня»" in sp and "пусто" not in sp)
check("17zc. «не трогать никогда»: слово в нижнем регистре, без дублей, действует на устройства",
      C.extra_blocked_names() == ["телевизор"] and iot._device_sensitive({"type": "devices.types.media_device.tv", "name": "Телевизор"}))
check("17zd. замки без фразы включить нельзя", tc.post("/setup/api/home", json={"owner": "Р", "allow_locks": True}).status_code == 400
      and C.allow_locks() is False)
check("17ze. короткая фраза отвергнута",
      tc.post("/setup/api/home", json={"owner": "Р", "allow_locks": True, "lock_phrase": "123"}).status_code == 400)
r = tc.post("/setup/api/home", json={"owner": "Рустем", "people": [["Айгуль", "жена"]], "rooms": [["детская", "Спальня"]],
                                      "blocked": ["телевизор"], "allow_locks": True, "lock_phrase": LOCKPH})
check("17zf. с кодовой фразой замки включаются, и работает прежняя защита iot",
      r.status_code == 200 and r.json()["locks_enabled"] is True and C.allow_locks() and
      iot._lock_gate({"protected": True}, "открой замок") and iot._lock_gate({"protected": True}, "открой замок " + LOCKPH) is None
      and iot._lock_gate({"protected": True}, "открой замок").startswith("need_phrase"))
st17 = tc.get("/setup/api/state")
check("17zg. кодовая фраза нигде не показывается: ни в state, ни на странице",
      LOCKPH not in st17.text and LOCKPH not in tc.get("/setup").text and st17.json()["lock_phrase"] == {"set": True, "source": "setup"})
tc.post("/setup/api/home", json={"owner": "Рустем", "allow_locks": True})
check("17zh. пустое поле фразы не затирает сохранённую", C.lock_phrase() == LOCKPH and C.allow_locks())
r = tc.post("/setup/api/home", json={"owner": "Рустем", "allow_locks": False, "clear_lock_phrase": True})
check("17zi. «стереть фразу» стирает, замки выключены", r.status_code == 200 and C.lock_phrase() == "" and not C.allow_locks())
tc.post("/setup/api/home", json={"owner": "Рустем", "people": [["Айгуль", "жена"]], "allow_locks": False})

# --- вебхук ---
sj = tc.get("/setup/api/state").json()
wurl = sj["webhook"]["url"]
wp = "/" + wurl.split("/", 3)[3]
check("17zj. адрес вебхука содержит секрет, домен берётся из запроса", wurl == "http://testserver/alice/" + SECRET, wurl)
body_ping = {"session": {"new": False, "user": {"user_id": "wzz"}}, "request": {"original_utterance": "ping"}, "version": "1.0"}
check("17zk. адрес из /setup принимает main.py (200 pong), чужой секрет = 403",
      tc.post(wp, json=body_ping).json()["response"]["text"] == "pong" and tc.post("/alice/nope", json=body_ping).status_code == 403)
os.environ["PUBLIC_URL"] = "https://koresh.example.com/"
check("17zl. PUBLIC_URL главнее заголовков запроса",
      tc.get("/setup/api/state").json()["webhook"]["url"] == "https://koresh.example.com/alice/" + SECRET)
os.environ.pop("PUBLIC_URL")
os.environ.pop("WEBHOOK_SECRET")
w0 = tc.get("/setup/api/state").json()["webhook"]
check("17zm. без WEBHOOK_SECRET адреса нет, секрет не задан", w0["secret_set"] is False and w0["url"] == "")
os.environ["WEBHOOK_SECRET"] = SECRET

# --- «услышал» ---
def stub(text="Привет от мока", delay=0.0, log=None):
    async def _c(**kw):
        if log is not None:
            log.append(kw)
        if delay:
            await asyncio.sleep(delay)
        return text_resp(text)
    return _c


setupwiz.reset_for_tests()
tc2 = _TC(main.app)
lg(tc2)
brain._create = stub()
os.environ["HOLD_TIMEOUT"] = "3.8"
r0 = tc2.get("/setup/api/heard?since=1e12").json()
since = r0["now"]
check("17zn. до запроса Яндекса поллинг молчит", tc2.get(f"/setup/api/heard?since={since}").json()["heard"] is False)
T17[0] += 5
tc.post(wp, json=body_ping)
h1 = tc2.get(f"/setup/api/heard?since={since}").json()
check("17zo. ping от Яндекса -> «услышал» (kind=ping)", h1["heard"] is True and h1["kind"] == "ping")
T17[0] += 5
since2 = h1["now"]
fresh("hz1")
tc.post(wp, json={"session": {"new": False, "user": {"user_id": "hz1"}}, "request": {"original_utterance": "включи свет в детской"}, "version": "1.0"})
h2 = tc2.get(f"/setup/api/heard?since={since2}").json()
check("17zp. настоящая реплика -> «услышал: “включи свет в детской”»", h2["heard"] and h2["kind"] == "phrase" and h2["text"] == "включи свет в детской")
check("17zq. только новые: повторный опрос с новым since молчит", tc2.get(f"/setup/api/heard?since={h2['now']}").json()["heard"] is False)

# вне сессии мастера реплики не запоминаются
setupwiz.reset_for_tests()
tc.post(wp, json={"session": {"new": False, "user": {"user_id": "hz2"}}, "request": {"original_utterance": "тайная реплика"}, "version": "1.0"})
tc3 = _TC(main.app)
lg(tc3)
check("17zr. пока мастер закрыт, разговоры нигде не копятся", tc3.get("/setup/api/heard?since=0").json()["heard"] is False)

# --- тест без Станции ---
setupwiz.reset_for_tests()
tc4 = _TC(main.app)
lg(tc4)
since = tc4.get("/setup/api/heard?since=1e12").json()["now"]
T17[0] += 5
LOGC = []
brain._create = stub("Я тут, всё слышу.", log=LOGC)
r = tc4.post("/setup/api/self-test", json={})
check("17zs. «тест без Станции» ходит на свой вебхук и возвращает ответ мозга", r.status_code == 200 and r.json()["text"] == "Я тут, всё слышу."
      and len(LOGC) == 1, r.text[:150])
check("17zt. тестовая реплика НЕ считается «услышал от Яндекса»", tc4.get(f"/setup/api/heard?since={since}").json()["heard"] is False)
check("17zu. после теста память тестового пользователя стёрта (диск и оперативка)",
      setupwiz.SELFTEST_UID not in main.STATE and setupwiz.SELFTEST_UID not in memory.STORES
      and not any(p.name.startswith("mem-") for p in Path(SDIR).glob("mem-*.json") if
                  p.name == "mem-" + hashlib.sha256(setupwiz.SELFTEST_UID.encode()).hexdigest()[:24] + ".json"))
tc.post(wp, json={"session": {"new": False, "user": {"user_id": "hz3"}}, "request": {"original_utterance": "привет"}, "version": "1.0"},
        headers={setupwiz.SELFTEST_HEADER: "forged-value"})
check("17zv. поддельный заголовок самотеста не прячет настоящий запрос", tc4.get(f"/setup/api/heard?since={since}").json()["heard"] is True)
os.environ["HOLD_TIMEOUT"] = "0.05"
setupwiz.SELFTEST_RETRY_DELAY = 0.4
brain._create = stub("Медленный ответ.", delay=0.25)
fresh(setupwiz.SELFTEST_UID)
r = tc4.post("/setup/api/self-test", json={})
check("17zw. мозг думает дольше окна -> тест сам переспрашивает и приносит настоящий ответ",
      r.status_code == 200 and r.json()["text"] == "Медленный ответ.", r.text[:150])
os.environ["HOLD_TIMEOUT"] = "3.8"
setupwiz.SELFTEST_RETRY_DELAY = 2.0

# --- проверка мозга ---
class _Err(Exception):
    def __init__(self, msg, code):
        super().__init__(msg)
        self.status_code = code


def brain_check(create, key=KEYV):
    T17[0] += 10
    if key:
        os.environ["ANTHROPIC_API_KEY"] = key
    else:
        os.environ.pop("ANTHROPIC_API_KEY", None)
    brain._create = create
    return tc4.post("/setup/api/check-brain", json={}).json()


CALLS_B = []
good = brain_check(stub("ок", log=CALLS_B))
check("17zx. ключ работает: ровно ОДИН дешёвый вызов (max_tokens<=16, без tools)",
      good.get("status") == "ok" and len(CALLS_B) == 1 and CALLS_B[0]["max_tokens"] <= 16 and "tools" not in CALLS_B[0])
n_before = len(CALLS_B)
tc4.get("/setup/api/state"); tc4.get("/setup/api/devices"); tc4.get("/setup/api/heard?since=0"); tc4.get("/setup")
check("17zy. живой вызов только по кнопке: state/devices/heard/страница мозг не трогают", len(CALLS_B) == n_before)


async def _raise(e):
    raise e


def raiser(e):
    async def _c(**kw):
        raise e
    return _c


check("17zz1. неверный ключ (401) -> auth", brain_check(raiser(_Err("invalid x-api-key", 401))).get("status") == "auth")
check("17zz2. нет денег -> billing", brain_check(raiser(_Err("Your credit balance is too low", 400))).get("status") == "billing")
check("17zz3. перегрузка (529) -> busy", brain_check(raiser(_Err("overloaded", 529))).get("status") == "busy")
check("17zz4. нет такой модели (404) -> model", brain_check(raiser(_Err("model not found", 404))).get("status") == "model")
check("17zz5. ключа нет в переменных -> nokey и без вызова", brain_check(stub("ок", log=CALLS_B), key="").get("status") == "nokey")
T17[0] += 0.5
os.environ["ANTHROPIC_API_KEY"] = KEYV
check("17zz6. антиспам кнопки: два нажатия подряд -> 429", (lambda: (tc4.post("/setup/api/check-brain", json={}), tc4.post("/setup/api/check-brain", json={}).status_code)[1])() == 429)
os.environ.pop("ANTHROPIC_API_KEY", None)
brain._create = _boom_create

# --- «Готово» и колбэк ---
setupwiz.reset_for_tests()
tcf = _TC(main.app)
lg(tcf)
SENT = []


def cb_handler(request: httpx.Request) -> httpx.Response:
    SENT.append((str(request.url), dict(request.headers), request.content))
    return httpx.Response(200, json={"ok": True})


setupwiz._TRANSPORT = httpx.MockTransport(cb_handler)
r = tcf.post("/setup/api/finish", json={})
check("17zz7. без INSTALL_* колбэка нет (off), «Готово» проходит", r.status_code == 200 and r.json()["callback"] == "off" and not SENT)
check("17zz8. после «Готово» сессия погашена, страница закрыта", tcf.get("/setup/api/state").status_code == 401
      and tcf.get("/setup/api/me").json()["closed"] is True)
check("17zz9. верный код БЕЗ «перенастроить» -> 403 closed", lg(tcf).status_code == 403 and lg(tcf).json()["error"] == "closed")
check("17zza. неверный код с «перенастроить» -> 401", lg(tcf, "wrong", True).status_code == 401)
check("17zzb. код + «перенастроить» -> страница открыта снова", lg(tcf, SC, True).status_code == 200
      and tcf.get("/setup/api/state").status_code == 200 and C.setup_done() is False)

os.environ["INSTALL_CALLBACK_URL"] = "https://callback.example.test/api/install/callback"
os.environ["INSTALL_TOKEN"] = INSTALL_TOK
r = tcf.post("/setup/api/finish", json={})
body = SENT[0][2] if SENT else b""
hdr = {k.lower(): v for k, v in (SENT[0][1] if SENT else {}).items()}
exp_sig = "sha256=" + _hmac.new(INSTALL_TOK.encode(), hdr.get("x-koresh-timestamp", "").encode() + b"." + body, hashlib.sha256).hexdigest()
check("17zzc. колбэк: один POST на заданный адрес, X-Koresh-Timestamp (в пределах 10 минут) и верная подпись HMAC-SHA256 по INSTALL_TOKEN целиком",
      r.json().get("callback") == "sent" and len(SENT) == 1 and SENT[0][0] == os.environ["INSTALL_CALLBACK_URL"]
      and hdr.get("x-koresh-signature") == exp_sig and abs(int(hdr.get("x-koresh-timestamp", "0")) - time.time()) < 600
      and hdr.get("content-type") == "application/json", str(hdr)[:200])
check("17zzc2. подпись совпадает с эталоном signCallback (install.js, посчитан через node: тот же вход -> тот же hex)",
      setupwiz.sign_callback("0123456789abcdef.SECRETpart-XYZ_987", "1790000000",
                             b'{"status":"installed","token":"0123456789abcdef","version":"1.2.3"}')
      == "sha256=a57bffd5ae80debf57994876285a4a435ecf88da2b33a70a22032dd5b3a649d2")
bj = json.loads(body.decode()) if body else {}
check("17zzd. тело колбэка: ровно {token (часть ДО точки), status:installed, version}, секретная часть не уходит",
      set(bj) == {"token", "status", "version"} and bj["status"] == "installed" and bj["token"] == INSTALL_PUB and bj["version"] == C.VERSION
      and "SECRETpart" not in body.decode())
secrets_all = [KEYV, SECRET, LOCKPH, "test-iot-token", SC, "Рустем", "Айгуль", "hz1", "включи свет"]
check("17zze. в теле колбэка нет ни ключей, ни разговоров, ни данных клиента", not any(s in body.decode() for s in secrets_all))
SENT.clear()
lg(tcf, SC, True)


def cb_boom(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("нет связи")


setupwiz._TRANSPORT = httpx.MockTransport(cb_boom)
r = tcf.post("/setup/api/finish", json={})
check("17zzf. падение колбэка не ломает «Готово»: 200, callback=failed, страница закрыта",
      r.status_code == 200 and r.json()["callback"] == "failed" and C.setup_done() is True)
lg(tcf, SC, True)
setupwiz._TRANSPORT = httpx.MockTransport(lambda req: httpx.Response(500))
r = tcf.post("/setup/api/finish", json={})
check("17zzg. колбэк ответил 500 -> failed, «Готово» всё равно прошло", r.status_code == 200 and r.json()["callback"] == "failed" and C.setup_done())
os.environ["INSTALL_CALLBACK_URL"] = "http://evil.example.test/x"
lg(tcf, SC, True)
setupwiz._TRANSPORT = httpx.MockTransport(cb_handler)
SENT.clear()
r = tcf.post("/setup/api/finish", json={})
check("17zzh. колбэк по голому http:// не уходит", r.json()["callback"] == "failed" and not SENT)
for k in ("INSTALL_CALLBACK_URL", "INSTALL_TOKEN"):
    os.environ.pop(k, None)
setupwiz._TRANSPORT = None

# --- system стабилен ---
setupwiz.reset_for_tests()
tcs = _TC(main.app)
lg(tcs, SC, True)
SYS = []
brain._create = stub("ок", log=SYS)
fresh("sy1")
tcs.post("/setup/api/character", json={"name": "Бро", "tone": "derzkiy", "gender": "male"})
for i in range(4):
    run(say("sy1", f"реплика {i}"))
    tcs.get("/setup/api/state"); tcs.get("/setup/api/heard?since=0")
s1 = {json.dumps(k["system"], ensure_ascii=False) for k in SYS}
check("17zzi. system байт-в-байт один в 4 запросах при неизменном конфиге (страница и опросы его не трогают)", len(s1) == 1 and len(SYS) >= 4)
tcs.post("/setup/api/character", json={"name": "Бро", "tone": "vezhlivyy", "gender": "male"})
SYS.clear()
for i in range(3):
    run(say("sy1", f"после смены {i}"))
s2 = {json.dumps(k["system"], ensure_ascii=False) for k in SYS}
check("17zzj. смена характера через /setup даёт новый system, и дальше он снова стабилен", len(s2) == 1 and s2 != s1)
brain._create = _boom_create

# --- сборка: переменные и документы ---
envx3 = (HERE / ".env.example").read_text(encoding="utf-8")
tv3 = json.loads((HERE / "template-variables.json").read_text(encoding="utf-8"))
vmap = {v["name"]: v for v in tv3["variables"]}
setup3 = (HERE / "SETUP.md").read_text(encoding="utf-8")
check("17zzk. .env.example и шаблон Railway: SETUP_CODE, YANDEX_OAUTH_CLIENT_ID, INSTALL_CALLBACK_URL, INSTALL_TOKEN",
      all(n in envx3 and n in vmap for n in ("SETUP_CODE", "YANDEX_OAUTH_CLIENT_ID", "INSTALL_CALLBACK_URL", "INSTALL_TOKEN")))
check("17zzl. SETUP_CODE в шаблоне генерируется само; YANDEX_IOT_TOKEN необязателен",
      "secret(" in vmap["SETUP_CODE"]["default"] and vmap["YANDEX_IOT_TOKEN"]["required"] is False and "/setup" in vmap["YANDEX_IOT_TOKEN"]["description"])
check("17zzm. SETUP.md описывает /setup, SETUP_CODE, колбэк и пометки [ПРОВЕРИТЬ]",
      "/setup" in setup3 and "SETUP_CODE" in setup3 and "INSTALL_CALLBACK_URL" in setup3 and "X-Koresh-Signature" in setup3 and "[ПРОВЕРИТЬ]" in setup3)
check("17zzn. Dockerfile копирует setupwiz.py и setupwiz.html", "setupwiz.html" in (HERE / "Dockerfile").read_text(encoding="utf-8"))
check("17zzo. на странице есть пометки [ПРОВЕРИТЬ] для непроверенных мест интерфейса Яндекса", html17.count("chk()") >= 4 and "ПРОВЕРИТЬ" in html17)

# --- журнал без секретов ---
sys.stdout = _tee.real
logtxt = _tee.buf.getvalue()
leaks = [n for n, s in (("код установки", SC), ("ключ Anthropic", KEYV), ("секрет вебхука", SECRET), ("кодовая фраза", LOCKPH),
                        ("токен умного дома", "test-iot-token"), ("токен установки", INSTALL_TOK), ("секрет установки", "SECRETpart"), ("токен env", "env-token-should-win"))
         if s in logtxt]
check("17zzp. в журнале (весь вывод раздела) нет кода, ключей, токенов, секрета вебхука и кодовой фразы", not leaks, str(leaks))
check("17zzq. журнал не пуст: события настройки логируются (имена, без значений)", "config: сохранено" in logtxt and "setup: неверный код" in logtxt)

# уборка: следующие разделы живут как раньше
for k in ("SETUP_CODE", "ANTHROPIC_API_KEY", "HOLD_TIMEOUT", "INSTALL_CALLBACK_URL", "INSTALL_TOKEN", "WEBHOOK_SECRET"):
    os.environ.pop(k, None)
os.environ["MEMORY_DIR"] = MEMDIR
os.environ["YANDEX_IOT_TOKEN"] = "test-iot-token"
setupwiz._now = time.time
setupwiz.reset_for_tests()
setupwiz._TRANSPORT = None
C._MEM_ONLY.clear()
reset_all()

# ================= 18. честность о действиях: «включил» без успешного вызова подменяется =================
import honesty  # noqa: E402

_YANDEX_OK = iot._TRANSPORT


def scripted(*steps):
    """Модель по сценарию: каждый вызов отдаёт следующий ответ."""
    it = iter(steps)

    async def _c(**kw):
        return next(it)
    return _c


def hon_handler(mode):
    def _h(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/user/info"):
            return httpx.Response(200, json={"status": "ok", **USER_INFO})
        CALLS.append((request.method, request.url.path, None))
        if mode == "unreachable":
            return httpx.Response(200, json={"status": "ok", "devices": [{"id": "lamp-bed", "capabilities": [
                {"type": "devices.capabilities.on_off", "state": {"instance": "on", "action_result": {
                    "status": "ERROR", "error_code": "DEVICE_UNREACHABLE"}}}]}]})
        if mode == "http500":
            return httpx.Response(500, text="boom")
        if mode == "token":
            return httpx.Response(401, json={"status": "error"})
        if mode == "raise":
            raise httpx.ConnectError("нет связи")
        return httpx.Response(200, json={"status": "ok"})
    return _h


def lamp_turn(final_text, mode="ok", second_control=False):
    steps = [tool_resp("h1", "list_devices", {}),
             tool_resp("h2", "control_device", {"target": "device", "target_id": "lamp-bed",
                                                "capability": "devices.capabilities.on_off", "instance": "on", "value": True})]
    if second_control:
        steps.append(tool_resp("h3", "control_device", {"target": "device", "target_id": "lock1",
                                                        "capability": "devices.capabilities.on_off", "instance": "on", "value": True}))
    steps.append(text_resp(final_text))
    return steps


def think_once(steps, mode="ok", uid=None):
    keep = {k: os.environ.get(k) for k in ("GENDER", "TONE")}
    reset_all()
    for k, v in keep.items():
        if v is not None:
            os.environ[k] = v
    os.environ["YANDEX_IOT_TOKEN"] = "test-iot-token"
    iot._TRANSPORT = httpx.MockTransport(hon_handler(mode))
    brain._create = scripted(*steps)
    hist = [{"role": "user", "content": "включи свет в спальне"}]
    out = run(brain.think(hist, uid))
    iot._TRANSPORT = _YANDEX_OK
    return out, hist


ALL_BAD = True
for g, claim_txt, did_not in (("male", "Включил свет в спальне.", "не сделал"), ("female", "Включила свет в спальне.", "не сделала")):
    for t in C.TONES:
        os.environ["GENDER"] = g
        os.environ["TONE"] = t
        # а) инструмент не вызывался
        out, hist = think_once([text_resp(claim_txt)])
        ok_a = (out != claim_txt and "лючил" not in out and did_not in out and "не дошла" in out
                and not any(c[0] == "POST" for c in CALLS))
        if g == "male":
            ok_a = ok_a and "не сделала" not in out
        else:
            ok_a = ok_a and not re.search(r"не сделал(?!а)", out)
        check(f"18a. {t}/{g}: «{claim_txt[:8]}» без вызова -> честная замена в своём роде", ok_a, out)
        # история тоже честная
        last = hist[-1]
        check(f"18a2. {t}/{g}: в истории вранья нет", last["role"] == "assistant"
              and all("лючил" not in (b.get("text", "") if isinstance(b, dict) else getattr(b, "text", "")) for b in last["content"]))
        # б) успех
        out, _ = think_once(lamp_turn(claim_txt))
        check(f"18b. {t}/{g}: успешный iot -> текст как есть", out == claim_txt and len(post_calls()) == 1, out)
        # в) ошибка устройства
        for mode, why in (("unreachable", "устройство не на связи"), ("http500", "Яндекс ответил ошибкой 500"),
                          ("token", "токен умного дома не принят"), ("raise", "умный дом не ответил")):
            out, _ = think_once(lamp_turn(claim_txt), mode)
            check(f"18c. {t}/{g}/{mode}: ошибка iot -> «не вышло» с причиной", why in out and "лючил" not in out
                  and out.startswith(("Не вышло", "Хуйня, не вышло", "Не получилось")), out)
os.environ.pop("GENDER", None)
os.environ["TONE"] = "derzkiy"

# замок: отказ сервера -> причина про настройки
out, _ = think_once([tool_resp("k1", "control_device", {"target": "device", "target_id": "lock1",
                                                          "capability": "devices.capabilities.on_off", "instance": "on", "value": True}),
                     text_resp("Открыл замок.")], uid=None)
check("18d. замок отключён, а модель врёт «открыл» -> причина «отключено в настройках»",
      "отключено в настройках" in out and "Открыл" not in out, out)
# смешанный ход: лампа ок, замок отказал, модель отчиталась за всё
out, _ = think_once(lamp_turn("Включил всё.", second_control=True))
check("18e. часть вышла, часть нет, модель врёт про всё -> «получилось не всё» с причиной",
      out.startswith("Получилось не всё") and "отключено" in out, out)
out, _ = think_once(lamp_turn("Лампу включил, а замок не открыл: он отключён в настройках.", second_control=True))
check("18f. часть вышла, модель сама честно сказала про неудачу -> текст как есть",
      out == "Лампу включил, а замок не открыл: он отключён в настройках.", out)

# «Готово» после remember: память записана, это правда; без ничего -- враньё
out, _ = think_once([tool_resp("m1", "remember", {"fact": "жену зовут Айгуль"}), text_resp("Готово.")], uid="hon-u1")
check("18g. «Готово» после успешного remember -> как есть", out == "Готово.", out)
out, _ = think_once([text_resp("Готово!")])
check("18h. «Готово!» без единого действия -> замена", out != "Готово!" and "не сделал" in out, out)
out, _ = think_once([Resp([], "end_turn")])
check("18i. пустой ответ модели без действий (старое «Готово.») -> замена", out != "Готово." and "не сделал" in out, out)
out, _ = think_once(lamp_turn("") if False else [tool_resp("h1", "list_devices", {}),
                    tool_resp("h2", "control_device", {"target": "device", "target_id": "lamp-bed", "capability": "devices.capabilities.on_off",
                                                       "instance": "on", "value": True}), Resp([], "end_turn")])
check("18j. пустой финал после успешного iot -> «Готово.» как раньше", out == "Готово.", out)

# список глаголов: каждый ловится без вызова, и не ловятся отрицания и не-утверждения
CLAIMS = ["Включил свет", "Выключила свет", "Вырубил лампу", "Врубила телек", "Зажёг свет", "Зажгла свет", "Погасил", "Потушила",
          "Поставил таймер на пять минут", "Установила яркость", "Запустил сценарий", "Запустила кино", "Сделал", "Сделала.",
          "Открыл ворота", "Закрыла ставни", "Добавил в список", "Убавил звук", "Прибавила свет", "Сменил режим",
          "Отправил сообщение жене", "Позвонила маме", "Заказал пиццу", "Готово.", "Всё готово!", "Сделано.", "Да, уже готово.",
          "Хорошо, свет включил."]
NOT_CLAIMS = ["Не включил: замки отключены.", "Я не выключила, нет такой лампы.", "Включаю, секунду.", "Могу включить, скажи какую.",
              "Лампа сейчас включена.", "Три минуты вари.", "Не сделала: нужна кодовая фраза.", "Ни хрена не включил, Яндекс молчит.",
              "Какую комнату включить?", "Готов к работе."]
miss = [c for c in CLAIMS if not honesty.claims_action(c)]
check("18k. все глаголы-утверждения (оба рода) ловятся", not miss, str(miss))
false_pos = [c for c in NOT_CLAIMS if honesty.claims_action(c)]
check("18l. отрицания, будущее, состояние и вопросы не ловятся", not false_pos, str(false_pos))
bad = []
for c in CLAIMS:
    out, _ = think_once([text_resp(c)])
    if out == c:
        bad.append(c)
check("18m. каждое утверждение без вызова реально заменяется ходом мозга", not bad, str(bad))
bad = []
for c in NOT_CLAIMS:
    out, _ = think_once([text_resp(c)])
    if out != c:
        bad.append(c)
check("18n. не-утверждения проходят ходом мозга без изменений", not bad, str(bad))

# честные фразы: тон, род, мат только в derzkiy_mat
for t in C.TONES:
    for g in ("male", "female"):
        for kind in ("fail", "none", "partial"):
            ph = prompts.honest(kind, "причина", t, g)
            good = "причина" in ph and (("не сделала" in ph) if g == "female" and kind != "partial" else "не сделала" not in ph)
            if t != "derzkiy_mat":
                good = good and not MAT_RE.search(ph)
            check(f"18o. honest {t}/{g}/{kind}: причина, род, мат только где положено", good, ph)
check("18p. derzkiy_mat: в честной фразе мат есть, он тут к месту", bool(MAT_RE.search(prompts.honest("fail", "x", "derzkiy_mat", "male"))))

# системный промпт: правило есть, в своём роде, и без утечек
for g, must in (("male", "«включил»"), ("female", "«включила»")):
    sp = prompts.system_prompt(tone="derzkiy", gender=g)
    check(f"18q. промпт {g}: правило «говори только правду о сделанном» в своём роде", "Про сделанное говори только правду" in sp and must in sp)

# через разговор целиком: ответ Алисе тоже честный
reset_all()
os.environ["YANDEX_IOT_TOKEN"] = "test-iot-token"
brain._create = scripted(text_resp("Включил свет в спальне."))
fresh("hon-m")
r = run(main.handle("hon-m", "включи свет в спальне", False))
check("18r. ответ Алисе тоже честный (main.handle)", r["response"]["text"] != "Включил свет в спальне." and "не сделал" in r["response"]["text"], repr(r))
st = main.STATE["hon-m"]
check("18s. в истории диалога у пользователя вранья нет", all("Включил свет" not in json.dumps(
    [getattr(b, "text", b.get("text", "") if isinstance(b, dict) else "") for b in m["content"]], ensure_ascii=False)
    for m in st.dialog if m["role"] == "assistant" and isinstance(m["content"], list)))
reset_all()
os.environ["YANDEX_IOT_TOKEN"] = "test-iot-token"


# ================= 19. страница /setup: ссылки, «скопировать», голос, токен, названия (настоящая отрисовка в node) =================
import shutil  # noqa: E402
import subprocess  # noqa: E402

DOM_JS = r"""
const fs = require('fs'), vm = require('vm');
const html = fs.readFileSync(process.argv[2], 'utf8');
const scen = JSON.parse(process.argv[3]);
const js = html.split('<script>')[1].split('</script>')[0];
class Txt { constructor(t) { this.nodeType = 3; this.text = String(t); } }
class Element {
  constructor(tag) { this.tag = tag; this.attrs = {}; this.children = []; this.handlers = {}; this.nodeType = 1; this.value = ''; this.disabled = false; this.classList = {toggle() {}, add() {}, remove() {}}; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  set className(v) { this.attrs['class'] = v; }
  get className() { return this.attrs['class'] || ''; }
  append(...k) { for (const c of k) this.children.push(typeof c === 'string' ? new Txt(c) : c); }
  replaceChildren(...k) { this.children = []; this.append(...k); }
  addEventListener(ev, fn) { (this.handlers[ev] = this.handlers[ev] || []).push(fn); }
  set textContent(v) { this.children = [new Txt(v)]; }
  remove() {} focus() {} select() {} setSelectionRange() {}
}
const reg = {};
const COPIED = [];
const TIMERS = [];
const FETCHLOG = [];
let CUR = null;
const ctx = {
  Element, console, Promise, JSON, Object, Array, String, Number, RegExp, encodeURIComponent, Math, Date, setTimeout,
  document: {createElement: t => new Element(t), createTextNode: t => new Txt(t), getElementById: id => (reg[id] = reg[id] || new Element('div')),
             body: new Element('body'), execCommand: () => false},
  window: {scrollTo() {}, getSelection: () => ({removeAllRanges() {}, addRange() {}})},
  navigator: {clipboard: {writeText: t => { COPIED.push(t); return Promise.resolve(); }}},
  fetch: async (url, opt) => { const f = (CUR && CUR.fetch) || {}; const key = String(url).replace('/setup/api/', '').split('?')[0];
                          FETCHLOG.push({k: key, body: opt && opt.body ? JSON.parse(opt.body) : null});
                          const r = key in f ? f[key] : {authed: false}; const bad = r.__status || 200;
                          return {status: bad, ok: bad < 400, json: async () => r}; },
  confirm: () => true, setInterval: (fn) => { TIMERS.push(fn); return TIMERS.length; }, clearInterval: (id) => { TIMERS[id - 1] = null; },
  Range: class {}, 
};
ctx.document.createRange = () => ({selectNodeContents() {}});
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(js + "\n;globalThis.__T = {setS(v) { S = v; }, stepIot, stepAlice, stepCheck, renderLogin, stepWa, renderWaOnly};", ctx);
function ser(n) { return n.nodeType === 3 ? n.text : {t: n.tag, a: n.attrs, v: n.value, c: n.children.map(ser)}; }
function walk(n, f) { f(n); if (n.nodeType === 1) n.children.forEach(c => walk(c, f)); }
(async () => {
  const out = [];
  for (const sc of scen) {
    ctx.__T.setS(sc.S);
    COPIED.length = 0;
    TIMERS.length = 0;
    FETCHLOG.length = 0;
    CUR = sc;
    let tree = ctx.__T[sc.fn](...(sc.args || []));
    if (!tree) tree = reg['app'];
    const tick0 = () => new Promise(r => setTimeout(r, 0));
    const textOf = n => n.nodeType === 3 ? n.text : n.children.map(textOf).join('');
    await tick0();
    for (const st of (sc.seq || [])) {
      if (st.click) {
        const found = [];
        walk(st.root === 'app' ? reg['app'] : tree, n => { if (n.nodeType === 1 && n.tag === 'button' && textOf(n) === st.click) found.push(n); });
        for (const b of found) for (const fn of (b.handlers.click || [])) await fn();
      }
      if (st.input) { const f = []; walk(tree, n => { if (n.nodeType === 1 && n.tag === 'input' && n.attrs['aria-label'] === st.input) f.push(n); });
        for (const n of f) { n.value = st.value; for (const fn of (n.handlers.input || [])) await fn(); } }
      if (st.checkAll) walk(tree, n => { if (n.nodeType === 1 && n.tag === 'input' && n.attrs.type === 'checkbox') n.checked = true; });
      if (st.fire) for (let i = 0; i < st.fire; i++) { for (const t of TIMERS.slice()) if (t) await t(); await tick0(); }
      await tick0();
    }
    const clicks = [];
    walk(tree, n => { if (n.nodeType === 1 && n.tag === 'button' && (n.attrs['class'] || '').includes('sm')) clicks.push(n); });
    for (const b of clicks) for (const fn of (b.handlers.click || [])) await fn();
    out.push({tree: ser(tree), copied: COPIED.slice(), app: ser(reg['app']), calls: FETCHLOG.slice()});
  }
  console.log(JSON.stringify(out));
})();
"""


def _walk(n, f):
    f(n)
    if isinstance(n, dict):
        for c in n["c"]:
            _walk(c, f)


def _text(n):
    if isinstance(n, str):
        return n
    return "".join(_text(c) for c in n["c"])


def _nodes(tree, tag):
    res = []
    _walk(tree, lambda n: res.append(n) if isinstance(n, dict) and n["t"] == tag else None)
    return res


def _S(gender="male", name="Кореш", cid=""):
    return {"name": {"value": name, "source": "default"}, "gender": {"value": gender, "source": "default"}, "yandex_client_id": cid,
            "iot": {"set": False, "source": ""}, "webhook": {"secret_set": True, "url": "https://k.up.railway.app/alice/SECRETXYZ"},
            "tone": {"value": "derzkiy", "source": "default"}}


wiz_src = (HERE / "setupwiz.html").read_text(encoding="utf-8")
check("19a. в странице нет ложного «Не совпадает» (голос шага 5 больше не врёт)", "Не совпадает" not in wiz_src and "не совпадает" not in wiz_src)
check("19b. поле токена не type=password (браузер не подставляет сохранённый пароль)", "{type: 'password', class: 'mask'" not in wiz_src
      and "name: 'koresh-yandex-token'" in wiz_src)
check("19c. кнопки и ссылки >= 44px, длинные значения переносятся (мобилка 390)",
      re.search(r"\.btn\{[^}]*min-height:44px", wiz_src) and not re.search(r"\.btn\.sm\{[^}]*min-height", wiz_src)
      and "overflow-wrap:anywhere" in wiz_src and re.search(r"\.val\{[^}]*flex-wrap:wrap", wiz_src))
if not shutil.which("node"):
    print("SKIP  19d-19o. node не найден: проверки отрисовки страницы пропущены (в CI node есть)")
else:
    scen = [("stepIot", _S(cid="")), ("stepIot", _S(cid="0123456789abcdef0123456789abcdef")),
            ("stepAlice", _S("male")), ("stepAlice", _S("female")), ("stepAlice", _S("male", "Bro")),
            ("stepAlice", _S("male", "Бро")), ("stepAlice", _S("female", "Кореш Дома")), ("stepCheck", _S("male", "Бро"))]
    _jsf = Path(tempfile.mkdtemp()) / "dom.js"
    _jsf.write_text(DOM_JS, encoding="utf-8")
    proc = subprocess.run(["node", str(_jsf), str(HERE / "setupwiz.html"), json.dumps([{"fn": f, "S": s} for f, s in scen], ensure_ascii=False)],
                          capture_output=True, text=True, timeout=60)
    ok_run = proc.returncode == 0 and proc.stdout.strip().startswith("[")
    check("19d. страница отрисовывается в node (шаги 2, 5, 6)", ok_run, (proc.stderr or proc.stdout)[:300])
    if ok_run:
        R = json.loads(proc.stdout.strip().splitlines()[-1])
        own, shared, al_m, al_f, al_lat, al_ru, al_multi, chk6 = R
        # ссылки
        def anchors(r):
            return [n for n in _nodes(r["tree"], "a")]
        allA = [a for r in R for a in anchors(r)]
        check("19e. ВСЕ ссылки на странице открываются в новой вкладке и с noopener", allA and all(
            a["a"].get("target") == "_blank" and "noopener" in a["a"].get("rel", "") for a in allA), str([a["a"] for a in allA][:3]))
        hrefs_own = {a["a"]["href"] for a in anchors(own)}
        check("19f. шаг 2 (своё приложение): oauth.yandex.ru/client/new - кнопка-ссылка", "https://oauth.yandex.ru/client/new" in hrefs_own)
        hrefs_al = {a["a"]["href"] for a in anchors(al_m)}
        check("19g. шаг 5: dialogs.yandex.ru/developer - кнопка-ссылка", "https://dialogs.yandex.ru/developer" in hrefs_al)
        loose = []
        for r in (own, shared, al_m):
            def f(n):
                if isinstance(n, dict) and n["t"] in ("span", "b") and re.search(r"oauth\.yandex\.ru/client/new|dialogs\.yandex\.ru/developer", _text(n)):
                    loose.append(_text(n))
            _walk(r["tree"], f)
        check("19h. адреса не остались голым текстом (нет mono-спанов с этими адресами без ссылки)", not loose, str(loose))
        sh_href = {a["a"]["href"] for a in anchors(shared)}
        check("19i. общее приложение: кнопка «Войти через Яндекс» ведёт на authorize с ClientID",
              any(h.startswith("https://oauth.yandex.ru/authorize?response_type=token&client_id=01234567") for h in sh_href))
        # «скопировать»
        check("19j. шаг 2: скопировать можно название «Кореш», Redirect URI, iot:view и iot:control (кнопки нажаты, буфер получил значения)",
              {"Кореш", "https://oauth.yandex.ru/verification_code", "iot:view", "iot:control"} <= set(own["copied"]), str(own["copied"]))
        check("19k. шаг 5: скопировать можно название «Кореш» и активационное имя «кореш дома»",
              {"Кореш", "кореш дома"} <= set(al_m["copied"]), str(al_m["copied"]))
        # интерфейс Яндекса
        ot = _text(own["tree"])
        check("19l. шаг 2: «Дополнительные → Название доступа → iot:view, iot:control», «в Основных ничего не отмечать», Госуслуги",
              all(x in ot for x in ("Дополнительные", "Название доступа", "iot:view", "iot:control", "Основные", "Госуслуг")))
        check("19m. предупреждение про Госуслуги есть и у общего приложения", "Госуслуг" in _text(shared["tree"]))
        # токен
        tok = [n for n in _nodes(own["tree"], "input")]
        # поле токена живёт в stepIot: ищем по placeholder
        tk = [n for n in tok if "y0_" in n["a"].get("placeholder", "")]
        check("19n. поле токена: не password, autocomplete=off, подсказка про y0_, пусто при показе",
              len(tk) == 1 and tk[0]["a"].get("type") != "password" and tk[0]["a"].get("autocomplete") == "off" and tk[0]["v"] == "", str(tk))
        # голос шага 5
        tm, tf = _text(al_m["tree"]), _text(al_f["tree"])
        check("19o. шаг 5: male = мужской нейроголос, female = голос Алисы; ложного «Не совпадает» нет ни там, ни там",
              "Не совпадает" not in tm + tf and "нейроголос" in tm and "голос Алисы" in tf and "(сейчас там мужской)" in tm and "(сейчас там женский)" in tf)
        # названия
        def names(r):
            t = _text(r["tree"])
            return t
        check("19p. название навыка по-русски «Кореш», активация двумя словами «кореш дома» (ASSISTANT_NAME по умолчанию)",
              "Название навыка (по-русски, не латиницей): Кореш" in names(al_m) and "кореш дома" in al_m["copied"])
        check("19q. имя «Бро» -> «бро дома»; латиница «Bro» -> «Кореш» / «кореш дома»; «Кореш Дома» остаётся двумя словами",
              "бро дома" in al_ru["copied"] and "Бро" in al_ru["copied"]
              and "кореш дома" in al_lat["copied"] and "Кореш" in al_lat["copied"] and "Bro" not in al_lat["copied"]
              and "кореш дома" in al_multi["copied"] and "кореш дома дома" not in al_multi["copied"], str((al_ru["copied"], al_lat["copied"], al_multi["copied"])))
        check("19r. [ПРОВЕРИТЬ правила Яндекса] у активационного имени", "ПРОВЕРИТЬ правила Яндекса" in names(al_m))
        check("19s. шаг 6 подсказывает активационное имя, а не просто имя", "запусти навык бро дома" in _text(chk6["tree"]), _text(chk6["tree"])[:200])


# ================= 20. Активация по коду: персональный пакет (сервер лицензий замокан) =================
import activation  # noqa: E402
import contextlib  # noqa: E402
import io  # noqa: E402
import stat  # noqa: E402

_ENV20 = ("YANDEX_IOT_TOKEN", "SETUP_CODE", "MEMORY_DIR", "ASSISTANT_NAME", "TONE", "GENDER", "HOUSEHOLD", "BLOCKED_NAMES",
          "WEBHOOK_SECRET", "USAGE_FILE", "ANTHROPIC_API_KEY")
_SAVED20 = {k: os.environ.get(k) for k in _ENV20}
_DIR20 = tempfile.mkdtemp()
os.environ["MEMORY_DIR"] = _DIR20
os.environ["USAGE_FILE"] = os.path.join(_DIR20, "usage.json")
for _k in ("YANDEX_IOT_TOKEN", "ASSISTANT_NAME", "TONE", "GENDER", "HOUSEHOLD", "BLOCKED_NAMES"):
    os.environ.pop(_k, None)
os.environ["SETUP_CODE"] = "act-setup-code-20"
os.environ["WEBHOOK_SECRET"] = "act-hook-secret-20"
os.environ["ANTHROPIC_API_KEY"] = "sk-test-act"
activation.reset_for_tests()

SECRETS20 = ["Мурзик", "Алексей", "Алматы", "Борис"]       # поля пакета: должны быть только в зашифрованном виде и в оперативке
PACK20 = {"schema": 1, "version": 1, "ASSISTANT_NAME": "Борис", "TONE": "derzkiy_mat", "GENDER": "male", "owner": "Алексей",
          "devices": ["light", "locks"], "city": "Алматы", "people": {}, "rooms": {}, "blocked": [],
          "notes": "Город хозяина: Алматы. В доме есть: свет, замок или ворота. От хозяина: у нас кот Мурзик."}
GOOD_CODE = "KR-ABCD-EFGH-JKMN"
YID_A, YID_B = "1000000001", "2000000002"
IOT_TOK = "test-iot-token"


class FakeLicense:
    """Повторяет протокол сервиса лицензий: start (адрес сервера клиента + секрет обмена) -> ссылка входа на сам сервис лицензий ->
    (браузер: вход Яндексом, «Это ваш сервер?» -> Да = complete) -> exchange только с секретом; refresh по токену обновления."""

    def __init__(self):
        self.bound = None
        self.installs = []
        self.handles = {}                       # handle -> {install_id, state, yid, secret, domain}
        self.login_prefix = None                # подмена адреса ссылки входа (проверка: клиент не пойдёт на чужой адрес)
        self.version = 1
        self.pack = dict(PACK20)
        self.revoked = False
        self.down = False
        self.requests = []
        self.tokens = {}                        # install_id -> токен обновления

    def complete(self, handle, yid, now=None):
        h = self.handles[handle]
        if self.bound and self.bound != yid:
            h["state"] = ("fail", "wrong_account", "Этот код уже привязан к другому аккаунту Яндекса. Напишите в поддержку.")
        elif h["install_id"] not in self.installs and len(self.installs) >= 3:
            h["state"] = ("fail", "install_limit", "Для этого кода исчерпан лимит переустановок. Напишите в поддержку.")
        else:
            self.bound = yid
            if h["install_id"] not in self.installs:
                self.installs.append(h["install_id"])
            h["state"] = ("done", yid)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("сервер лицензий лежит")
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        self.requests.append((path, body, dict(request.headers)))
        if path == "/v1/install/start":
            if body.get("code") != GOOD_CODE:
                return httpx.Response(404, json={"ok": False, "error": "bad_code", "message": "Код не подошёл. Проверьте, что вы ввели его без ошибок."})
            if not body.get("server_domain") or len(body.get("exchange_secret") or "") < 32:
                return httpx.Response(400, json={"ok": False, "error": "bad_domain", "message": "нужен адрес сервера и секрет обмена"})
            hd = "h" * 63 + str(len(self.handles))
            self.handles[hd] = {"install_id": body["install_id"], "state": ("wait",),
                                "secret": body["exchange_secret"], "domain": body["server_domain"]}
            prefix = self.login_prefix or "https://license.example.test"
            return httpx.Response(200, json={"ok": True, "handle": hd, "expires_in": 600, "login_url": prefix + "/yandex/start?handle=" + hd})
        if path == "/v1/code/check":
            # как настоящий: ок, если код есть и либо ещё не активирован, либо активирован именно этой установкой; иначе ОДИН отказ на всё
            ok = body.get("code") == GOOD_CODE and not self.revoked and (self.bound is None or body.get("install_id") in self.installs)
            if ok:
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(404, json={"ok": False, "error": "bad_code", "message": "Код не подошёл. Проверьте, что вы ввели его без ошибок."})
        if path == "/v1/install/exchange":
            h = self.handles.get(body.get("handle"))
            if not h:
                return httpx.Response(404, json={"ok": False, "error": "bad_state", "message": "ссылка недействительна"})
            if body.get("exchange_secret") != h["secret"] or body.get("install_id") != h["install_id"]:
                return httpx.Response(403, json={"ok": False, "error": "bad_install", "message": "Это не ваша установка."})
            st = h["state"]
            if st[0] == "wait":
                return httpx.Response(202, json={"ok": True, "status": "pending"})
            if st[0] == "fail":
                return httpx.Response(403, json={"ok": False, "error": st[1], "message": st[2]})
            if st[0] == "used":
                return httpx.Response(410, json={"ok": False, "error": "used", "message": "ссылка использована"})
            h["state"] = ("used",)
            self.tokens[h["install_id"]] = "rt-" + "r" * 30 + h["install_id"][-6:]
            return httpx.Response(200, json={"ok": True, "status": "ok", "pack": self.pack, "pack_version": self.version,
                                             "seed": "seed-" + "s" * 40, "yandex_id": st[1], "refresh_token": self.tokens[h["install_id"]],
                                             "yandex_token": IOT_TOK})
        if path == "/v1/pack/refresh":
            if request.headers.get("authorization") != f"Bearer {self.tokens.get(body.get('install_id'))}":
                return httpx.Response(401, json={"ok": False, "error": "unauthorized"})
            if self.revoked:
                return httpx.Response(410, json={"ok": False, "status": "revoked"})
            if self.version <= body.get("since", 0):
                return httpx.Response(200, json={"ok": True, "status": "same", "pack_version": self.version})
            return httpx.Response(200, json={"ok": True, "status": "update", "pack": self.pack, "pack_version": self.version})
        return httpx.Response(404)


def _logged_in(c):
    r = c.post("/setup/api/login", json={"code": os.environ["SETUP_CODE"], "reopen": True})
    assert r.status_code == 200, r.text


def _read_all_files(d):
    out = {}
    for f in Path(d).iterdir():
        if f.is_file():
            out[f.name] = f.read_bytes()
    return out


BASE_PROMPT20 = prompts.system_prompt()
LIC = FakeLicense()
activation.LICENSE_URL = "https://license.example.test"
activation._TRANSPORT = httpx.MockTransport(LIC)
setupwiz._TRANSPORT = httpx.MockTransport(yandex_handler)
setupwiz.reset_for_tests()
tc20 = _TC(main.app)
_logged_in(tc20)
LOG20 = io.StringIO()

# --- 1. полный путь: код -> вход Яндексом -> пакет на Volume ---
with contextlib.redirect_stdout(LOG20):
    r_bad = tc20.post("/setup/api/license/start", json={"code": "KR-WRONG-WRONG-WRNG"})
    pack_after_bad = C.pack_active()
    r_start = tc20.post("/setup/api/license/start", json={"code": GOOD_CODE})
    hd20 = next(iter(LIC.handles))
    r_pend = tc20.get("/setup/api/license/status")
    pack_while_pending = C.pack_active()
    LIC.complete(hd20, YID_A)
    r_done = tc20.get("/setup/api/license/status")
    r_again = tc20.get("/setup/api/license/status")
check("20a. неверный код: ответ с понятным текстом, пакета нет", r_bad.status_code == 400 and "не подошёл" in r_bad.json()["message"] and not pack_after_bad)
check("20b. верный код: ссылка входа ведёт на сервер лицензий (там кука и «Это ваш сервер?»), handle отдельно клиенту не отдаётся",
      r_start.status_code == 200 and r_start.json()["authorize_url"] == "https://license.example.test/yandex/start?handle=" + hd20
      and hd20 not in r_start.text.replace(r_start.json()["authorize_url"], ""))
check("20c. пока человек не вошёл — pending, пакета нет", r_pend.json()["status"] == "pending" and not pack_while_pending)
check("20d. после входа: пакет принят, токен умного дома проверен и сохранён, устройства видны",
      r_done.json()["status"] == "ok" and r_done.json()["saved"] is True and r_done.json()["iot"] is True
      and r_done.json()["devices"]["count"] == 4 and C.stored("YANDEX_IOT_TOKEN") == IOT_TOK, r_done.text[:200])
check("20e. обмен одноразовый: повторный опрос ничего не отдаёт", r_again.json()["status"] == "none")
files20 = _read_all_files(_DIR20)
check("20f. на Volume лежат pack.enc и license.json с правами 600",
      {"pack.enc", "license.json", "config.json"} <= set(files20)
      and all(stat.S_IMODE(os.stat(os.path.join(_DIR20, n)).st_mode) == 0o600 for n in ("pack.enc", "license.json", "config.json")))
check("20g. pack.enc зашифрован: ни одно поле пакета открытым текстом не найдено",
      not any(s.encode() in files20["pack.enc"] for s in SECRETS20) and files20["pack.enc"].startswith(b"KP1"))
check("20h. в license.json и config.json полей пакета нет",
      not any(s.encode() in files20["license.json"] + files20["config.json"] for s in SECRETS20))
check("20i. в Variables (os.environ) полей пакета нет", not any(s in " ".join(os.environ.values()) for s in SECRETS20))
check("20j. в журнале (stdout всего потока активации) полей пакета, кода, токенов нет",
      not any(s in LOG20.getvalue() for s in SECRETS20 + [GOOD_CODE, IOT_TOK, "seed-", "rt-rrrr", hd20]), LOG20.getvalue()[:300])
leak_sources = {
    "health": tc20.get("/health").text, "state": tc20.get("/setup/api/state").text,
    "license_start": r_start.text, "license_status": r_done.text + r_pend.text,
}
check("20k. /health, /setup/api/state и ответы активации не содержат заметок, города, хозяина, кода и ключей",
      not any(s in t for t in leak_sources.values() for s in ["Мурзик", "Алексей", "Алматы", GOOD_CODE, "seed-", "rt-rrrr"]),
      str({k: [s for s in ["Мурзик", "Алексей", "Алматы"] if s in t] for k, t in leak_sources.items()}))
check("20l. /health при пакете не светит род, а тон и его источник показывает (диагностика)", "gender" not in tc20.get("/health").json()
      and "tone" in tc20.get("/health").json() and "tone_source" in tc20.get("/health").json())
sent_bodies = [b for p, b, h in LIC.requests if p != "/v1/pack/refresh"]
check("20m. клиент шлёт серверу только код/handle/install_id, адрес сервера и секрет обмена: никакого Yandex ID от клиента",
      all(set(b) <= {"code", "install_id", "handle", "server_domain", "exchange_secret"} for b in sent_bodies) and all(YID_A not in json.dumps(b) for b in sent_bodies))
_starts20 = [b for p, b, h in LIC.requests if p == "/v1/install/start" and b.get("code") == GOOD_CODE]
_exch20 = [b for p, b, h in LIC.requests if p == "/v1/install/exchange"]
SECRET20 = _starts20[0]["exchange_secret"]
check("20ma. при старте уходят адрес ЭТОГО сервера и случайный секрет обмена (>=32 символов); обмен идёт с тем же секретом",
      _starts20[0]["server_domain"] == "testserver" and len(SECRET20) >= 32 and all(b.get("exchange_secret") == SECRET20 for b in _exch20) and len(_exch20) >= 2)
check("20mb. секрет обмена нигде не светится: не в ссылке входа, не в ответах /setup, не в /health, не в журнале, не на диске",
      SECRET20 not in r_start.text + r_done.text + r_pend.text + tc20.get("/health").text + tc20.get("/setup/api/state").text
      and SECRET20 not in LOG20.getvalue() and not any(SECRET20.encode() in b for b in files20.values()))

# --- 2. пакет работает в промпте и в настоящем запросе к мозгу ---
p20 = prompts.system_prompt()
check("20n. пакет в промпте: имя, хозяин, заметки; тон «с матом» из пакета", "Борис" in p20 and "Алексей" in p20 and "Мурзик" in p20 and "мат" in p20.lower())
check("20o. промпт стабилен: два вызова подряд побайтно равны", prompts.system_prompt() == p20)
check("20p. источник значений = pack, пока нет переменной Railway",
      C.source("ASSISTANT_NAME") == "pack" and C.assistant_name() == "Борис" and C.tone() == "derzkiy_mat")
os.environ["ASSISTANT_NAME"] = "Гоша"
check("20q. переменная Railway главнее пакета", C.assistant_name() == "Гоша" and C.source("ASSISTANT_NAME") == "env")
os.environ.pop("ASSISTANT_NAME")
C.save_values({"ASSISTANT_NAME": "Конфиг", "TONE": "vezhlivyy"})
check("20r. ручной выбор на /setup (config.json) главнее пакета", C.assistant_name() == "Конфиг" and C.tone() == "vezhlivyy" and C.source("TONE") == "setup")
C.save_values({"ASSISTANT_NAME": None, "TONE": None})
SYS20 = []
brain._create = stub("ок", log=SYS20)
fresh("act-u1")
run(say("act-u1", "привет"))
sys_sent = json.dumps(SYS20[0].get("system"), ensure_ascii=False) if SYS20 else ""
check("20s. пакет доезжает до Anthropic: в system реального запроса есть заметки и имя", "Мурзик" in sys_sent and "Борис" in sys_sent)

# --- 3. нет пакета / битый пакет -> обычный публичный Кореш ---
activation.reset_for_tests()
check("20t. без пакета промпт побайтно как раньше", prompts.system_prompt() == BASE_PROMPT20)
check("20u. старт с диска: load() достаёт пакет в оперативку", activation.load() is True and "Мурзик" in prompts.system_prompt())
blob = files20["pack.enc"]
(Path(_DIR20) / "pack.enc").write_bytes(blob[:40])
activation.reset_for_tests()
check("20v. обрезанный pack.enc -> пакета нет, промпт публичный, без падения", activation.load() is False and prompts.system_prompt() == BASE_PROMPT20)
(Path(_DIR20) / "pack.enc").write_bytes(b"KP1" + os.urandom(80))
check("20w. мусор в pack.enc -> то же самое", activation.load() is False and prompts.system_prompt() == BASE_PROMPT20)
(Path(_DIR20) / "pack.enc").write_bytes(blob)
st_ok = json.loads((Path(_DIR20) / "license.json").read_text(encoding="utf-8"))
for k, v in (("yandex_id", YID_B), ("install_id", "ki-" + "z" * 32), ("seed", "seed-wrong")):
    (Path(_DIR20) / "license.json").write_text(json.dumps({**st_ok, k: v}), encoding="utf-8")
    activation.reset_for_tests()
    check(f"20x-{k}. ключ привязан: подмена {k} -> расшифровки нет", activation.load() is False and "Мурзик" not in prompts.system_prompt())
(Path(_DIR20) / "license.json").write_text(json.dumps(st_ok), encoding="utf-8")
(Path(_DIR20) / "pack.enc").unlink()
activation.reset_for_tests()
check("20y. нет pack.enc -> публичный Кореш и вебхук отвечает",
      activation.load() is False and run(say("act-u2", "привет")) != "")
(Path(_DIR20) / "pack.enc").write_bytes(blob)

# --- 4. отказы сервера ---
LIC2 = FakeLicense()
activation._TRANSPORT = httpx.MockTransport(LIC2)
setupwiz.reset_for_tests()
tc21 = _TC(main.app)
_logged_in(tc21)


def _flow(client, lic, yid, install):
    """Отдельная установка = отдельный Volume (свой каталог и свой install_id)."""
    d = tempfile.mkdtemp()
    os.environ["MEMORY_DIR"] = d
    activation.reset_for_tests()
    (Path(d) / "license.json").write_text(json.dumps({"install_id": install}), encoding="utf-8")
    client.post("/setup/api/license/start", json={"code": GOOD_CODE})
    hd = list(lic.handles)[-1]
    lic.complete(hd, yid)
    return client.get("/setup/api/license/status").json(), d


r1, d2 = _flow(tc21, LIC2, YID_A, "ki-" + "1" * 32)
check("20z1. первый аккаунт: ок", r1["status"] == "ok")
r2, _ = _flow(tc21, LIC2, YID_B, "ki-" + "2" * 32)
check("20z2. чужой аккаунт: отказ с текстом, пакет не обновился", r2["status"] == "failed" and r2["error"] == "wrong_account" and "поддержк" in r2["message"])
_flow(tc21, LIC2, YID_A, "ki-" + "2" * 32)
_flow(tc21, LIC2, YID_A, "ki-" + "3" * 32)
r4, _ = _flow(tc21, LIC2, YID_A, "ki-" + "4" * 32)
check("20z3. четвёртая установка: «напишите в поддержку»", r4["status"] == "failed" and r4["error"] == "install_limit" and "поддержк" in r4["message"])

# --- 5. сервер лицензий лёг (дальше работаем с первой установкой, её каталог d2) ---
os.environ["MEMORY_DIR"] = d2
LIC2.down = True
activation.reset_for_tests()
check("20z4. сервер лежит, пакет на диске: Кореш стартует с пакетом", activation.load() is True and "Мурзик" in prompts.system_prompt())
check("20z5. обновление при недоступном сервере молчит (fail), пакет цел", run(activation.refresh_once()) == "fail" and "Мурзик" in prompts.system_prompt())
rs = tc21.post("/setup/api/license/start", json={"code": GOOD_CODE})
check("20z6. /setup при лежащем сервере: понятный ответ, установка не застревает", rs.status_code == 502 and "продолжить" in rs.json()["message"] and tc21.get("/setup/api/state").status_code == 200)
with TestClient(main.app) as lifespan_client:       # реальный старт приложения (lifespan)
    hz = lifespan_client.get("/health")
    check("20z7. lifespan: старт при лежащем сервере проходит, /health 200, пакет подтянут с диска",
          hz.status_code == 200 and C.pack_active() and "Мурзик" in prompts.system_prompt())
LIC2.down = False

# --- 6. обновление и отзыв на границе эпизода ---
LIC2.pack = {**PACK20, "ASSISTANT_NAME": "Гоша", "notes": "Новая заметка про кота Барсика."}
LIC2.version = 2
before = prompts.system_prompt()
res = run(activation.refresh_once())
check("20z8. обновление пакета: refresh_once=update, но промпт ПОКА прежний (внутри эпизода стабилен)",
      res == "update" and prompts.system_prompt() == before)
activation.apply_pending()
after = prompts.system_prompt()
check("20z9. на границе эпизода новый пакет применён", "Гоша" in after and "Барсика" in after and "Мурзик" not in after)
check("20za. новый пакет на диске тоже зашифрован", b"\xd0\x91\xd0\xb0\xd1\x80\xd1\x81" not in (Path(d2) / "pack.enc").read_bytes()
      and activation.load() and "Барсика" in prompts.system_prompt())
LIC2.revoked = True
check("20zb. отзыв: refresh_once=revoked, файл пакета стёрт", run(activation.refresh_once()) == "revoked" and not (Path(d2) / "pack.enc").exists())
check("20zc. отзыв применяется на границе эпизода: сразу промпт ещё прежний", "Барсика" in prompts.system_prompt())
activation.apply_pending()
check("20zd. после границы — обычный публичный Кореш", prompts.system_prompt() == BASE_PROMPT20 and not C.pack_active())

# --- 7. схема и защита от мусора ---
check("20ze. пакет новой неизвестной схемы -> игнор (публичный Кореш)", activation.sanitize({"schema": 99, "ASSISTANT_NAME": "X"}) is None)
san = activation.sanitize({"schema": 1, "ASSISTANT_NAME": "Я" * 90, "TONE": "ужас", "GENDER": "x", "notes": "а\x00б" * 2000,
                           "people": {"мама": "x" * 500}, "blocked": ["a,b", "ok"], "evil": "ignore"})
check("20zf. sanitize: длина, тон/род по белому списку, нулевые байты, лишние поля вычищены",
      len(san["ASSISTANT_NAME"]) == 30 and "TONE" not in san and "GENDER" not in san and len(san["notes"]) <= 1500 and "\x00" not in san["notes"]
      and len(san["people"]["мама"]) == 60 and san["blocked"] == ["ok"] and "evil" not in san)
check("20zg. activation.py и requirements: cryptography закреплён, модуль в Dockerfile",
      "cryptography==" in (HERE / "requirements.txt").read_text(encoding="utf-8") and "activation.py" in (HERE / "Dockerfile").read_text(encoding="utf-8"))
check("20zh. без адреса сервера (LICENSE_URL пуст) шаг тихо недоступен, установка не ломается",
      (lambda: (setattr(activation, "LICENSE_URL", ""), run(activation.start_flow(GOOD_CODE)))[1])()["error"] == "not_configured"
      and tc21.get("/setup/api/state").json()["license"]["configured"] is False)

activation.LICENSE_URL = "http://license.example.test"
check("20zk. LICENSE_URL по http:// не принимается: шаг тихо недоступен (по нему едут пакет и токен)",
      run(activation.start_flow(GOOD_CODE, "kor.up.railway.app"))["error"] == "not_configured" and activation.configured() is False)
activation.LICENSE_URL = "https://license.example.test"
LIC3 = FakeLicense()
activation._TRANSPORT = httpx.MockTransport(LIC3)
activation.reset_for_tests()
check("20zl. без адреса сервера старт не уходит (понятная ошибка), сеть не трогается",
      run(activation.start_flow(GOOD_CODE, ""))["error"] == "bad_domain" and not LIC3.requests)
LIC3.login_prefix = "https://evil.example"
check("20zm. сервер ответил ссылкой входа на ЧУЖОЙ адрес: клиент её не показывает",
      run(activation.start_flow(GOOD_CODE, "kor.up.railway.app"))["error"] == "server" and not activation._FLOW["handle"])
LIC3.login_prefix = None
_okf = run(activation.start_flow(GOOD_CODE, "kor.up.railway.app"))
check("20zn. честный старт: ссылка на сервер лицензий, секрет в оперативке (не на диске)",
      _okf["ok"] and _okf["authorize_url"].startswith("https://license.example.test/yandex/start?handle=") and len(activation._FLOW["secret"]) >= 32
      and activation._FLOW["secret"].encode() not in b"".join(_read_all_files(os.environ["MEMORY_DIR"]).values()))
activation._FLOW["secret"] = "x" * 40            # секрет не тот (как будто handle украли без секрета)
_bad = run(activation.poll_flow())
check("20zo. обмен с чужим секретом: отказ, пакет не сохранён, поток сброшен",
      _bad["status"] == "failed" and not C.pack_active() and not activation._FLOW["handle"])
activation.LICENSE_URL = ""
activation._TRANSPORT = None

# --- страница: блок «Код активации» виден только когда сервер лицензий подключён ---
if shutil.which("node"):
    _s_on = {**_S(cid="0123456789abcdef0123456789abcdef"), "license": {"configured": True, "active": False}}
    _s_off = {**_S(cid="0123456789abcdef0123456789abcdef"), "license": {"configured": False, "active": False}}
    _jsf20 = Path(tempfile.mkdtemp()) / "dom.js"
    _jsf20.write_text(DOM_JS, encoding="utf-8")
    _p20 = subprocess.run(["node", str(_jsf20), str(HERE / "setupwiz.html"), json.dumps([{"fn": "stepIot", "S": _s_on}, {"fn": "stepIot", "S": _s_off}], ensure_ascii=False)],
                          capture_output=True, text=True, timeout=60)
    _ok20 = _p20.returncode == 0 and _p20.stdout.strip().startswith("[")
    if _ok20:
        _on, _off = json.loads(_p20.stdout.strip().splitlines()[-1])
        _code_in = [n for n in _nodes(_on["tree"], "input") if n["a"].get("placeholder") == "KR-XXXX-XXXX-XXXX"]
        check("20zi. страница: блок «Код активации» с полем кода и кнопкой «Войти через Яндекс», ручной путь остался",
              len(_code_in) == 1 and "Код активации" in _text(_on["tree"]) and "Войти через Яндекс" in _text(_on["tree"])
              and "Проверить и сохранить" in _text(_on["tree"]))
        check("20zj. страница: без подключённого сервера лицензий блока нет",
              "Код активации" not in _text(_off["tree"]) and not [n for n in _nodes(_off["tree"], "input") if "KR-" in n["a"].get("placeholder", "")])
    else:
        check("20zi. страница с блоком активации отрисовывается в node", False, (_p20.stderr or _p20.stdout)[:300])

# --- уборка: окружение и состояние как до секции ---
activation.LICENSE_URL = ""
activation._TRANSPORT = None
setupwiz._TRANSPORT = None
activation.reset_for_tests()
setupwiz.reset_for_tests()
for _k, _v in _SAVED20.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v
brain._create = _boom_create
main.STATE.pop("act-u1", None)
main.STATE.pop("act-u2", None)


# ================= 21. ТЗ-28: один код вместо двух, мелочи /setup (сервер лицензий замокан) =================
_ENV21 = ("SETUP_CODE", "MEMORY_DIR", "WEBHOOK_SECRET", "ANTHROPIC_API_KEY", "YANDEX_IOT_TOKEN", "YANDEX_OAUTH_CLIENT_ID", "ASSISTANT_NAME",
          "TONE", "GENDER", "HOUSEHOLD", "BLOCKED_NAMES", "USAGE_FILE")
_SAVED21 = {k: os.environ.get(k) for k in _ENV21}
_DIR21 = tempfile.mkdtemp()
os.environ["MEMORY_DIR"] = _DIR21
os.environ["USAGE_FILE"] = os.path.join(_DIR21, "usage.json")
for _k in ("YANDEX_IOT_TOKEN", "YANDEX_OAUTH_CLIENT_ID", "ASSISTANT_NAME", "TONE", "GENDER", "HOUSEHOLD", "BLOCKED_NAMES"):
    os.environ.pop(_k, None)
SC21 = "t28-setup-code-777"
os.environ["SETUP_CODE"] = SC21
os.environ["WEBHOOK_SECRET"] = "t28-hook-secret"
os.environ["ANTHROPIC_API_KEY"] = "sk-test-t28"
activation.reset_for_tests()
LIC21 = FakeLicense()
activation.LICENSE_URL = "https://license.example.test"
activation._TRANSPORT = httpx.MockTransport(LIC21)
setupwiz._TRANSPORT = httpx.MockTransport(yandex_handler)
setupwiz.reset_for_tests()


def lg21(c, code, reopen=False):
    return c.post("/setup/api/login", json={"code": code, "reopen": reopen})


def checks21():
    return [(b, h) for p, b, h in LIC21.requests if p == "/v1/code/check"]


# --- 21a. старый путь жив и сервер лицензий не трогает ---
c_a = _TC(main.app)
r = lg21(c_a, SC21)
check("21a. SETUP_CODE по-прежнему пускает (запасной для нас), сервер лицензий при этом не дёргается",
      r.status_code == 200 and not LIC21.requests and c_a.get("/setup/api/state").json()["login_code"] == "")

# --- 21b. вход кодом KR-… ---
setupwiz.reset_for_tests()
c_b = _TC(main.app)
r = lg21(c_b, GOOD_CODE)
sc_b = r.headers.get("set-cookie", "").lower()
check("21b. код KR-… пускает: сессия, httpOnly, SameSite=Strict, 2 часа", r.status_code == 200 and "httponly" in sc_b and "samesite=strict" in sc_b and "max-age=7200" in sc_b, r.text[:200])
ck = checks21()
check("21c. образ спросил сервер лицензий: ровно один /v1/code/check с кодом и install_id этой установки, ничего лишнего",
      len(ck) == 1 and set(ck[0][0]) == {"code", "install_id"} and ck[0][0]["code"] == GOOD_CODE and ck[0][0]["install_id"] == activation.install_id(), str(ck))
st_b = c_b.get("/setup/api/state").json()
check("21d. после входа KR-кодом state знает этот код (блок «Код активации» заполнится им); у входа по SETUP_CODE пусто",
      st_b["login_code"] == GOOD_CODE and st_b["license"]["configured"] is True)
check("21e. код не уходит никуда лишнему: ни в /health, ни в журнал входа",
      GOOD_CODE not in c_b.get("/health").text and SC21 not in json.dumps(LIC21.requests, default=str))

# --- 21f. регистр, пробелы, дефисы ---
setupwiz.reset_for_tests()
LIC21.requests.clear()
c_f = _TC(main.app)
r = lg21(c_f, "  kr abcd efgh jkmn ")
check("21f. «kr abcd efgh jkmn» с пробелами принимается; серверу лицензий уходит каноническая форма",
      r.status_code == 200 and checks21()[-1][0]["code"] == GOOD_CODE, r.text[:200])

# --- 21g. чужой / битый KR -> отказ без утечек ---
setupwiz.reset_for_tests()
LIC21.requests.clear()
c_g = _TC(main.app)
r_missing = lg21(c_g, "KR-ZZZZ-ZZZZ-ZZZZ")
LIC21.bound, LIC21.installs = YID_A, ["ki-someone-else-installation-id"]
r_foreign = lg21(c_g, GOOD_CODE)
LIC21.bound, LIC21.installs = None, []
r_garbage = lg21(c_g, "KR-12")
check("21g. несуществующий, чужой (активирован другой установкой) и битый KR -> один и тот же отказ 401 без куки",
      r_missing.status_code == r_foreign.status_code == r_garbage.status_code == 401
      and r_missing.json()["error"] == r_foreign.json()["error"] == r_garbage.json()["error"] == "bad_code"
      and r_missing.json()["message"] == r_foreign.json()["message"] == r_garbage.json()["message"]
      and not c_g.cookies.get("koresh_setup"), f"{r_missing.text} | {r_foreign.text} | {r_garbage.text}")
check("21h. код, активированный ЭТОЙ установкой, входит снова (после переустановки страницы/перезапуска)",
      (setattr(LIC21, "bound", YID_A), setattr(LIC21, "installs", [activation.install_id()]), lg21(_TC(main.app), GOOD_CODE).status_code)[2] == 200)
LIC21.bound, LIC21.installs = None, []

# --- 21i. счётчик неверных кодов общий, блок работает и для KR ---
setupwiz.reset_for_tests()
LIC21.requests.clear()
c_i = _TC(main.app)
codes = [lg21(c_i, f"KR-ZZZZ-ZZZZ-ZZZ{i}").status_code for i in range(5)]
n_req = len(LIC21.requests)
r_lock = lg21(c_i, GOOD_CODE)
check("21i. неверный KR x5: 401 x4, на пятой блок 429; во время блока верный код не пускает и сервер лицензий не дёргается",
      codes == [401, 401, 401, 401, 429] and r_lock.status_code == 429 and len(LIC21.requests) == n_req, str(codes))
check("21j. пока блок, SETUP_CODE тоже не пускает (счётчик один на оба кода)", lg21(c_i, SC21).status_code == 429)

# --- 21k. сервер лицензий лёг / не подключён ---
setupwiz.reset_for_tests()
LIC21.down = True
c_k = _TC(main.app)
r_down = lg21(c_k, GOOD_CODE)
LIC21.down = False
check("21k. сервер лицензий лежит: понятный ответ 502, это НЕ неверная попытка (счётчик не растёт), входа по SETUP_CODE хватает",
      r_down.status_code == 502 and "не ответил" in r_down.json()["message"] and setupwiz._fails["n"] == 0 and lg21(c_k, SC21).status_code == 200, r_down.text[:200])
setupwiz.reset_for_tests()
activation.LICENSE_URL = ""
r_off = lg21(_TC(main.app), GOOD_CODE)
activation.LICENSE_URL = "https://license.example.test"
check("21l. сервер лицензий не подключён к образу: KR-код просто не подходит, без сетевых вызовов", r_off.status_code == 401 and r_off.json()["error"] == "bad_code")
setupwiz.reset_for_tests()
LIC21.requests.clear()
r_nokr = lg21(_TC(main.app), "hello-world")
check("21m. не похоже на KR-код (и не SETUP_CODE): отказ, к серверу лицензий не ходим", r_nokr.status_code == 401 and not LIC21.requests)

# --- 21n. сессия и закрытие ---
setupwiz.reset_for_tests()
c_n = _TC(main.app)
lg21(c_n, GOOD_CODE)
c_n.post("/setup/api/logout", json={})
check("21n. выход гасит и сессию, и запомненный код", c_n.get("/setup/api/state").status_code == 401 and not setupwiz._session_codes)
c_o = _TC(main.app)
lg21(c_o, GOOD_CODE)
C.set_setup_done(True)
r_closed = lg21(_TC(main.app), GOOD_CODE)
r_reopen = lg21(_TC(main.app), GOOD_CODE, reopen=True)
check("21o. после «Готово» KR-код без «Перенастроить» не пускает (403 closed), с ним пускает", r_closed.status_code == 403 and r_closed.json()["error"] == "closed" and r_reopen.status_code == 200)
C.set_setup_done(False)
setupwiz.reset_for_tests()
c_p = _TC(main.app)
lg21(c_p, GOOD_CODE)
T_BEFORE = setupwiz._now
setupwiz._now = lambda: T_BEFORE() + 7300
c_p.get("/setup/api/state")
check("21p. сессия по KR-коду истекает за 2 часа вместе с запомненным кодом", c_p.get("/setup/api/state").status_code == 401 and not setupwiz._session_codes)
setupwiz._now = T_BEFORE

# --- 21q. «Привет» / запуск навыка: пустая реплика не превращается в «» ---
setupwiz.reset_for_tests()
c_q = _TC(main.app)
lg21(c_q, SC21)
since_q = c_q.get("/setup/api/heard?since=1e12").json()["now"]
setupwiz._now = lambda: T_BEFORE() + 10
wp21 = "/alice/" + os.environ["WEBHOOK_SECRET"]
brain._create = stub("Привет от мока")
c_q.post(wp21, json={"session": {"new": True, "user": {"user_id": "t28-launch"}}, "request": {"original_utterance": ""}, "version": "1.0"})
h_q = c_q.get(f"/setup/api/heard?since={since_q}").json()
check("21q. запуск навыка с пустой репликой: услышано, kind=launch, text пуст (страница покажет «Навык запущен»)", h_q["heard"] is True and h_q["kind"] == "launch" and h_q["text"] == "", str(h_q))
setupwiz._now = lambda: T_BEFORE() + 20
c_q.post(wp21, json={"session": {"new": False, "user": {"user_id": "t28-launch"}}, "request": {"original_utterance": "привет"}, "version": "1.0"})
h_q2 = c_q.get(f"/setup/api/heard?since={since_q}").json()
check("21r. потом настоящая реплика заменяет запуск: kind=phrase, text=привет", h_q2["kind"] == "phrase" and h_q2["text"] == "привет")
setupwiz._now = T_BEFORE
main.STATE.pop("t28-launch", None)

# --- страница в node: вход, заполненный код, «Готово, жми Дальше», плашка, «Навык запущен», активационное имя ---
wiz21 = (HERE / "setupwiz.html").read_text(encoding="utf-8")
check("21s. на странице нет SETUP_CODE (клиент про него не слышит); вход описан как «код из бота»",
      "SETUP_CODE" not in wiz21 and "код из бота" in wiz21.lower() and "Код установки" not in wiz21)
DEV21 = {"count": 1, "devices": [{"name": "Лампа", "type": "свет", "room": "зал", "locked": False}], "scenarios": [], "rooms": ["зал"], "locked_count": 0, "locks_enabled": False}
SL = {"configured": True, "active": False}
TONES21 = ["derzkiy_mat", "derzkiy", "vezhlivyy"]
S_LOGIN = {**_S(cid=""), "license": SL, "login_code": GOOD_CODE, "tones": TONES21}
S_NOLOGIN = {**_S(cid=""), "license": SL, "login_code": ""}
S_IOT = {**_S(cid=""), "license": SL, "login_code": "", "iot": {"set": True, "source": "setup"}}
S_CID = {**_S(cid="0123456789abcdef0123456789abcdef"), "license": SL, "login_code": ""}
SEQ_OK = [{"click": "Войти через Яндекс"}, {"fire": 1}]
FETCH_OK = {"license/start": {"ok": True, "authorize_url": "https://license.example.test/yandex/start?handle=" + "a" * 64},
            "license/status": {"status": "ok", "saved": True, "iot": True, "devices": DEV21, "persisted": True}}
S_NAME = {**_S("male", "Бро"), "license": SL}
scen21 = [
    {"fn": "renderLogin", "S": S_NOLOGIN},                                                     # 0 вход
    {"fn": "stepIot", "S": S_LOGIN},                                                           # 1 код заполнен
    {"fn": "stepIot", "S": S_NOLOGIN},                                                         # 2 код пуст
    {"fn": "stepIot", "S": S_LOGIN, "fetch": FETCH_OK, "seq": SEQ_OK + [{"click": "Дальше"}]},   # 3 успех + «Дальше»
    {"fn": "stepIot", "S": S_NOLOGIN},                                                         # 4 плашка видна (нет токена, нет ClientID)
    {"fn": "stepIot", "S": S_IOT},                                                             # 5 плашки нет (токен есть)
    {"fn": "stepIot", "S": S_LOGIN, "fetch": FETCH_OK, "seq": SEQ_OK},                         # 6 плашка гаснет после успеха активации
    {"fn": "stepIot", "S": S_CID},                                                             # 7 есть ClientID: плашки нет и так
    {"fn": "stepCheck", "S": S_NAME, "fetch": {"heard": {"ok": True, "heard": True, "text": "", "kind": "launch", "now": 5}}, "seq": [{"fire": 1}]},   # 8
    {"fn": "stepCheck", "S": S_NAME, "fetch": {"heard": {"ok": True, "heard": True, "text": "привет", "kind": "phrase", "now": 5}}, "seq": [{"fire": 1}]},  # 9
    {"fn": "stepCheck", "S": S_NAME, "fetch": {"heard": {"ok": True, "heard": True, "text": "", "kind": "ping", "now": 5}}, "seq": [{"fire": 1}]},  # 10
    {"fn": "stepAlice", "S": S_NAME},                                                           # 11
]
if not shutil.which("node"):
    print("SKIP  21t-21z. node не найден: проверки страницы пропущены (в CI node есть)")
else:
    _j21 = Path(tempfile.mkdtemp()) / "dom.js"
    _j21.write_text(DOM_JS, encoding="utf-8")
    _p21 = subprocess.run(["node", str(_j21), str(HERE / "setupwiz.html"), json.dumps(scen21, ensure_ascii=False)], capture_output=True, text=True, timeout=60)
    _ok21 = _p21.returncode == 0 and _p21.stdout.strip().startswith("[")
    check("21t. страница с новыми блоками отрисовывается в node", _ok21, (_p21.stderr or _p21.stdout)[:300])
    if _ok21:
        R21 = json.loads(_p21.stdout.strip().splitlines()[-1])
        login_t = _text(R21[0]["tree"])
        login_in = [n for n in _nodes(R21[0]["tree"], "input")]
        check("21u. экран входа: «код из бота», поле под KR-XXXX-XXXX-XXXX, про SETUP_CODE/«код установки» ни слова",
              "из бота" in login_t and "SETUP_CODE" not in login_t and "установки" not in login_t
              and len(login_in) == 1 and "KR-" in login_in[0]["a"].get("placeholder", ""), login_t[:200])

        def code_input(r):
            return [n for n in _nodes(r["tree"], "input") if n["a"].get("placeholder") == "KR-XXXX-XXXX-XXXX"]
        ci1, ci2 = code_input(R21[1]), code_input(R21[2])
        check("21v. после входа KR-кодом поле блока «Код активации» уже заполнено им; без KR-входа пусто",
              len(ci1) == 1 and ci1[0]["v"] == GOOD_CODE and len(ci2) == 1 and ci2[0]["v"] == "")
        t3, app3 = _text(R21[3]["tree"]), _text(R21[3]["app"])
        check("21w. активация прошла: заметное «Готово, жми «Дальше»» + кнопка «Дальше» прямо там; нажатие ведёт на шаг 3",
              "Готово, жми «Дальше»" in t3 and any(_text(b) == "Дальше" for b in _nodes(R21[3]["tree"], "button")) and "Шаг 3. Характер" in app3, t3[-300:] + " || " + app3[:200])
        check("21x. в успехе по-прежнему: «Код принят», список устройств", "Код принят" in t3 and "Вижу 1 устройство" in t3)

        def plaque(r):
            return [n for n in _nodes(r["tree"], "div") if "note" in n["a"].get("class", "") and _text(n).startswith("Общее приложение")]
        pl4, pl5, pl6, pl7 = plaque(R21[4]), plaque(R21[5]), plaque(R21[6]), plaque(R21[7])
        check("21y. жёлтая плашка «Общее приложение не подключено…»: есть, пока умный дом не подключён; нет, если токен уже есть",
              len(pl4) == 1 and "hidden" not in pl4[0]["a"].get("class", "") and not [p for p in pl5 if "hidden" not in p["a"].get("class", "")] and not pl7)
        check("21z. плашка гаснет сама, как только активация по коду подключила умный дом",
              len(pl6) == 1 and "hidden" in pl6[0]["a"].get("class", ""), str([p["a"] for p in pl6]))
        t8, t9, t10 = _text(R21[8]["tree"]), _text(R21[9]["tree"]), _text(R21[10]["tree"])
        check("21za. запуск навыка (пустая реплика): «Навык запущен — скажи «привет»», а не «Кореш тебя услышал: «»»",
              "Навык запущен — скажи «привет»" in t8 and "«»" not in t8 and "услышал" not in t8, t8[-200:])
        check("21zb. настоящая реплика показывается как раньше; ping тоже", "Кореш тебя услышал: «привет»" in t9 and "Яндекс достучался" in t10)
        check("21zc. шаг «Проверка»: «ровно то активационное имя, которое ты вписала в Диалогах (мы предлагали «бро дома»)»; пример команды сохранён",
              "ровно то активационное имя, которое ты вписала в Диалогах" in t8 and "(мы предлагали «бро дома»)" in t8 and "запусти навык бро дома" in t8, t8[:300])
        t11 = _text(R21[11]["tree"])
        check("21zd. шаг «Алиса»: активационное имя названо так же, как в боте («активационное имя», две слова), копируется кнопкой",
              "Активационное имя, два слова" in t11 and "бро дома" in R21[11]["copied"])
check("21ze. SETUP.md и README описывают вход кодом из бота (и что SETUP_CODE запасной)",
      "код из бота" in (HERE / "SETUP.md").read_text(encoding="utf-8").lower() and "запасн" in (HERE / "SETUP.md").read_text(encoding="utf-8").lower())

# --- уборка ---
brain._create = _boom_create
activation.LICENSE_URL = ""
activation._TRANSPORT = None
setupwiz._TRANSPORT = None
setupwiz._now = T_BEFORE
activation.reset_for_tests()
setupwiz.reset_for_tests()
C.set_setup_done(False)
for _k, _v in _SAVED21.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v


# ================= 22. WhatsApp в клиентском Корешe (ТЗ-29): код живёт в selftest_wa.py =================
exec(compile((HERE / "selftest_wa.py").read_text(encoding="utf-8"), "selftest_wa.py", "exec"), globals())

# ================= 23. Деньги на мозги (ТЗ-31): заглушка на нуле, WhatsApp хозяину, счёт в долларах, порог 80%: код в selftest_balance.py =================
exec(compile((HERE / "selftest_balance.py").read_text(encoding="utf-8"), "selftest_balance.py", "exec"), globals())

# ================= 24. Модули (ТЗ-32): макросы, напоминания и действия по времени, детский режим, уведомления в очередь Телеграма: код в selftest_modules.py =================
exec(compile((HERE / "selftest_modules.py").read_text(encoding="utf-8"), "selftest_modules.py", "exec"), globals())

# ================= 25. «Мой Кореш» (ТЗ-34): голосовая ссылка на настройки, адрес сервера серверу лицензий, страница /setup/moduli: код в selftest_moy.py =================
exec(compile((HERE / "selftest_moy.py").read_text(encoding="utf-8"), "selftest_moy.py", "exec"), globals())

# ================= 26. «Кореш говорит первым» (ТЗ-35): звонок на Станцию через сценарий Алисы, очередь, тихие часы, лимиты, голосовые команды, страницы: код в selftest_proactive.py =================
exec(compile((HERE / "selftest_proactive.py").read_text(encoding="utf-8"), "selftest_proactive.py", "exec"), globals())

# ================= 27. Тон «С матом» не доходил до ответа: память с «мат не включается», приоритет env/setup/пакет, смена тона = новый эпизод: код в selftest_tone.py =================
exec(compile((HERE / "selftest_tone.py").read_text(encoding="utf-8"), "selftest_tone.py", "exec"), globals())

# ================= 28. WhatsApp-секретарь (ТЗ-36): входящие от «своих» голосом на Станции, голосовая настройка, ответ, страница /setup/sekretar: код в selftest_secretary.py =================
exec(compile((HERE / "selftest_secretary.py").read_text(encoding="utf-8"), "selftest_secretary.py", "exec"), globals())

# ================= 14. Dockerfile копирует каждый модуль (новый файл не должен выпасть из образа) =================
_copy = next(l for l in (HERE / "Dockerfile").read_text(encoding="utf-8").splitlines() if l.startswith("COPY ") and ".py" in l)
_mods = {f.name for f in HERE.glob("*.py") if not f.name.startswith("selftest")}
check("14. Dockerfile: COPY содержит все модули сборки", _mods <= set(_copy.split()), str(_mods - set(_copy.split())))

# ================= 8. следов выпиленного нет =================
# ("whats", "app") убран из списка: с ТЗ-29 WhatsApp в сборке нарочно (сайдкар wa/, модуль wa.py). Остальные следы выпиленного ловятся как раньше.
PARTS = [("code", "_task"), ("git", "hub"), ("tele", "gram"), ("cr", "m"),
         ("sel", "ena"), ("mac", "agent"), ("Ри", "та")]
PAT = re.compile("|".join(a + b for a, b in PARTS), re.I)
hits = []
for f in sorted(HERE.rglob("*")):
    if not f.is_file() or any(x in f.parts for x in (".git", "__pycache__", ".venv", "node_modules")) or f.name == "package-lock.json":
        continue
    # CI-файлы, README про образ и LICENSE (там правообладатель): это законно
    if f.relative_to(HERE).parts[0] == ".git" + "hub" or f.name in ("README.md", "LICENSE"):
        continue
    try:
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if PAT.search(line):
                hits.append(f"{f.relative_to(HERE)}:{i}: {line.strip()[:80]}")
    except UnicodeDecodeError:
        pass
check("8. в сборке нет следов выпиленного (0 совпадений)", not hits, "; ".join(hits[:5]))

print(f"\n{N} проверок, упало: {len(FAILED)}")
if FAILED:
    print("ПАДАЮТ:", *FAILED, sep="\n  ")
    sys.exit(1)
print("ВОРОТА ЗЕЛЁНЫЕ")
