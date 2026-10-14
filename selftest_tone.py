# Ворота 27 (инцидент 15.10): на /setup выбран «С матом», а Кореш на «поматерись» отказывался. Причины и защита:
#  1) память: заметка/пересказ «мат у меня не включается», записанные при прежнем тоне, перебивали новый блок тона;
#  2) смена имени/тона/рода должна начинать новый эпизод сразу;
#  3) приоритет: /setup > переменная (шаблон Railway ставит TONE=derzkiy) > пакет активации;
#  4) /health показывает tone и tone_source. Живых вызовов нет.
_SAVED27 = {k: os.environ.get(k) for k in ("TONE", "GENDER", "ASSISTANT_NAME")}


def _clean27():
    for k in ("TONE", "GENDER", "ASSISTANT_NAME"):
        os.environ.pop(k, None)
    C.save_values({"TONE": None, "GENDER": None, "ASSISTANT_NAME": None})
    C.clear_pack()


MATP27 = "Мат должен быть НАСТОЯЩИЙ"
reset_all()
_clean27()
C.save_values({"ASSISTANT_NAME": "Кореш", "TONE": "derzkiy_mat", "GENDER": "male"})
C.set_pack({"TONE": "derzkiy", "ASSISTANT_NAME": "Кореш", "GENDER": "male"})
check("27a. stored=derzkiy_mat + пакет=derzkiy -> в промпте блок с матом, источник setup",
      C.tone() == "derzkiy_mat" and C.source("TONE") == "setup" and MATP27 in prompts.system_prompt() and "БЕЗ мата" not in prompts.system_prompt())
os.environ["TONE"] = "derzkiy"
check("27b. стартовая переменная TONE=derzkiy НЕ глушит выбор «С матом» с /setup", C.tone() == "derzkiy_mat" and C.source("TONE") == "setup"
      and MATP27 in prompts.system_prompt())
C.save_values({"TONE": None})
_env_ok27 = C.tone() == "derzkiy" and C.source("TONE") == "env"
os.environ.pop("TONE")
check("27c. на /setup ничего не выбрано -> работает переменная; убрали переменную -> пакет", _env_ok27 and C.source("TONE") == "pack")
check("27d. /health показывает tone и tone_source", (lambda j: j.get("tone") == "derzkiy" and j.get("tone_source") == "pack")(TestClient(main.app).get("/health").json()))
C.save_values({"TONE": "derzkiy_mat"})
check("27e. /health: после выбора на /setup tone_source=setup", (lambda j: j.get("tone") == "derzkiy_mat" and j.get("tone_source") == "setup")(TestClient(main.app).get("/health").json()))
sp27 = prompts.system_prompt()
check("27f. промпт: характер задаётся только блоком тона, память про «мат не включён» объявлена устаревшей",
      "Характер: тон, мат, имя и род заданы ТОЛЬКО блоком" in sp27 and "устарело" in sp27)
check("27g. блок «С матом»: на прямую просьбу поматериться матерится без отказов и ссылок на настройки, с рамками",
      "Просит поматериться" in sp27 and "без отказов" in sp27 and "не оскорбляй группы людей" in sp27)
check("27h. блоки «без мата» и «вежливый» этого указания не содержат", "Просит поматериться" not in prompts.system_prompt("derzkiy") and "Просит поматериться" not in prompts.system_prompt("vezhlivyy"))

# --- распознавание утверждений о собственном характере ---
CLAIMS27 = ["мат у меня не включается", "Я не матерюсь, у меня вежливый режим", "Кореш не ругается, такая настройка", "мата у меня нет",
            "отказался материться: мат отключён в настройках", "я вежливый, без мата", "матом не ругаюсь, это запрещено"]
OK27 = ["жену зовут Айгуль", "не ест лук", "купил матрас в спальню", "сдаёт математику в пятницу", "любит дерзкий юмор", "хозяин любит мат",
        "боится материала из Китая", "ремонт в детской"]
check("27i. утверждения «мат не включается / я вежливый» распознаются", all(memory.is_character_claim(c) for c in CLAIMS27),
      str([c for c in CLAIMS27 if not memory.is_character_claim(c)]))
check("27j. обычные факты (матрас, математика, материал, вкусы) не задеваются", not any(memory.is_character_claim(c) for c in OK27),
      str([c for c in OK27 if memory.is_character_claim(c)]))

