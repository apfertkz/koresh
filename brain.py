"""Мозг: Claude API + инструменты умного дома. Один ход диалога с циклом инструментов."""
import json

import config as C
import honesty
import iot
import memory
import modules
import prompts
import usage
import wa

_client = None

TOOLS = [
    {
        "name": "list_devices",
        "description": "Список устройств умного дома, групп и сценариев хозяина с их id, комнатой и текущим состоянием. Вызывай перед управлением, если не знаешь id.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "control_device",
        "description": "Отправить команду устройству или группе либо запустить сценарий умного дома Яндекса.",
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "enum": ["device", "group", "scenario"]},
                "target_id": {"type": "string"},
                "capability": {"type": "string", "description": "напр. devices.capabilities.on_off, devices.capabilities.range, devices.capabilities.color_setting, devices.capabilities.mode, devices.capabilities.toggle. Для scenario не нужен."},
                "instance": {"type": "string", "description": "напр. on, brightness, temperature, hsv, thermostat"},
                "value": {"description": "bool / number / object в зависимости от умения"},
            },
            "required": ["target", "target_id"],
        },
    },
    {
        "name": "remember",
        "description": "Запомнить НАДОЛГО устойчивый факт о хозяине: имена близких, вкусы, работа, привычки, на что болеет, обещание вернуться к теме. Одна короткая фраза до 200 символов («не ест лук», «жену зовут Айгуль»). Не вызывай для мелочей на один раз и НИКОГДА для паролей, кодов, номеров карт и других секретов. Вызывай тихо, не объявляя об этом.",
        "input_schema": {
            "type": "object",
            "properties": {"fact": {"type": "string", "description": "короткий факт, до 200 символов"}},
            "required": ["fact"],
        },
    },
    {
        "name": "forget",
        "description": "Стереть память по просьбе хозяина. fact: что забыть (лучше теми же словами, что в заметках). all=true: стереть вообще всё, что помнишь о хозяине и прошлых разговорах («забудь всё»).",
        "input_schema": {
            "type": "object",
            "properties": {"fact": {"type": "string"}, "all": {"type": "boolean"}},
        },
    },
]


def tools() -> list[dict]:
    """Инструменты хода: умный дом и память всегда, WhatsApp -- только когда он привязан и есть кому писать (детям не даём),
    модули (макросы, напоминания, детский режим) -- только включённые флагом в пакете лицензии."""
    out = TOOLS + wa.TOOLS if wa.available() and not modules.kids_on() else TOOLS
    extra = modules.tools()
    return out + extra if extra else out


async def _create(**kw):
    """Единственная точка выхода к Anthropic. Ворота подменяют её заглушкой."""
    global _client
    if _client is None:
        from anthropic import AsyncAnthropic
        _client = AsyncAnthropic(api_key=C.anthropic_key())
    return await _client.messages.create(**kw)


def _recent_user_text(history: list[dict], n: int = 2) -> str:
    """Последние реплики хозяина дословно (не пересказ модели) — по ним проверяется кодовая фраза."""
    texts = [m["content"] for m in history if m.get("role") == "user" and isinstance(m.get("content"), str)]
    return " ".join(texts[-n:])


def _tokens(resp) -> int:
    u = getattr(resp, "usage", None)
    if u is None:
        return 0
    return sum(int(getattr(u, k, 0) or 0) for k in
               ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))


def request_params() -> dict:
    """Параметры запроса, не зависящие от истории. Нет temperature/top_p, thinking:disabled, forced tool_choice и
    prefill: на sonnet-5-5 это 400. effort идёт через extra_body, чтобы не зависеть от версии SDK (в 0.69.0 нет поля)."""
    kw = {"model": C.model(), "max_tokens": C.max_tokens()}
    if C.supports_effort():
        kw["extra_body"] = {"output_config": {"effort": C.effort()}}
    return kw


# признаки 400 про привязку thinking-блоков к истории (а не про деньги, effort или кривые параметры)
HISTORY_400_MARKERS = ("signature", "different conversation", "block_binding", "prefix_mismatch", "prefix mismatch",
                       "thinking block", "redacted_thinking", "cannot be modified")


def _is_status(e: Exception, code: int, *names: str) -> bool:
    return getattr(e, "status_code", None) == code or any(c.__name__ in names for c in type(e).__mro__)


def error_kind(e: Exception) -> str:
    """auth | billing | busy | history | bad | down. Текст ошибки читаем только для классификации, наружу он не идёт."""
    msg = str(e).lower()
    code = getattr(e, "status_code", None)
    if _is_status(e, 401, "AuthenticationError") or _is_status(e, 403, "PermissionDeniedError"):
        return "auth"
    if code == 402 or any(k in msg for k in ("credit balance", "billing", "usage limit", "spend limit")):
        return "billing"
    if code in (429, 529) or (isinstance(code, int) and code >= 500) \
            or any(c.__name__ in ("RateLimitError", "InternalServerError", "OverloadedError") for c in type(e).__mro__):
        return "busy"
    if _is_status(e, 400, "BadRequestError"):
        if any(k in msg for k in HISTORY_400_MARKERS):
            return "history"
        return "bad"
    return "down"


def is_history_error(e: Exception) -> bool:
    return error_kind(e) == "history"


def _refusal_category(resp):
    sd = getattr(resp, "stop_details", None)
    return sd.get("category") if isinstance(sd, dict) else getattr(sd, "category", None)


