"""Настройки: переменные окружения > файл MEMORY_DIR/config.json (его пишет страница /setup) > умолчания.
Читаются при каждом обращении, чтобы ворота и страница настройки применялись без перезапуска."""
import json
import os
import time

TONES = ("derzkiy_mat", "derzkiy", "vezhlivyy")
DEFAULT_MODEL = "claude-sonnet-5-5"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = "low"            # голосовому чату нужна скорость; справочник: low для чата
# Потолок ответа с запасом под размышления модели (они считаются в max_tokens). Ответ Алисе режется на 1000 символов
# (~450-500 токенов кириллицы), так что на low хватает ~1500 токенов запаса; выше уровень -- больше запас.
MAX_TOKENS = {"low": 2048, "medium": 3072, "high": 4096, "xhigh": 8192, "max": 12288}
VERSION = os.environ.get("APP_VERSION", "dev")


# Что можно задать через /setup и хранить в config.json. Ключ Anthropic и секрет вебхука сюда НЕ входят: только переменные.
STORED_KEYS = ("YANDEX_IOT_TOKEN", "ASSISTANT_NAME", "TONE", "GENDER", "HOUSEHOLD", "BLOCKED_NAMES",
               "LOCK_PHRASE", "ALLOW_LOCKS", "WA_CONTACTS", "WA_HIDDEN", "WA_NICKS", "TZ_OFFSET", "KIDS_PHRASE")
SECRET_KEYS = ("YANDEX_IOT_TOKEN", "LOCK_PHRASE", "WA_CONTACTS", "WA_HIDDEN", "WA_NICKS", "KIDS_PHRASE")          # значения этих ключей не показываем и не логируем
_CFG = {"sig": None, "data": None}                        # кэш файла по отпечатку (inode, mtime, размер)
_MEM_ONLY: dict = {}                                       # копия, если диск недоступен: живёт до перезапуска
# Слой «пакет активации» (см. activation.py): персональные настройки из сервера лицензий. Живёт ТОЛЬКО в оперативке,
# на диск открытым не пишется. Приоритет: переменная Railway > пакет > config.json > умолчание.
PACK_KEYS = ("ASSISTANT_NAME", "TONE", "GENDER")
_PACK: dict = {}
# Модули (ТЗ-32): флаги из пакета лицензии. Нет флага = модуля нет. Отдельный слой от _PACK, потому что рубильник обязан срабатывать
# сразу после обновления пакета (раз в час), а не ждать границы эпизода: см. activation.refresh_once.
MODULE_NAMES = ("macros", "timers", "kids")
_MODULES: dict = {}          # явные значения рубильника: имя -> True | False
_PROMO = {"all": False}      # акция запуска / промокод «все модули» (pack.promo_all_modules): всё включено, кроме явно выключенного


def set_modules(flags, promo: bool = False) -> None:
    _MODULES.clear()
    _PROMO["all"] = promo is True
    if isinstance(flags, dict):
        _MODULES.update({k: flags[k] for k in MODULE_NAMES if isinstance(flags.get(k), bool)})


def module_on(name: str) -> bool:
    """Явный флаг главнее акции: рубильник «выключить» работает и у клиента с акцией."""
    if name in _MODULES:
        return _MODULES[name]
    return name in MODULE_NAMES and _PROMO["all"]


def set_pack(pack: dict) -> None:
    _PACK.clear()
    _PACK.update(json.loads(json.dumps(pack)))
    set_modules(_PACK.get("modules"), _PACK.get("promo_all_modules") is True)


def clear_pack() -> None:
    _PACK.clear()
    _MODULES.clear()
    _PROMO["all"] = False


def pack() -> dict:
    return _PACK


def pack_active() -> bool:
    return bool(_PACK)


def config_path() -> str:
    return os.path.join(env("MEMORY_DIR", "/data"), "config.json")


def _read_config() -> dict:
    """Весь config.json как dict ({"values": {...}, "setup": {...}}). Нет файла / битый -> копия из оперативки или {}."""
    path = config_path()
    try:
        st = os.stat(path)
    except OSError:
        return _MEM_ONLY.get(path) or {}
    sig = (path, st.st_ino, st.st_mtime_ns, st.st_size)
    if _CFG["sig"] == sig and _CFG["data"] is not None:
        return _CFG["data"]
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = _MEM_ONLY.get(path) or {}
    _CFG.update(sig=sig, data=data)
    return data


