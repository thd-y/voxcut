# Decision log

Every design decision VoxCut was built on, in the order it was made. Each entry: number, title,
what was decided, why, and which earlier decision it refines or replaces. Code comments and
commit messages cite these numbers (`D31`, `C1`). Product facts only: the working notes behind
them (plans, reviews, labeling sessions) stay outside this repository.

## Core design (2026-10-14): D1–D24, C1–C7

**D1 — Layout.** Three modules with fixed roles: `audio.py` (ffmpeg, soundfile, the model), `intervals.py` (pure logic, no torch), `cli.py` (commands), with matching test files. *Why:* the smallest arrangement that keeps the pure logic testable without the model.

**D2 — ffmpeg.** One shared helper finds ffmpeg (`shutil.which` plus `ffmpeg -version`), fails with a clear message when it is missing, and runs every call as an argument list, never through a shell. The 16 kHz copy uses an explicit resampler, `aresample=16000:resampler=swr`; the filter string and the ffmpeg version go into `probs.json` and the manifest next to the source sha256. *Why:* the resampler and the binary decide sample positions, so both must be pinned and recorded.

**D3 — Hysteresis: a port of Silero's state machine.** The state machine from silero-vad 6.2.3 (`get_speech_timestamps_from_probs`, MIT) is ported into `intervals.py` without the max-speech branch and without padding; `neg_threshold` defaults to `max(threshold − 0.15, 0.01)` as upstream, and the effective value goes into the manifest. Merge gap, padding and re-merge are VoxCut's own. *Why:* segmentation must run without torch (D1), and porting the upstream function keeps behaviour identical. The anchor tests are D22.

**D4 — A faster-whisper reference row.** Reports carry a row `fw_params_voxcut_postproc`: the parameters faster-whisper's VAD filter uses (threshold 0.5, neg_threshold 0.35, min_silence 2000 ms, pad 400 ms), run through VoxCut's own pipeline on the same probabilities. VoxCut does not import faster-whisper. *Why:* a fair baseline for the downstream ASR stage without a dependency on it. The `vad_filter` check moved to the pilot (D17).

**D5 — Source length.** `probs` decodes the source at its native rate and counts samples (`source_n_samples`, `source_sr`, `channels`, `codec_name`); `export` clamps the end of a clip to the real end of file. *Why:* the dangerous case is a length understated by a broken header; counting decoded samples exposes it. The broken-header test became D15; the codec warning became a refusal in D18.

**D6 — Interrupted runs.** One function `n_frames(n16k) = ceil(n16k / 512)` serves `probs` and `segment`; `probs.json` stores `n_frames` and the source sha256, and later steps refuse on a mismatch. Every artifact is written to `<name>.tmp` and published with `os.replace`, the json last. *Why:* a crash must never leave a file that looks finished. Directory layout and reuse: D19.

**D7 — Parsing `labels.txt`.** Labels are normalized with `strip().lower()` and checked against one constant set (`scored`, `me`, `other_speech`, `vocals_music`), printed in the error. Lines split strictly on tabs; every error (unknown label, label outside `scored`, start ≥ end, not a number) is collected and reported with its line number in one failure. `evaluate` checks the last end against the source length and prints a summary of the reference before any metric. *Why:* a labeling mistake must be found in one pass, not one error at a time.

**D8 — Zero denominators and choosing a config.** Two recall metrics: `recall_sec` and `recall_events` (share of `me` events covered at least `event_cov`, default 0.5). A zero denominator gives `n/a`, never 0 or 1; aggregates are sum over sum; reports have one row per session plus `ALL`. A config is chosen as the lowest false-alarm rate with `recall_sec ≥ R1` and `recall_events ≥ R2`. *Why:* averages of ratios and silent zeros hide misses. Refined by D20; numbers in D36.

**D9 — Tests: pytest.** Table tests use `parametrize`; tests needing ffmpeg or torch carry the `slow` marker and skip without them. Required checks: merge-gap and padding edges, clips bit-identical to the source slice, two `probs` runs giving identical arrays, compressed formats refused before the model runs. *Why:* the approved behaviour needs executable proof.

**D10 — Config.** TOML read with `tomllib` into a dataclass; a `[meta]` table (name, note) is skipped by the validator; an unknown key fails with a close-match suggestion; the manifest records the effective parameters and the sha256 of the normalized config. *Why:* a typo in a threshold must fail loudly, and key order must not change the config's identity.

**D11 — Export mechanism.** Clips are cut with soundfile seek/read/write. *Why:* sample-exact and a few lines. Its other parts were replaced by D18.

**D12 — A canonical FLAC copy.** Replaced entirely by D18.

**D13 — Deferred: tie `labels.txt` to the source sha256.** Recorded as deferred work.

**D14 — Deferred: the ASR stage's environment and the faster-whisper `vad_filter` check.** Recorded as deferred work; the check moved into the pilot (D17).

**D15 — A broken WAV header.** For WAV sources `probs` walks the RIFF chunks with `struct`: bytes after `data` that do not parse into valid chunks up to the end of file, or a `data` size of 0 or 0xFFFFFFFF, fail with both lengths (by header and by file size) and the ffmpeg command that repairs the file. *Why:* measured: ffmpeg and libsndfile both trust the header, and ffmpeg's `-ignore_length` turns trailing metadata chunks into audio.

