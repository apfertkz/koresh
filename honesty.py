"""Честность о действиях. Модель может сказать «включил», не вызвав инструмент или получив от него ошибку.
Сервер ведёт журнал действий хода и сверяет с ним итоговый текст: утверждение без успешного действия
подменяется честной фразой (в тоне и роде ассистента). Второй вызов модели не нужен."""
import re

import prompts

# Утверждения «я это сделал» в первом лице, оба рода. Основа глагола + окончание (-л, -ла, -ли).
_VERBS = (
    r"включил|выключил|вырубил|врубил|зажг[ёе]?л|зажёг|зажег|погасил|потушил|поставил|установил|выставил|запустил|сделал|"
    r"открыл|закрыл|добавил|убавил|прибавил|приглушил|затемнил|сменил|переключил|поменял|настроил|"
    r"активировал|отправил|позвонил|заказал|убрал|задал|нажал"
)
CLAIM_VERB = re.compile(r"(?<![а-яё])(?:" + _VERBS + r")(?:а|и)?(?![а-яё])", re.I)
# WhatsApp: что хозяин услышит как «ушло». Проверяется только когда умение включено у этого клиента (иначе «написал» -- обычное слово).
_WA_VERBS = r"написал|скинул|передал|послал|отправил"
CLAIM_WA = re.compile(r"(?<![а-яё])(?:" + _WA_VERBS + r")(?:а|и)?(?![а-яё])", re.I)
_VERBS_IOT = _VERBS.replace("|отправил", "")
CLAIM_VERB_IOT = re.compile(r"(?<![а-яё])(?:" + _VERBS_IOT + r")(?:а|и)?(?![а-яё])", re.I)
NO_WA_ACTION = "сообщение не отправлено, ничего не ушло"
_QUOTED = re.compile(r"«[^»]*»")
CLAIM_READY = re.compile(r"(?:^|[.!?…,]\s*)(?:(?:ну|да|так|вот)[,\s]+)*(?:(?:всё|все)\s+)?(?:уже\s+)?(?:готово|сделано)(?![а-яё])", re.I)
NEGATION = {"не", "ни", "нифига", "хрен", "хер"}
# оговорка о неудаче в самом тексте
ADMISSION = re.compile(r"не вышло|не получилось|не удалось|не смог|не смогла|не могу|не буду|не сделал|не включ|не выключ|"
                       r"отключ[её]н|отключено|нужна кодовая|нужен новый|ошибк|не доступн|не на связи|"
                       r"не (?:открыл|закрыл|запустил|поставил|выставил|установил)", re.I)

# Просил ли хозяин ДЕЙСТВИЕ с домом, сценарием, макросом, напоминанием или сообщением (по намерению, ещё до ответа модели).
# Болтовня, шутки и мат («поматерись», «ну давай») действием не являются: «сделал» в ответе на них не повод подменять ответ.
_INTENT_VERBS = (r"включ|выключ|вкл|выкл|вруб|вырубь|врубай|зажг|погас|потуш|открой|открыть|закрой|закрыть|запри|отопри|поставь|поставить|установи|выстав|"
                 r"запусти|запустить|убав|прибав|приглуш|затемн|смени|поменяй|переключ|настро|активир|задай|нажми|"
                 r"отправь|отправить|напиши|написать|скинь|передай|перешли|позвони|закажи|убери|добавь|напомни|запомни|удали|отмени|сохрани|создай")
_INTENT_NOUNS = (r"свет|ламп|люстр|торшер|розетк|кондиционер|обогревател|телевизор|шторы|штор|жалюзи|замок|ворота|сигнализац|сценари|макрос|"
                 r"напоминани|таймер|будильник|яркост|громкост|температур|детск\w+\s+режим|режим|вотсап|ватсап|whatsapp|сообщени")
ACTION_INTENT = re.compile(r"(?<![а-яё])(?:" + _INTENT_VERBS + r")|(?<![а-яё])(?:" + _INTENT_NOUNS + r")|(?<![а-яё])(?:сделай|давай)\W+(?:\w+\W+){0,2}?"
                           r"(?:ярче|темнее|тише|громче|теплее|холоднее|светлее)", re.I)


def wants_action(user_text: str | None) -> bool:
    """None (текст реплики не передан) = считаем, что действие просили: так вело себя старое правило."""
    if user_text is None:
        return True
    return bool(ACTION_INTENT.search(user_text.lower()))


REASONS = {
    "kids_only_light": "в детском режиме управляется только свет, остальное у взрослых",
    "locks_disabled": "управление замками, воротами и сигнализацией отключено в настройках",
    "need_phrase": "для этого нужна кодовая фраза",
}
DEVICE_ERRORS = {
    "DEVICE_UNREACHABLE": "устройство не на связи",
    "DEVICE_BUSY": "устройство занято",
    "DEVICE_NOT_FOUND": "устройство не найдено",
    "INVALID_ACTION": "устройство не умеет такого",
    "INVALID_VALUE": "устройство не принимает такое значение",
    "NOT_SUPPORTED_IN_CURRENT_MODE": "в текущем режиме устройство так не умеет",
    "ACCOUNT_LINKING_ERROR": "аккаунт устройства отвалился от Яндекса",
}
NO_ACTION = "команда до умного дома не дошла"


