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
