"""Долгая память Кореша: заметки о человеке + пересказы прошлых разговоров, на диске сервера.

Один JSON на пользователя Алисы в MEMORY_DIR (на Railway это Volume с mount path /data). Запись атомарная
(tmp + rename). Каталог недоступен -- память живёт в оперативке, в лог пишется одна честная строка, разговор не падает.

В разговор память попадает ТОЛЬКО в начале эпизода (history.fresh_episode) и внутри эпизода не меняется:
системный промпт и префикс запросов остаются неизменными (кэш, привязка thinking-блоков). Подробности -- MEMORY.md."""
import hashlib
import json
import os
import re
import time

import config as C

MAX_NOTES = 100                 # заметок о человеке
NOTE_CHARS = 200                # символов в заметке
MAX_EPISODES = 10               # пересказов хранится отдельно, старше -- склеиваются в archive
SUMMARY_CHARS = 1400            # потолок одного пересказа
SUMMARY_LINES = 10
ARCHIVE_CHARS = 1500            # склейка старых пересказов (хвост, т.е. самое свежее из старого)
BLOCK_SUMMARY_BUDGET = 6000     # сколько символов пересказов попадает в начало эпизода


# ---------- атомарная запись ----------
def atomic_write(path: str, text: str) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ---------- секреты: что в память не попадает никогда ----------
SECRET_WORDS = re.compile(
    r"парол|password|passwd|passphrase|\bpin\b|пин[\s\-]?код|пинкод|\bcvv\b|\bcvc\b|"
    r"кодов\w*\s+(?:слов|фраз)|секретн\w*\s+(?:слов|код|фраз)|одноразов\w*\s+код|код\s+(?:из\s+)?(?:смс|sms)|"
    r"(?:смс|sms)[\s\-]?код|токен|\btoken\b|api[\s\-]?key|ключ\s+(?:api|доступа)|sk-ant|"
    r"номер\w*\s+(?:карт|счёт|счет)|\bиин\b|\biban\b", re.I)
CODE_DIGITS = re.compile(r"(?:\bкод\w*|\bcode\b)\W{0,12}\d{3,}", re.I)
LONG_DIGITS = re.compile(r"(?:\d[ \-]?){12,}")          # номера карт (13-19), ИИН (12), счета
TOKEN_LIKE = re.compile(r"\b(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{24,}\b")


def _squash(s: str) -> str:
    return re.sub(r"[\W_]+", "", s.casefold())


def has_lock_phrase(text: str) -> bool:
    phrase = C.lock_phrase()
    if not phrase:
        return False
    sq = _squash(phrase)
    return bool(sq) and sq in _squash(text)


def looks_secret(text: str) -> bool:
    """Пароли, коды, номера карт и токены. Лучше лишний отказ, чем утёкший секрет."""
    return bool(has_lock_phrase(text) or SECRET_WORDS.search(text) or CODE_DIGITS.search(text)
                or LONG_DIGITS.search(text) or TOKEN_LIKE.search(text))


def scrub_summary(text: str) -> str:
    """Пересказ построчно: строка с секретом выбрасывается целиком, остальное режется по потолкам."""
    lines = []
    for ln in (text or "").splitlines():
        ln = " ".join(ln.split())
        if ln and not looks_secret(ln):
            lines.append(ln)
    lines = lines[-SUMMARY_LINES:] if len(lines) > SUMMARY_LINES else lines
    out = "\n".join(lines)
    if len(out) > SUMMARY_CHARS:
        out = out[-SUMMARY_CHARS:]
        out = out[out.find("\n") + 1:] if "\n" in out else out      # не начинаем с огрызка строки
    return out.strip()


# ---------- сопоставление «забудь, что ...» ----------
def _norm(t: str) -> str:
    return " ".join(re.findall(r"\w+", t.casefold()))


def _stems(t: str) -> set[str]:
    return {w[:5] for w in re.findall(r"\w+", t.casefold()) if len(w) >= 3}


def _matches(query: str, text: str, threshold: float) -> bool:
    nq, nt = _norm(query), _norm(text)
    if not nq:
        return False
    if nq in nt or nt in nq:
        return True
    qs = _stems(query)
    return bool(qs) and len(qs & _stems(text)) / len(qs) >= threshold


# ---------- хранилище одного пользователя ----------
_warned: dict[str, bool] = {}


