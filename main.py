"""Кореш: вебхук навыка Алисы + мозг на Claude.

Алиса -> POST /alice/{WEBHOOK_SECRET} -> мозг думает в фоне; не успел за HOLD_TIMEOUT —
отдаём заглушку, а ответ подхватываем на следующей реплике («заглушка -> переспрос»).
"""
import asyncio
import hmac
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException, Request

import activation
import brain
import config as C
import history
import memory
import modules
import notify
import prompts
import setupwiz
import summarize
import tts
import usage
import wa

@asynccontextmanager
async def lifespan(_app):
    """Старт: персональный пакет с Volume -> в оперативку (нет/битый -> обычный Кореш); обновление пакета раз в час, тихо.
    Планировщик напоминаний (модуль «таймеры») крутится всегда, а действует, только пока модуль включён в пакете."""
    activation.load()
    task = asyncio.create_task(activation.refresh_loop())
    sched = asyncio.create_task(modules.scheduler_loop())
    wa.autostart()                      # WhatsApp-демон: только если номер уже привязан
    try:
        yield
    finally:
        task.cancel()
        sched.cancel()
        await wa.shutdown()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.include_router(setupwiz.router)         # страница настройки /setup (выключена, пока не задан SETUP_CODE)

@dataclass
class UserState:
    uid: str = ""
    dialog: list[dict] = field(default_factory=list)   # текущий «эпизод»: растёт только в конец (см. history.py)
    recap: str = ""                                    # пересказы прошлых разговоров, как они вошли в начало эпизода
    inflight: object = None        # asyncio.Task мозга, ещё думающая
    gen: int = 0                   # номер реплики: фоновое закрытие эпизода отменяется, если пришла новая
    closing: object = None         # asyncio.Task фонового закрытия эпизода (пересказ haiku)


STATE: dict[str, UserState] = {}
BG: set = set()                    # сильные ссылки на фоновые задачи, иначе их может собрать сборщик мусора


def state_for(uid: str) -> UserState:
    st = STATE.get(uid)
    if st is None:
        st = STATE[uid] = UserState(uid=uid)
    return st


def alice_reply(text: str, end: bool = False) -> dict:
    """Ответ навыку. Если в тексте есть ссылка на пополнение баланса: на экране она видна, голосом не читается
    (из tts вырезана целиком), и под ответом кнопка «Пополнить баланс» (протокол Диалогов: response.buttons)."""
    spoken = text.replace(prompts.BILLING_URL, " ") if prompts.BILLING_URL in text else text
    resp = {"text": text[:1000], "tts": tts.clean(re.sub(r"\s+", " ", spoken).strip()), "end_session": end}
    if prompts.BILLING_URL in text:
        resp["buttons"] = [{"title": "Пополнить баланс", "url": prompts.BILLING_URL, "hide": False}]
    return {"response": resp, "version": "1.0"}


def close_sync(st: UserState, store: memory.Store) -> None:
    """Запасное закрытие эпизода прямо в начале реплики (фоновое не успело): локальный пересказ, без модели и задержки."""
    text = store.episode_filter(history.local_summary(st.dialog))
    if text:
        store.add_episode(text)
    store.episode_closed()
    st.dialog = []


def begin_turn(st: UserState, p: str) -> int:
    """Готовим историю к новой реплике. Ничего из середины не вырезаем: либо дописываем в конец, либо (эпизод
    закрыт) начинаем новый: долгая память + пересказы в первом сообщении, дальше только дописываем.
    Возвращает индекс новой реплики хозяина в st.dialog."""
    store = memory.store_for(st.uid)
    history.trim_tail(st.dialog)
    st.gen += 1
    if store.wiped:                      # «забудь всё»: старая история в оперативке тоже не должна пережить просьбу
        st.dialog = []
        store.episode_closed()
        print("memory: забыто всё -> новый эпизод без старой истории", flush=True)
    elif history.needs_compaction(st.dialog):
        close_sync(st, store)
        print("history: новый эпизод (эпизод переполнен)", flush=True)
    if not st.dialog:                    # начало эпизода: единственное место, где память попадает в разговор
        activation.apply_pending()       # и где применяется обновлённый пакет активации (промпт внутри эпизода стабилен)
        st.recap = store.render_summaries()
        st.dialog = history.fresh_episode(store.render_block())
    st.dialog.append({"role": "user", "content": p})
    return len(st.dialog) - 1