**D16 — Pilot, and labeling by candidates with an audit.** A minimal `probs` + `segment` on one or two sessions; candidates at threshold 0.1 are labeled in full; outside the candidates a random 20 % share, chosen in advance, is audited for misses; `scored` windows are chosen before listening; hard examples form a separate set outside `ALL`. *Why:* full manual labeling of hours of audio is too expensive, and the audit measures what the candidates miss. Refined by D28.

**D17 — A faster-whisper spike in the pilot.** After one pilot session is labeled, `transcribe(vad_filter=True)` runs in a throwaway environment and is compared with the labels. *Why:* if it lost nothing at an acceptable volume, v1 could be smaller. Outcome: D33 (no reduction).

**D18 — Export: one path.** Suitability is checked in `probs`, before the model: `sf.info` succeeds, its frame count equals ffmpeg's, the codec is PCM or FLAC (plus the RIFF walk of D15). Otherwise the run refuses with a ready command (`ffmpeg -i <src> -c:a flac <src>.flac`, or an RF64 WAV for float PCM) and the note that this file is the source from now on. `export` only checks the source sha256 and cuts, reading in the source's own sample type, and clips keep the source's container and subtype; bit-exact tests per subtype. *Why:* one export path with no transcoding cannot drift; ffmpeg's `atrim` is sample-exact but needs decoding from the start of the file for every clip. Replaces D12 and parts of D11.

**D19 — Immutable run directories.** `<out>/<stem>.<sha8>.raw/` holds `probs.npy` and `probs.json`; inside it, `seg-<config sha8>/` holds `run.json`, `raw.jsonl` and `manifest.jsonl`. Every directory is published from `<dir>.tmp` with `os.replace` into a name that does not exist; there is no `--force`; a repeated config reuses its directory. `run.json` is always written, even with zero detections, and records the source, config and environment (silero and model versions, ffmpeg version and filter, torch threads); an environment mismatch on reuse refuses with the command that deletes the directory. `clips/` is the only later addition, published the same way. Orphaned `.tmp` directories are removed when any command starts. *Why:* a result can be trusted only if nothing ever overwrites it. Refines D6.

**D20 — Metric specification v2.** Tolerance is 100 ms. Two false-alarm rates: `FA_bound` excludes ±tolerance around `me`, `FA_asr` does not; selection and acceptance use `FA_asr`. One acceptance rule for development and holdout: `recall_sec ≥ R1`, `recall_events ≥ R2`, `fully_missed ≤ M`, `FA_asr ≤ max(dev × 1.5, dev + 1 min/h)`. Clip minutes per hour have a ceiling V. The share of `scored` covered by intervals is reported, and above 0.9 the report says "≈ the whole file". False alarms are intervals within `scored` minus `me`, `other_speech` and `vocals_music`. *Why:* a recall-first detector needs a precise, testable definition of what it may cost. Refines D8; numbers in D36, V in D38.

**D21 — The `channel` parameter.** `"mono"`, a channel index, or `"mix"`; a multichannel source without a value is an error. The value is recorded and is part of the probabilities directory key; clips keep all source channels. *Why:* a phase-inverted stereo pair cancels in a downmix and would hide speech. Sessions: D27.

**D22 — The port's anchor: four tests.** (a) `segment_raw` against `get_speech_timestamps_from_probs` with every parameter explicit, on edge cases; (b) the streaming probability loop bit-identical to Silero's own loop on synthetic audio; (c) a short real speech fragment (outside git, skipped without it) gives at least one segment; (d) coordinates checked with an impulse at a known sample. silero-vad and torch are upgraded only together with these tests. *Why:* the port is only as good as its proof of equivalence. Replaces D3's anchor.

**D23 — Deferred: Silero through onnxruntime without torch.** To be done if torch breaks `torch.jit.load` or the environment becomes a problem.

**D24 — `sweep` across sessions.** `voxcut sweep <configs…> --session <probs-dir> <labels> […]`: labels are passed explicitly with each probabilities directory; one row per config and session, `ALL` as sum over sum, plus the faster-whisper reference row. *Why:* configs are compared on all labeled sessions at once. Refines D19.

**C1 — No memory growth in the Silero loop.** The loop runs under `torch.inference_mode()` with `model.reset_states()` before each file; a test runs 50 000 frames and allows under 50 MB of growth. *Why:* measured: without it memory grows by about 21 KB per frame, about 7 GB for three hours.

**C2 — ffmpeg failures refuse.** A non-zero return code or a decode error refuses and publishes nothing, including after partial output. *Why:* a truncated decode would look like a short recording.

**C3 — Reproducible probabilities.** `torch.set_num_threads(1)` explicitly, recorded as `torch_threads` in `probs.json`. *Why:* thread count changes floating-point results.

**C4 — Stale text removed.** The design's faster-whisper notes now point to the reference row (D4), and the acceptance budget uses the D20 formula. *Why:* the design must not contradict its decisions.

**C5 — Metrics only inside `scored`.** Every interval is intersected with `scored` before any metric. *Why:* unlabeled audio must not count as a miss or a false alarm.

