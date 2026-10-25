// Pure functions of app.js (D48). DOM and Basecoat are checked in a headless browser.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const app = require("../../voxcut/ui/static/app.js");

test("parseSessionName", () => {  // D68: the date through Intl, units from the catalog (L7)
  assert.deepEqual(app.parseSessionName("2026-08-11_21-30_4h16m", "ru"),
    { date: "11 авг. 2026", time: "21:30", length: "4 ч 16 мин" });  // D95: no «г.»
  assert.deepEqual(app.parseSessionName("2026-08-11_21-30_4h16m", "en"),
    { date: "11 Aug 2026", time: "21:30", length: "4 h 16 min" });  // D71: en-GB
  assert.equal(app.parseSessionName("2026-01-02_08-05_45m", "ru").length, "45 мин");
  assert.equal(app.parseSessionName("2026-01-02_08-05_45m", "en").length, "45 min");
  assert.equal(app.parseSessionName("2026-01-02_08-05_45m", "ru").date, "2 янв. 2026");
  assert.equal(app.parseSessionName("2026-01-02_08-05_45m", "ru", "short").date, "02.01.26");
  assert.equal(app.parseSessionName("2026-12-31_00-00_5h", "ru").length, "5 ч");
  assert.equal(app.parseSessionName("2026-12-31_00-00_5h", "en").length, "5 h");
  for (const lang of ["ru", "en"]) {
    assert.equal(app.parseSessionName("folder01", lang), null);
    assert.equal(app.parseSessionName("2026-02-31_00-00_5h", lang, "short"), null);
    assert.equal(app.parseSessionName("2026-13-01_00-00_5h", lang), null);
    assert.equal(app.parseSessionName("2026-00-01_00-00_5h", lang), null);
    assert.equal(app.parseSessionName("2026-02-31_00-00_5h", lang), null);  // no roll-over into March
    assert.equal(app.parseSessionName("2026-08-11_25-99_5h", lang), null);  // no «25:99»
  }
  const row = { name: "2026-08-11_21-30_4h16m" };  // D95: full in lists, digits in the header
  assert.equal(app.sessionLabel(row, "ru"), "11 авг. 2026, 21:30 · 4 ч 16 мин");
  assert.equal(app.sessionLabel(row, "en"), "11 Aug 2026, 21:30 · 4 h 16 min");
  assert.equal(app.sessionLabel(row, "ru", "short"), "11.08.26, 21:30 · 4 ч 16 мин");
  assert.equal(app.sessionLabel(row, "en", "short"), "11/08/26, 21:30 · 4 h 16 min");
  assert.equal(app.sessionLabel({ name: "folder01" }, "en"), "folder01");
});

test("clockOf", () => {
  assert.equal(app.clockOf("2026-08-11T21:48:42"), "21:48:42");
  assert.equal(app.clockOf(null), "");
});

test("fmtDur", () => {
  assert.equal(app.fmtDur(7.4, "ru"), "7 с");
  assert.equal(app.fmtDur(111.2, "ru"), "1 мин 51 с");
  assert.equal(app.fmtDur(60, "ru"), "1 мин 0 с");
  assert.equal(app.fmtDur(7.4, "en"), "7 s");
  assert.equal(app.fmtDur(111.2, "en"), "1 min 51 s");
});

test("fmtPos", () => {
  assert.equal(app.fmtPos(2.3), "0:02");
  assert.equal(app.fmtPos(273), "4:33");
  assert.equal(app.fmtPos(3725), "1:02:05");
});

const clip = (id, marks = [], own = "", notes = [], start = 0, end = 1) => ({
  clip_id: id, start_s: start, end_s: end, marks, own_mark: own,
  notes: notes.map((value) => ({ value, own: true })),
});

test("status", () => {
  assert.deepEqual(app.status(clip("a")), { kind: "undecided", value: "", others: false });
  assert.deepEqual(app.status(clip("a", ["моё"])), { kind: "kept", value: "моё", others: false });
  assert.deepEqual(app.status(clip("a", ["шум"])), { kind: "rejected", value: "шум", others: false });
  assert.equal(app.status(clip("a", ["вокал", "моё"])).kind, "conflict");
  assert.deepEqual(app.status(clip("a", ["вокал", "моё"], "моё")),
    { kind: "kept", value: "моё", others: true });
  assert.deepEqual(app.status(clip("a", ["абв"])), { kind: "rejected", value: "абв", others: false });
});