async def close_episode(st: UserState, gen: int) -> None:
    """Фон, ПОСЛЕ ответа Алисе: пересказ эпизода одним вызовом haiku (сбой -> локальный), запись на диск, новый эпизод.
    Пришла новая реплика (gen сменился) -> ничего не трогаем: её begin_turn сам справится локальным путём."""
    if st.gen != gen:
        return
    store = memory.store_for(st.uid)
    snap = list(st.dialog)
    text = await summarize.summarize(snap)
    if st.gen != gen:
        return
    text = store.episode_filter(text) or store.episode_filter(history.local_summary(snap))
    if text:
        store.add_episode(text)
    store.episode_closed()
    activation.apply_pending()           # граница эпизода: обновлённый пакет активации применяется здесь
    st.recap = store.render_summaries()
    st.dialog = history.fresh_episode(store.render_block())
    print(f"history: эпизод закрыт в фоне, пересказ {len(text)} симв.", flush=True)


def _user_turns(dialog: list[dict]) -> int:
    return sum(1 for who, _ in history.text_turns(dialog) if who == "Хозяин")


def schedule_close(st: UserState, force: bool = False) -> None:
    if (st.closing is not None and not st.closing.done()) or st.inflight is not None:
        return
    if force:
        if _user_turns(st.dialog) < 2:
            return
    elif not history.needs_compaction(st.dialog):
        return
    t = asyncio.create_task(close_episode(st, st.gen))
    st.closing = t
    BG.add(t)
    t.add_done_callback(BG.discard)


async def drain() -> None:
    """Дождаться фоновых закрытий эпизодов (для ворот и аккуратной остановки)."""
    while BG:
        await asyncio.gather(*list(BG), return_exceptions=True)


def cancel_background() -> None:
    for t in list(BG):
        t.cancel()


def after_reply(st: UserState) -> None:
    """После ответа: снимок эпизода на диск (переживёт перезапуск) и, если эпизод переполнен, фоновое закрытие."""
    memory.store_for(st.uid).set_open(history.local_summary(st.dialog))
    schedule_close(st)


def deliver(st: UserState, text: str) -> dict:
    after_reply(st)
    days = usage.take_low_warning()          # потратили >= 80% суммы с последнего пополнения: один раз предупреждаем
    if days is not None:
        print(f"usage: потрачено {usage.spent_usd()} из {usage.budget_usd()} $ -> предупреждение (примерно {days} дн.)", flush=True)
        text = prompts.phrase("lowbalance", days=days) + "\n" + prompts.BILLING_URL + "\n" + text
        notify_owner_bg(prompts.phrase("lowbalance_wa", days=days), "balance_low")
    return alice_reply(text)


async def run_brain(st: UserState, user_idx: int) -> str:
    """Один ход мозга + страховка: если API всё же вернул 400 про thinking/историю, один раз начинаем с чистого
    листа (пересказ + эта же реплика) и состояние пользователя сбрасываем на это чистое начало."""
    try:
        return await brain.think(st.dialog, st.uid)
    except Exception as e:  # noqa: BLE001
        if not brain.is_history_error(e):
            raise
        user_text = st.dialog[user_idx]["content"]
        print("history: 400 про историю -> один повтор с чистым началом", flush=True)
        store = memory.store_for(st.uid)
        text = store.episode_filter(history.local_summary(st.dialog[:user_idx]))
        if text:
            store.add_episode(text)
        store.episode_closed()
        st.recap = store.render_summaries()
        st.dialog = history.fresh_episode(store.render_block())
        st.dialog.append({"role": "user", "content": user_text})
        return await brain.think(st.dialog, st.uid)


ERROR_TEXTS = {
    "auth": "Мозг не принимает ключ. Нужно проверить ключ Anthropic в настройках.",
    "billing": prompts.PHRASES["derzkiy"]["billing"],      # запасной текст; живой берётся по тону и роду: prompts.phrase("billing")
    "busy": "Мозг сейчас перегружен. Попробуй ещё раз через минуту.",
    "history": "У меня сбилась память разговора. Повтори, что хотел, я начну с чистого листа.",
    "bad": "Мозг не принял запрос. Попробуй ещё раз, а если повторяется, смотри журнал сервиса.",
    "down": "Мозг сейчас не отвечает. Попробуй ещё раз через минуту.",
}


def notify_owner_bg(text: str, kind: str | None = None) -> None:
    """Служебное сообщение хозяину в WhatsApp «себе», в фоне, не задерживая ответ Алисе. Нет привязки / сбой -> молча.
    kind (balance_low | balance_zero): то же самое уходит в очередь Телеграма через сервер лицензий (ТЗ-32), там появится кнопка «Пополнить баланс»."""
    async def _go():
        try:
            await wa.notify_owner(text)
        except Exception as e:  # noqa: BLE001
            print(f"notify: WhatsApp хозяину не ушёл ({type(e).__name__})", flush=True)
        if kind:
            try:
                await notify.post(kind, text)
            except Exception as e:  # noqa: BLE001
                print(f"notify: очередь не приняла ({type(e).__name__})", flush=True)
    try:
        t = asyncio.get_running_loop().create_task(_go())
    except RuntimeError:
        return
    BG.add(t)
    t.add_done_callback(BG.discard)


