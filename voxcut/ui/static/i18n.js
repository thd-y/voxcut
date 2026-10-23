// UI language (D67, D69): catalogs I18N.en / I18N.ru are loaded before this file; the language
// lives in the page's memory only. Own keys only: a key or code from data never reaches the prototype.
"use strict";
(() => {
  const LANGS = ["en", "ru"];
  const pickLang = (value) => (typeof value === "string" && LANGS.includes(value) ? value : "en");
  const catalog = (lang) => globalThis.I18N[pickLang(lang)];
  const own = (cat, key) => (typeof key === "string" && Object.hasOwn(cat, key) ? cat[key] : undefined);

  // {name} → text; an array joins with ", " ("lines" with "\n"), a descriptor renders as an error.
  // null if a parameter is missing: the caller shows its neutral phrase instead.
  function fill(lang, template, params) {
    let missing = false;
    const text = template.replace(/\{(\w+)\}/g, (_, name) => {
      if (!Object.hasOwn(params, name)) {
        missing = true;
        return "";
      }
      const v = params[name];
      if (Array.isArray(v)) return v.join(name === "lines" ? "\n" : ", ");
      return v && typeof v === "object" ? errText(lang, v) : String(v);
    });
    return missing ? null : text;
  }

  function translate(lang, key, params = {}) {
    const cat = catalog(lang);
    const unknown = cat["common.unknown"];
    if (!params || typeof params !== "object") return unknown;
    let value = own(cat, key);
    if (value && typeof value === "object") {  // plural: the form by params.n
      value = typeof params.n === "number"
        ? own(value, new Intl.PluralRules(pickLang(lang)).select(params.n)) : undefined;
    }
    if (typeof value !== "string") return unknown;
    return fill(lang, value, params) ?? unknown;
  }

  // D66: a server error is {code, params}; anything unknown gets one neutral phrase.
  function errText(lang, desc) {
    const cat = catalog(lang);
    const unknown = cat["err.unknown"];
    const template = desc && typeof desc === "object" && typeof desc.code === "string"
      ? own(cat, `err.${desc.code}`) : undefined;
    const params = desc && desc.params;
    if (typeof template !== "string" || !params || typeof params !== "object") return unknown;
    return fill(lang, template, params) ?? unknown;
  }

  // A value from the server or the journal (U5): its label if the catalog has one, else as is.
  const label = (lang, prefix, value) => {
    const cat = catalog(lang);
    return Object.hasOwn(cat, prefix + value) ? cat[prefix + value] : value;
  };

  const i18n = { LANGS, pickLang, translate, errText, label };
  globalThis.i18n = i18n;
  if (typeof module === "object") {
    require("./i18n/en.js");
    require("./i18n/ru.js");
    module.exports = i18n;
  }
})();