test("counts", () => {
  const c = app.counts([clip("a"), clip("b", ["моё"]), clip("c", ["шум"]),
    clip("d", ["вокал", "моё"]), clip("e", ["абв"]), clip("f", ["вокал"])]);
  assert.deepEqual(c, { all: 6, undecided: 2, kept: 1, rejected: 3,
    reasons: { вокал: 1, музыка: 0, шум: 1, другое: 1 } });
});

test("nextUndecided", () => {
  const cs = [clip("a", ["моё"]), clip("b"), clip("c", ["шум"]), clip("d")];
  assert.equal(app.nextUndecided(cs, 1), 3);
  assert.equal(app.nextUndecided(cs, 3), 1);
  assert.equal(app.nextUndecided([clip("a", ["моё"])], 0), -1);
});

test("visibleClips", () => {
  const cs = [clip("a"), clip("b", ["моё"], "", ["Ёлка у дома"]), clip("c", ["шум"]),
    clip("d", ["вокал", "моё"]), clip("e", ["моё"])];
  const ids = (xs) => xs.map((x) => x.clip_id);
  assert.deepEqual(ids(app.visibleClips(cs, { tab: "review", filter: "all", query: "" })),
    ["a", "b", "c", "d", "e"]);
  assert.deepEqual(ids(app.visibleClips(cs, { tab: "review", filter: "undecided", query: "" })),
    ["a", "d"]);
  assert.deepEqual(ids(app.visibleClips(cs, { tab: "review", filter: "rejected", query: "" })), ["c"]);
  assert.deepEqual(ids(app.visibleClips(cs, { tab: "mine", filter: "mine", query: "" })), ["b", "e"]);
  assert.deepEqual(ids(app.visibleClips(cs, { tab: "mine", filter: "noted", query: "" })), ["b"]);
  assert.deepEqual(ids(app.visibleClips(cs, { tab: "mine", filter: "mine", query: "  ЁЛКА " })), ["b"]);
  assert.deepEqual(ids(app.visibleClips(cs, { tab: "mine", filter: "mine", query: "елка" })), ["b"]);
});

test("gapLabel", () => {
  assert.equal(app.gapLabel(273, "ru"), "пауза 4:33");
  assert.equal(app.gapLabel(273, "en"), "gap 4:33");
});

test("backupTime", () => {  // D61: «Последний бэкап» with the year; D68: Intl of the UI language
  const tz = process.env.TZ;
  process.env.TZ = "Europe/Moscow";
  try {
    assert.equal(app.backupTime("2026-10-22T06:53:19.402+03:00", "ru"), "22.10.2026, 06:53");
    assert.equal(app.ideaTime("2026-08-11T04:25:00+03:00", "ru"), "11.08, 04:25");
    assert.equal(app.ideaTime("2026-08-11T04:25:00+03:00", "en"), "11/08, 04:25");  // D71: en-GB, 24 h
    assert.equal(app.backupTime("2026-10-22T13:28:00+03:00", "en"), "22/10/2026, 13:28");
    assert.equal(app.ideaTime("мусор", "en"), "");
    assert.equal(app.backupTime("", "ru"), "");
    assert.equal(app.backupTime("garbage", "ru"), "garbage");
  } finally {
    if (tz === undefined) delete process.env.TZ;
    else process.env.TZ = tz;
  }
});

test("gapBefore", () => {
  const cs = [clip("a", [], "", [], 0, 10), clip("b", [], "", [], 39.9, 45), clip("c", [], "", [], 75, 80)];
  assert.equal(app.gapBefore(cs, 0), null);
  assert.equal(app.gapBefore(cs, 1), null);
  assert.equal(app.gapBefore(cs, 2), 30);
});

test("undo", () => {
  const cs = [clip("a", ["моё"], "моё"), clip("b")];
  const stack = [app.undoEntry(cs[0])];
  assert.deepEqual(stack[0], { clip_id: "a", prev: "моё" });
  assert.deepEqual(app.undoTarget(stack, cs), { entry: { clip_id: "a", prev: "моё" }, index: 0 });
  assert.equal(stack.length, 0);
  stack.push({ clip_id: "zzz", prev: "" });
  assert.equal(app.undoTarget(stack, cs).index, -1);
  assert.equal(app.undoTarget(stack, cs), null);
});