**C6 — CPU torch.** Setup installs the CPU build of torch from the PyTorch CPU index before the package. *Why:* the default build pulls more than 2 GB of CUDA libraries.

**C7 — Block-wise export.** Export copies a clip in blocks in the source's native sample type instead of reading it whole. *Why:* hours of audio must never be loaded into memory at once.

## Session input from hourly files (2026-10-20): D25–D27

The recorder this was built for writes 16 kHz, 16-bit, two identical channels, one file per hour, named `YYYYMMDDHHMMSS.WAV` after its start time, with no timestamp inside the WAV. Consecutive files are not seamless: the next one starts about 1–2 s after the previous one ends.

**D25 — A session is assembled into one WAV.** `voxcut session <dir> --out <out> [--chain N] [--start "YYYY-MM-DD HH:MM[:SS]"]` builds `<name>.<sha8>.session/` with `<name>.wav`, `session.json` and `run.json`. `<name>` is `<YYYY-MM-DD>_<HH-MM>_<H>h<MM>m` (start of the first file, total length); the sha8 covers the chain's names, sizes and frame counts plus `--start`. The originals stay untouched as provenance. Order of checks: file-name parse (one function), RIFF structure of each file, `sf.info`, chains split at gaps (a pause up to 30 min refuses, longer starts a new chain), one chain or `--chain N`, the 4 GiB RIFF limit before copying, then one block-wise pass that copies, hashes the inputs and checks whether the channels are identical. `--start` corrects a wrong recorder clock and records the offset. `segment` adds the session id, source file, local start time and an `at_seam` flag to every interval and writes `seams.jsonl`, a window on every seam regardless of the detector; `export` cuts those windows into `clips/seams/`. *Why:* speech must be found across hour boundaries, and the seams, where about a second is lost, must be listened to.

**D26 — Seam thresholds.** `GAP_MIN = −2 s`, `GAP_MAX = 5 s` (split), pauses up to 1800 s recognized (refused for now), `SEAM_TOL_S = 1.0 s`; recorded in `session.json` and `run.json` and checked on reuse. *Why:* measured gaps on real files; revisiting them is deferred until the seams have been listened to.

**D27 — Channel for sessions.** For a session whose channels are identical, `probs` without `--channel` takes channel 0 and records why; an explicit `--channel` wins; ordinary stereo still requires it. *Why:* identical channels carry no choice. An exception to D21.

## The first calibration pilot (2026-10-21): D28–D38

One multi-hour session was labeled by hand: candidates in full, plus a 20 % audit outside them.

**D28 — The audit outside candidates.** 30-second windows chosen at random before listening: free time is the session minus candidate intervals (threshold 0.1); slots of 30 s from the start of each free stretch, wholly inside it; `round(0.2 × free / 30)` slots without replacement, seed 1. Windows are labeled as `scored` and heard in full; they may only grow to the end of a phrase that crosses their edge. *Why:* a reproducible, unbiased estimate of what the candidates miss. Refines D16.

**D29 — Reset at seams.** For a session source, `probs` resets the Silero state before the frame that holds each seam; the frame numbers are recorded as `seam_reset_frames`, and a directory without them or with others is not reused (refusal with the delete command). *Why:* measured: a phrase right after a seam scored 0.017 in the assembled file and 0.986 in its own file; after the seam the probabilities differed for almost three minutes. Refines D25.

**D30 — Labeling rule for `me`.** Whispering, mumbling and unintelligible speech of the target speaker are `me`; laughter, breaths and other non-speech sounds are not labeled. Boundaries run from the start of the first word to the end of the last, with no margin. *Why:* the reference describes speech; the margin around it belongs to the config (`pad_ms`), where its cost shows up as false alarms. Refines D7.

**D31 — Two Silero passes, probabilities as their maximum.** Pass A resets only at seams (D29); pass B also resets before every frame that is a multiple of 3750 (120 s). `probs.npy = np.maximum(A, B)`; `reset_period_frames: 3750` is recorded and checked on reuse. Applies to every source. *Why:* during long music the model's recurrent state goes deaf: whispers in the audit windows scored 0.000–0.009 in one continuous pass and 0.05–0.26 after a fresh reset. A reset can also hurt a phrase, so both passes are kept. On the pilot at threshold 0.1: fully missed events 0 of 128 (5 with pass A alone), at a higher false-alarm rate. Shorter periods added false alarms for little gain. Refines D3 and D29.

**D32 — Starting config v1.** threshold 0.1, `neg_threshold` by D3 (0.01), min_speech 0, min_silence 2000 ms, pad 1000 ms, merge_gap 10000 ms. *Why:* on the pilot it missed no event fully and kept phrases whole (merge_gap cut 503 fragments to 297 clips); the faster-whisper parameters without D31 fully missed 41 of 128 events. The final choice follows D8/D20 (D36).

**D33 — Rejected detectors.** faster-whisper large-v3 with `vad_filter` lost most quiet events; without VAD it invents text over music and is ASR, out of scope for v1. Demucs vocal separation before Silero added a little recall at five times the false alarms. TEN VAD took music for speech. *Why:* none beat D31 on recall at a bearable cost. Closes D17.

