// English UI strings (D67): the source language. Keys and {params} match ru.js
// (tests/js/i18n.test.js); err.<code> cover the registry of voxcut/errors.py (test_errors.py).
"use strict";
(globalThis.I18N ??= {}).en = {
  "common.unknown": "(no text)",
  // frontend only: no server code
  "err.unknown": "unknown error",
  "err.offline": "no connection to VoxCut",
  "err.not_saved": "not saved",
  "err.http_status": "HTTP {status}",
};
if (typeof module === "object") module.exports = globalThis.I18N.en;