test("keyAction", () => {
  const ctx = { inField: false, dialogOpen: false, screen: "review", rechoose: null };
  const cases = [
    [{ code: "Digit2" }, {}, "mark:вокал"],
    [{ code: "Digit1" }, {}, "mark:моё"],
    [{ code: "Digit2", repeat: true }, {}, null],
    [{ code: "Digit1" }, { inField: true }, null],
    [{ code: "Escape" }, { inField: true }, "escape"],
    [{ code: "Enter", ctrlKey: true }, { inField: true, dialogOpen: true }, "save-idea"],
    [{ code: "Enter", ctrlKey: true }, { inField: true }, null],
    [{ code: "Digit3" }, { dialogOpen: true }, null],
    [{ code: "Escape" }, { dialogOpen: true }, "escape"],
    [{ code: "KeyZ", ctrlKey: true }, {}, "undo"],
    [{ code: "KeyZ", ctrlKey: true, shiftKey: true }, {}, null],
    [{ code: "KeyC", ctrlKey: true }, {}, null],
    [{ code: "Digit1", altKey: true }, {}, null],
    [{ code: "Space" }, {}, "play"],
    [{ code: "Space", repeat: true }, {}, null],
    [{ code: "ArrowLeft" }, {}, "prev"],
    [{ code: "ArrowRight" }, {}, "next"],
    [{ code: "ArrowRight", shiftKey: true }, {}, "seek:5"],
    [{ code: "ArrowLeft", shiftKey: true }, {}, "seek:-5"],
    [{ code: "Slash" }, {}, null],
    [{ code: "Slash" }, { screen: "mine" }, "search"],
    [{ code: "Slash", shiftKey: true }, {}, "help"],
    [{ code: "KeyT" }, {}, null],
    [{ code: "KeyA" }, {}, null],
    [{ code: "Space" }, { screen: "library" }, null],
    [{ code: "Digit1" }, { screen: "library" }, null],
    [{ code: "KeyN" }, { screen: "library" }, null],
    [{ code: "KeyM" }, { screen: "library" }, null],
    [{ code: "KeyG" }, { screen: "library" }, "library"],
    [{ code: "KeyI" }, { screen: "library" }, "idea"],
    [{ code: "KeyN" }, {}, "note"],
    [{ code: "KeyM" }, {}, "tab"],
    [{ code: "KeyG" }, {}, "library"],
    [{ code: "Numpad5" }, {}, "mark:другое"],
    [{ code: "Digit6" }, {}, null],
    // D52: re-choice in «Мои клипы»
    [{ code: "KeyR" }, { screen: "mine" }, "rechoose"],
    [{ code: "KeyR" }, {}, null],
    [{ code: "KeyR" }, { screen: "library" }, null],
    [{ code: "Digit3" }, { screen: "mine" }, null],
    [{ code: "Digit3" }, { screen: "mine", rechoose: "menu" }, "pick:музыка"],
    [{ code: "Numpad1" }, { screen: "mine", rechoose: "menu" }, "pick:моё"],
    [{ code: "Digit3", repeat: true }, { screen: "mine", rechoose: "menu" }, "noop"],
    [{ code: "Escape" }, { screen: "mine", rechoose: "menu" }, "cancel"],
    [{ code: "KeyN" }, { screen: "mine", rechoose: "menu" }, "noop"],
    [{ code: "Space" }, { screen: "mine", rechoose: "menu" }, "noop"],
    [{ code: "Enter" }, { screen: "mine", rechoose: "menu" }, "noop"],
    [{ code: "Digit6" }, { screen: "mine", rechoose: "menu" }, "noop"],
    [{ code: "Tab" }, { screen: "mine", rechoose: "menu" }, null],
    [{ code: "Digit3", ctrlKey: true }, { screen: "mine", rechoose: "menu" }, null],
    [{ code: "Space" }, { screen: "mine", rechoose: "музыка" }, "noop"],
    [{ code: "Enter", repeat: true }, { screen: "mine", rechoose: "музыка" }, "noop"],
    [{ code: "Tab" }, { screen: "mine", rechoose: "музыка" }, null],
    [{ code: "Enter" }, { screen: "mine", rechoose: "музыка" }, "confirm"],
    [{ code: "Digit3" }, { screen: "mine", rechoose: "музыка" }, "confirm"],
    [{ code: "Digit3", repeat: true }, { screen: "mine", rechoose: "музыка" }, "noop"],
    [{ code: "Digit4" }, { screen: "mine", rechoose: "музыка" }, "cancel"],
    [{ code: "Escape" }, { screen: "mine", rechoose: "музыка" }, "cancel"],
    [{ code: "KeyN" }, { screen: "mine", rechoose: "музыка" }, "noop"],
    [{ code: "KeyZ", ctrlKey: true }, { screen: "mine", rechoose: "музыка" }, null],
  ];
  for (const [ev, c, want] of cases) {
    const e = { ctrlKey: false, metaKey: false, altKey: false, shiftKey: false, repeat: false, ...ev };
    assert.equal(app.keyAction(e, { ...ctx, ...c }), want, JSON.stringify([ev, c]));
  }
});

