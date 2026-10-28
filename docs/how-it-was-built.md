English | [Русский](how-it-was-built.ru.md)

# How it was built

VoxCut was built in two weeks (14–27 October 2026) by a human and an AI coding agent. This page describes the method, six episodes where something went wrong and how it was caught, and what is still not done.

## Method

**Roles.** The human set the requirements and made every design decision. The agent wrote the code, the tests and the documents, and proposed options with their costs. When a task needed a decision that was not on record, the agent stopped and asked.

**The loop.** Decision → plan → test → code → mutation → review.
- A new feature starts with a design discussion. Its outcome is numbered decisions in the [decision log](decisions.md).
- A plan splits the work into layers (pure logic, then audio, then CLI, then UI), each task with a done criterion.
- Per task: the test for the criterion first, then the code; lint and fast tests green; then one or two deliberate mutations of the code, each of which must turn a test red; then a commit.
- At each layer boundary a fresh reviewer reads that layer's diff and tries to break the hard rules it touches. At the end of a plan, a review of the whole diff and the full test suite.

**Hard rules.** A few rules are not negotiable, because breaking them loses speech or data: never modify the source recording; artifacts are immutable run directories; ffmpeg only through one helper, never through a shell; the model loop only under `inference_mode` with one thread; heavy work only under a memory cap.

**Measured acceptance.** "Don't lose the voice" was turned into numbers before any tuning: recall by time, recall by phrase, phrases missed entirely, and a budget for minutes of clips per hour. The labeling protocol, including a random audit of the audio outside the candidates, was fixed before listening.

**Numbers.** over 360 commits in this repository; 136 decisions and 7 corrections; 23 plans and 7 design specs (kept outside this repository); about 4,100 lines of Python and 2,300 of JavaScript; about 1,000 tests (some 950 Python cases, 62 JavaScript).

## Episodes

**1. A memory leak found before the code existed.** Before writing the model loop, short probes checked the environment. One of them showed memory growing by about 21 KB per audio frame without `torch.inference_mode()`: about 7 GB for a three-hour recording. That became a hard rule and a test that runs 50,000 frames ([C1](decisions.md); [27168c7](../../../commit/27168c7f1bbfa7e1dce1956f606081112dea64d7)).

**2. A memory test that measured the wrong thing.** A mutation that exported a whole interval at once, instead of in blocks, passed the memory test. The test read `ru_maxrss`, a process-wide peak that an earlier test or the pytest parent with torch loaded had already raised. The fix measures peak memory in a fresh process via `VmHWM` ([766df3a](../../../commit/766df3a88baa26f10164fde0d08d7e8dffd9f988)).

**3. Whispers under music went unheard.** The pilot's audit windows held whispers that scored 0.000–0.009 in one continuous pass: over long music the model's recurrent state goes deaf. After a fresh reset the same whispers scored 0.05–0.26. VoxCut now runs a second pass with a reset every 120 s and keeps the maximum. Fully missed events went from 5 of 128 to 0 ([D31](decisions.md); [a56ee75](../../../commit/a56ee75239c984ec40922aaebde21b5c39861b35)).

**4. The holdout said no.** The starting config was chosen on the development part and failed on the held-out hour: recall by time 0.945 against a threshold of 0.975. Every uncovered piece was the edge of a whisper next to a clip. A 3-second pad fixed it, and the log says plainly that the pad was chosen looking at the holdout, so the holdout is spent ([D36, D37](decisions.md); [b15a012](../../../commit/b15a0122baad01e418ddd83dbfdd2725f6e4b90e)).

**5. A heavy run took the whole system down.** Under WSL, a heavy run ran out of memory, and the out-of-memory kill took WSL's init with it: the whole Linux environment died. Since then every heavy step runs as a systemd user unit with `MemoryMax=5G` and no swap; the UI's jobs run as such units too ([9abcf30](../../../commit/9abcf3088ccd8d11cd3d3d3b1702726789d4214d)).

**6. A review found an unbounded read.** The local UI server accepted `Content-Length: -1`, and the body reader then read the socket without a limit. A review caught it; the header must now be a plain decimal within a limit ([3fcba6a](../../../commit/3fcba6aefe1f78061381e19515fdb9dfa28f7cdc)).

## What didn't work out

- **The holdout is spent.** The only labeled session has been used for both choosing and checking. The next honest test needs a new labeled session.
- **No speech recognition.** VoxCut stops at clips with a manifest; transcription is a later stage.
- **One platform for the UI.** Windows 11 with WSL2 and Microsoft Edge only.
- **Precision stays low.** Detectors that might separate singing from speech made things worse ([D33](decisions.md)), so a human still reviews most clips.

Process documents (specs, plans, review notes) were removed from the history when the repository was published. Commit messages still mention them.
