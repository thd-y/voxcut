// i18n.js (D67): language choice, templates, plural, error descriptors, data labels.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const i18n = require("../../voxcut/ui/static/i18n.js");
const { I18N } = globalThis;

const d = (code, params = {}) => ({ code, params });

// A temporary key in both catalogs for the duration of fn.
function withKey(key, ru, en, fn) {
  I18N.ru[key] = ru;
  I18N.en[key] = en;
  try {
    fn();
  } finally {
    delete I18N.ru[key];
    delete I18N.en[key];
  }
}

test("pickLang", () => {
  assert.equal(i18n.pickLang("ru"), "ru");
  assert.equal(i18n.pickLang("en"), "en");
  for (const bad of ["RU", "ru-RU", "__proto__", "", undefined, null, ["ru"], {}]) {
    assert.equal(i18n.pickLang(bad), "en", JSON.stringify(bad));
  }
  assert.deepEqual(i18n.LANGS, ["en", "ru"]);
});

test("translate", () => {
  withKey("test.hello", "привет, {name}", "hello, {name}", () => {
    assert.equal(i18n.translate("ru", "test.hello", { name: "Аня" }), "привет, Аня");
    assert.equal(i18n.translate("en", "test.hello", { name: "<b>x</b>" }), "hello, <b>x</b>");
    assert.equal(i18n.translate("en", "test.hello", {}), I18N.en["common.unknown"]);  // missing param
    assert.equal(i18n.translate("ru", "test.hello"), I18N.ru["common.unknown"]);
  });
  for (const key of ["no.such.key", "__proto__", "constructor", "toString"]) {
    assert.equal(i18n.translate("ru", key), I18N.ru["common.unknown"], key);
    assert.equal(i18n.translate("en", key), I18N.en["common.unknown"], key);
  }
  assert.notEqual(I18N.ru["common.unknown"], I18N.en["common.unknown"]);
  withKey("test.err", "ошибка: {error}", "error: {error}", () => {
    const desc = d("offline");
    assert.equal(i18n.translate("ru", "test.err", { error: desc }), `ошибка: ${I18N.ru["err.offline"]}`);
    assert.equal(i18n.translate("en", "test.err", { error: desc }), `error: ${I18N.en["err.offline"]}`);
    assert.notEqual(I18N.ru["err.offline"], I18N.en["err.offline"]);
  });
});

test("translate plural", () => {
  withKey("test.files", { one: "{n} one", few: "{n} few", many: "{n} many", other: "{n} other" },
    { one: "{n} one", other: "{n} other" }, () => {
      const ru = [[0, "many"], [1, "one"], [2, "few"], [5, "many"], [11, "many"], [21, "one"],
        [22, "few"], [25, "many"], [111, "many"]];
      for (const [n, form] of ru) assert.equal(i18n.translate("ru", "test.files", { n }), `${n} ${form}`);
      for (const [n, form] of [[0, "other"], [1, "one"], [2, "other"]]) {
        assert.equal(i18n.translate("en", "test.files", { n }), `${n} ${form}`);
      }
      assert.equal(i18n.translate("en", "test.files", {}), I18N.en["common.unknown"]);  // no n
      assert.equal(i18n.translate("en", "test.files", null), I18N.en["common.unknown"]);  // never throws
    });
});

test("errText", () => {
  assert.equal(i18n.errText("ru", d("http_need_key")), "нужен ключ");
  assert.equal(i18n.errText("ru", d("session_foreign_names", { dir: "/in", files: ["a.WAV", "b.WAV"] })),
    "/in: имена не по правилу диктофона YYYYMMDDHHMMSS.wav: a.WAV, b.WAV");
  assert.equal(i18n.errText("ru", d("session_chain_range", { n: 3, total: 2, lines: ["  1: a", "  2: b"] })),
    "--chain 3 вне диапазона 1..2:\n  1: a\n  2: b");
  assert.equal(i18n.errText("ru", d("job_after_done_failed", { cause: d("restore_journal_broken", { detail: "x" }) })),
    "после завершения: журнал архива не читается (x); файлы восстановлены, журнал не слит, повтор не поможет");
  assert.equal(i18n.errText("ru", d("legacy_error", { detail: "<b>старый</b>" })), "<b>старый</b>");  // text, S7
  for (const lang of ["ru", "en"]) {
    const unknown = I18N[lang]["err.unknown"];
    for (const bad of [d("no_such_code"), d("__proto__"), d("constructor"), d("toString"), null, "строка",
      {}, d("legacy_error", null)]) {
      assert.equal(i18n.errText(lang, bad), unknown, JSON.stringify(bad));
    }
    assert.equal(i18n.errText(lang, d("legacy_error", {})), unknown);  // a param the template needs is missing
  }
  assert.notEqual(I18N.ru["err.unknown"], I18N.en["err.unknown"]);
  assert.equal(i18n.errText("en", d("http_status", { status: 502 })), "HTTP 502");
});

test("label", () => {
  withKey("mark.шум", "шум", "noise", () => {
    assert.equal(i18n.label("en", "mark.", "шум"), "noise");
    assert.equal(i18n.label("ru", "mark.", "шум"), "шум");
  });
  for (const value of ["__proto__", "смех", "toString", "constructor"]) {
    assert.equal(i18n.label("en", "mark.", value), value);
    assert.equal(i18n.label("en", "", value), value);
  }
});

// D67: the two catalogs say the same things: same keys, same {params}, the plural forms each language needs.
const EN_CYRILLIC_OK = [];  // en values that are data samples (none yet)
const FORMS = { ru: ["few", "many", "one", "other"], en: ["one", "other"] };
const fields = (s) => [...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();

test("catalogs agree", () => {
  assert.deepEqual(Object.keys(I18N.en).sort(), Object.keys(I18N.ru).sort());
  for (const key of Object.keys(I18N.ru)) {
    const ru = I18N.ru[key];
    const en = I18N.en[key];
    if (typeof ru === "object") {
      assert.deepEqual(Object.keys(ru).sort(), FORMS.ru, key);
      assert.equal(typeof en, "object", key);
      assert.deepEqual(Object.keys(en).sort(), FORMS.en, key);
      const want = fields(ru.other);
      for (const form of FORMS.ru) assert.deepEqual(fields(ru[form]), want, `${key}.${form}`);
      for (const form of FORMS.en) assert.deepEqual(fields(en[form]), want, `${key}.${form}`);
    } else {
      assert.equal(typeof en, "string", key);
      assert.deepEqual(fields(en), fields(ru), key);
    }
    const texts = typeof en === "object" ? Object.values(en) : [en];
    if (!EN_CYRILLIC_OK.includes(key)) {
      for (const t of texts) assert.doesNotMatch(t, /[а-яёА-ЯЁ]/, key);
    }
  }
});