# --- remember не пишет утверждения о собственных настройках ---
api27 = mem_api()
_clean27()
C.save_values({"TONE": "derzkiy_mat"})
fresh("t27")
st27 = memory.store_for("t27")
r1 = st27.remember("мат у меня не включается")
r2 = st27.remember("жену зовут Айгуль")
check("27k. remember: «мат не включается» отклонён, обычный факт записан", r1[0] == "refused" and r2[0] == "ok" and [n["t"] for n in st27.notes] == ["жену зовут Айгуль"], str(r1))
check("27l. пересказ эпизода: строки про свои настройки выбрасываются, остальное остаётся",
      memory.scrub_summary("Говорили про кино.\nОн сказал, что мат у меня не включается.\nОбещал вернуться к дивану.") == "Говорили про кино.\nОбещал вернуться к дивану.")

# --- отравленная память с диска (до исправления) чистится при первом показе модели; в промпт/пересказ не попадает ---
api27 = mem_api()
_clean27()
C.save_values({"TONE": "derzkiy_mat"})
fresh("t27p")
pf = mem_file("t27p")
pf.write_text(json.dumps({"v": 1, "notes": [{"t": "мат у меня не включается", "ts": 1}, {"t": "жену зовут Айгуль", "ts": 2}],
                          "episodes": [{"text": "Просил поматериться.\nЯ отказался: мат отключён, ты же знаешь.\nГоворили про диван.", "ts": 3}],
                          "archive": "Давно: я вежливый и не ругаюсь.\nЕздили в горы.", "open": ""}, ensure_ascii=False), encoding="utf-8")
restart()
run(say("t27p", "поматерись"))
fb27 = first_block(api27.log[-1]) or ""
check("27m. отравленная память: утверждений про «мат не включается» в начале разговора нет, обычные факты остались",
      "мат у меня не включается" not in fb27 and "мат отключён" not in fb27 and "не ругаюсь" not in fb27
      and "Айгуль" in fb27 and "диван" in fb27 and "горы" in fb27, fb27[:400])
check("27n. отпечаток характера сохранён на диск; чужой файл без отпечатка не мешает",
      mem_json("t27p").get("char") == C.character_sig() and "мат у меня не включается" not in pf.read_text(encoding="utf-8"))
check("27o. в начале разговора пересказ помечен: про мой характер и мат всё устарело", "устарело" in fb27 and "Тон общения" in fb27)

# --- смена тона/имени/рода на /setup начинает новый эпизод СРАЗУ ---
api27 = mem_api()
_clean27()
C.save_values({"TONE": "derzkiy", "ASSISTANT_NAME": "Кореш", "GENDER": "male"})
fresh("t27e")
run(say("t27e", "первая реплика"))
run(say("t27e", "вторая реплика"))
check("27p. без смены характера эпизод продолжается: второй запрос идёт без пересказа в начале", first_block(api27.log[-1]) is None)
C.save_values({"TONE": "derzkiy_mat"})
run(say("t27e", "поматерись"))
last27 = api27.log[-1]
check("27q. смена тона на /setup -> следующая реплика открывает НОВЫЙ эпизод (старая история не тянется), в промпте блок с матом",
      first_block(last27) is not None and MATP27 in last27["system"] and len(last27["msgs"]) == 3, f"{len(last27['msgs'])} {first_block(last27)}")
run(say("t27e", "ещё"))
check("27r. после этого эпизод снова стабилен (кэш промпта не рвётся каждый ход)", first_block(api27.log[-1]) is not None and len(api27.log[-1]["msgs"]) == 5, str(len(api27.log[-1]["msgs"])))
C.save_values({"GENDER": "female"})
run(say("t27e", "и после смены рода"))
check("27s. смена рода тоже начинает новый эпизод", first_block(api27.log[-1]) is not None and len(api27.log[-1]["msgs"]) == 3)
C.save_values({"ASSISTANT_NAME": "Бро"})
run(say("t27e", "и после смены имени"))
check("27t. смена имени тоже начинает новый эпизод, имя в промпте", first_block(api27.log[-1]) is not None and "Тебя зовут Бро" in api27.log[-1]["system"])

# --- модель, держащая в истории старый отказ: после смены тона истории нет ---
check("27u. описание remember запрещает записывать про свой характер", "не про свой характер" in json.dumps(brain.TOOLS, ensure_ascii=False))

_clean27()
for _k, _v in _SAVED27.items():
    if _v is not None:
        os.environ[_k] = _v