**D34 — No confidence filter on clips in v1.** The best single feature, `p_max`, reached AUC 0.85, but no threshold on it, on mean probability, duration or loudness removed a music clip without losing speech. The manifest already carries `p_max` and `p_mean` for consumers to sort by. *Why:* any filter cost recall.

**D35 — A pseudo-holdout by hours.** Until a second session exists: development is the session minus its third hour, the holdout is the third hour (the densest and hardest). R1, R2, M and V are fixed on development before `sweep`, acceptance runs on the holdout. Done with data alone: two label files with `scored` cut at the boundaries. *Why:* an honest check needs data the choice never saw; D31 and D32 were chosen on the whole session, so the real check is a second session. Refines D20.

**D36 — Strict acceptance thresholds.** R1: `recall_sec ≥ 0.975`; R2: `recall_events ≥ 0.98`; M: `fully_missed = 0`; V: at most 20 clip-minutes per hour. Among 19 configs on development, `v1-start` (D32) won: recall_sec 0.979, recall_events 1.000, fully_missed 0, FA_asr 10.80 min/h, 19.03 clip-min/h; the holdout budget is therefore 16.2 min/h. *Why:* recall first; the price is about seven more minutes of audio per four-hour session than the next config, in fewer and unbroken clips. Fixes D20's numbers.

**D37 — Padding of 3 seconds.** `v1-start` failed on the holdout (recall_sec 0.945, recall_events 0.966). Every uncovered piece was the edge of a whisper or mumble, 0.9–4.3 s long, right next to a clip, scored 0.001–0.031 by the model: only padding can bring it back. A pad of 3000 ms is the smallest that passes R1, R2 and M on both parts: development 0.999 / 1.000 / 0, holdout 0.981 / 1.000 / 0, FA_asr 13.1 and 17.4 min/h (holdout budget 19.7). Volume: 15.7 clip-min/h over the session. `configs/v1.toml` holds it. *Why:* recall first. The pad was chosen looking at the holdout, so the holdout is spent; the next honest check is a new session. Refines D32.

**D38 — V over the whole session, only when choosing.** A new column `clip_min_h_session`: final-interval minutes per hour of the whole session; only configs at or below 20 may be chosen. On acceptance of a new session V is a reported number, not a criterion, next to `clip_per_me`, clip seconds per labeled second of `me`; a sharp rise means the config catches noise. *Why:* clip volume grows with the amount of speech, so an absolute limit compares configs on the same data, not sessions. Refines D20 and D36.

## The interface (2026-10-21): D39–D40

**D39 — Import through `inbox/`.** Recordings enter the product through `inbox/`; after a successful import they are moved into `data/sessions/<session_id>/originals/`, unchanged, with sha256 checked before and after. Nothing else may move, change or delete a recording. *Why:* the user only drops files into one folder, and the moved bytes are proven identical. Refines the rule that the source recording is never modified.

**D40 — Data lives inside the product.** `inbox/`, `data/` and `backups/` sit at the product's root, outside git; the product and its data are removed together; moving or saving data happens only through backup (`full` or `light`) and backup import. The CLI keeps `--out`; the interface always passes paths inside `data/`. Run directories stay immutable. *Why:* one folder to back up, nothing scattered over the machine.

## Interface redesign (2026-10-22): D41–D48