test("launchRect", () => {
  assert.deepEqual(app.launchRect({ width: 2560, height: 1392, left: 0, top: 0 }),
    { width: 1280, height: 800, left: 640, top: 296 });  // D62, probe 2026-10-22
  assert.deepEqual(app.launchRect({ width: 1093, height: 574, left: 0, top: 0 }),
    { width: 984, height: 517, left: 55, top: 29 });  // 1366×768 at 125%
  assert.deepEqual(app.launchRect({ width: 1920, height: 1040, left: -1920, top: 0 }),
    { width: 1280, height: 800, left: -1600, top: 120 });  // second monitor on the left
  const r = app.launchRect({ width: 1001, height: 701, left: 0, top: 0 });
  assert.ok(Object.values(r).every(Number.isInteger));
});

test("autoOpenTarget", () => {
  const s = (id, state) => ({ session_id: id, state });
  const list = [s("a", "imported"), s("b", "clips"), s("c", "clips")];
  assert.equal(app.autoOpenTarget([], null), null);
  assert.equal(app.autoOpenTarget(list, null), "c");
  assert.equal(app.autoOpenTarget(list, "c"), null);  // tried once: no retry every poll
  assert.equal(app.autoOpenTarget([s("a", "imported"), s("b", "probs")], null), "a");
});

test("afterOpenError", () => {  // the state keeps the descriptor, the screen renders it (D67)
  const desc = { code: "run_unreadable", params: { detail: "x" } };
  const server = { message: "прогон не читается: x", descriptor: desc };
  const offline = { message: "нет связи с VoxCut", offline: true, descriptor: { code: "offline", params: {} } };
  assert.deepEqual(app.afterOpenError(true, server), { loaded: true, openError: null });  // it stays
  assert.deepEqual(app.afterOpenError(true, offline), { loaded: true, openError: null });
  assert.deepEqual(app.afterOpenError(false, offline), { loaded: null, openError: null });  // skeleton + alert
  assert.deepEqual(app.afterOpenError(false, server), { loaded: null, openError: desc });
  const d = (code, params = {}) => ({ code, params });
  assert.deepEqual(app.afterOpenError(false, app.codedError(d("http_need_key"))),
    { loaded: null, openError: d("http_need_key") });
  assert.deepEqual(app.afterOpenError(false, new Error("boom")),  // a JS error: its text as is
    { loaded: null, openError: d("legacy_error", { detail: "boom" }) });
});

test("retryAfter", () => {
  assert.equal(app.retryAfter({ offline: true }, false), true);  // nothing on screen: retry on the next good poll
  assert.equal(app.retryAfter({ offline: true }, true), false);  // the open session stays, the owner clicks again
  assert.equal(app.retryAfter({ offline: true, stale: true }, false), false);  // a newer open replaced it
  assert.equal(app.retryAfter({}, false), false);  // a server error: «Сессия не открылась», no hammering
});


test("statusText", () => {  // D67: the status line from the catalog, marks through mark.* (L14)
  const cases = [
    [clip("a", ["моё"]), "оставлен", "kept"],
    [clip("a", ["вокал", "моё"], "моё"), "оставлен · есть другие решения", "kept · other decisions exist"],
    [clip("a", ["вокал"]), "отброшен: вокал", "rejected: vocals"],
    [clip("a", ["вокал", "моё"], "вокал"), "отброшен: вокал · есть другие решения",
      "rejected: vocals · other decisions exist"],
    [clip("a", ["смех"]), "отброшен: смех", "rejected: смех"],  // a value without a label: as is (U5)
    [clip("a", ["вокал", "моё"]), "разные решения: вокал, моё", "different decisions: vocals, kept"],
    [clip("a", ["смех", "шум"]), "разные решения: смех, шум", "different decisions: смех, noise"],
    [clip("a"), "не разобран", "not reviewed"],
  ];
  for (const [c, ru, en] of cases) {
    assert.equal(app.statusText(c, "ru"), ru);
    assert.equal(app.statusText(c, "en"), en);
  }
});

