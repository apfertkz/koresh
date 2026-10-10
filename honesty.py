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
CLAIM_READY = re.compile(r"(?:^|[.!?…,]\s*)(?:(?:ну|да|так|вот)[,\s]+)*(?:(?:всё|все)\s+)?(?:уже\s+)?(?:готово|сделано)(?![а-яё])", re.I)
NEGATION = {"не", "ни", "нифига", "хрен", "хер"}
# оговорка о неудаче в самом тексте
ADMISSION = re.compile(r"не вышло|не получилось|не удалось|не смог|не смогла|не могу|не буду|не сделал|не включ|не выключ|"
                       r"отключ[её]н|отключено|нужна кодовая|нужен новый|ошибк|не доступн|не на связи|"
                       r"не (?:открыл|закрыл|запустил|поставил|выставил|установил)", re.I)

REASONS = {
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

    def __init__(self):
        self.events: list[dict] = []

    def control(self, target: str, result: str) -> None:
        ok = (result or "").strip() == "ok"
        self.events.append({"tool": "control", "target": target, "ok": ok, "reason": "" if ok else reason_of(result)})
        print(f"action: control {target} -> {'ok' if ok else 'fail'}", flush=True)

    def list_failed(self, error_text: str) -> None:
        self.events.append({"tool": "list", "target": "", "ok": False, "reason": reason_of(error_text)})

    def memory(self, ok: bool) -> None:
        self.events.append({"tool": "memory", "target": "", "ok": ok, "reason": ""})

    @property
    def iot_ok(self) -> int:
        return sum(1 for e in self.events if e["tool"] == "control" and e["ok"])

    @property
    def any_ok(self) -> int:
        return sum(1 for e in self.events if e["ok"])

    def failures(self) -> list[str]:
        return [e["reason"] for e in self.events if e["tool"] in ("control", "list") and not e["ok"]]


def _negated(text: str, start: int) -> bool:
    words = re.findall(r"[а-яёa-z]+", text[:start].lower())
    return bool(words) and words[-1] in NEGATION


def claims_action(text: str) -> bool:
    """Есть ли в тексте утверждение «я сделал» (не отрицание: «не включил» не считается)."""
    t = text or ""
    if CLAIM_READY.search(t):
        return True
    return any(not _negated(t, m.start()) for m in CLAIM_VERB.finditer(t))


def check(text: str, journal: Journal, tone: str | None = None, gender: str | None = None) -> str:
    """Вернуть текст как есть или честную замену. Решает только код, по журналу хода."""
    if not claims_action(text):
        return text
    if journal.iot_ok:
        if not journal.failures() or ADMISSION.search(text):
            return text                   # было успешное действие; про неудачи модель сама сказала
        return prompts.honest("partial", journal.failures()[-1], tone, gender)
    if CLAIM_READY.search(text) and not CLAIM_VERB.search(text) and journal.any_ok:
        return text                       # «Готово» после remember/forget: память записана, это правда
    fails = journal.failures()
    return prompts.honest("fail" if fails else "none", fails[-1] if fails else NO_ACTION, tone, gender)