def stored(name: str) -> str:
    """Значение из config.json строкой (HOUSEHOLD -> JSON). Нет -> ''."""
    v = (_read_config().get("values") or {}).get(name)
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, bool):
        return "1" if v else ""
    return str(v).strip()


def source(name: str) -> str:
    """Откуда берётся значение: env (переменная Railway) | setup (config.json) | default."""
    if (os.environ.get(name) or "").strip():
        return "env"
    if name in PACK_KEYS and _PACK.get(name):
        return "pack"
    return "setup" if name in STORED_KEYS and stored(name) else "default"


def _write_config(data: dict) -> bool:
    """Атомарно (временный файл + rename), права 600. False -> диск недоступен, значения живут до перезапуска."""
    path = config_path()
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=1))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        _MEM_ONLY.pop(path, None)
        _CFG.update(sig=None, data=None)
        return True
    except OSError as e:
        _MEM_ONLY[path] = data
        print(f"config: не удалось записать config.json ({type(e).__name__}) -> настройки живут до перезапуска", flush=True)
        try:
            os.remove(tmp)
        except OSError:
            pass
        _CFG.update(sig=None, data=None)
        return False


def save_values(updates: dict) -> bool:
    """updates: {ИМЯ: строка | dict | None}. None / '' -> убрать ключ. Только из STORED_KEYS. Значения в журнал не пишем."""
    bad = [k for k in updates if k not in STORED_KEYS]
    if bad:
        raise KeyError(bad[0])
    data = json.loads(json.dumps(_read_config()))           # глубокая копия
    values = data.setdefault("values", {})
    for k, v in updates.items():
        if v is None or v == "" or v == {}:
            values.pop(k, None)
        else:
            values[k] = v
    data["saved_at"] = int(time.time())
    ok = _write_config(data)
    print(f"config: сохранено {', '.join(sorted(updates))}" + ("" if ok else " (только в памяти)"), flush=True)
    return ok


def setup_done() -> bool:
    return bool((_read_config().get("setup") or {}).get("done"))


def set_setup_done(done: bool) -> bool:
    data = json.loads(json.dumps(_read_config()))
    data.setdefault("setup", {})["done"] = bool(done)
    if done:
        data["setup"]["done_at"] = int(time.time())
    return _write_config(data)


def env(name: str, default: str = "") -> str:
    v = (os.environ.get(name) or "").strip()
    if not v and name in PACK_KEYS:
        v = str(_PACK.get(name) or "").strip()
    if not v and name in STORED_KEYS:
        v = stored(name)
    return v or default.strip()


def flag(name: str) -> bool:
    return env(name).lower() in ("1", "true", "yes", "on", "да")


def anthropic_key() -> str:
    return env("ANTHROPIC_API_KEY")


def model() -> str:
    return env("MODEL", DEFAULT_MODEL)


def effort() -> str:
    """Мусор -> low: опечатка в настройке не должна включать дорогое и медленное размышление."""
    e = env("EFFORT", DEFAULT_EFFORT).lower()
    return e if e in EFFORTS else DEFAULT_EFFORT


def supports_effort(model_name: str | None = None) -> bool:
    """claude-haiku-4-5 и древнее параметра effort не знают -> 400. Для них не шлём."""
    m = (model_name or model()).lower()
    return not (m.startswith("claude-3") or "haiku-4" in m)


def max_tokens() -> int:
    return MAX_TOKENS[effort()]


def assistant_name() -> str:
    return env("ASSISTANT_NAME", "Кореш")


def tone() -> str:
    """Неизвестное значение -> «дерзкий без мата»: ошибка в настройке не должна включать мат."""
    t = env("TONE", "derzkiy").lower()
    return t if t in TONES else "derzkiy"


GENDERS = ("male", "female")


def gender() -> str:
    """Род ассистента о себе: male (по умолчанию, бренд «Кореш») | female (голос Алисы). Мусор -> male."""
    g = env("GENDER", "male").lower()
    return g if g in GENDERS else "male"