def reason_of(result: str) -> str:
    """Человеческая причина по ответу iot.control (не «ok»)."""
    r = (result or "").strip()
    low = r.lower()
    for key, text in REASONS.items():
        if low.startswith(key):
            return text
    if "не подключён" in low:
        return "умный дом не подключён"
    if "токен умного дома не принят" in low:
        return "токен умного дома не принят, нужен новый"
    if "неизвестный id" in low:
        return "такого устройства нет в списке"
    if "не ответил" in low:
        return "умный дом не ответил"
    if "target должен" in low:
        return "команда составлена криво"
    m = re.match(r"error (\d{3})", low)
    if m:
        return f"Яндекс ответил ошибкой {m.group(1)}"
    m = re.match(r"error:\s*([A-Z_]+)", r)
    if m:
        return DEVICE_ERRORS.get(m.group(1), f"устройство вернуло ошибку {m.group(1)}")
    return "умный дом вернул ошибку"


class Journal:
    """Что за этот ход реально случилось с умным домом (и с памятью, для «Готово» после remember/forget)."""

    def __init__(self, wa_active: bool = False):
        self.events: list[dict] = []
        self.wa_active = wa_active                 # у клиента включён WhatsApp: «написал/скинул» тоже проверяем
        self.wa_events: list[dict] = []
        self.mod_drafted = False                   # ход закончился черновиком макроса или напоминания (модули, ТЗ-32)
        self.user_text: str | None = None          # реплика хозяина в этом ходе; None = не передана (старое поведение: проверяем всегда)

    @property
    def tried(self) -> bool:
        """Модель в этом ходе пыталась что-то делать инструментами (умный дом, макрос, WhatsApp): тогда её «сделал» проверяем в любом случае."""
        return bool(self.events or self.wa_events)

    def module(self, ok: bool, reason: str = "") -> None:
        """Итог инструмента модулей (сохранение макроса, напоминание, детский режим): успех или причина отказа."""
        self.events.append({"tool": "module", "target": "", "ok": ok, "reason": reason})

    def control(self, target: str, result: str) -> None:
        ok = (result or "").strip() == "ok"
        self.events.append({"tool": "control", "target": target, "ok": ok, "reason": "" if ok else reason_of(result)})
        print(f"action: control {target} -> {'ok' if ok else 'fail'}", flush=True)

    def list_failed(self, error_text: str) -> None:
        self.events.append({"tool": "list", "target": "", "ok": False, "reason": reason_of(error_text)})

    def memory(self, ok: bool) -> None:
        self.events.append({"tool": "memory", "target": "", "ok": ok, "reason": ""})

    def wa_draft(self, ok: bool, reason: str = "") -> None:
        self.wa_events.append({"kind": "draft", "ok": ok, "reason": reason})

    def wa_send(self, ok: bool, reason: str = "") -> None:
        self.wa_events.append({"kind": "send", "ok": ok, "reason": reason})
        print(f"action: whatsapp -> {'ok' if ok else 'fail'}", flush=True)

    @property
    def wa_ok(self) -> int:
        return sum(1 for e in self.wa_events if e["kind"] == "send" and e["ok"])

    @property
    def wa_drafted(self) -> bool:
        return any(e["kind"] == "draft" and e["ok"] for e in self.wa_events)

    def wa_failures(self) -> list[str]:
        return [e["reason"] for e in self.wa_events if not e["ok"]]

    @property
    def iot_ok(self) -> int:
        return sum(1 for e in self.events if e["tool"] == "control" and e["ok"])

    @property
    def mod_ok(self) -> int:
        return sum(1 for e in self.events if e["tool"] == "module" and e["ok"])

    @property
    def any_ok(self) -> int:
        return sum(1 for e in self.events if e["ok"])

    def failures(self) -> list[str]:
        return [e["reason"] for e in self.events if e["tool"] in ("control", "list", "module") and not e["ok"]]


def _negated(text: str, start: int) -> bool:
    words = re.findall(r"[а-яёa-z]+", text[:start].lower())
    return bool(words) and words[-1] in NEGATION


def claims_action(text: str, wa_active: bool = False) -> bool:
    """Есть ли в тексте утверждение «я сделал» (не отрицание: «не включил» не считается).
    wa_active: «отправил» и родня -- про WhatsApp, их проверяет claims_wa."""
    t = text or ""
    if CLAIM_READY.search(t):
        return True
    rx = CLAIM_VERB_IOT if wa_active else CLAIM_VERB
    return any(not _negated(t, m.start()) for m in rx.finditer(t))


def claims_wa(text: str) -> bool:
    t = text or ""
    return any(not _negated(t, m.start()) for m in CLAIM_WA.finditer(t))


def check(text: str, journal: Journal, tone: str | None = None, gender: str | None = None) -> str:
    """Вернуть текст как есть или честную замену. Решает только код, по журналу хода."""
    # внутри кавычек черновика слова хозяина, не заявления ассистента
    t = _QUOTED.sub(" ", text) if journal.wa_drafted else text
    if not journal.tried and not wants_action(journal.user_text):
        return text                       # болтовня, шутки, мат: действия никто не просил и модель ничего не вызывала -> «сделал» тут просто слово
    if journal.wa_active and claims_wa(t) and not journal.wa_ok and not ADMISSION.search(text):
        fails = journal.wa_failures()
        return prompts.honest("fail" if fails else "none", fails[-1] if fails else NO_WA_ACTION, tone, gender)
    if not claims_action(t, journal.wa_active):
        return text
    if journal.iot_ok or journal.mod_ok:
        if not journal.failures() or ADMISSION.search(text):
            return text                   # было успешное действие; про неудачи модель сама сказала
        return prompts.honest("partial", journal.failures()[-1], tone, gender)
    if CLAIM_READY.search(t) and not CLAIM_VERB.search(t) and (journal.any_ok or journal.wa_ok):
        return text                       # «Готово» после remember/forget: память записана, это правда
    fails = journal.failures()
    return prompts.honest("fail" if fails else "none", fails[-1] if fails else NO_ACTION, tone, gender)
