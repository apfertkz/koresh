"""Счётчик расхода: токены за сутки (UTC) и доллары на балансе Anthropic. Хранится в памяти и в маленьком файле на диске
памяти (MEMORY_DIR), чтобы обновление образа не обнуляло потолок и остаток посреди дня.

Доллары считаем сами из response.usage: прочитать остаток баланса с обычного ключа Anthropic нельзя (только в консоли).
Остаток поэтому оценочный: «сколько клиент закинул» минус «сколько мы насчитали». Кнопка «Я пополнил» на /setup обнуляет счёт."""
import json
import os
import time

import config as C
import memory

# ---------- цены: ЕДИНСТВЕННОЕ место. $ за 1 млн токенов (вход, выход, чтение кэша, запись кэша на 5 минут) ----------
# claude-sonnet-5-5: $2 / $10 / $0,20 / $2,50. claude-haiku-5-5 (пересказ эпизода): $0,10 / $0,50 / $0,01 / $0,125.
# Источник: справочник Claude API на 06.10.2026, совпадает с setup-bot/scripts/estimate.js; перед тем как называть цены
# клиенту, сверить со страницей pricing Anthropic. Неизвестная модель считается по цене sonnet (завышенный счёт безопаснее).
PRICES = {
    "sonnet": {"in": 2.00, "out": 10.00, "cache_read": 0.20, "cache_write": 2.50},
    "haiku": {"in": 0.10, "out": 0.50, "cache_read": 0.01, "cache_write": 0.125},
}
DEFAULT_PRICE = "sonnet"
DEFAULT_BUDGET_USD = 5.0
WARN_FRACTION = 0.8                 # с этой доли потраченного Кореш один раз предупреждает
WINDOW_DAYS = 7                     # средний расход для «хватит на N дней»
KEEP_DAYS = 14

_state = {"day": "", "used": 0, "loaded": False,
          "budget": DEFAULT_BUDGET_USD, "spent": 0.0, "since": 0.0, "daily": {},
          "warned": False, "zero_at": 0.0}


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _now() -> float:
    return time.time()


def reset_for_tests():
    _state.update(day="", used=0, loaded=False, budget=DEFAULT_BUDGET_USD, spent=0.0, since=0.0, daily={},
                  warned=False, zero_at=0.0)
    try:
        os.remove(C.usage_file())
    except OSError:
        pass


