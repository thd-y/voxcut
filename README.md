English | [Русский](README.ru.md)

[![check](https://github.com/thd-y/voxcut/actions/workflows/check.yml/badge.svg)](https://github.com/thd-y/voxcut/actions/workflows/check.yml)

# VoxCut

Finds one person's speech, whispers included, in hours of microphone recordings with music playing, and cuts it into clips for a later transcription step.

**TL;DR**
- Drop a recorder's hourly WAV files into a folder. VoxCut assembles the session, runs Silero VAD over it and cuts generous clips with a manifest.
- Review the clips with the keyboard: keep, or reject as vocals, music, noise or other. Nothing is ever deleted.
- Recall comes first. On the calibration session it missed no labeled phrase; the cost is about 16 minutes of clips to listen to per hour of recording.
- Local only: a Python CLI and a browser UI on 127.0.0.1. Nothing leaves the machine.

![VoxCut: import, processing, keyboard review, a note, My clips](docs/demo.gif)

## Why it's hard

Two goals pull in opposite directions. The detector must not lose a single phrase, including whispers and mumbling, and it must not drown in vocals, music and noise.

- **Whispers under music.** Over long music the model's recurrent state goes deaf: in one continuous pass, whispers scored 0.000–0.009. VoxCut runs a second pass that resets the model every 120 s and takes the maximum of the two ([D31](docs/decisions.md)).
- **Singing looks like speech.** Separating vocals first (Demucs) or a different VAD made things worse: five times the false alarms for a little recall ([D33](docs/decisions.md)).

So the split is deliberate: the detector throws nothing away, and a quick keyboard review removes the rest.

## Status

There is no full automation. Recall is measured and high; precision is low by design. Most clips need a human decision, and the review screen is built to make that fast.

## First calibration

One multi-hour session was labeled by hand: every candidate in full, plus a random 20 % audit of the audio outside the candidates. One hour was held out.

- The starting config passed on development and **failed on the held-out hour**: the edges of whispers, up to 4 s long, fell just outside the clips ([D36](docs/decisions.md)).
- Padding clips by 3 s fixed it: development 0.999 recall by time, 1.000 by phrase, no phrase missed; held-out hour 0.981 and 1.000, none missed. The price is 15.7 clip-minutes per hour of recording ([D37](docs/decisions.md)).
- The padding was chosen looking at the held-out hour, so that hour is spent. The next honest test is a new labeled session.

**Next:** recalibrate on new labeled sessions, and use rejected clips as negatives to tune the detector.

## Quick start

```sh
sudo apt install ffmpeg libsndfile1 python3.12-venv
make setup    # .venv with the CPU build of torch
make ui       # opens the interface in an Edge window
```

Put a session's WAV files into `inbox/<any folder>/`, then Import and Process in the Library.

**Requirements.** The interface: Windows 11 with WSL2 (systemd enabled) and Microsoft Edge. The CLI (`.venv/bin/voxcut --help`): any Linux with Python 3.12 and ffmpeg.

## How it works

1. **Session.** Hourly files are checked (RIFF structure, decoder agreement, codec) and assembled bit-exactly into one WAV; seams are recorded.
2. **Probabilities.** Silero VAD in two passes, with resets at seams and every 120 s; the maximum is kept.
3. **Segments.** A port of Silero's hysteresis, then padding and merging of nearby speech.
4. **Clips.** Cut bit-exactly from the source, with a manifest of times, session and seams for transcription.
5. **Review.** Decisions and notes go into an append-only journal. Every run directory is immutable.

Heavy steps run as systemd user units with a memory cap, so a large recording cannot take the machine down.

## Security

The UI server binds only to 127.0.0.1, checks Host and Origin, and requires a per-window key obtained by exchanging a one-time launch code; a strict Content-Security-Policy forbids inline code. Known limitation: the launch code is visible on Edge's command line to other local processes for up to 30 seconds, until it is used.

## More

- [Architecture](docs/architecture.md): a diagram of the modules and the stack.
- [How it was built](docs/how-it-was-built.md): the method, and what went wrong along the way.
- [Decision log](docs/decisions.md): every design decision, D1–D136.
- Not included: speech recognition (the clips are its input), other operating systems and browsers for the UI.
- Built with an AI coding agent. A human set the requirements and made the decisions; the agent wrote the code, tests and documents under that review.
- License: [MIT](LICENSE). Third-party: [notices](THIRD_PARTY_NOTICES.md).