def household(with_pack: bool = True) -> dict:
    """HOUSEHOLD — необязательный JSON. Понимаются ключи:
    owner (как обращаться), people ({имя: кто это}), rooms ({как говорят: как названо в Яндексе}), notes (строка)."""
    raw = env("HOUSEHOLD")
    data: dict = {}
    if raw:
        try:
            parsed = json.loads(raw)
            data = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            print("HOUSEHOLD: невалидный JSON, игнорирую", flush=True)
    if not with_pack or (os.environ.get("HOUSEHOLD") or "").strip() or not _PACK:
        return data
    data = dict(data)                     # пакет перекрывает config.json, но не переменную Railway
    for k in ("owner", "people", "rooms"):
        if _PACK.get(k):
            data[k] = _PACK[k]
    return data


def pack_notes() -> str:
    return str(_PACK.get("notes") or "").strip()


def webhook_secret() -> str:
    return env("WEBHOOK_SECRET")


def daily_token_cap() -> int:
    try:
        return int(env("DAILY_TOKEN_CAP", "300000"))   # 0 = без потолка
    except ValueError:
        return 300000


def hold_timeout() -> float:
    """Сколько ждём мозг, прежде чем отдать заглушку. Яндекс режет вебхук примерно на 3-4,5 с."""
    try:
        return float(env("HOLD_TIMEOUT", "3.8"))
    except ValueError:
        return 2.6


def iot_token() -> str:
    return env("YANDEX_IOT_TOKEN")


def allow_locks() -> bool:
    return flag("ALLOW_LOCKS")


def lock_phrase() -> str:
    return env("LOCK_PHRASE")


def tz_offset() -> float:
    """Часовой пояс хозяина, часы от UTC. По умолчанию Алматы (+5). Мусор -> +5."""
    try:
        v = float(env("TZ_OFFSET", "5").replace(",", "."))
    except ValueError:
        return 5.0
    return v if -12.0 <= v <= 14.0 else 5.0


def kids_phrase() -> str:
    """Кодовая фраза взрослого для выключения детского режима (задаётся на /setup). Пусто -> детский режим не включается."""
    return env("KIDS_PHRASE")


def wa_enabled() -> bool:
    """WA_ENABLED=0 (или off/false/нет) -- аварийный выключатель WhatsApp: демон не стартует, инструментов у модели нет."""
    return env("WA_ENABLED", "1").lower() not in ("0", "false", "no", "off", "нет")


def wa_contacts() -> dict:
    """WA_CONTACTS -- JSON {имя: номер}: кому Кореш может писать в WhatsApp (вписывает клиентка на /setup).
    Номера живут только здесь и в запросе к WhatsApp, в запросы к модели не уходят никогда."""
    raw = env("WA_CONTACTS")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print("WA_CONTACTS: невалидный JSON, игнорирую", flush=True)
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _json_env(name: str, kind):
    raw = env(name)
    if not raw:
        return kind()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(f"{name}: невалидный JSON, игнорирую", flush=True)
        return kind()
    return data if isinstance(data, kind) else kind()


def wa_hidden() -> set:
    """WA_HIDDEN -- JSON-список номеров (цифры) из синхронизации WhatsApp, которых клиентка скрыла: Кореш о них не знает."""
    return {str(x) for x in _json_env("WA_HIDDEN", list)}


def wa_nicks() -> dict:
    """WA_NICKS -- JSON {номер цифрами: [прозвища]} («жена» -> Оля): по прозвищу контакт находит сервер."""
    out = {}
    for k, v in _json_env("WA_NICKS", dict).items():
        if isinstance(v, list):
            out[str(k)] = [str(x) for x in v]
    return out


def wa_dir() -> str:
    """Каталог WhatsApp на Volume: сессия (auth/), признак привязки, лимиты, согласие. Права 0700."""
    return os.path.join(memory_dir(), "wa")


def extra_blocked_names(with_pack: bool = True) -> list[str]:
    own = [x.strip().lower() for x in env("BLOCKED_NAMES").split(",") if x.strip()]
    if not with_pack:
        return own
    return own + [x for x in (_PACK.get("blocked") or []) if x not in own]


def memory_dir() -> str:
    """Каталог долгой памяти (на Railway это Volume, mount path /data)."""
    return env("MEMORY_DIR", "/data")


def memory_model() -> str:
    """Дешёвая модель для пересказа закрытого эпизода (вне ответа пользователю)."""
    return env("MEMORY_MODEL", "claude-haiku-5-5")


def usage_file() -> str:
    """Суточный счётчик лежит рядом с памятью, чтобы переживать обновление образа. USAGE_FILE -- ручная подмена."""
    return env("USAGE_FILE") or os.path.join(memory_dir(), "usage.json")