test("summaryText", () => {
  const c = app.counts([clip("a", ["моё"]), clip("b", ["моё"]), clip("c", ["моё"]), clip("d", ["вокал"]),
    clip("e", ["музыка"])]);
  assert.equal(app.summaryText(c, "ru"), "Оставлено 3 · Отброшено 2: вокал 1, музыка 1, шум 0, другое 0");
  assert.equal(app.summaryText(c, "en"), "Kept 3 · Rejected 2: vocals 1, music 1, noise 0, other 0");
});

test("chainText", () => {  // plural «файл/файла/файлов» (decision 4); ISO start and minutes as is (L8)
  const ch = (n, imported = false) => ({ chain: 1, files: Array.from({ length: n }, (_, i) => `${i}.WAV`),
    start_local: "2025-03-01T08:29:21", duration_s: 3600, imported });
  assert.equal(app.chainText(ch(1), "ru"), "цепочка 1: 1 файл, начало 2025-03-01T08:29:21, 60.0 мин");
  assert.equal(app.chainText(ch(2), "ru"), "цепочка 1: 2 файла, начало 2025-03-01T08:29:21, 60.0 мин");
  assert.equal(app.chainText(ch(5), "ru"), "цепочка 1: 5 файлов, начало 2025-03-01T08:29:21, 60.0 мин");
  assert.equal(app.chainText(ch(21, true), "ru"),
    "цепочка 1: 21 файл, начало 2025-03-01T08:29:21, 60.0 мин, уже импортирована");
  assert.equal(app.chainText(ch(1), "en"), "chain 1: 1 file, start 2025-03-01T08:29:21, 60.0 min");
  assert.equal(app.chainText(ch(2, true), "en"),
    "chain 1: 2 files, start 2025-03-01T08:29:21, 60.0 min, already imported");
});

const SID = "2026-08-11_21-30_4h16m.0badface";
const SESSIONS = [{ session_id: SID, name: "2026-08-11_21-30_4h16m" }];
const job = (kind, state, extra = {}) => ({ job_id: `${kind}-${state}`, kind, state, stage: null,
  subject: { process: { session_id: SID }, import: { source: "24_oct" }, backup: { mode: "light" },
    restore: { name: "voxcut-backup_x_full.tar" } }[kind] || {}, failure: null, ...extra });

test("jobSubject", () => {  // D79: the session through sessionLabel, else the raw id
  assert.equal(app.jobSubject(job("process", "done"), SESSIONS, "ru"), "11 авг. 2026, 21:30 · 4 ч 16 мин");
  assert.equal(app.jobSubject(job("process", "done"), [], "ru"), "11 авг. 2026, 21:30 · 4 ч 16 мин");  // review: the label from the id
  assert.equal(app.jobSubject(job("import", "done", { subject: { source: "" } }), [], "ru"), "inbox/ (корень)");
  assert.equal(app.jobSubject(job("restore", "done"), [], "en"), "voxcut-backup_x_full.tar");
  assert.equal(app.jobSubject(job("odd", "done"), [], "en"), "");
});

test("jobLine", () => {  // D79: «{kind} {subject} — {state}»; the stage also on a failed one
  const label = "11 авг. 2026, 21:30 · 4 ч 16 мин";
  assert.equal(app.jobLine(job("process", "failed", { stage: "probs" }), SESSIONS, "ru"),
    `обработка ${label} — упала на „поиске речи“`);
  assert.equal(app.jobLine(job("process", "failed", { stage: "probs" }), SESSIONS, "en"),
    "processing 11 Aug 2026, 21:30 · 4 h 16 min — failed at “speech detection”");
  assert.equal(app.jobLine(job("process", "failed"), [], "ru"), "обработка 11 авг. 2026, 21:30 · 4 ч 16 мин — ошибка");
  assert.equal(app.jobLine(job("import", "done"), [], "ru"), "импорт 24_oct — готово");
  assert.equal(app.jobLine(job("backup", "done"), [], "ru"), "бэкап light — готово");
  assert.equal(app.jobLine(job("process", "running", { stage: "segment" }), SESSIONS, "ru"),
    `обработка ${label} — разметка клипов`);
  assert.equal(app.jobLine(job("process", "running"), SESSIONS, "ru"), `обработка ${label} — идёт`);
  assert.equal(app.jobLine(job("process", "done", { stage: "export" }), SESSIONS, "ru"), `обработка ${label} — готово`);
  assert.equal(app.jobLine(job("backup", "queued"), [], "en"), "backup light — queued");
  assert.equal(app.jobLine(job("odd", "failed", { stage: "newstage" }), [], "ru"), "odd — упала на „newstage“");
});

