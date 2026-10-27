"""Error contract (D66): a machine code plus explicit params; the UI translates, the backend never
knows its language. The English template here is en.js's, byte for byte; it is str(exc) for the CLI
and the job log."""

import re

# code -> (param names, English template). Templates use {name} only; precision is in the value.
CODES: dict[str, tuple[tuple[str, ...], str]] = {
    # any module
    "internal_error": ((), "internal server error"),
    "legacy_error": (("detail",), "{detail}"),
    # intervals.py
    "session_bad_start": (("text",),
        ("--start {text}: expected local time, e.g. \"2025-03-01 09:00\" or \"2025-03-01 "
         "09:00:05\"")),
    "session_overlap": (("a", "b", "gap", "tol"),
        "files {a} and {b} overlap in time by {gap} s (tolerance {tol} s)"),
    "session_pause": (("minutes", "a", "b"),
        ("a {minutes} min pause between parts ({a} → {b}); joining across pauses is not "
         "supported yet")),
    "session_chain_choose": (("n", "lines"),
        ("the folder has {n} file chains, pick one with --chain N:\n"
         "{lines}")),
    "session_chain_range": (("n", "total", "lines"),
        ("--chain {n} is out of range 1..{total}:\n"
         "{lines}")),
    # audio.py
    "ffmpeg_missing": ((), "ffmpeg not found, install it: sudo apt install ffmpeg"),
    "ffmpeg_version_failed": (("path", "rc", "detail"),
        "{path} -version exited with code {rc}: {detail}"),
    "ffmpeg_failed": (("rc", "detail", "cmd"),
        ("ffmpeg exited with code {rc}: {detail}\n"
         "command: {cmd}")),
    "ffprobe_missing": ((), "ffprobe not found, install it: sudo apt install ffmpeg"),
    "ffprobe_failed": (("rc", "detail", "cmd"),
        ("ffprobe exited with code {rc}: {detail}\n"
         "command: {cmd}")),
    "source_no_audio": (("src",), "{src}: no audio track"),
    "source_not_riff": (("src",), "{src}: not a RIFF/RF64 WAVE"),
    "source_wav_broken": (("src", "detail", "fix"),
        ("{src}: broken WAV structure: {detail}.\n"
         "Re-save it: {fix}")),
    "source_unreadable": (("src", "detail", "fix"),
        ("{src}: soundfile cannot read the file ({detail}).\n"
         "Convert it: {fix}\n"
         "then use that file as the source")),
    "source_format": (("src", "format", "fix"),
        ("{src}: format {format}, v1 accepts only WAV or FLAC.\n"
         "Convert it: {fix}\n"
         "then use that file as the source")),
    "source_codec": (("src", "codec", "fix"),
        ("{src}: codec {codec}, sample-accurate decoding is not guaranteed.\n"
         "Convert it: {fix}\n"
         "then use that file as the source")),
    "source_frames_mismatch": (("src", "frames", "n", "fix"),
        ("{src}: soundfile sees {frames} samples, ffmpeg decodes {n}.\n"
         "Convert it: {fix}\n"
         "then use that file as the source")),
    "source_stereo": (("channels", "max"),
        "the source has {channels} channel(s): set channel = an index 0..{max} or \"mix\""),
    "source_bad_channel": (("channel", "max"),
        "channel = {channel}: expected \"mono\", \"mix\" or an index 0..{max}"),
    "source_copy_samples": (("path", "got", "expected"),
        "{path}: {got} samples, expected {expected}"),
    "run_env_changed": (("dir", "detail", "rm"),
        ("{dir}: the environment changed ({detail}).\n"
         "The result will not be reused. Delete the folder and run again: rm -rf {rm}")),
    "run_missing": (("path",), "{path} is missing: the folder is incomplete or the wrong one"),
    "run_no_reset_period": (("dir", "rm"),
        ("{dir}: probs were computed without Silero's second pass (D31).\n"
         "Delete the folder and run again: rm -rf {rm}")),
    "run_session_sha": (("path", "src"),
        ("{path}: session_sha256 does not match the sha256 of {src}; the session is not from "
         "this file")),
    "run_session_block": (("dir", "rm"),
        ("{dir}: the session block in probs.json does not match the session.json next to the "
         "source.\n"
         "Delete the folder and run again: rm -rf {rm}")),
    "run_no_seam_reset": (("dir", "rm"),
        ("{dir}: probs were computed without a Silero reset at the seams (D29).\n"
         "Delete the folder and run again: rm -rf {rm}")),
    "run_session_samples": (("src", "expected", "got"),
        "{src}: session.json has {expected} samples, the source has {got}"),
    "clips_exist": (("dir",), "{dir} already exists: clips are never overwritten (D19)"),
    "export_source_changed": (("src", "dir"),
        "{src}: sha256 does not match the manifest of {dir}, the source has changed"),
    "session_no_wavs": (("dir",), "{dir}: no WAV files"),
    "session_foreign_names": (("dir", "files"),
        "{dir}: names do not follow the recorder's YYYYMMDDHHMMSS.wav pattern: {files}"),
    "session_format_mismatch": (
        ("file", "sr", "channels", "subtype", "first_sr", "first_channels", "first_subtype"),
        ("{file}: {sr} Hz, {channels} ch, {subtype}; first file of the chain: {first_sr} Hz, "
         "{first_channels} ch, {first_subtype}")),
    "session_subtype": (("file", "subtype"),
        "{file}: subtype {subtype}, a session needs PCM or FLOAT"),
    "session_too_long": (("hours",),
        ("a {hours} h session does not fit in a WAV (RIFF limit 4 GiB); split the folder into "
         "parts")),
    "session_wav_mismatch": (("dir", "wav", "n", "rm"),
        ("{dir}: {wav} is missing or its length is not {n} samples.\n"
         "Delete the folder and run again: rm -rf {rm}")),
    # cli.py
    "probs_frames_mismatch": (("dir", "frames", "n_frames", "n16k"),
        "{dir}: probs.npy has {frames} frames, probs.json has n_frames={n_frames}, n16k={n16k}"),
    "config_channel_mismatch": (("channel", "probs_channel", "dir"),
        "config: channel={channel}, but probs were computed for channel={probs_channel} ({dir})"),
    "seg_probs_replaced": (("dir", "rm"),
        ("{dir}: probs.npy was replaced after segmentation.\n"
         "Delete the folder and run again: rm -rf {rm}")),
    "seg_seam_tol_changed": (("dir", "was", "now", "rm"),
        ("{dir}: seam_tol_s was {was}, now {now}.\n"
         "Delete the folder and run again: rm -rf {rm}")),
    "eval_sha_mismatch": (("dir",),
        "{dir}: source_sha256 of the manifest, run.json and probs.json do not match"),
    # library.py
    "session_delete_pending": (("session",),
        "deleting session {session} was cut short: Retry in the job log finishes it"),
    "session_import_pending": (("session",),
        ("unfinished import: the recording files are inside {session}, do not delete them; "
         "“Process” will check sha256 and finish the import")),
    "session_import_manual": (("session",),
        ("unfinished import: the recording files are inside {session}, do not delete them; a "
         "manual check is needed")),
    "session_unreadable": (("detail",), "{detail}"),
    "inbox_unreadable": (("detail",), "{detail}"),
    "inbox_no_source": (("source",), "no such source in the inbox: {source}"),
    "import_chain_gone": (("files",),
        ("chain {files} is not in the inbox (already imported or the files changed); nothing "
         "was changed")),
    "import_inbox_changed": ((),
        "the inbox changed during the import; nothing was changed, try again"),
    "import_symlink": (("file",),
        ("{file}: a link, not a file; put the recording file itself in the inbox; nothing was "
         "changed")),
    "import_unfinished_elsewhere": (("session",),
        ("some files are already in session {session} (unfinished import): press “Process” on "
         "it; import cancelled, files left in place")),
    "import_partly_imported": (("session", "files"),
        ("some files are already in session {session}: {files}; import cancelled, files left in "
         "place")),
    "import_dir_unfinished": (("dir",),
        ("{dir} already exists (originals.tmp: unfinished import); nothing was changed. If it "
         "holds recording files, do not delete them: they are the only copies")),
    "import_dir_other": (("dir",),
        ("{dir} already exists (with other files); nothing was changed. If it holds recording "
         "files, do not delete them: they are the only copies")),
    "import_move_stuck": (("file", "detail", "files", "dir"),
        ("{file}: move failed ({detail}); could not move back: {files}; they are in {dir}. Do "
         "not delete them: they are the only copies; the other files are in the inbox")),
    "import_move_failed": (("file", "detail"),
        "{file}: move failed ({detail}); files are in the inbox, nothing was changed"),
    "delete_no_session": (("session",), "no such session: {session}"),
    "delete_incomplete": (("session",),
        "session {session} is not fully imported: Process finishes the import first"),
    "delete_sha_mismatch": (("files",), ("sha256 differs, the recordings stay in the session: "
                                         "{files}")),
    "delete_inbox_exists": (("dir",), "{dir} already exists: move that folder away and retry"),
    "delete_move_failed": (("file", "detail"),
        "could not move {file} to Inbox: {detail}; a retry finishes the delete"),
    "delete_originals_left": (("files",), ("recordings left in originals/, the session was not "
                                           "deleted: {files}")),
    "finish_no_pending": (("session",),
        "{session}: no import.pending.json, the import cannot be finished automatically"),
    "finish_both_dirs": (("session",),
        ("{session}: both originals and originals.tmp exist; recording files are inside, do not "
         "delete them, a manual check is needed")),
    "finish_none_in_originals": (("session",),
        ("{session}: no recording files in originals: they are in the inbox or not restored "
         "from a backup; nothing was changed")),
    "finish_missing": (("session", "files"),
        "{session}: not found in originals or in the inbox: {files}; nothing was changed"),
    "finish_changed": (("files",),
        ("{files}: a different file in the inbox (sha256 does not match the one recorded when "
         "the import started); nothing was changed")),
    "finish_move_failed": (("file", "source", "detail"),
        ("{file}: move from inbox/{source} to originals failed ({detail}); moved files stay in "
         "originals, the rest in the inbox; “Process” will continue from there")),
    "finish_sha_mismatch": (("files", "dir"),
        "sha256 after the move does not match: {files}; files left in {dir}"),
    "finish_session_mismatch": (("got", "expected", "dir"),
        "after the move the session is {got}, expected {expected}; files left in {dir}"),
    "process_no_session": (("session",), "no session {session}"),
    "process_light": ((), ("session from a light backup: the model cannot run, a full backup is "
                           "needed")),
    "process_assembled_other": (("got", "expected"), ("assembly produced {got}, expected "
                                                      "{expected}")),
    "backup_bad_mode": (("mode",), "unknown backup type: {mode}"),
    "backup_exists": (("name",), "{name} already exists; backups are never overwritten"),
    "backup_full_refused": (("files",),
        ("full backup cancelled: entries outside originals/ or links: {files} (originals.tmp is "
         "an unfinished import, “Process” usually finishes it)")),
    "restore_bad_member": (("member",),
        "invalid archive member: {member}; nothing unpacked"),
    "restore_no_manifest": (("name",), "{name}: backup.json is missing or broken"),
    "restore_mismatch": (("name", "files"),
        "{name}: sha256 or contents do not match backup.json: {files}; nothing unpacked"),
    "restore_no_archive": (("name",), "no archive in the inbox: {name}"),
    "restore_conflicts": (("lines",),
        ("data/ already has other versions of the files, nothing changed:\n"
         "{lines}")),
    "restore_no_space": (("name", "need", "free"),
        "{name}: unpacking needs {need} MB, {free} MB free; nothing unpacked"),
    "restore_bad_target": (("path",),
        "{path}: the target leads outside data/ through a link; nothing restored"),
    # journal.py
    "journal_unknown_fields": ((), "unknown event fields"),
    "journal_bad_ts": ((), "ts: an ISO date with a time zone is required"),
    "journal_bad_event_id": ((), "event_id: 32 hex digits required"),
    "journal_unknown_kind": ((), "unknown event kind"),
    "journal_bad_value": ((), "value: a string is required"),
    "journal_value_too_long": (("limit",), "text longer than {limit} characters"),
    "journal_bad_session_id": ((), "session_id"),
    "journal_bad_span": ((), "span: session_id and 0 <= start_s < end_s are required"),
    "journal_no_span": ((), "this event kind has no span"),
    "journal_bad_idea_ref": ((), "idea_deleted: value must be the idea's event_id"),
    "journal_broken_middle": (("where", "pos"),
        "{where}: broken line in the middle of the journal (byte {pos})"),
    "journal_tail_skipped": (("where", "detail"),
        "{where}: the last journal line is broken and was skipped ({detail})"),
    # jobs.py
    "job_unknown_kind": (("kind",), "unknown job kind: {kind}"),
    "job_bad_params": (("kind", "keys"), "{kind} parameters: exactly {keys} required"),
    "job_no_source": ((), "no such source in the inbox"),
    "job_bad_chain": ((), "chain: an integer >= 1"),
    "job_bad_files": ((), "files: a non-empty list of file names"),
    "job_bad_start": ((), "start: a string or null"),
    "job_bad_session_id": ((), "session_id: a string"),
    "job_no_session": ((), "no such session"),
    "job_session_not_hidden": ((), "only a hidden session can be deleted"),
    "job_session_busy": ((), "the session already has a running or queued job"),
    "job_cancel_delete": ((), ("a delete cannot be cancelled: it is short, and a retry finishes "
                               "a cut one")),
    "job_cannot_process": ((), ("the session cannot be processed (light backup or unfinished "
                                "import)")),
    "job_bad_mode": ((), "mode: full or light"),
    "job_no_archive": ((), "no such archive in the inbox"),
    "job_retry_not_open": ((), "can only retry a failed job whose failure is open"),
    "job_retry_duplicate": ((), "the same job is already queued or running"),
    "job_files_gone": ((), "the chain's files are no longer in the inbox folder"),
    "job_bad_id": ((), "job_id: 32 hex digits required"),
    "job_slice_limit": (("slice", "detail"),
        "the memory limit on {slice} was not applied ({detail}); the job was not started"),
    "job_unit_failed": (("detail",), "systemd-run: {detail}"),
    "job_start_failed": (("detail",), "{detail}"),
    "job_unexpected": (("detail",), "{detail}"),
    "job_dead": ((), "the job process ended without a result (possibly OOM)"),
    "job_unit_unknown_kind": (("kind",), "unknown job kind {kind}"),
    "job_after_done_failed": (("cause",), "after completion: {cause}"),
    # ui/server.py
    "http_bad_host": ((), "invalid Host"),
    "http_cors": ((), "CORS is not supported"),
    "http_foreign_origin": ((), "request from a foreign origin"),
    "http_need_length": ((), "Content-Length required"),
    "http_body_too_large": ((), "body larger than 64 KiB"),
    "http_not_json": ((), "body is not JSON"),
    "http_not_object": ((), "body is not a JSON object"),
    "http_need_key": ((), "key required"),
    "http_not_found": ((), "not found"),
    "http_method": ((), "method not supported"),
    "http_no_code": ((), "no launch code"),
    "http_bad_code": ((), "launch code is invalid"),
    "http_no_session": ((), "no such session"),
    "http_no_run": ((), "no such run"),
    "run_unreadable": (("detail",), "run is unreadable: {detail}"),
    "http_get_only": ((), "GET only"),
    "http_no_clip": ((), "no such clip"),
    "http_clip_fields": ((), "fields: event_id, kind, value, clip_id"),
    "http_unknown_clip": ((), "unknown clip_id"),
    "http_heard_value": ((), "heard: value is empty"),
    "http_idea_fields": ((), "fields: event_id, kind, value, session_id?"),
    "http_idea_deleted_fields": ((), "fields: event_id, kind, value"),
    "http_no_idea": ((), "no such idea"),
    "http_hide_fields": ((), "fields: event_id, kind, session_id"),
    "http_session_busy": ((), "the session has a running or queued job: wait for it or cancel it"),
    "http_journal_kind": ((), "kind: mark, note, heard, idea or idea_deleted"),
    "http_body_empty": ((), "body: {}"),
    "inbox_open_failed": (("detail",), "File Explorer unavailable: {detail}"),
    "http_jobs_unavailable": ((), "jobs unavailable"),
    "http_job_fields": ((), "fields: job_id, kind, params"),
    "http_no_job": ((), "no such job"),
    # ui/server.py, on_done of a restore job (a JobError cause)
    "restore_journal_broken": (("detail",),
        ("the archive's journal is unreadable ({detail}); files restored, journal not merged, "
         "retrying will not help")),
    "restore_journal_not_merged": (("detail",),
        ("the archive's journal was not merged ({detail}); files restored, restore this archive "
         "again to merge the journal")),
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
