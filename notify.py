"""Уведомления хозяину в Телеграм через наш бот (ТЗ-32).

Образ сам в Телеграм не ходит: у клиента нет токена бота. Он кладёт уведомление в очередь на сервере лицензий
(POST /v1/notify, авторизация как у обновления пакета: install_id + токен обновления), а наш бот забирает очередь
и шлёт по tg_id этой установки. Кнопку-ссылку («Пополнить баланс») выбирает СЕРВЕР по виду уведомления, ссылку от образа он не принимает.

Правила: сбой тихий (уведомление не должно ронять разговор), сеть упала -> короткая очередь на диске (до 20 штук, живут сутки)
и повтор на следующем обновлении пакета; 4xx от сервера -> не повторяем. Деньги не чаще раза в сутки гарантирует вызывающий
код (usage.take_*), и ещё раз сервер. Без активации по коду (нет токена обновления) уведомление молча не уходит никуда."""
import asyncio
import json
import os
import re
import time

import activation
import config as C
import wa

MONEY_KINDS = ("balance_low", "balance_zero")
KINDS = MONEY_KINDS + ("reminder", "timer_done", "module", "settings_link")
OUTBOX = "notify-outbox.json"
OUTBOX_MAX = 20
OUTBOX_TTL = 24 * 3600
BILLING_URL = "https://console.anthropic.com/settings/billing"
TIMEOUT = 5.0

_BG: set = set()


def _now() -> float:
    return time.time()


def _outbox_path() -> str:
    return os.path.join(C.memory_dir(), OUTBOX)


def _read_outbox() -> list:
    try:
        with open(_outbox_path(), encoding="utf-8") as f:
            d = json.load(f)
        return [x for x in d if isinstance(x, dict)] if isinstance(d, list) else []
    except (OSError, ValueError):
        return []


def _write_outbox(items: list) -> None:
    try:
        if not items:
            try:
                os.remove(_outbox_path())
            except OSError:
                pass
            return
        os.makedirs(C.memory_dir(), exist_ok=True)
        tmp = f"{_outbox_path()}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
        os.replace(tmp, _outbox_path())
    except OSError:
        pass


def clean_text(kind: str, text: str) -> str:
    t = str(text or "")
    if kind in MONEY_KINDS:
        t = t.replace(BILLING_URL, " ")             # ссылка идёт кнопкой
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+", " ", t)).strip()[:500]


def _body(iid: str, kind: str, text: str) -> dict:
    b = {"install_id": iid, "kind": kind, "text": text}
    dom = activation.server_domain()
    if dom:
        b["server_domain"] = dom         # адрес своего сервера: бот по нему даёт ссылку «⚙️ Мой Кореш» и после 7 суток доступа
    return b


async def _post_once(kind: str, text: str) -> str:
    """sent | drop | retry."""
    auth = activation.auth()
    if not auth:
        return "drop"
    iid, rt = auth
    try:
        r = await activation._post("/v1/notify", _body(iid, kind, text),
                                   headers={"Authorization": f"Bearer {rt}"}, timeout=TIMEOUT)
    except Exception as e:  # noqa: BLE001
        print(f"notify: очередь недоступна ({type(e).__name__})", flush=True)
        return "retry"
    if r.status_code == 200:
        return "sent"
    if r.status_code >= 500:
        print(f"notify: сервер ответил {r.status_code}", flush=True)
        return "retry"
    print(f"notify: сервер отклонил {r.status_code}", flush=True)
    return "drop"


async def post(kind: str, text: str) -> str:
    """Положить уведомление в очередь на сервере лицензий. sent | queued (в локальной очереди на повтор) | skip | drop."""
    if kind not in KINDS or not activation.configured() or not activation.auth():
        return "skip"
    body = clean_text(kind, text)
    if not body:
        return "skip"
    res = await _post_once(kind, body)
    if res == "retry":
        items = [x for x in _read_outbox() if _now() - float(x.get("at") or 0) < OUTBOX_TTL]
        items.append({"kind": kind, "text": body, "at": _now()})
        _write_outbox(items[-OUTBOX_MAX:])
        return "queued"
    return res


SETTINGS_GAP = 60.0
_LAST_LINK = {"at": 0.0}


async def settings_link() -> str:
    """Голосовое «пришли ссылку на настройки» (ТЗ-34): положить settings_link в очередь, бот пришлёт две кнопки-ссылки в Телеграм.
    Итог для честного ответа голосом: queued | again (только что слали) | no_tg (сервер лицензий не знает Телеграма клиента) | off (нет активации) | fail.
    Локальной очереди на повтор нет: ссылка, которая пришла через час, уже не то, что просили."""
    if not activation.configured() or not activation.auth():
        return "off"
    t = _now()
    if t - _LAST_LINK["at"] < SETTINGS_GAP:
        return "again"
    iid, rt = activation.auth()
    try:
        r = await activation._post("/v1/notify", _body(iid, "settings_link", "Ссылки на настройки твоего Кореша."),
                                   headers={"Authorization": f"Bearer {rt}"}, timeout=TIMEOUT)
    except Exception as e:  # noqa: BLE001
        print(f"notify: ссылка на настройки: сеть {type(e).__name__}", flush=True)
        return "fail"
    if r.status_code != 200:
        print(f"notify: ссылка на настройки: сервер ответил {r.status_code}", flush=True)
        return "fail"
    try:
        status = str((r.json() or {}).get("status") or "")
    except ValueError:
        status = ""
    if status == "no_tg":
        return "no_tg"
    _LAST_LINK["at"] = t
    return "queued"


async def flush() -> int:
    """Повторить то, что не ушло. Зовётся после обновления пакета (раз в час). Возвращает, сколько ушло."""
    items = _read_outbox()
    if not items or not activation.auth():
        return 0
    left, sent = [], 0
    for x in items:
        if _now() - float(x.get("at") or 0) >= OUTBOX_TTL or x.get("kind") not in KINDS:
            continue
        res = await _post_once(x["kind"], str(x.get("text") or ""))
        if res == "retry":
            left.append(x)
        elif res == "sent":
            sent += 1
    _write_outbox(left)
    return sent


async def send_owner(kind: str, text: str, wa_too: bool = True) -> None:
    """Хозяину: в WhatsApp «себе» (если привязан) и в очередь Телеграма. Любая ошибка молча проглатывается."""
    if wa_too:
        try:
            await wa.notify_owner(text)
        except Exception as e:  # noqa: BLE001
            print(f"notify: WhatsApp хозяину не ушёл ({type(e).__name__})", flush=True)
    try:
        await post(kind, text)
    except Exception as e:  # noqa: BLE001
        print(f"notify: очередь не приняла ({type(e).__name__})", flush=True)


def post_bg(kind: str, text: str) -> None:
    """Фоном, не задерживая ответ Алисе."""
    async def _go():
        try:
            await post(kind, text)
        except Exception as e:  # noqa: BLE001
            print(f"notify: очередь не приняла ({type(e).__name__})", flush=True)
    try:
        t = asyncio.get_running_loop().create_task(_go())
    except RuntimeError:
        return
    _BG.add(t)
    t.add_done_callback(_BG.discard)


async def drain() -> None:
    while _BG:
        await asyncio.gather(*list(_BG), return_exceptions=True)