test("jobClock and jobDuration", () => {  // D79: start (or submission) time; how long it ran or runs
  const tz = process.env.TZ;
  process.env.TZ = "Europe/Moscow";
  try {
    assert.equal(app.jobClock({ started: "2026-08-11T04:25:00+03:00", submitted: "2026-08-11T04:20:00+03:00" }, "ru"), "11.08, 04:25");
    assert.equal(app.jobClock({ started: null, submitted: "2026-08-11T04:20:00+03:00" }, "ru"), "11.08, 04:20");
    assert.equal(app.jobClock({}, "ru"), "");
  } finally {
    if (tz === undefined) delete process.env.TZ;
    else process.env.TZ = tz;
  }
  const now = new Date("2026-10-24T10:12:30+03:00");
  assert.equal(app.jobDuration({ state: "running", started: "2026-10-24T10:00:00+03:00" }, now, "ru"), "идёт 12 мин");
  assert.equal(app.jobDuration({ state: "done", started: "2026-10-24T01:00:00+03:00",
    finished: "2026-10-24T05:16:10+03:00" }, now, "ru"), "4 ч 16 мин");
  assert.equal(app.jobDuration({ state: "done", started: "2026-10-24T01:00:00+03:00",
    finished: "2026-10-24T03:00:00+03:00" }, now, "en"), "2 h");
  assert.equal(app.jobDuration({ state: "failed", started: "2026-10-24T01:00:00+03:00",
    finished: "2026-10-24T01:00:40+03:00" }, now, "ru"), "40 с");
  assert.equal(app.jobDuration({ state: "queued", started: null }, now, "ru"), "");
  assert.equal(app.jobDuration({ state: "cancelled", started: "2026-10-24T01:00:00+03:00", finished: null }, now, "ru"), "");
});

test("journalRows", () => {  // D81: newest first; done backups in a row become one group
  const b = (n, state = "done", kind = "backup") => ({ ...job(kind, state), job_id: String(n) });
  const ids = (rows) => rows.map((r) => (r.group ? r.group.map((j) => j.job_id) : r.job.job_id));
  assert.deepEqual(ids(app.journalRows([b(1), b(2), b(3, "failed", "process"), b(4), b(5), b(6)])),
    [["6", "5", "4"], "3", ["2", "1"]]);
  assert.deepEqual(ids(app.journalRows([b(1), b(2, "done", "import"), b(3)])), ["3", "2", "1"]);
  assert.deepEqual(ids(app.journalRows([b(1), b(2, "failed"), b(3)])), ["3", "2", "1"]);
  assert.deepEqual(ids(app.journalRows([b(1), b(2, "cancelled"), b(3), b(4, "running")])), ["4", "3", "2", "1"]);
  assert.deepEqual(app.journalRows([]), []);
});

test("groupKey", () => {  // an open group stays open when a newer backup joins it
  const b = (n) => ({ ...job("backup", "done"), job_id: String(n) });
  const before = app.journalRows([b(1), b(2)])[0].group;
  const after = app.journalRows([b(1), b(2), b(3)])[0].group;
  assert.equal(app.groupKey(after), app.groupKey(before));
});

test("jobLine failed without a stage", () => {  // ru: «ошибка» fits every kind; «упала на» only with a stage
  assert.equal(app.jobLine(job("import", "failed"), [], "ru"), "импорт 24_oct — ошибка");
});

