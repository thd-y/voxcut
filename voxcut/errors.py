"""Error contract (D66): a machine code plus explicit params; the UI translates, the backend never
knows its language. The Russian template here keeps str(exc) for the CLI and logs as it was."""

import re

# code -> (param names, Russian template). Templates use {name} only; precision is in the value.
CODES: dict[str, tuple[tuple[str, ...], str]] = {
    # any module
    "internal_error": ((), "внутренняя ошибка сервера"),
    "legacy_error": (("detail",), "{detail}"),
    # intervals.py
    "session_bad_start": (("text",),
        ("--start {text}: ожидается локальное время, например \"2025-03-01 09:00\" или "
         "\"2025-03-01 09:00:05\"")),
    "session_overlap": (("a", "b", "gap", "tol"),
        "файлы {a} и {b} перекрываются по времени на {gap} с (допуск {tol} с)"),
    "session_pause": (("minutes", "a", "b"),
        "между частями пауза {minutes} мин ({a} → {b}); склейка пауз пока не поддерживается"),
    "session_chain_choose": (("n", "lines"),
        ("в папке {n} цепочки файлов, выберите одну через --chain N:\n"
         "{lines}")),
    "session_chain_range": (("n", "total", "lines"),
        ("--chain {n} вне диапазона 1..{total}:\n"
         "{lines}")),
    # audio.py
    "ffmpeg_missing": ((), "ffmpeg не найден, установите: sudo apt install ffmpeg"),
    "ffmpeg_version_failed": (("path", "rc", "detail"),
        "{path} -version завершился с кодом {rc}: {detail}"),
    "ffmpeg_failed": (("rc", "detail", "cmd"),
        ("ffmpeg завершился с кодом {rc}: {detail}\n"
         "команда: {cmd}")),
    "ffprobe_missing": ((), "ffprobe не найден, установите: sudo apt install ffmpeg"),
    "ffprobe_failed": (("rc", "detail", "cmd"),
        ("ffprobe завершился с кодом {rc}: {detail}\n"
         "команда: {cmd}")),
    "source_no_audio": (("src",), "{src}: нет аудиодорожки"),
    "source_not_riff": (("src",), "{src}: не RIFF/RF64 WAVE"),
    "source_wav_broken": (("src", "detail", "fix"),
        ("{src}: битая структура WAV: {detail}.\n"
         "Пересохраните: {fix}")),
    "source_unreadable": (("src", "detail", "fix"),
        ("{src}: soundfile не читает файл ({detail}).\n"
         "Конвертируйте: {fix}\n"
         "дальше источник — этот файл")),
    "source_format": (("src", "format", "fix"),
        ("{src}: формат {format}, в v1 только WAV или FLAC.\n"
         "Конвертируйте: {fix}\n"
         "дальше источник — этот файл")),
    "source_codec": (("src", "codec", "fix"),
        ("{src}: кодек {codec}, сэмпл-точность не гарантирована.\n"
         "Конвертируйте: {fix}\n"
         "дальше источник — этот файл")),
    "source_frames_mismatch": (("src", "frames", "n", "fix"),
        ("{src}: soundfile видит {frames} сэмплов, ffmpeg декодирует {n}.\n"
         "Конвертируйте: {fix}\n"
         "дальше источник — этот файл")),
    "source_stereo": (("channels", "max"),
        "в источнике {channels} канала(ов): задайте channel = индекс 0..{max} или \"mix\""),
    "source_bad_channel": (("channel", "max"),
        "channel = {channel}: ожидается \"mono\", \"mix\" или индекс 0..{max}"),
    "source_copy_samples": (("path", "got", "expected"),
        "{path}: {got} сэмплов, ожидалось {expected}"),
    "run_env_changed": (("dir", "detail", "rm"),
        ("{dir}: окружение изменилось ({detail}).\n"
         "Результат не переиспользуется. Удалите каталог и запустите заново: rm -rf {rm}")),
    "run_missing": (("path",), "нет {path}: каталог неполный или не тот"),
    "run_no_reset_period": (("dir", "rm"),
        ("{dir}: probs посчитаны без второго прохода Silero (D31).\n"
         "Удалите каталог и запустите заново: rm -rf {rm}")),
    "run_session_sha": (("path", "src"),
        "{path}: session_sha256 не совпадает с sha256 {src}; сессия не от этого файла"),
    "run_session_block": (("dir", "rm"),
        ("{dir}: блок session в probs.json не совпадает с session.json рядом с источником.\n"
         "Удалите каталог и запустите заново: rm -rf {rm}")),
    "run_no_seam_reset": (("dir", "rm"),
        ("{dir}: probs посчитаны без сброса Silero на стыках (D29).\n"
         "Удалите каталог и запустите заново: rm -rf {rm}")),
    "run_session_samples": (("src", "expected", "got"),
        "{src}: в session.json {expected} сэмплов, в источнике {got}"),
    "clips_exist": (("dir",), "{dir} уже существует: клипы не перезаписываются (D19)"),
    "export_source_changed": (("src", "dir"),
        "{src}: sha256 не совпадает с манифестом {dir}, источник изменился"),
    "session_no_wavs": (("dir",), "{dir}: нет WAV-файлов"),
    "session_foreign_names": (("dir", "files"),
        "{dir}: имена не по правилу диктофона YYYYMMDDHHMMSS.wav: {files}"),
    "session_format_mismatch": (
        ("file", "sr", "channels", "subtype", "first_sr", "first_channels", "first_subtype"),
        ("{file}: {sr} Гц, {channels} кан., {subtype}; первый файл цепочки: {first_sr} Гц, "
         "{first_channels} кан., {first_subtype}")),
    "session_subtype": (("file", "subtype"),
        "{file}: subtype {subtype}, для сессии нужен PCM или FLOAT"),
    "session_too_long": (("hours",),
        "сессия {hours} ч не помещается в WAV (предел RIFF 4 ГиБ); разбейте папку на части"),
    "session_wav_mismatch": (("dir", "wav", "n", "rm"),
        ("{dir}: {wav} нет или его длина не {n} сэмплов.\n"
         "Удалите каталог и запустите заново: rm -rf {rm}")),
    # cli.py
    "probs_frames_mismatch": (("dir", "frames", "n_frames", "n16k"),
        "{dir}: в probs.npy {frames} кадров, в probs.json n_frames={n_frames}, n16k={n16k}"),
    "config_channel_mismatch": (("channel", "probs_channel", "dir"),
        "конфиг: channel={channel}, а probs посчитаны для channel={probs_channel} ({dir})"),
    "seg_probs_replaced": (("dir", "rm"),
        ("{dir}: probs.npy подменён после сегментации.\n"
         "Удалите каталог и запустите заново: rm -rf {rm}")),
    "seg_seam_tol_changed": (("dir", "was", "now", "rm"),
        ("{dir}: seam_tol_s было {was}, сейчас {now}.\n"
         "Удалите каталог и запустите заново: rm -rf {rm}")),
    "eval_sha_mismatch": (("dir",),
        "{dir}: source_sha256 манифеста, run.json и probs.json не совпадают"),
    # library.py
    "session_import_pending": (("session",),
        ("незавершённый импорт: файлы записи внутри {session}, не удаляйте их; «Обработать» "
         "сверит sha256 и завершит импорт")),
    "session_import_manual": (("session",),
        ("незавершённый импорт: файлы записи внутри {session}, не удаляйте их; нужна ручная "
         "проверка")),
    "session_unreadable": (("detail",), "{detail}"),
    "inbox_no_source": (("source",), "нет такого источника во входящих: {source}"),
    "import_chain_gone": (("files",),
        ("цепочки {files} нет во входящих (уже импортирована или файлы изменились); ничего не "
         "тронуто")),
    "import_inbox_changed": ((),
        "входящие изменились во время импорта; ничего не тронуто, повторите"),
    "import_symlink": (("file",),
        "{file}: ссылка, а не файл; положите во входящие сам файл записи; ничего не тронуто"),
    "import_unfinished_elsewhere": (("session",),
        ("часть файлов уже в сессии {session} (незавершённый импорт): нажмите у неё "
         "«Обработать»; импорт отменён, файлы на месте")),
    "import_partly_imported": (("session", "files"),
        "часть файлов уже в сессии {session}: {files}; импорт отменён, файлы на месте"),
    "import_dir_unfinished": (("dir",),
        ("{dir} уже есть (originals.tmp: незавершённый импорт); ничего не тронуто. Если внутри "
         "файлы записи, не удаляйте их: это единственные копии")),
    "import_dir_other": (("dir",),
        ("{dir} уже есть (с другими файлами); ничего не тронуто. Если внутри файлы записи, не "
         "удаляйте их: это единственные копии")),
    "import_move_stuck": (("file", "detail", "files", "dir"),
        ("{file}: перенос не удался ({detail}); вернуть не удалось: {files} — они в {dir}, не "
         "удаляйте их: это единственные копии; остальные файлы во входящих")),
    "import_move_failed": (("file", "detail"),
        "{file}: перенос не удался ({detail}); файлы во входящих, ничего не тронуто"),
    "finish_no_pending": (("session",),
        "{session}: нет import.pending.json, импорт не завершить автоматически"),
    "finish_both_dirs": (("session",),
        ("{session}: есть и originals, и originals.tmp; файлы записи внутри, не удаляйте их, "
         "нужна ручная проверка")),
    "finish_none_in_originals": (("session",),
        ("{session}: файлов записи нет в originals: они во входящих или не восстановлены из "
         "бэкапа; ничего не тронуто")),
    "finish_missing": (("session", "files"),
        "{session}: не найдены ни в originals, ни во входящих: {files}; ничего не тронуто"),
    "finish_changed": (("files",),
        ("{files}: во входящих другой файл (sha256 не совпадает с началом импорта); ничего не "
         "тронуто")),
    "finish_move_failed": (("file", "source", "detail"),
        ("{file}: перенос из inbox/{source} в originals не удался ({detail}); перенесённые "
         "остаются в originals, остальные во входящих; «Обработать» продолжит с места")),
    "finish_sha_mismatch": (("files", "dir"),
        "sha256 после переноса не совпадает: {files}; файлы оставлены в {dir}"),
    "finish_session_mismatch": (("got", "expected", "dir"),
        "после переноса сессия {got}, ожидалась {expected}; файлы оставлены в {dir}"),
    "process_no_session": (("session",), "нет сессии {session}"),
    "process_light": ((), "сессия из light-бэкапа: модель не прогнать, нужен full-бэкап"),
    "process_assembled_other": (("got", "expected"), "сборка дала {got}, ожидалась {expected}"),
    "backup_bad_mode": (("mode",), "неизвестный вид бэкапа: {mode}"),
    "backup_exists": (("name",), "{name} уже есть; бэкапы не перезаписываются"),
    "backup_full_refused": (("files",),
        ("full-бэкап отменён: записи вне originals/ или ссылки — {files} (originals.tmp — "
         "незавершённый импорт, обычно его доделывает «Обработать»)")),
    "restore_bad_member": (("member",),
        "недопустимый член архива: {member}; ничего не распаковано"),
    "restore_no_manifest": (("name",), "{name}: нет или битый backup.json"),
    "restore_mismatch": (("name", "files"),
        "{name}: sha256 или состав не совпадает с backup.json: {files}; ничего не распаковано"),
    "restore_no_archive": (("name",), "нет архива во входящих: {name}"),
    "restore_conflicts": (("lines",),
        ("в data/ уже есть другие версии файлов, ничего не изменено:\n"
         "{lines}")),
}

_FIELD = re.compile(r"\{(\w+)\}")


def _value(name: str, v) -> str:
    if isinstance(v, dict):
        return render(v["code"], v["params"])
    if isinstance(v, list):
        return ("\n" if name == "lines" else ", ").join(str(x) for x in v)
    return str(v)


def render(code: str, params: dict) -> str:
    return _FIELD.sub(lambda m: _value(m[1], params[m[1]]), CODES[code][1])


class Coded(Exception):
    """First base of a UI-facing exception: class SessionError(Coded, ValueError)."""

    # shortcut: not picklable/copyable (args holds the rendered text); add __reduce__ if ever needed

    def __init__(self, code: str, **params):
        names = CODES[code][0]
        if set(params) != set(names):
            raise TypeError(f"{code}: params {sorted(params)} != {sorted(names)}")
        self.code, self.params = code, params
        super().__init__(render(code, params))


def descriptor(exc: BaseException) -> dict:
    if isinstance(exc, Coded):
        return {"code": exc.code, "params": exc.params}
    return {"code": "internal_error", "params": {}}