async def think(history: list[dict], uid: str | None = None) -> str:
    """history — список messages, будет дополнен (только в конец). uid — чья долгая память (remember/forget)."""
    def make_system():
        return [{"type": "text", "text": prompts.system_prompt(), "cache_control": {"type": "ephemeral"}}]
    system = make_system()
    sig = modules.prompt_signature()
    modules.note_turn(uid, _recent_user_text(history, 1))      # считаем реплику; «нет/стоп» стирает черновик макроса или напоминания
    wa_active = wa.available() and not modules.kids_on()
    journal = honesty.Journal(wa_active=wa_active)          # что за этот ход реально сделано с умным домом и WhatsApp
    ut_now = _recent_user_text(history, 1)                  # просили ли действие: болтовню с «сделал» не подменяем
    if wa.is_affirmation(ut_now) or modules.current_draft(uid) or wa.current_draft(uid):
        ut_now = _recent_user_text(history, 3)              # «да» на «Сохранить?» / «Отправить?»: просьба была в предыдущих репликах
    journal.user_text = ut_now
    tool_list = tools()
    if wa_active:
        wa.note_turn(uid, _recent_user_text(history, 1))      # считаем реплику хозяина; «нет/стоп» стирает черновик

    def finish(text: str) -> str:
        """Утверждение «сделал» без успешного действия -> честная фраза (и в истории тоже, чтобы модель не верила вранью).
        Ход закончился черновиком WhatsApp: хозяин услышит ровно то, что уйдёт (иначе подставляется каноничная фраза)."""
        text = modules.kids_clean(text)
        if journal.wa_drafted:
            fixed = wa.ensure_readback(uid, text)
        elif journal.mod_drafted:
            fixed = modules.ensure_readback(uid, text)
        else:
            fixed = honesty.check(text, journal)
        if fixed != text:
            print("honesty: заявление о действии без успешного вызова заменено", flush=True)
            kept = [b for b in history[-1]["content"] if getattr(b, "type", None) != "text"] \
                if isinstance(history[-1].get("content"), list) else []
            history[-1] = {"role": "assistant", "content": kept + [{"type": "text", "text": fixed}]}
        return fixed

    for _ in range(5):
        if usage.exceeded():
            return prompts.phrase("limit")
        resp = await _create(system=system, tools=tool_list, messages=history, **request_params())
        usage.add(_tokens(resp), usage.cost_of(resp, C.model()))
        if resp.stop_reason == "refusal":      # классификатор отказал; пустой ход в историю не кладём
            print(f"brain refusal: {_refusal_category(resp)}", flush=True)
            return prompts.phrase("refusal")
        history.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            if text:
                return finish(text)
            return prompts.phrase("cut") if resp.stop_reason == "max_tokens" else finish("Готово.")

        results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            a = block.input
            try:
                if block.name == "list_devices":
                    listing = await iot.devices()
                    if isinstance(listing, dict) and listing.get("error"):
                        journal.list_failed(str(listing["error"]))
                    out = json.dumps(listing, ensure_ascii=False)
                elif block.name == "control_device":
                    out = await iot.control(a["target"], a["target_id"], a.get("capability", ""),
                                            a.get("instance", ""), a.get("value"), _recent_user_text(history))
                    journal.control(str(a.get("target", "")), out)
                elif block.name == "whatsapp_draft" and wa_active:
                    res = await wa.tool_draft(uid, str(a.get("to", "")), str(a.get("text", "")))
                    journal.wa_draft(res.ok, res.reason)
                    out = res.text
                elif block.name == "whatsapp_confirm" and wa_active:
                    res = await wa.tool_confirm(uid, _recent_user_text(history, 1))
                    journal.wa_send(res.ok, res.reason)
                    out = res.text
                elif block.name in modules.TOOL_NAMES:
                    ut = _recent_user_text(history, 2 if block.name == "macro_run" else 1)
                    res = await modules.dispatch(block.name, a, uid, ut, journal)
                    if block.name != "macro_run":            # макрос сам записал каждое действие в журнал
                        journal.module(res.ok, res.reason)
                    if res.drafted:
                        journal.mod_drafted = True
                    out = res.text
                elif block.name == "remember":
                    out = memory.store_for(uid).remember(str(a.get("fact", "")))[1] if uid else "error: память недоступна"
                    journal.memory(not out.startswith(("error", "refused")))
                elif block.name == "forget":
                    out = memory.store_for(uid).forget(str(a.get("fact", "")), bool(a.get("all"))) if uid \
                        else "error: память недоступна"
                    journal.memory(not out.startswith(("error", "refused")))
                else:
                    out = "unknown tool"
            except Exception as e:  # noqa: BLE001
                code = getattr(getattr(e, "response", None), "status_code", "")      # код ответа Яндекса (401 / 429 / 5xx), без токена и без тела
                print(f"tool error [{block.name}]: {type(e).__name__}" + (f" {code}" if code else ""), flush=True)
                out = "error: умный дом не ответил"
                if block.name == "control_device":
                    journal.control(str(a.get("target", "")), out)
                elif block.name == "list_devices":
                    journal.list_failed(out)
                elif block.name == "whatsapp_draft":
                    journal.wa_draft(False, "WhatsApp не ответил")
                elif block.name == "whatsapp_confirm":
                    journal.wa_send(False, "WhatsApp не ответил")
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(out)[:6000]})
        history.append({"role": "user", "content": results})
        if modules.prompt_signature() != sig:                  # включили/выключили детский режим: дальше говорим в новом тоне
            sig = modules.prompt_signature()
            system, tool_list = make_system(), tools()
    return "Что-то пошло не так, повтори."
