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
    "session_delete_pending": (("session",),
        "удаление сессии {session} оборвалось: «Повторить» в журнале задач доделает его"),
    "session_import_pending": (("session",),
        ("незавершённый импорт: файлы записи внутри {session}, не удаляйте их; «Обработать» "
         "сверит sha256 и завершит импорт")),
    "session_import_manual": (("session",),
        ("незавершённый импорт: файлы записи внутри {session}, не удаляйте их; нужна ручная "
         "проверка")),
    "session_unreadable": (("detail",), "{detail}"),
    "inbox_unreadable": (("detail",), "{detail}"),
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
    "delete_no_session": (("session",), "нет такой сессии: {session}"),
    "delete_incomplete": (("session",),
        "сессия {session} импортирована не до конца: сначала «Обработать» доделывает импорт"),
    "delete_sha_mismatch": (("files",), "sha256 не совпал, записи оставлены в сессии: {files}"),
    "delete_inbox_exists": (("dir",), "{dir} уже существует: уберите эту папку и повторите"),
    "delete_move_failed": (("file", "detail"),
        "не удалось перенести {file} во входящие: {detail}; повтор доделает удаление"),
    "delete_originals_left": (("files",), "в originals/ остались записи, сессия не удалена: {files}"),
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
    # journal.py
    "journal_unknown_fields": ((), "неизвестные поля события"),
    "journal_bad_ts": ((), "ts: нужна дата ISO с часовым поясом"),
    "journal_bad_event_id": ((), "event_id: нужны 32 hex"),
    "journal_unknown_kind": ((), "неизвестный вид события"),
    "journal_bad_value": ((), "value: нужна строка"),
    "journal_value_too_long": (("limit",), "текст длиннее {limit} символов"),
    "journal_bad_session_id": ((), "session_id"),
    "journal_bad_span": ((), "отрезок: нужны session_id и 0 <= start_s < end_s"),
    "journal_no_span": ((), "у этого вида события нет отрезка"),
    "journal_bad_idea_ref": ((), "idea_deleted: value — event_id идеи"),
    "journal_broken_middle": (("where", "pos"),
        "{where}: битая строка в середине журнала (байт {pos})"),
    "journal_tail_skipped": (("where", "detail"),
        "{where}: последняя строка журнала битая и пропущена ({detail})"),
    # jobs.py
    "job_unknown_kind": (("kind",), "неизвестный вид задачи: {kind}"),
    "job_bad_params": (("kind", "keys"), "параметры {kind}: нужны ровно {keys}"),
    "job_no_source": ((), "нет такого источника во входящих"),
    "job_bad_chain": ((), "chain: целое >= 1"),
    "job_bad_files": ((), "files: непустой список имён файлов"),
    "job_bad_start": ((), "start: строка или null"),
    "job_bad_session_id": ((), "session_id: строка"),
    "job_no_session": ((), "нет такой сессии"),
    "job_session_not_hidden": ((), "удалить можно только скрытую сессию"),
    "job_session_busy": ((), "у сессии уже идёт или ждёт задача"),
    "job_cancel_delete": ((), "удаление не отменяется: оно короткое, оборванное доделывает повтор"),
    "job_cannot_process": ((), "сессию нельзя обработать (light-бэкап или незавершённый импорт)"),
    "job_bad_mode": ((), "mode: full или light"),
    "job_no_archive": ((), "нет такого архива во входящих"),
    "job_retry_not_open": ((), "повторить можно только упавшую задачу с открытым сбоем"),
    "job_retry_duplicate": ((), "такая же задача уже ждёт или идёт"),
    "job_files_gone": ((), "файлов цепочки больше нет в папке входящих"),
    "job_bad_id": ((), "job_id: нужны 32 hex"),
    "job_slice_limit": (("slice", "detail"),
        "лимит памяти на {slice} не применился ({detail}); задача не запущена"),
    "job_unit_failed": (("detail",), "systemd-run: {detail}"),
    "job_start_failed": (("detail",), "{detail}"),
    "job_unexpected": (("detail",), "{detail}"),
    "job_dead": ((), "процесс задачи завершился без итога (возможно, OOM)"),
    "job_unit_unknown_kind": (("kind",), "неизвестный вид задачи {kind}"),
    "job_after_done_failed": (("cause",), "после завершения: {cause}"),
    # ui/server.py
    "http_bad_host": ((), "неверный Host"),
    "http_cors": ((), "CORS не поддерживается"),
    "http_foreign_origin": ((), "чужой источник запроса"),
    "http_need_length": ((), "нужен Content-Length"),
    "http_body_too_large": ((), "тело больше 64 КиБ"),
    "http_not_json": ((), "тело не JSON"),
    "http_not_object": ((), "тело не JSON-объект"),
    "http_need_key": ((), "нужен ключ"),
    "http_not_found": ((), "не найдено"),
    "http_method": ((), "метод не поддерживается"),
    "http_no_code": ((), "нет кода запуска"),
    "http_bad_code": ((), "код запуска недействителен"),
    "http_no_session": ((), "нет такой сессии"),
    "http_no_run": ((), "нет такого прогона"),
    "run_unreadable": (("detail",), "прогон не читается: {detail}"),
    "http_get_only": ((), "только GET"),
    "http_no_clip": ((), "нет такого клипа"),
    "http_clip_fields": ((), "поля: event_id, kind, value, clip_id"),
    "http_unknown_clip": ((), "неизвестный clip_id"),
    "http_heard_value": ((), "heard: value пустой"),
    "http_idea_fields": ((), "поля: event_id, kind, value, session_id?"),
    "http_idea_deleted_fields": ((), "поля: event_id, kind, value"),
    "http_no_idea": ((), "нет такой идеи"),
    "http_hide_fields": ((), "поля: event_id, kind, session_id"),
    "http_session_busy": ((), "у сессии идёт или ждёт задача: дождитесь её или отмените"),
    "http_journal_kind": ((), "kind: mark, note, heard, idea или idea_deleted"),
    "http_body_empty": ((), "тело: {}"),
    "inbox_open_failed": (("detail",), "проводник недоступен: {detail}"),
    "http_jobs_unavailable": ((), "задачи недоступны"),
    "http_job_fields": ((), "поля: job_id, kind, params"),
    "http_no_job": ((), "нет такой задачи"),
    # ui/server.py, on_done of a restore job (a JobError cause)
    "restore_journal_broken": (("detail",),
        ("журнал архива не читается ({detail}); файлы восстановлены, журнал не слит, повтор не "
         "поможет")),
    "restore_journal_not_merged": (("detail",),
        ("журнал из архива не слит ({detail}); файлы восстановлены, восстановите этот архив "
         "ещё раз, чтобы слить журнал")),
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


# A param is str, number, list of str or a nested descriptor: a Path breaks json.dumps of a job
# status, None renders differently in Python and JS.
def _param_ok(v) -> bool:
    if isinstance(v, dict):
        return isinstance(v.get("code"), str) and isinstance(v.get("params"), dict)
    if isinstance(v, list):
        return all(isinstance(x, str) for x in v)
    return isinstance(v, (str, int, float)) and not isinstance(v, bool)


class Coded(Exception):
    """First base of a UI-facing exception: class SessionError(Coded, ValueError)."""

    # shortcut: not picklable/copyable (args holds the rendered text); add __reduce__ if ever needed

    def __init__(self, code: str, **params):
        names = CODES[code][0]
        if set(params) != set(names):
            raise TypeError(f"{code}: params {sorted(params)} != {sorted(names)}")
        for name, v in params.items():
            if not _param_ok(v):
                raise TypeError(f"{code}: param {name} has type {type(v).__name__}")
        self.code, self.params = code, params
        super().__init__(render(code, params))


def descriptor(exc: BaseException) -> dict:
    if isinstance(exc, Coded):
        return {"code": exc.code, "params": exc.params}
    return {"code": "internal_error", "params": {}}