def _f(v, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if x == x and x not in (float("inf"), float("-inf")) else default
    except (TypeError, ValueError):
        return default


def _load():
    if _state["loaded"]:
        return
    _state["loaded"] = True
    try:
        with open(C.usage_file(), encoding="utf-8") as f:
            d = json.load(f)
        daily = d.get("daily") if isinstance(d.get("daily"), dict) else {}
        _state.update(day=str(d.get("day", "")), used=int(d.get("used", 0)),
                      budget=max(0.0, _f(d.get("budget"), DEFAULT_BUDGET_USD)), spent=max(0.0, _f(d.get("spent"))),
                      since=_f(d.get("since")), warned=bool(d.get("warned")), zero_at=_f(d.get("zero_at")),
                      daily={str(k): max(0.0, _f(v)) for k, v in daily.items()})
    except Exception:  # noqa: BLE001  нет файла / битый файл — считаем с нуля
        pass


def _save():
    try:
        memory.atomic_write(C.usage_file(), json.dumps({
            "day": _state["day"], "used": _state["used"], "budget": _state["budget"], "spent": round(_state["spent"], 6),
            "since": _state["since"], "daily": {k: round(v, 6) for k, v in _state["daily"].items()},
            "warned": _state["warned"], "zero_at": _state["zero_at"]}))
    except OSError as e:  # диск недоступен -- считаем в памяти процесса и не роняем разговор
        memory._log_disk("disk", False, type(e).__name__)


def _roll():
    _load()
    if _state["day"] != _today():
        _state.update(day=_today(), used=0)


def used() -> int:
    _roll()
    return _state["used"]


def add(tokens: int, usd: float = 0.0):
    """tokens -- в суточный потолок; usd -- в счёт долларов (0 = только токены, как раньше)."""
    _roll()
    _state["used"] += max(0, int(tokens))
    usd = max(0.0, _f(usd))
    if usd > 0:
        _state["spent"] += usd
        today = _today()
        daily = _state["daily"]
        daily[today] = daily.get(today, 0.0) + usd
        for k in sorted(daily)[:-KEEP_DAYS]:
            daily.pop(k, None)
    _save()


def exceeded() -> bool:
    cap = C.daily_token_cap()
    return cap > 0 and used() >= cap


# ---------- доллары ----------
def price_for(model: str | None) -> dict:
    m = (model or "").lower()
    return PRICES["haiku"] if "haiku" in m else PRICES[DEFAULT_PRICE]


def cost_usd(u, model: str | None = None) -> float:
    """Стоимость одного ответа в долларах по response.usage (вход, выход с размышлениями, чтение и запись кэша)."""
    if u is None:
        return 0.0
    p = price_for(model)

    def g(k):
        return int((u.get(k) if isinstance(u, dict) else getattr(u, k, 0)) or 0)
    return (g("input_tokens") * p["in"] + g("output_tokens") * p["out"] + g("cache_read_input_tokens") * p["cache_read"]
            + g("cache_creation_input_tokens") * p["cache_write"]) / 1_000_000


def cost_of(resp, model: str | None = None) -> float:
    return cost_usd(getattr(resp, "usage", None), model)


def budget_usd() -> float:
    _load()
    return _state["budget"]


def spent_usd() -> float:
    _load()
    return round(_state["spent"], 4)


def remaining_usd() -> float:
    return max(0.0, budget_usd() - _state["spent"])


def topup(amount) -> float:
    """«Я пополнил»: новая сумма на счету, счётчик с нуля, предупреждения снова разрешены. 0 = следить не надо (автопополнение)."""
    _load()
    amt = round(min(max(0.0, _f(amount, DEFAULT_BUDGET_USD)), 10000.0), 2)
    _state.update(budget=amt, spent=0.0, since=_now(), daily={}, warned=False, zero_at=0.0)
    _save()
    return amt


def avg_daily_usd() -> float:
    """Средний расход в день за последние до 7 дней. Окно начинается с даты пополнения (до неё трат «с этого баланса» нет),
    а если пополнения не отмечали, с первого дня, за который есть траты; один день данных -- делим на один день."""
    _load()
    daily = _state["daily"]
    now = _now()
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    first = time.strftime("%Y-%m-%d", time.gmtime(_state["since"])) if _state["since"] else (min(daily) if daily else today)
    days = []
    for i in range(WINDOW_DAYS):
        d = time.strftime("%Y-%m-%d", time.gmtime(now - i * 86400))
        if d < first:
            break
        days.append(d)
    window = max(1, len(days))
    return sum(daily.get(d, 0.0) for d in days) / window


def days_left() -> int | None:
    """На сколько дней хватит остатка при среднем расходе. None = не знаем (нет расхода или счёт не ведётся)."""
    if budget_usd() <= 0:
        return None
    avg = avg_daily_usd()
    if avg <= 0:
        return None
    return max(1, round(remaining_usd() / avg))


def snapshot() -> dict:
    _roll()
    return {"budget_usd": budget_usd(), "spent_usd": spent_usd(), "remaining_usd": round(remaining_usd(), 4),
            "days_left": days_left(), "avg_daily_usd": round(avg_daily_usd(), 4)}


# ---------- предупреждения ----------
def take_low_warning():
    """Потрачено >= 80% суммы и ещё не предупреждали с последнего пополнения -> число дней (>=1), и больше не вернёт.
    Иначе None. Если средний расход неизвестен, даёт 1 (честнее «хватит ненадолго», чем молчать)."""
    _load()
    b = _state["budget"]
    if b <= 0 or _state["warned"] or _state["spent"] < b * WARN_FRACTION:
        return None
    _state["warned"] = True
    _save()
    return days_left() or 1


NOTICE_GAP = 24 * 3600


def take_zero_notice() -> bool:
    """Деньги кончились (Anthropic ответил billing-ошибкой): True, если хозяину пора писать в WhatsApp. Первая же ошибка
    -> True, дальше не чаще раза в сутки (пока нуль держится, напомним раз в день; починили и снова упало в тот же день -- молчим)."""
    _load()
    now = _now()
    if _state["zero_at"] and now - _state["zero_at"] < NOTICE_GAP:
        return False
    _state["zero_at"] = now
    _save()
    return True