test("groupLine", () => {  // D81: «бэкап ×7 — готово · последний 23.10, 10:37»
  const tz = process.env.TZ;
  process.env.TZ = "Europe/Moscow";
  try {
    const g = [{ finished: "2026-10-23T10:37:06+03:00" }, { finished: "2026-10-22T10:00:00+03:00" }];
    assert.equal(app.groupLine(g, "ru"), "бэкап ×2 — готово · последний 23.10, 10:37");
    assert.equal(app.groupLine(g, "en"), "backup ×2 — done · last 23/10, 10:37");
  } finally {
    if (tz === undefined) delete process.env.TZ;
    else process.env.TZ = tz;
  }
});

test("failedBadge", () => {  // D84: the newest open failure, +N for the rest
  const f = (id, failure) => ({ ...job("process", "failed"), job_id: id, failure });
  assert.deepEqual(app.failedBadge([f("a", "open"), f("b", "resolved"), f("c", "open"), job("backup", "running")]),
    { job: f("c", "open"), more: 1 });
  assert.equal(app.failedBadge([f("a", "moot"), job("backup", "done")]), null);
});

test("sessionJobMark", () => {  // D85: a queued or running process outranks an open failure
  const failed = { ...job("process", "failed"), job_id: "f", failure: "open" };
  const running = { ...job("process", "running"), job_id: "r" };
  assert.deepEqual(app.sessionJobMark([failed, running], SID), { kind: "running", job: running });
  assert.deepEqual(app.sessionJobMark([failed], SID), { kind: "failed", job: failed });
  assert.equal(app.sessionJobMark([{ ...failed, failure: "resolved" }], SID), null);
  assert.equal(app.sessionJobMark([failed, running], "other"), null);
  assert.equal(app.sessionJobMark([{ ...job("backup", "running"), subject: { session_id: SID } }], SID), null);
});

test("logLines", () => {  // D80: stage lines stand out; a failed job's last non-empty line is red
  assert.deepEqual(app.logLines(["== probs", "x", "Traceback", ""], true).map((l) => l.cls), ["stage", "", "error", ""]);
  assert.deepEqual(app.logLines(["== probs", "x"], false).map((l) => l.cls), ["stage", ""]);
  assert.deepEqual(app.logLines([], true), []);
});

test("errDesc", () => {  // a thrown null or a body without a descriptor still gives a descriptor
  assert.deepEqual(app.errDesc(null), { code: "legacy_error", params: { detail: "null" } });
  assert.deepEqual(app.errDesc(new Error("x")), { code: "legacy_error", params: { detail: "x" } });
  const desc = { code: "offline", params: {} };
  assert.equal(app.errDesc({ descriptor: desc }), desc);
});

test("splitHidden", () => {  // D91: hidden sessions go under «Hidden: N», order kept
  const s = [{ session_id: "a", hidden: false }, { session_id: "b", hidden: true },
    { session_id: "c" }, { session_id: "d", hidden: true }];
  const { shown, hidden } = app.splitHidden(s);
  assert.deepEqual(shown.map((x) => x.session_id), ["a", "c"]);
  assert.deepEqual(hidden.map((x) => x.session_id), ["b", "d"]);
});

test("jobSubject of a delete", () => {  // D94: the session as for process; gone → the raw id
  const del = { job_id: "d", kind: "delete", state: "done", subject: { session_id: SID } };
  assert.equal(app.jobSubject(del, SESSIONS, "en"), "11 Aug 2026, 21:30 · 4 h 16 min");
  assert.equal(app.jobSubject(del, [], "en"), "11 Aug 2026, 21:30 · 4 h 16 min");  // the trace row of a delete
  assert.equal(app.jobSubject({ ...del, subject: { session_id: "odd" } }, [], "en"), "odd");
});

test("autoOpenTarget skips hidden sessions", () => {  // D91
  const s = (id, state, hidden) => ({ session_id: id, state, hidden });
  assert.equal(app.autoOpenTarget([s("a", "clips"), s("b", "clips", true)], null), "a");
  assert.equal(app.autoOpenTarget([s("a", "imported"), s("b", "clips", true)], null), "a");
  assert.equal(app.autoOpenTarget([s("a", "clips", true)], null), null);
});

test("startValue", () => {  // D94: the start a delete left; what the owner typed (even "") wins
  assert.equal(app.startValue(new Map(), "k", "2025-03-01 09:00"), "2025-03-01 09:00");
  assert.equal(app.startValue(new Map([["k", ""]]), "k", "2025-03-01 09:00"), "");
  assert.equal(app.startValue(new Map([["k", "x"]]), "k", null), "x");
  assert.equal(app.startValue(new Map(), "k", null), "");
});