def brain_error_text(e: Exception) -> str:
    kind = brain.error_kind(e)
    # текст ошибки не печатаем: в нём может быть ключ
    print(f"brain error: {type(e).__name__} {getattr(e, 'status_code', '')} -> {kind}", flush=True)
    if kind == "billing":                    # деньги на мозги кончились: говорим в характере и один раз в сутки пишем хозяину
        if usage.take_zero_notice():
            notify_owner_bg(prompts.phrase("billing_wa"), "balance_zero")
        return prompts.phrase("billing") + "\n" + prompts.BILLING_URL
    return ERROR_TEXTS[kind]


def _task_result(t) -> str:
    try:
        return t.result()
    except Exception as e:  # noqa: BLE001
        return brain_error_text(e)


async def _await_inflight(st: UserState) -> dict:
    """Ждём уже запущенную мысль ещё HOLD_TIMEOUT. Готово — отдаём; нет — «ещё думаю»."""
    try:
        text = await asyncio.wait_for(asyncio.shield(st.inflight), timeout=C.hold_timeout())
        st.inflight = None
        return deliver(st, text)
    except asyncio.TimeoutError:
        return alice_reply(prompts.phrase("still"))
    except Exception as e:  # noqa: BLE001
        st.inflight = None
        return alice_reply(brain_error_text(e))


STOP_RE = re.compile(r"(стоп|хватит|пока|закройся|выход|выйти)\W*")
HELP_RE = re.compile(r"(помощь|помоги|что ты умеешь|что ты можешь|что умеешь)\W*")


async def handle(uid: str, phrase: str, new_session: bool) -> dict:
    st = state_for(uid)
    p = phrase.strip()
    low = p.lower()

    if low == "ping":                      # проверка живости от Яндекс Диалогов, мозг не трогаем
        return alice_reply("pong")
    if STOP_RE.fullmatch(low):
        st.inflight = None
        schedule_close(st, force=True)     # сессия кончилась: пересказ разговора в фоне, не задерживая «пока»
        return alice_reply(prompts.phrase("bye"), end=True)
    if HELP_RE.fullmatch(low):             # модерации Яндекса нужны «Помощь» и «Что ты умеешь»
        return alice_reply(prompts.phrase("help"))

    # мысль уже думается с прошлой реплики — ЛЮБАЯ реплика значит «ну готово?»
    if st.inflight is not None:
        if st.inflight.done():
            text = _task_result(st.inflight)
            st.inflight = None
            return deliver(st, text)
        return await _await_inflight(st)

    if new_session and not p:
        return alice_reply(prompts.phrase("greet"))
    if not p:
        return alice_reply("Ну? Говори.")

    if usage.exceeded():                   # потолок на сутки: мозг не вызываем вообще
        return alice_reply(prompts.phrase("limit"))

    st.inflight = asyncio.create_task(run_brain(st, begin_turn(st, p)))
    try:
        text = await asyncio.wait_for(asyncio.shield(st.inflight), timeout=C.hold_timeout())
        st.inflight = None
        return deliver(st, text)
    except asyncio.TimeoutError:
        return alice_reply(prompts.phrase("hold"))
    except Exception as e:  # noqa: BLE001  ошибка API не должна превращаться в 500
        st.inflight = None
        return alice_reply(brain_error_text(e))


@app.post("/alice/{secret}")
async def alice_webhook(secret: str, request: Request):
    expected = C.webhook_secret()
    # пустой WEBHOOK_SECRET = вебхук закрыт для всех
    if not expected or not hmac.compare_digest(secret.encode(), expected.encode()):
        raise HTTPException(status_code=403)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400)
    session = body.get("session") or {}
    uid = (session.get("user") or {}).get("user_id") or (session.get("application") or {}).get("application_id") \
        or session.get("user_id") or "anon"
    req = body.get("request") or {}
    phrase = req.get("original_utterance", "") or req.get("command", "")
    setupwiz.note_request(phrase, request.headers.get(setupwiz.SELFTEST_HEADER))     # шаг «Проверка» мастера /setup
    return await handle(str(uid), phrase, bool(session.get("new", False)))


@app.get("/health")
async def health():
    out = {"ok": True, "version": C.VERSION, "tone": C.tone(), "gender": C.gender(), "model": C.model(), "effort": C.effort(),
           "locks": C.allow_locks() and bool(C.lock_phrase()),
           "iot": bool(C.iot_token()), "tokens_today": usage.used(),
           "spent_usd": usage.spent_usd(), "budget_usd": usage.budget_usd(),
           "memory": "disk" if memory.disk_ok() else "ram"}
    if C.pack_active():                    # персональное (тон, род) наружу не светим
        out.pop("tone"), out.pop("gender")
    return out