def _log_disk(kind: str, ok: bool, detail: str = "") -> None:
    """Пишем только при смене состояния диска, без содержимого памяти."""
    if _warned.get(kind, False) == (not ok):      # состояние не сменилось -> молчим
        return
    _warned[kind] = not ok
    if ok:
        print(f"memory: диск {C.memory_dir()} снова доступен", flush=True)
    else:
        hint = " На Railway: Volume с mount path /data и переменная RAILWAY_RUN_UID=0." if "Permission" in detail else ""
        print(f"memory: каталог {C.memory_dir()} недоступен ({detail}) -> работаю в памяти процесса, "
              f"после перезапуска память пропадёт.{hint}", flush=True)


class Store:
    def __init__(self, uid: str):
        self.uid = uid
        self.notes: list[dict] = []        # {"t": текст, "ts": unix}
        self.episodes: list[dict] = []     # {"text": пересказ, "ts": unix}, <= MAX_EPISODES
        self.archive = ""                  # склейка более старых пересказов
        self.open = ""                     # локальный снимок незакрытого эпизода (на случай перезапуска)
        # не хранится: что просили забыть в текущем эпизоде -> пересказ этого эпизода не должен это вернуть
        self.forgotten: list[str] = []
        self.wiped = False
        self._load()

    @property
    def path(self) -> str:
        return os.path.join(C.memory_dir(), "mem-" + hashlib.sha256(self.uid.encode()).hexdigest()[:24] + ".json")

    # --- диск ---
    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            if isinstance(e, ValueError):          # битый файл: откладываем в сторону, начинаем с чистого
                try:
                    os.replace(self.path, self.path + ".corrupt")
                except OSError:
                    pass
                print("memory: файл памяти битый, отложен в .corrupt, начинаю с чистого", flush=True)
            else:
                _log_disk("disk", False, type(e).__name__)
            return
        if not isinstance(d, dict):
            return
        self.notes = [n for n in d.get("notes", []) if isinstance(n, dict) and isinstance(n.get("t"), str)][:MAX_NOTES]
        self.episodes = [e for e in d.get("episodes", []) if isinstance(e, dict) and isinstance(e.get("text"), str)]
        self.archive = str(d.get("archive", ""))[-ARCHIVE_CHARS:]
        self.open = str(d.get("open", ""))
        if self.open.strip():                       # прошлый процесс умер посреди эпизода -> это тоже разговор
            self.add_episode(self.open, save=False)
            self.open = ""
            self.save()

    def save(self) -> bool:
        data = {"v": 1, "notes": self.notes, "episodes": self.episodes, "archive": self.archive, "open": self.open}
        try:
            atomic_write(self.path, json.dumps(data, ensure_ascii=False))
            _log_disk("disk", True)
            return True
        except OSError as e:
            _log_disk("disk", False, type(e).__name__)
            return False

    # --- заметки ---
    def remember(self, fact: str) -> tuple[str, str]:
        """-> (статус, текст для модели). Статусы: ok | dup | empty | refused | full."""
        fact = " ".join((fact or "").split())
        if not fact:
            return "empty", "error: пустая заметка"
        if looks_secret(fact):
            return "refused", ("refused: это похоже на пароль, код, номер карты или секрет, такое не запоминаю. "
                               "Скажи хозяину, что секреты ты не хранишь, лучше с шуткой.")
        fact = fact[:NOTE_CHARS].rstrip()
        nf = _norm(fact)
        for n in self.notes:
            nn = _norm(n["t"])
            if nf == nn or nf in nn:
                return "dup", "ok: уже помню это"
            if nn in nf:                              # новая заметка полнее старой -> обновляем, не плодим
                n["t"], n["ts"] = fact, int(time.time())
                self.save()
                return "ok", f"ok: уточнил заметку (всего {len(self.notes)})"
        if len(self.notes) >= MAX_NOTES:
            return "full", (f"full: уже {MAX_NOTES} заметок. Новое не записано. Если это важнее старого, "
                            "сначала забудь что-нибудь ненужное через forget.")
        self.notes.append({"t": fact, "ts": int(time.time())})
        self.save()
        return "ok", f"ok: запомнил (всего {len(self.notes)})"

    def forget(self, fact: str = "", everything: bool = False) -> str:
        if everything:
            self.notes, self.episodes, self.archive, self.open = [], [], "", ""
            self.wiped = True
            self.save()
            return ("ok: стёрто всё, что я знал о хозяине и прошлых разговорах. "
                    "В этом разговоре веди себя так, будто этого никогда не знал, и не упоминай забытое.")
        fact = (fact or "").strip()
        if not fact:
            return "error: что забыть? укажи fact или all=true"
        kept = [n for n in self.notes if not _matches(fact, n["t"], 0.6)]
        removed = len(self.notes) - len(kept)
        self.notes = kept
        dropped = self._drop_summary_lines(fact)
        self.forgotten.append(fact)
        if removed or dropped:
            self.save()
            return ("ok: забыл. В этом разговоре не упоминай забытое и веди себя так, будто не знал. "
                    f"Осталось заметок: {len(self.notes)}")
        shown = "; ".join(n["t"] for n in self.notes[:30]) or "ничего не помню"
        return f"not_found: такой заметки нет. Сейчас помню: {shown}"

    def _drop_summary_lines(self, fact: str) -> int:
        n = 0

        def clean(text: str) -> str:
            nonlocal n
            keep = []
            for ln in text.splitlines():
                if _matches(fact, ln, 0.5):
                    n += 1
                else:
                    keep.append(ln)
            return "\n".join(keep)

        for e in self.episodes:
            e["text"] = clean(e["text"])
        self.episodes = [e for e in self.episodes if e["text"].strip()]
        self.archive = clean(self.archive)
        self.open = clean(self.open)
        return n

    # --- пересказы ---
    def episode_filter(self, text: str) -> str:
        """Пересказ закрывающегося эпизода: без секретов и без того, что в нём же попросили забыть."""
        if self.wiped:
            return ""
        text = scrub_summary(text)
        if self.forgotten:
            text = "\n".join(ln for ln in text.splitlines()
                             if not any(_matches(q, ln, 0.5) for q in self.forgotten))
        return text.strip()

    def episode_closed(self) -> None:
        self.forgotten, self.wiped = [], False
        self.open = ""

    def add_episode(self, text: str, save: bool = True) -> None:
        text = scrub_summary(text)
        if not text:
            return
        self.episodes.append({"text": text, "ts": int(time.time())})
        while len(self.episodes) > MAX_EPISODES:
            old = self.episodes.pop(0)
            merged = (self.archive + "\n" + old["text"]).strip()
            if len(merged) > ARCHIVE_CHARS:         # режем с головы по границе строки: свежее из старого живёт дольше
                merged = merged[-ARCHIVE_CHARS:]
                merged = merged[merged.find("\n") + 1:] if "\n" in merged else merged
            self.archive = merged
        if save:
            self.save()

    def set_open(self, text: str) -> None:
        """Снимок текущего эпизода: после перезапуска он станет пересказом. Пишем только при изменении."""
        if self.wiped:
            return
        text = scrub_summary(text)
        if self.forgotten:
            text = "\n".join(ln for ln in text.splitlines() if not any(_matches(q, ln, 0.5) for q in self.forgotten))
        if text != self.open:
            self.open = text
            self.save()

    # --- что показать модели в начале эпизода ---
    def render_summaries(self) -> str:
        picked, used = [], 0
        for e in reversed(self.episodes):
            line = f"[{time.strftime('%d.%m', time.gmtime(e.get('ts', 0)))}]\n{e['text']}"
            if used + len(line) > BLOCK_SUMMARY_BUDGET and picked:
                break
            picked.append(line)
            used += len(line)
        picked.reverse()
        parts = []
        if self.archive.strip() and used + len(self.archive) <= BLOCK_SUMMARY_BUDGET + ARCHIVE_CHARS:
            parts.append("[давно]\n" + self.archive.strip())
        parts += picked
        return "\n".join(parts)

    def render_block(self) -> str:
        """Текст памяти для начала эпизода ('' -- помнить пока нечего)."""
        out = []
        if self.notes:
            out.append("Что я знаю о хозяине (мои заметки):\n" + "\n".join("- " + n["t"] for n in self.notes))
        s = self.render_summaries()
        if s:
            out.append("О прошлых разговорах (старое сверху):\n" + s)
        return "\n\n".join(out)


def disk_ok() -> bool:
    """Можно ли писать в MEMORY_DIR прямо сейчас (для /health)."""
    d = C.memory_dir()
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return False
    return os.access(d, os.W_OK)


# ---------- кэш хранилищ процесса ----------
STORES: dict[str, Store] = {}


def store_for(uid: str) -> Store:
    st = STORES.get(uid)
    if st is None:
        st = STORES[uid] = Store(uid)
    return st


def reset_cache() -> None:
    """Имитация перезапуска процесса: оперативная копия забыта, дальше читаем с диска."""
    STORES.clear()
    _warned.clear()