**D41 — One decision per clip.** A clip is either kept or rejected with a reason: vocals, music, noise or other. A mixed clip (the user's speech over music) is kept. Stored as `mark` on the clip's span in the journal; older marks are read without migration. *Why:* reviewing is one question per clip, and a rejected clip stays traceable to its session and span.

**D42 — Review rhythm.** Key `1` keeps, `2`–`5` reject with a reason, by physical key in any keyboard layout; after a decision the next undecided clip by time is selected; `Ctrl+Z` undoes the window's decisions in order. *Why:* review by keyboard, without the mouse. Autoplay changed by D53.

**D43 — Two screens.** A session screen (tabs Review and My clips, a list plus a card, the session timeline on top) and the Library (sessions, import, processing, jobs, backup). `p_max` is not shown. *Why:* the user reviews one session at a time; everything else is housekeeping.

**D44 — After review a session is "My clips".** The review summary can be hidden; rejected clips leave the view (files and journal intact, visible through a filter). My clips covers one session, with continuous play, notes and search in notes. A slot for a transcript is reserved; the module is out of scope. *Why:* the outcome of review is the set of kept clips.

**D45 — Look.** Basecoat 1.0.2, Luma style, vendored locally with its sha256 pinned in a test; no Basecoat JS and no Tailwind build. Theme switch in the header, following the system by default. *Why:* a consistent look with no build step and nothing loaded from the network.

**D46 — Backend change for the redesign.** Only a static-file table entry for the vendored file and its test; API, journal, CSP and jobs unchanged. *Why:* the redesign is a front-end change.

**D47 — Own decision and own note.** `journal.clip_view` also returns `own_mark` (the mark exactly on the clip's span) and `own: bool` for each note. *Why:* a mark made on another run's span must be labeled as such, and undo must be exact. Extends D46.

**D48 — Interface logic tests.** `node --test` on the pure functions of `app.js` (queue, next clip, undo, filters, search, session-name parsing); DOM and Basecoat are not unit-tested. Without node, `make check` skips the JS tests with a warning. *Why:* the review logic is where a wrong clip would be decided.

## After the redesign check (2026-10-22): D49–D55

**D49 — Open the inbox in Explorer.** The Library's import block has an Explorer button: `POST /api/inbox/open` without a body; the server opens `inbox/` (converted with `wslpath -w`), a path fixed on the server, arguments as a list, the process detached; the request passes the same POST protections; a missing `inbox/` is created. *Why:* the user drops recordings in from Windows.

**D50 — The source folder after import.** After a successful import, `inbox/<source>/` is removed with `os.rmdir` only if empty; `inbox/` itself and files in it are never removed. A subfolder without WAV files is not listed as an error; files left behind are reported once, in the import log. *Why:* keep the inbox tidy without ever deleting a file.

**D51 — Separators in the ideas list.** A thin line between ideas, each with its date and time in small type. *Why:* ideas are read as a dated list.

**D52 — Re-choosing in My clips.** No decision panel in My clips; key `R` (any layout) or a click opens a menu with the same five decisions and keys. A choice different from the current status asks for confirmation (`Enter`, the same digit or "Yes"; `Esc`, another digit or "No" cancels); the same choice writes nothing. A confirmed change is an ordinary decision and joins the undo stack. *Why:* protection against a stray key on clips already decided.

**D53 — Autoplay.** Moving to a clip plays it: after a decision, a click on a list row, `←`/`→`. Exceptions: undo and a click on the session timeline select without playing; opening a session or switching tabs plays nothing; if no next clip exists, nothing plays. `Space` plays and pauses. *Why:* reviewing is listening; the exceptions are moves the user makes to look, not to listen. Changes D42.

**D54 — Pauses in the list.** A pause is one row: a line, "pause 4:33", a line. The 120 s threshold stays. *Why:* compact and readable.

**D55 — Icons.** Lucide icons (ISC), a pinned version, only the needed glyphs as one local SVG sprite next to its license, sha256 pinned in a test; no Lucide JS, nothing from the network, CSP unchanged. Icon buttons keep their `aria-label`. *Why:* clear controls with no new attack surface.

## The ideas field (2026-10-22): D56–D58

**D56 — Deleting an idea.** Each idea has a small cross; a click asks for confirmation inline; "Yes" appends `kind: "idea_deleted"` with the idea's `event_id`. The idea leaves the screen; its text stays in the journal and backups, which only grow. The server accepts `idea_deleted` only for an existing idea and is idempotent. *Why:* deletion without breaking the append-only journal.

**D57 — Scrolling.** The ideas list scrolls inside its card, which is never taller than the visible window. *Why:* the list grows without bound.

**D58 — Expanding.** A button in the ideas header opens a large overlay with its own scroll and the same delete crosses; `Esc` or a button closes it. The two new icons join the D55 sprite. *Why:* reading long ideas.

## Audit fixes (2026-10-22): D59–D62

**D59 — The selected row stays centred.** When the selection changes, the list scrolls so the selected row sits in the middle, with past and next clips visible. *Why:* the neighbours give context.

**D60 — Favicon.** Its own `/favicon.svg` from the Lucide sprite (ISC). *Why:* a recognizable tab and no 404 in the console.

**D61 — Date of the last backup.** The Library shows the last backup with local date, year and time. *Why:* a backup's age is judged in days and years; idea times keep D51's format.

**D62 — Window size on launch.** The window opens at 1280×800, centred in the work area; on smaller screens at most 90 % of it per side. The page does it itself (`resizeTo`/`moveTo`), once, on entry with the launch code; reloads and manual moves are left alone; the size is not remembered. *Why:* Edge ignores window flags when it is already running; the page sees the work area and the display scaling.

## Review fixes (2026-10-22): D63–D65

**D63 — Real systemd tests outside CI.** Tests that start real units carry the `host` marker; CI runs everything except `host`, local `make test` runs everything. The environment check counts network isolation only on an explicit `ENETUNREACH` inside the unit while the same unit without isolation reaches the network; anything else fails. *Why:* the CI runner's user units do not cut the network, so a pass there proves nothing.

**D64 — Export in the Library uses the session's own WAV.** `export_clips(seg_dir, source=None)`: without `source` the path from `run.json` (CLI as before), with it that file; the sha256 is checked either way. The artifact formats do not change. *Why:* a product folder can be moved; the absolute path in `run.json` is metadata, not a dependency.

**D65 — "Process" finishes an interrupted move.** If an import stopped mid-move, `finish_import` takes the missing files by name from `inbox/<source>/` (regular files, not links), checks each sha256 against `import.pending.json` and moves them the same way (D39); a file missing everywhere or with another sha256 refuses before any move. Duplicate-import checks include unfinished imports. A rollback never overwrites: a file whose place is taken stays in `originals.tmp`. *Why:* an interrupted import must be finishable without losing or duplicating a recording.

## Localization (2026-10-23): D66–D77

**D66 — Error and status contract.** Everything that reaches the UI travels as a descriptor `{"code", "params"}` with a whitelist of parameters and no message text. Codes and templates live in one pure module, `voxcut/errors.py`; `str(exc)` gives the plain text for CLI and logs. An unexpected error becomes `internal_error` with details in the log; inside a job it becomes `job_unexpected` with the exception's type and text. Diagnostics (OS error text, systemd or ffmpeg output) travel as an explicit untranslated `detail`. *Why:* the UI translates codes; the server never builds sentences for it.

**D67 — UI localization.** `static/i18n.js` (`t(key, params)`, `Intl.PluralRules`, a neutral fallback), flat catalogs `en.js` (source) and `ru.js`, errors as `err.<code>`; static HTML through `data-i18n*` attributes. Switching language live resets nothing; state holds descriptors. Mark values are data; only their labels are translated. *Why:* a small module with no dependency, and switching that loses no state.

**D68 — Dates by UI language.** Session dates, idea times and the last backup use `Intl.DateTimeFormat` of the current language; numbers stay as they are. *Why:* dates read naturally in each language. Refines D51 and D61.

**D69 — UI language in memory only.** The choice lives in the page until the window closes, like the theme; every launch starts in English; browser hints are ignored. *Why:* no stored state, no fingerprinting.

**D70 — Stage names.** `job.stage` is shown through catalog entries (import, assembly, speech detection, clip boundaries, cutting clips); an unknown stage shows its raw name. *Why:* plain words instead of internal names.

**D71 — English dates.** The English UI uses the `en-GB` locale with a 24-hour clock. *Why:* day-first dates and one clock across both languages. Refines D68.

**D72 — `http_no_code`.** "no launch code" instead of a bare "code". *Why:* readable.

**D73 — Error wording.** English `top.job_failed` is "{kind} failed"; dashes in two English errors became a colon and a semicolon; `journal_value_too_long` reads "text longer than {limit} characters". *Why:* readable.

**D74 — Diagnostics untranslated.** Text inside `detail` stays as produced. *Why:* diagnostics are for logs and bug reports. Revised by D114, which made the backend's own text English.

**D75 — Job row and empty state.** While a job runs and has a stage, its row reads "{kind}: {stage}"; in any other state "{kind}: {state}", without the stage. The empty state of a session without a row points to "Process" in the Library. *Why:* a finished job's stage is noise. Refines D70.

**D76 — The language button.** Its label is the current language (EN or RU); its tooltip and `aria-label` name the action; normal weight, muted colour. *Why:* the button shows state, the tooltip explains it.

**D77 — Interface font.** Mulish (OFL 1.1, variable weight, Cyrillic and Latin, tabular figures by default) vendored unchanged as TTF with its sha256 pinned and its license next to it; `--font-sans: "Mulish", system-ui, "Segoe UI", sans-serif`; monospace stays system. *Why:* chosen from 31 Google Fonts sans-serifs with Cyrillic, weights 400–600 and tabular figures; TTF keeps the file verifiable against its source without a conversion tool.

## Library rework (2026-10-24): D78–D89

A design critique found the Jobs card clipped its content without scrolling: at 1280×800 about one and a half of twelve jobs were visible, and failed ones were unreachable by wheel or Tab.

**D78 — Two-column layout.** Wide column: Sessions, then the Job log. Narrow column: Inbox, Backup, Ideas. Cards take their content's height and nothing is clipped; the whole Library screen scrolls; a narrow window stacks them in one column. Criterion: at 1280×800 and 1000×700 with 12+ jobs every row and button is reachable by wheel and Tab, and focus never lands on something invisible. *Why:* the clipped card hid failures.

**D79 — A job row names its subject.** Newest first, with the start (or submit) time and the duration. Row: "{kind} {subject} — {state}". The backend's job view adds `subject` from a whitelist per kind (`process` → session id, `import` → source, `backup` → mode, `restore` → name); files, start times and paths never leave the server, and every subject value is already visible in the state. A failed job also names the stage it failed at. *Why:* a log of "processing — failed" without saying what failed is useless. Refines D75.

**D80 — The log opens under its row.** Collapsed by default behind a "Log ▸" button; system monospace 12 px on a muted background, up to about 320 px with its own scroll, scrolled to the end; stage lines in bold ink, a failed job's last line in red. A running job's log is open and follows new lines unless scrolled up. Open state and scroll survive polling. *Why:* the log is for the moment something goes wrong; the rest of the time it is noise.

**D81 — Finished jobs.** Each finished job is a muted row; consecutive finished backups fold into one row ("backup ×7 — done · last …") that expands on click. *Why:* routine backups must not bury other jobs.

**D82 — Retry for failed jobs.** A failed job of any kind has "Retry" while its failure is open (D84). `POST /api/jobs/{job_id}/retry` takes only a new job id: the server reuses the failed job's kind and parameters and submits through the same validation; the browser sends no parameters. Refused with a code when the parameters are stale, the job is not failed or its failure is not open, or the same job is already queued or running. Cancelled jobs have no retry. Same key, CSRF and security checks as any job submission. *Why:* one click to recover, with no way to smuggle parameters.

**D83 — Cancel in two steps.** A small outline button in a running or queued job's row; the first click turns it into "Cancel? Yes / No" in place; "No" and `Esc` restore it. *Why:* a stray click must not kill a job.

**D84 — Failure state, computed.** A failed job's failure is open, resolved or moot, computed by the server from the job records and the contents of `inbox/` and `backups/`; there is no manual dismissal. Resolved: the job's goal was reached later (the same session processed, every file imported, a later backup that covers it, the same archive restored). Moot: the goal became impossible (the import's source folder or the restore's archive is gone). Otherwise open. Job lists always include every open failure. An open failure shows a red badge (the newest one, "+N" for more); clicking it opens the Library, scrolls to the job, opens its log and focuses it. *Why:* a failure must neither be dismissed by hand nor vanish by accident; red stays only while the problem is real.

**D85 — Label on a session row.** While processing is queued or running, the row says so with the stage; with an open processing failure it shows a red label leading to the job. *Why:* the session list shows a session's state without opening the log.

**D86 — Out of scope.** Other critique minors (backup wording, a heading that looks like a label, header wrapping at 1000 px, a spinner), keyboard keys inside the Library and copying a log. *Why:* not needed for the rework.

**D87 — What resolves an import failure.** Only a later successful import from the same source folder whose files include all of the failed job's files. *Why:* recorder file names repeat across folders. Refines D84.

**D88 — A job that failed again.** A failure is retired when the same job (kind and parameters) failed later: only the newest stays red, older ones turn grey and say "retried". `failure` gains the value `"retried"`; checks run in the order resolved, retried, moot, open. *Why:* one red row per real problem. Refines D84.

**D89 — Import failures after an interrupted move.** An import failure is also resolved when all its files appear in some session's `import.json` with the same source, whoever finished the import; it is moot when any of its files is in neither the source folder nor a pending import of that folder. *Why:* "Process" can finish an interrupted import (D65) without an import job. Refines D84.

## Library: year, hiding and deleting a session (2026-10-25): D90–D96

**D90 — Delete means undo the import.** The product never destroys a recording. "Delete session" moves the files from `originals/` back to `inbox/<session_id>/`, unchanged, with sha256 checked before and after, together with the session's notes (D93); everything else in the session directory is erased. Only the user deletes a recording, by hand, from the inbox. *Why:* `originals/` often holds the only copy; a trash with expiry or an immediate delete were rejected. Adds the reverse of D39 as the second allowed move of a recording.

**D91 — Hide with a cross; delete only what is hidden.** Every session row has a small cross that hides it at once (reversible, no confirmation). Below the list, "Hidden: N · show" reveals hidden sessions with "Restore" and "Delete…"; delete asks for confirmation inline. A visible session has no delete button. A hidden session does not open from the list. *Why:* deleting is two deliberate steps away; hiding is one.

**D92 — Hiding lives in the journal.** Events `session_hidden` and `session_shown` with the session id; the last one wins. The journal stays append-only; hiding travels with backups and merges on restore. The server accepts these events only for an existing session. *Why:* no new state files and nothing lost on restore.

**D93 — Notes on delete.** Before erasing, the session's notes are written to `inbox/<session_id>/notes.txt`, one line per note with its time span, earliest first; no notes, no file. Ideas are not tied to sessions and stay; marks are not exported. Journal events are never erased: they reappear if the same files are imported again. *Why:* notes are the user's text; marks only mean something next to clips.

**D94 — Delete rules.** Delete is a job of its own kind and stays in the log as a trace. It refuses with a code when the session is not hidden, has a queued or running job, `inbox/<session_id>` already exists, or a sha256 does not match (moved files go back, the session stays). Order: checks, move recordings and `notes.txt` through a hidden temp folder in the inbox, `os.replace`, then erase the session directory (renamed to `*.tmp` first; an interrupted delete is finished by running it again). Processing jobs of the session and their failures leave the log. Backups are untouched. Details: the job writes `session_shown` when done, so a session that returns is visible; only a session with a finished import can be deleted; a corrected start time is saved as `start.txt` and prefilled on reimport, so the session id and its marks come back. Known limit: after a delete, a `light` backup brings the session back without its recording. *Why:* every step is reversible until the last, and nothing is lost on a crash.

**D95 — Year in session names.** The session list and job subjects use the full date with year; the header uses digits (`DD.MM.YY`). *Why:* sessions span years; the header has little room.

**D96 — Job-log minors.** Review findings of the Library rework are fixed from a list without new decisions. *Why:* already decided scope.

## The Review tab (2026-10-25): D97–D113

**D97 — A decision inside a filter.** If a decision takes the clip out of the current filter (keep in "rejected", reject in "kept"), the filter switches to "all" with the clip still selected and centred; otherwise the filter stays. Either way playback pauses and `Space` resumes. Undo works as before and does not switch the filter back. *Why:* a decision must never make the clip vanish from under the user. Changes D42 for those filters.

**D98 — Decision buttons.** Unpressed, all five are outline buttons, "Keep" included. Pressed "Keep" is filled green; a pressed reason is filled with the primary colour (black in light, white in dark). Same in the re-choice menu. *Why:* green means kept and nothing else.

**D99 — No badge on undecided clips.** A badge appears only with a decision. *Why:* the empty spot and the timeline mark already say "undecided".

**D100 — Each tab keeps its own selection.** Review and My clips each remember their selected clip; switching back does not play. *Why:* switching tabs must not lose the place.

**D101 — A failed decision write names the clip.** The toast reads "Not saved: 00:12:30 — music" with a "go to clip" button and the error below; it stays until closed; several failures stack. "Go to clip" selects the clip paused, switching tab, filter or session as needed. *Why:* a lost decision must be findable.

**D102 — Review minors.** Long notes truncate in the list and wrap in the card; no horizontal page scroll at any width; no pause separators in a filtered list; an `Esc — save` hint under an open note; advice on a missing clip; header and font-size cleanups. *Why:* fixes within decided scope.

**D103 — Narrow header and decision buttons.** When the header does not fit, badges, theme, language, keys and "Library" wrap to a second line on the right; below a 740 px card the decision buttons hide their icons and keep labels and keys. *Why:* five decisions stay on one line.

**D104 — Home restarts a clip.** `Home` plays the selected clip from its start. *Why:* re-listening is common.

**D105 — A clip that did not play.** A failed `play()` shows an ordinary toast with the reason; an interruption by moving on (`AbortError`) shows nothing. *Why:* silence must not look like an empty clip.

**D106 — Header of a reviewed session.** With nothing undecided, the counter is replaced by a short status in the counter's colour, not green. Word chosen in D109.

**D107 — Folding rejected clips.** In "all" on Review, consecutive rejected clips (a single one too) fold into one row "▸ N rejected". `→` on it expands it and selects the first clip, `←` the last; an expanded fold closes only by a click on "▾ N". A rejected clip with other decisions on its span does not fold. The selected clip is never hidden: reaching a folded clip any other way expands its fold. *Why:* reviewed noise must not push undecided clips off the screen.

**D108 — Minors.** Compact filter chips, a toast whose second line does not repeat its title, cheap CSS fixes at 640 px only. *Why:* polish within decided scope.

**D109 — "✓ reviewed".** The status of a reviewed session reads "✓ reviewed", the same word as the counter. *Why:* it names what happened. Refines D106.

**D110 — "Process now?" after imports.** When every import the page saw queued or running has finished, one dialog lists the sessions those imports created that can be processed and have no job yet; a session the user already started is left out; none, no dialog. "Process" queues them all, "Later", `Esc` or a click outside closes. Client only. *Why:* importing is almost always followed by processing.

**D111 — A line above the clip list.** A border-coloured line on top of the list on both tabs. *Why:* separates the filters from the clips.

**D112 — Filter chips at 1280 stay on two lines.** Measured: one line needs abbreviated labels, which were rejected. *Why:* readable labels beat one line. Closes D108's first point.

**D113 — Toasts at the top in a narrow window.** Below 900 px the toast stack sits at the top, at most 40 % of the height, scrolling beyond; a new toast always scrolls into view. *Why:* toasts must not cover the decision buttons at the bottom; the cost is covering part of the header until closed.

## Backend in English, canonical job logs (2026-10-27): D114–D117

**D114 — The backend in English.** Job logs, error templates in `errors.py` (byte for byte the English UI catalog), CLI output and help, diagnostics inside errors, the launcher console and exception texts are English. The UI translates through its catalogs only. *Why:* one language for logs and bug reports. Revises D74.

**D115 — Job log line format.** `DD.MM.YY HH:MM LEVEL message`, levels `INFO`, `WARNING`, `ERROR`, local time. First line `job <id8> <kind> started` with parameters; a stage is `stage=<name>`; the last is `job done in <duration>` or, after the traceback, `ERROR job failed after <duration>: <first line of the cause>`. *Why:* the conventional shape, easy to scan and grep.

**D116 — Log highlighting for the new format.** `stage=` lines bold ink, `ERROR` lines red; old logs are shown without highlighting. *Why:* the same reading aid as D80.

**D117 — CLI output.** Progress and warnings go to stderr in the D115 format (without the job's first and last lines); results to stdout without a prefix; a fatal error prints `voxcut: <text>` and exits with code 2. *Why:* pipes get clean results.

## Publishing (2026-10-27): D118–D133

**D118–D127, D132–D133: repository publishing.** How this repository was prepared for publication: what the README shows, languages, this log, how the history was cleaned, the demo, and the working setup afterwards. Not product decisions.

**D128 — Mechanical leak protection.** `tools/leakcheck.py` runs as pre-commit, commit-msg and pre-push hooks and in CI. It refuses private values (kept outside this repository), home paths, process documents and audio files in staged files, commit messages and every commit of a push, and in the tree also Cyrillic outside tests, translations, `*.ru.md`, code comments and an allowlist. Without the private values the hooks refuse; CI runs the general patterns only. Tests plant a leak of each kind. *Why:* a rule people must remember will eventually be forgotten; a hook is not.

**D129 — License.** MIT, copyright `thd`; [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) carries the full Silero MIT text and points to the vendored assets' licenses. *Why:* permissive, and the ported code keeps its notice.

**D130 — Security fixes before publishing.** API keys are capped (the oldest falls out), idle connections time out after 30 s, restore checks free space before unpacking and refuses a target that leads outside `data/` through a link, and CI actions are pinned by commit SHA with read-only token permissions. A known limit stays and is documented: the one-time launch code is visible on Edge's command line for up to 30 seconds. *Why:* each was a concrete local weakness; changing the launch scheme is a separate decision.

**D131 — Wording.** Code and comments say "user" for the person running the product; the recorder model is not named, only its file-name format `YYYYMMDDHHMMSS.WAV`. *Why:* the product is about any user with such a recorder.
