// Pure functions of app.js (D48). DOM and Basecoat are checked in a headless browser.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const app = require("../../voxcut/ui/static/app.js");

test("parseSessionName", () => {
  assert.deepEqual(app.parseSessionName("2026-08-11_21-30_4h16m"),
    { date: "11 авг", time: "21:30", length: "4 ч 16 мин" });
  assert.equal(app.parseSessionName("2026-01-02_08-05_45m").length, "45 мин");
  assert.equal(app.parseSessionName("2026-01-02_08-05_45m").date, "2 янв");
  assert.equal(app.parseSessionName("2026-12-31_00-00_5h").length, "5 ч");
  assert.equal(app.parseSessionName("folder01"), null);
  assert.equal(app.parseSessionName("2026-13-01_00-00_5h"), null);
});

test("clockOf", () => {
  assert.equal(app.clockOf("2026-08-11T21:48:42"), "21:48:42");
  assert.equal(app.clockOf(null), "");
});

test("fmtDur", () => {
  assert.equal(app.fmtDur(7.4), "7 с");
  assert.equal(app.fmtDur(111.2), "1 мин 51 с");
  assert.equal(app.fmtDur(60), "1 мин 0 с");
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
  assert.equal(app.gapLabel(273), "пауза 4:33");
});

test("backupTime", () => {  // D51 format for «Последний бэкап»
  const tz = process.env.TZ;
  process.env.TZ = "Europe/Moscow";
  try {
    assert.equal(app.backupTime("2026-10-22T06:53:19.402+03:00"), "22.10 06:53");
    assert.equal(app.backupTime(""), "");
    assert.equal(app.backupTime("мусор"), "мусор");
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
