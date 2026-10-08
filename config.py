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
               "LOCK_PHRASE", "ALLOW_LOCKS")
SECRET_KEYS = ("YANDEX_IOT_TOKEN", "LOCK_PHRASE")          # значения этих ключей не показываем и не логируем
_CFG = {"sig": None, "data": None}                        # кэш файла по отпечатку (inode, mtime, размер)
_MEM_ONLY: dict = {}                                       # копия, если диск недоступен: живёт до перезапуска


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


def household() -> dict:
    """HOUSEHOLD — необязательный JSON. Понимаются ключи:
    owner (как обращаться), people ({имя: кто это}), rooms ({как говорят: как названо в Яндексе}), notes (строка)."""
    raw = env("HOUSEHOLD")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print("HOUSEHOLD: невалидный JSON, игнорирую", flush=True)
        return {}
    return data if isinstance(data, dict) else {}


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


def extra_blocked_names() -> list[str]:
    return [x.strip().lower() for x in env("BLOCKED_NAMES").split(",") if x.strip()]


def memory_dir() -> str:
    """Каталог долгой памяти (на Railway это Volume, mount path /data)."""
    return env("MEMORY_DIR", "/data")


def memory_model() -> str:
    """Дешёвая модель для пересказа закрытого эпизода (вне ответа пользователю)."""
    return env("MEMORY_MODEL", "claude-haiku-5-5")


def usage_file() -> str:
    """Суточный счётчик лежит рядом с памятью, чтобы переживать обновление образа. USAGE_FILE -- ручная подмена."""
    return env("USAGE_FILE") or os.path.join(memory_dir(), "usage.json")
