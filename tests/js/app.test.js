// Pure functions of app.js (D48). DOM and Basecoat are checked in a headless browser.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const app = require("../../voxcut/ui/static/app.js");

test("parseSessionName", () => {  // D68: the date through Intl, units from the catalog (L7)
  assert.deepEqual(app.parseSessionName("2026-08-11_21-30_4h16m", "ru"),
    { date: "11 авг.", time: "21:30", length: "4 ч 16 мин" });
  assert.deepEqual(app.parseSessionName("2026-08-11_21-30_4h16m", "en"),
    { date: "Aug 11", time: "21:30", length: "4 h 16 min" });
  assert.equal(app.parseSessionName("2026-01-02_08-05_45m", "ru").length, "45 мин");
  assert.equal(app.parseSessionName("2026-01-02_08-05_45m", "en").length, "45 min");
  assert.equal(app.parseSessionName("2026-01-02_08-05_45m", "ru").date, "2 янв.");
  assert.equal(app.parseSessionName("2026-12-31_00-00_5h", "ru").length, "5 ч");
  assert.equal(app.parseSessionName("2026-12-31_00-00_5h", "en").length, "5 h");
  for (const lang of ["ru", "en"]) {
    assert.equal(app.parseSessionName("folder01", lang), null);
    assert.equal(app.parseSessionName("2026-13-01_00-00_5h", lang), null);
    assert.equal(app.parseSessionName("2026-00-01_00-00_5h", lang), null);
  }
  assert.equal(app.sessionLabel({ name: "2026-08-11_21-30_4h16m" }, "ru"), "11 авг., 21:30 · 4 ч 16 мин");
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
    assert.equal(app.ideaTime("2026-08-11T04:25:00+03:00", "en"), "08/11, 04:25 AM");
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
  assert.deepEqual(app.afterOpenError(false, app.codedError(d("http_need_key"), "ru")),
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
    [clip("a", ["вокал", "моё"]), "разные решения: вокал, моё", "different decisions: vocals, mine"],
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