test("jobAnnouncements", () => {  // D96: a state change is read out; the first poll is silent
  const run = job("backup", "running");
  assert.deepEqual(app.jobAnnouncements(null, [run], SESSIONS, "en"), []);
  assert.deepEqual(app.jobAnnouncements([run], [run], SESSIONS, "en"), []);
  const done = { ...run, state: "done" };
  assert.deepEqual(app.jobAnnouncements([run], [done], SESSIONS, "en"), [app.jobLine(done, SESSIONS, "en")]);
  assert.deepEqual(app.jobAnnouncements([], [run], SESSIONS, "en"), []);  // a new job has its toast
});

test("jobTick", () => {  // D96: «running 5 s» counts by the second for the first minute only
  const now = Date.parse("2026-10-25T10:00:30+03:00");
  const young = [{ state: "running", started: "2026-10-25T10:00:00+03:00" }];
  const old = [{ state: "running", started: "2026-10-25T09:58:00+03:00" }];
  assert.notEqual(app.jobTick(young, now), app.jobTick(young, now + 1000));
  assert.equal(app.jobTick(old, now), app.jobTick(old, now + 1000));
  assert.notEqual(app.jobTick(old, now), app.jobTick(old, now + 60000));
  assert.equal(app.jobTick([], now), app.jobTick([], now + 1000));
});

test("emptySessionsKey", () => {  // all hidden is not «no sessions yet»
  assert.equal(app.emptySessionsKey([]), "no_sessions");
  assert.equal(app.emptySessionsKey([{ hidden: true }]), "all_hidden");
  assert.equal(app.emptySessionsKey([{ hidden: true }, { hidden: false }]), null);
});

test("notSavedTitle", () => {  // D101: the clip by the time its list row shows, and the decision
  const at = { start_local: "2026-08-11T21:42:10", start_s: 750 };
  const bare = { start_local: null, start_s: 750 };
  assert.equal(app.notSavedTitle(at, "музыка", "ru"), "Не сохранено: 21:42:10 — музыка");
  assert.equal(app.notSavedTitle(bare, "музыка", "ru"), "Не сохранено: 12:30 — музыка");
  assert.equal(app.notSavedTitle(bare, app.KEEP, "ru"), "Не сохранено: 12:30 — оставлен");
  assert.equal(app.notSavedTitle(bare, "", "ru"), "Не сохранено: 12:30 — не разобран");
  assert.equal(app.notSavedTitle(bare, "шум", "en"), "Not saved: 12:30 — noise");
  assert.equal(app.notSavedTitle(bare, "", "en"), "Not saved: 12:30 — not reviewed");
});

test("afterDecision", () => {  // D97: called after the optimistic change, the clip already has its new decision
  const decided = (id, v) => clip(id, [v], v);
  const at = (cs, id, filter) => app.afterDecision(cs, cs.find((c) => c.clip_id === id), filter);
  // «отброшены»: kept leaves the filter → «все», same clip, silent; another reason stays in it
  let cs = [decided("a", "моё"), clip("b"), decided("c", "шум")];
  assert.deepEqual(at(cs, "a", "rejected"), { next: "a", filter: "all", play: false });
  cs = [decided("a", "музыка"), clip("b")];
  assert.deepEqual(at(cs, "a", "rejected"), { next: "a", filter: "rejected", play: false });
  // «оставлены»: a reason leaves it, kept again stays
  assert.deepEqual(at(cs, "a", "kept"), { next: "a", filter: "all", play: false });
  cs = [decided("a", "моё"), clip("b")];
  assert.deepEqual(at(cs, "a", "kept"), { next: "a", filter: "kept", play: false });
  // «все» and «не разобраны»: the next undecided plays (D42/D53)
  cs = [decided("a", "шум"), clip("b"), clip("c")];
  assert.deepEqual(at(cs, "a", "all"), { next: "b", filter: "all", play: true });
  assert.deepEqual(at(cs, "a", "undecided"), { next: "b", filter: "undecided", play: true });
  // the last undecided decided: stay on it, silent
  cs = [decided("a", "моё"), decided("b", "шум")];
  assert.deepEqual(at(cs, "b", "all"), { next: "b", filter: "all", play: false });
  assert.deepEqual(at(cs, "b", "undecided"), { next: "b", filter: "undecided", play: false });
});
