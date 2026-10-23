// VoxCut screen (redesign R1–R10): review, my clips, library. Vanilla JS, no build step.
// Security (S2, S3, S7): the key lives only in this module, all data goes out via textContent.
"use strict";
const { translate, errText } = typeof module === "object" ? require("./i18n.js") : globalThis.i18n;

// --- pure functions (no DOM; tested by node --test, D48) ---

function parseSessionName(name, lang) {
  const m = /^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})_(?:(\d+)h)?(?:(\d+)m)?$/.exec(name);
  if (!m || (!m[6] && !m[7]) || +m[2] < 1 || +m[2] > 12) return null;
  const [h, min] = [+m[6], +m[7]];
  const length = m[6] && m[7] ? translate(lang, "dur.hm", { h, m: min })
    : m[6] ? translate(lang, "dur.h", { h }) : translate(lang, "dur.m", { m: min });
  const date = new Intl.DateTimeFormat(lang, { day: "numeric", month: "short" })
    .format(new Date(+m[1], +m[2] - 1, +m[3]));  // D68
  return { date, time: `${m[4]}:${m[5]}`, length };
}

const clockOf = (startLocal) => (startLocal ? startLocal.slice(11, 19) : "");

function fmtDur(s, lang) {
  const t = Math.round(s);
  return t < 60 ? translate(lang, "dur.s", { s: t })
    : translate(lang, "dur.ms", { m: Math.floor(t / 60), s: t % 60 });
}

function fmtPos(s) {
  const t = Math.floor(s);
  const mm = Math.floor((t % 3600) / 60);
  const ss = String(t % 60).padStart(2, "0");
  return t >= 3600 ? `${Math.floor(t / 3600)}:${String(mm).padStart(2, "0")}:${ss}` : `${mm}:${ss}`;
}

const KEEP = "моё";
const REASONS = ["вокал", "музыка", "шум", "другое"];

// R1 + D47: an own decision on this clip's span wins; otherwise several values are a conflict.
function status(clip) {
  const own = clip.own_mark || "";
  const value = own || (clip.marks.length === 1 ? clip.marks[0] : "");
  if (!value) {
    return { kind: clip.marks.length ? "conflict" : "undecided", value: "", others: false };
  }
  return { kind: value === KEEP ? "kept" : "rejected", value,
    others: clip.marks.some((m) => m !== value) };
}

const isDecided = (clip) => ["kept", "rejected"].includes(status(clip).kind);

function counts(clips) {
  const c = { all: clips.length, undecided: 0, kept: 0, rejected: 0,
    reasons: Object.fromEntries(REASONS.map((r) => [r, 0])) };
  for (const clip of clips) {
    const s = status(clip);
    if (s.kind === "kept") c.kept++;
    else if (s.kind === "rejected") {
      c.rejected++;
      c.reasons[REASONS.includes(s.value) ? s.value : "другое"]++;
    } else c.undecided++;
  }
  return c;
}

function nextUndecided(clips, from) {
  const after = clips.findIndex((c, i) => i > from && !isDecided(c));
  return after >= 0 ? after : clips.findIndex((c) => !isDecided(c));
}

const norm = (text) => text.trim().toLocaleLowerCase("ru").replaceAll("ё", "е");

function visibleClips(clips, { tab, filter, query }) {
  if (tab === "mine") {
    const q = norm(query || "");
    return clips.filter((c) => status(c).kind === "kept"
      && (filter !== "noted" || c.notes.length > 0)
      && (!q || c.notes.some((n) => norm(n.value).includes(q))));
  }
  if (filter === "all") return clips;
  return clips.filter((c) => {
    const k = status(c).kind;
    return filter === "undecided" ? !isDecided(c) : k === filter;
  });
}

function gapBefore(clips, i, min = 30) {
  if (i <= 0) return null;
  const gap = clips[i].start_s - clips[i - 1].end_s;
  return gap >= min ? gap : null;
}

// Local time through Intl of the UI language (D68): ru «22.10, 04:25» (D51), with the year
// «22.10.2026, 04:25» (D61); "" if unparsable.
function localTime(ts, withYear, lang) {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "";
  return new Intl.DateTimeFormat(lang, { day: "2-digit", month: "2-digit",
    ...(withYear ? { year: "numeric" } : {}), hour: "2-digit", minute: "2-digit" }).format(d);
}

const ideaTime = (ts, lang) => localTime(ts, false, lang);

const backupTime = (value, lang) => localTime(value, true, lang) || value;  // D61; anything unparsable as is

const gapLabel = (seconds, lang) => translate(lang, "card.gap", { t: fmtPos(seconds) });  // D54

// D66: the state keeps the descriptor and renders it in the current language (D67).
function codedError(desc, lang) {
  const e = new Error(errText(lang, desc));
  e.descriptor = desc;
  return e;
}

// Any caught error as a descriptor; a JS error without one shows its text as is.
const errDesc = (e) => e.descriptor || { code: "legacy_error", params: { detail: String(e.message) } };

const undoEntry = (clip) => ({ clip_id: clip.clip_id, prev: clip.own_mark || "" });

function undoTarget(stack, clips) {
  const entry = stack.pop();
  if (!entry) return null;
  return { entry, index: clips.findIndex((c) => c.clip_id === entry.clip_id) };
}

const DECISIONS = [KEEP, ...REASONS];  // keys 1..5 (R2)
const DECISION_ICONS = { "моё": "check", "вокал": "mic-vocal", "музыка": "music", "шум": "audio-waveform", "другое": "ellipsis" };  // D55

// R10: physical keys (KeyboardEvent.code), the same in Russian and English layouts.
function keyAction(ev, { inField, dialogOpen, screen, rechoose = null }) {
  const { code } = ev;
  if (inField) {
    if (code === "Escape") return "escape";
    return dialogOpen && ev.ctrlKey && code === "Enter" ? "save-idea" : null;
  }
  const digit = /^(?:Digit|Numpad)([1-5])$/.exec(code);
  const plain = !ev.ctrlKey && !ev.altKey && !ev.metaKey;
  if (rechoose) {
    // D52: menu, then confirmation. Other plain keys are swallowed ("noop"), so Space or Enter
    // never presses a focused «Да»; a held key never picks or confirms. Tab still moves focus.
    if (code === "Escape") return "cancel";
    if (!plain || code === "Tab") return null;
    if (ev.repeat) return "noop";
    if (rechoose === "menu") return digit ? `pick:${DECISIONS[digit[1] - 1]}` : "noop";
    if (code === "Enter") return "confirm";
    if (digit) return DECISIONS[digit[1] - 1] === rechoose ? "confirm" : "cancel";
    return "noop";
  }
  if (dialogOpen) return code === "Escape" ? "escape" : null;
  if (ev.ctrlKey || ev.altKey || ev.metaKey) {
    return ev.ctrlKey && !ev.altKey && !ev.metaKey && !ev.shiftKey && code === "KeyZ" ? "undo" : null;
  }
  const work = screen !== "library";
  if (digit) return screen === "review" && !ev.repeat ? `mark:${DECISIONS[digit[1] - 1]}` : null;
  if (code === "ArrowLeft" || code === "ArrowRight") {
    if (!work) return null;
    if (ev.shiftKey) return code === "ArrowLeft" ? "seek:-5" : "seek:5";
    return code === "ArrowLeft" ? "prev" : "next";
  }
  if (code === "Slash") return ev.shiftKey ? "help" : (screen === "mine" ? "search" : null);
  const simple = { Space: work && !ev.repeat && "play", KeyN: work && "note", KeyM: work && "tab",
    KeyR: screen === "mine" && "rechoose",
    KeyI: "idea", KeyG: "library", Escape: "escape" };
  return (Object.hasOwn(simple, code) && simple[code]) || null;
}


// --- screen ---

let KEY = null;
let serverState = null;    // GET /api/state
let view = null;           // GET /api/session/{sid}
let sid = null;            // open session
let autoTried = null;      // session id the automatic open already tried
let retryOpen = null;      // [id, run] of the newest open lost with the link and nothing on screen
let runId = null;          // explicit run, null = newest
let openSeq = 0;           // only the newest session response is applied
let queue = Promise.resolve();  // journal writes in key-press order
let audioCtl = null;       // AbortController of the clip fetch in flight
let audioUrl = null;       // object URL of the clip in the player
let playingId = null;      // clip_id whose audio is in the player
let writes = 0;            // journal writes started; a reload sent before the latest one is stale
let listSig = "";
let scrolledId = null;
const shownWarnings = new Set();
const ui = {
  screen: "session", tab: "review", filter: "all", mineFilter: "mine", query: "", inOrder: false,
  selectedId: null, undo: [], pending: new Set(), drafts: new Map(), hideSummary: false, theme: null, fatal: false,
  lang: "en",  // D69: page memory only, every launch starts in English
  loaded: false, openError: null, audio: "idle", audioError: "", playWhenReady: false,
  noteClipId: null, noteSaving: false, expanded: false,
  rechoose: null,  // D52: null | "menu" | the decision awaiting confirmation
  ideaDelete: null, ideaDeleting: false,  // D56: the idea asked about, a delete in flight
};
const GAP_MIN_S = 120;  // one recording: 131 gaps >= 30 s (noise), 12 >= 120 s (real breaks)
const NAMES = { process: "обработка", import: "импорт", backup: "бэкап", restore: "восстановление" };
const JOB_STATES = { queued: "в очереди", running: "идёт", done: "готово", failed: "ошибка",
  cancelled: "отменена" };
const STATES = { imported: "импортирована", assembled: "собрана", probs: "вероятности посчитаны",
  segmented: "размечена", clips: "клипы готовы", incomplete: "неполная" };
const $ = (id) => document.getElementById(id);
const newId = () => crypto.randomUUID().replaceAll("-", "");
let darkQuery = null;       // matchMedia, set in start(): the pure part also loads in node

function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = String(text);
  if (cls) e.className = cls;
  return e;
}

// D55: Lucide icons come from <template id="icon-…"> in index.html (JS cannot create SVG here).
const icon = (name) => $(`icon-${name}`).content.firstElementChild.cloneNode(true);

function button(text, onClick, variant, size = "sm", iconName = null) {
  const b = el("button", text, "btn");
  if (iconName) b.prepend(icon(iconName));
  b.type = "button";
  if (variant) b.dataset.variant = variant;
  if (size) b.dataset.size = size;
  b.addEventListener("click", onClick);
  return b;
}

function toast(title, text, error) {
  const t = el("div", null, "toast");
  t.setAttribute("role", error ? "alert" : "status");
  t.setAttribute("aria-hidden", "false");
  if (error) t.dataset.category = "error";
  const content = el("div", null, "toast-content");
  const sec = el("section");
  sec.append(el("h2", title));
  if (text) sec.append(el("p", text));
  content.append(sec);
  t.append(content);
  $("toaster").append(t);
  setTimeout(() => {
    t.setAttribute("aria-hidden", "true");
    setTimeout(() => t.remove(), 400);
  }, error ? 6000 : 2500);
}

async function api(method, path, body) {
  const opts = { method, headers: { Authorization: `Bearer ${KEY}` } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let r;
  try {
    r = await fetch(path, opts);
  } catch {
    const e = codedError({ code: "offline", params: {} }, ui.lang);
    e.offline = true;
    throw e;
  }
  const data = await r.json().catch(() => ({}));
  if (r.status === 401 || r.status === 403) {  // the key is gone (server restarted): reopen the window
    ui.fatal = true;
    render();
  }
  if (!r.ok) throw codedError(data.error || { code: "http_status", params: { status: r.status } }, ui.lang);
  return data;
}

function applyTheme() {
  const dark = ui.theme ? ui.theme === "dark" : !!(darkQuery && darkQuery.matches);
  document.documentElement.classList.toggle("dark", dark);
  $("theme").setAttribute("aria-pressed", String(dark));
  if (view) drawTimeline();
}

// D62: canonical launch window, shrunk to 90% of a small screen, centred on its work area.
const WINDOW_W = 1280, WINDOW_H = 800, WINDOW_MAX = 0.9;
function launchRect(avail) {
  const width = Math.min(WINDOW_W, Math.round(avail.width * WINDOW_MAX));
  const height = Math.min(WINDOW_H, Math.round(avail.height * WINDOW_MAX));
  return { width, height, left: avail.left + Math.round((avail.width - width) / 2),
    top: avail.top + Math.round((avail.height - height) / 2) };
}

// The session opened on its own: the newest processed one, else the first; once per session id,
// so a session that fails to load is not re-requested on every 2 s poll.
function autoOpenTarget(sessions, tried) {
  if (!sessions.length) return null;
  const ready = sessions.filter((s) => s.state === "clips");
  const id = (ready[ready.length - 1] || sessions[0]).session_id;
  return id === tried ? null : id;
}

// A failed session open: the open session stays; without one, a server error explains itself
// on an empty screen, while a lost link leaves the skeleton and the «нет связи» alert.
function afterOpenError(hasView, e) {
  if (hasView) return { loaded: true, openError: null };
  return { loaded: null, openError: e.offline ? null : errDesc(e) };
}

// Any open lost with the link, with nothing on screen and no newer open, retries on the next good poll.
const retryAfter = (e, hasView) => !!e.offline && !e.stale && !hasView;

function fitWindow() {  // once, on a fresh launch code; Edge ignores --window-size when already running
  try {
    const r = launchRect({ width: screen.availWidth, height: screen.availHeight,
      left: screen.availLeft, top: screen.availTop });
    resizeTo(r.width, r.height);
    moveTo(r.left, r.top);
  } catch {}  // a window Edge will not move is not an error
}

async function start() {
  darkQuery = matchMedia("(prefers-color-scheme: dark)");
  applyTheme();
  darkQuery.addEventListener("change", applyTheme);
  const code = location.hash.slice(1);
  history.replaceState(null, "", "/");
  render();
  try {
    const r = await fetch("/api/exchange", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
    if (!r.ok) throw new Error();
    KEY = (await r.json()).key;
    fitWindow();
  } catch {
    ui.fatal = true;
    render();
    return;
  }
  bind();
  await poll();
  setInterval(poll, 2000);
}

// --- server state ---

let polling = false;
async function poll() {
  if (polling || ui.fatal) return;  // a slow response must not stack polls (each would restart openSession)
  polling = true;
  try {
    await pollOnce();
  } finally {
    polling = false;  // only after the session load too: a 2 s+ GET must not be restarted by the next poll
  }
}

async function pollOnce() {
  try {
    serverState = await api("GET", "/api/state");
    $("conn-alert").hidden = true;
  } catch (e) {
    if (e.offline) $("conn-alert").hidden = false;
    else if (!ui.fatal) toast("Ошибка", e.message, true);
    return;
  }
  for (const w of serverState.journal_warnings) {
    const sig = JSON.stringify(w);
    if (!shownWarnings.has(sig)) {
      shownWarnings.add(sig);
      toast("Журнал", errText(ui.lang, w), true);
    }
  }
  const sessions = serverState.sessions;
  const auto = sid === null ? autoOpenTarget(sessions, autoTried) : null;
  if (sid === null && retryOpen) {
    const [id, run] = retryOpen;
    retryOpen = null;
    await openSession(id, run).catch(fail);
  } else if (auto) {
    autoTried = auto;
    await openSession(auto).catch(fail);
  } else if (sid !== null && view && !runId) {
    const row = sessions.find((s) => s.session_id === sid);
    if (row && row.n_clips !== null && row.n_clips !== view.clips.length) await reloadView();
  }
  render();
}

function fail(e) {
  if (e.stale) return;  // a newer open replaced this one: its outcome is what counts
  if (e.offline) $("conn-alert").hidden = false;
  else if (!ui.fatal) toast("Ошибка", e.message, true);
}

const sessionPath = (id, run) =>
  `/api/session/${encodeURIComponent(id)}${run ? `?run=${encodeURIComponent(run)}` : ""}`;

function sessionRow(id) {
  return serverState && serverState.sessions.find((s) => s.session_id === id);
}

function sessionLabel(row, lang) {
  const p = parseSessionName(row.name, lang);
  return p ? translate(lang, "top.session", p) : row.name;
}

async function openSession(id, run) {
  const seq = ++openSeq;
  if (id !== sid) {
    ui.loaded = false;
    render();
  }
  let v;
  try {
    v = await api("GET", sessionPath(id, run));
  } catch (e) {
    e.stale = seq !== openSeq;
    if (!e.stale) retryOpen = retryAfter(e, !!view) ? [id, run] : null;
    if (!e.stale) {
      const r = afterOpenError(!!view, e);
      if (r.loaded !== null) ui.loaded = r.loaded;
      ui.openError = r.openError;
      render();
    }
    throw e;
  }
  if (seq !== openSeq) return;
  const changed = id !== sid || (run || null) !== runId;
  sid = id;
  runId = run || null;
  view = v;
  ui.loaded = true;
  ui.openError = null;
  retryOpen = null;
  if (changed) {
    closeNote();
    stopAudio();
    ui.rechoose = null;
    ui.undo = [];
    ui.hideSummary = false;
    ui.selectedId = null;
    ui.filter = "all";
    ui.query = "";
    $("search").value = "";
    const c = counts(view.clips);
    ui.tab = c.all && !c.undecided ? "mine" : "review";
  }
  keepSelection();
  render();
}

// After a write: re-read the session, unless another write is still in flight.
async function reloadView() {
  if (!sid || ui.pending.size) return;
  const seq = openSeq;  // not bumped: a reload must never cancel a newer openSession
  const w = writes;
  const v = await api("GET", sessionPath(sid, runId));
  // a write started after this GET was sent: the view is older than the screen; that write reloads again
  if (seq !== openSeq || w !== writes || ui.pending.size || !ui.loaded) return;
  view = v;
  keepSelection();
  render();
}

function keepSelection() {
  if (ui.selectedId && !view.clips.some((c) => c.clip_id === ui.selectedId)) {
    closeNote();
    ui.rechoose = null;  // a question about a clip that is gone must not apply to the next one
    ui.selectedId = null;  // the clip is not in this run
    stopAudio();
  }
  if (ui.selectedId) return;
  if (ui.tab === "review") {
    const i = nextUndecided(view.clips, -1);
    ui.selectedId = i >= 0 ? view.clips[i].clip_id : null;
  } else {
    const list = shown();
    ui.selectedId = list.length ? list[0].clip_id : null;
  }
  if (ui.selectedId) loadAudio(selected(), false);
}

const shown = () => (view ? visibleClips(view.clips, { tab: ui.tab,
  filter: ui.tab === "mine" ? ui.mineFilter : ui.filter, query: ui.query }) : []);
const selected = () => (view && view.clips.find((c) => c.clip_id === ui.selectedId)) || null;
const ownNote = (clip) => clip.notes.find((n) => n.own) || null;

// --- render ---

function render() {
  $("fatal").hidden = !ui.fatal;
  if (ui.fatal) {
    $("fatal-title").textContent = "Откройте окно заново: VoxCut.cmd или make ui";
    $("fatal-text").textContent = "Код запуска недействителен или устарел.";
    for (const id of ["top", "screen-session", "screen-library"]) $(id).hidden = true;
    return;
  }
  renderHeader();
  const lib = ui.screen === "library";
  $("screen-library").hidden = !lib;
  $("screen-session").hidden = lib;
  if (lib) renderLibrary();
  else renderSession();
}

function renderHeader() {
  const row = sid && sessionRow(sid);
  $("session-label").textContent = row ? sessionLabel(row, ui.lang) : "";
  for (const b of $("tabs").querySelectorAll("[role=tab]")) {
    b.setAttribute("aria-selected", String(ui.screen === "session" && b.dataset.tab === ui.tab));
  }
  $("work").setAttribute("aria-labelledby", `tab-${ui.tab}`);
  const c = view && ui.loaded ? counts(view.clips) : null;
  $("progress-label").textContent = c && c.all ? `разобрано ${c.all - c.undecided} из ${c.all}` : "";
  const badge = $("job-badge");
  const jobs = serverState ? serverState.jobs : [];
  const active = jobs.find((j) => j.state === "queued" || j.state === "running");
  const last = jobs[jobs.length - 1];
  badge.hidden = !active && !(last && last.state === "failed");
  if (active) {
    badge.dataset.variant = "secondary";
    badge.textContent = `${NAMES[active.kind] || active.kind}: ${active.stage || JOB_STATES[active.state]}`;
  } else if (!badge.hidden) {
    badge.dataset.variant = "destructive";
    badge.textContent = `ошибка: ${NAMES[last.kind] || last.kind}`;
  }
  $("library-btn").setAttribute("aria-pressed", String(ui.screen === "library"));
}

function showSessionEmpty(title, text, action) {
  $("work").hidden = true;
  $("timeline").hidden = true;
  $("session-empty").hidden = false;
  $("session-empty-title").textContent = title;
  $("session-empty-text").textContent = text;
  $("session-empty-action").hidden = !action;
}

function renderSession() {
  $("session-empty").hidden = true;
  if (serverState && !serverState.sessions.length) {
    showSessionEmpty("Сессий пока нет", "Положите записи в папку inbox и импортируйте их в Библиотеке.", true);
    return;
  }
  if (!view && ui.openError) {
    showSessionEmpty("Сессия не открылась", `${errText(ui.lang, ui.openError)}. Откройте другую сессию в Библиотеке.`, true);
    return;
  }
  if (!serverState || !ui.loaded || !view) {
    renderSkeleton();
    return;
  }
  if (!view.clips.length) {
    const row = sessionRow(sid);
    const active = serverState.jobs.find((j) => j.state === "queued" || j.state === "running");
    showSessionEmpty("Клипов пока нет", active
      ? `Идёт ${NAMES[active.kind] || active.kind}: ${active.stage || active.state}.`
      : `Сессия ${row ? `«${STATES[row.state] || row.state}»` : ""}: нажмите «Обработать» в Библиотеке.`, !active);
    return;
  }
  $("work").hidden = false;
  $("timeline").hidden = false;
  renderFilters();
  renderList();
  renderCard();
  drawTimeline();
}

function renderSkeleton() {
  $("work").hidden = false;
  $("timeline").hidden = false;
  $("filters").replaceChildren();
  listSig = "";
  $("clip-list").replaceChildren(...Array.from({ length: 8 }, () => el("div", null, "skeleton skeleton-row")));
  $("clip-title").replaceChildren(el("div", null, "skeleton skeleton-title"));
  setKids($("clip-sub"), "", () => []);
  $("clip-saving").hidden = true;
  $("summary").hidden = true;
  $("card-top").hidden = false;
  $("card-body").hidden = true;
  setDecisionsEnabled(false);
}

function setKids(node, sig, build) {
  if (node.dataset.sig === sig) return;
  node.dataset.sig = sig;
  node.replaceChildren(...build());
}

function chip(text, active, onClick) {
  const b = el("button", text, "badge");
  b.type = "button";
  if (!active) b.dataset.variant = "outline";
  b.setAttribute("aria-pressed", String(active));
  b.addEventListener("click", onClick);
  return b;
}

function renderFilters() {
  const c = counts(view.clips);
  const mine = ui.tab === "mine";
  $("mine-tools").hidden = !mine;
  const set = (key, value) => () => {
    ui[key] = value;
    render();
  };
  if (mine) {
    const kept = view.clips.filter((x) => status(x).kind === "kept");
    const noted = kept.filter((x) => x.notes.length).length;
    setKids($("filters"), JSON.stringify(["mine", kept.length, noted, ui.mineFilter]), () => [
      chip(`мои ${kept.length}`, ui.mineFilter === "mine", set("mineFilter", "mine")),
      chip(`с заметкой ${noted}`, ui.mineFilter === "noted", set("mineFilter", "noted"))]);
  } else {
    const f = [["all", "все", c.all], ["undecided", "не разобраны", c.undecided],
      ["kept", "оставлены", c.kept], ["rejected", "отброшены", c.rejected]];
    setKids($("filters"), JSON.stringify(["review", f, ui.filter]), () => f.map(([k, label, n]) =>
      chip(`${label} ${n}`, ui.filter === k, set("filter", k))));
  }
}

function statusBadge(clip) {
  const s = status(clip);
  const b = el("span", null, "badge");
  if (s.kind === "kept") {
    b.dataset.state = "kept";
    b.textContent = "оставлен";
  } else if (s.kind === "rejected") {
    b.dataset.variant = "secondary";
    b.textContent = s.value;
  } else if (s.kind === "conflict") {
    b.dataset.variant = "outline";
    b.classList.add("conflict");
    b.textContent = "разные решения";
  } else {
    b.dataset.variant = "outline";
    b.textContent = "—";
  }
  if (s.others) b.append(" · есть другие");
  if (ui.pending.has(clip.clip_id)) b.append(" ", el("span", null, "spinner"));
  return b;
}

function statusText(clip) {
  const s = status(clip);
  if (s.kind === "kept") return s.others ? "оставлен · есть другие решения" : "оставлен";
  if (s.kind === "rejected") return `отброшен: ${s.value}${s.others ? " · есть другие решения" : ""}`;
  if (s.kind === "conflict") return `разные решения: ${clip.marks.join(", ")}`;
  return "не разобран";
}

const firstLine = (clip) => {
  const n = ownNote(clip) || clip.notes[0];
  return n ? n.value.split("\n")[0] : "";
};

function renderList() {
  const list = shown();
  const sig = JSON.stringify([ui.selectedId, [...ui.pending], list.map((c) =>
    [c.clip_id, c.own_mark, c.marks, c.heard, firstLine(c)])]);
  if (sig === listSig) return;  // polls must not rebuild the list (scroll, hover)
  listSig = sig;
  if (!list.length) {
    const box = el("div", null, "list-empty");
    box.append(el("p", "Ничего не найдено"), button("Сбросить фильтр", () => {
      ui.filter = "all";
      ui.mineFilter = "mine";
      ui.query = "";
      $("search").value = "";
      render();
    }, "outline"));
    $("clip-list").replaceChildren(box);
    return;
  }
  const rows = [];
  list.forEach((c, i) => {
    const gap = gapBefore(list, i, GAP_MIN_S);
    if (gap !== null) rows.push(el("p", gapLabel(gap, ui.lang), "gap"));
    const item = el("article", null, `item${c.clip_id === ui.selectedId ? " selected" : ""}`);
    if (c.clip_id === ui.selectedId) item.setAttribute("aria-current", "true");
    item.dataset.size = "xs";
    item.setAttribute("role", "listitem");
    item.dataset.clip = c.clip_id;
    const sec = el("section");
    sec.append(el("h4", `${clockOf(c.start_local) || fmtPos(c.start_s)} · ${fmtDur(c.duration_s, ui.lang)}`));
    const line = firstLine(c);
    if (line) sec.append(el("p", line, "snippet muted"));
    const aside = el("aside");
    if (c.heard) {
      const dot = el("span", null, "dot");
      dot.title = "слушал";
      aside.append(dot);
    }
    // in my clips every row is kept: only extra states earn a badge there
    if (ui.tab !== "mine" || status(c).others || ui.pending.has(c.clip_id)) aside.append(statusBadge(c));
    item.append(sec, aside);
    item.addEventListener("click", () => select(c.clip_id, { play: true }));  // D53
    rows.push(item);
  });
  $("clip-list").replaceChildren(...rows);
  if (scrolledId !== ui.selectedId) {
    scrolledId = ui.selectedId;
    const sel = $("clip-list").querySelector(".item.selected");
    if (sel) sel.scrollIntoView({ block: "center" });  // D59: past and next clips both in view
  }
}

// D52: on «Мои клипы» the 1-5 panel shows only inside the re-choice menu.
function renderRechoose() {
  const mine = ui.tab === "mine";
  $("decisions").hidden = mine && ui.rechoose !== "menu";
  $("rechoose").hidden = !mine || !selected();
  $("rechoose-btn").hidden = ui.rechoose !== null;
  $("rechoose-hint").hidden = ui.rechoose !== "menu";
  const asking = mine && ui.rechoose !== null && ui.rechoose !== "menu";
  $("rechoose-confirm").hidden = !asking;
  if (asking) $("rechoose-question").textContent = `Отбросить как «${ui.rechoose}»?`;
}

function setDecisionsEnabled(on) {
  for (const b of $("decisions").children) b.disabled = !on;
  $("play-btn").disabled = !on;
  $("note-btn").disabled = !on;
}

function summaryShown() {
  if (!view || ui.screen !== "session" || ui.tab !== "review" || ui.hideSummary) return false;
  const c = counts(view.clips);
  return c.all > 0 && c.undecided === 0;
}

// Clip keys act only on a clip that is on screen: not behind the summary, not during a load.
const clipOnScreen = () => ui.screen === "session" && ui.loaded && !summaryShown() && !!selected();

function renderCard() {
  const c = counts(view.clips);
  const summary = summaryShown();
  $("summary").hidden = !summary;
  $("card-top").hidden = summary;
  $("card-body").hidden = summary;
  if (summary) {
    $("summary-text").textContent = `Оставлено ${c.kept} · Отброшено ${c.rejected}: `
      + REASONS.map((r) => `${r} ${c.reasons[r]}`).join(", ");
    return;
  }
  const clip = selected();
  setDecisionsEnabled(!!clip);
  renderRechoose();  // also with no clip: a question must not outlive its clip
  if (!clip) {
    $("clip-title").textContent = ui.tab === "mine" ? "Нет оставленных клипов" : "Клип не выбран";
    setKids($("clip-sub"), "", () => []);
    $("clip-saving").hidden = true;
    $("note-view").replaceChildren();
    $("other-notes").replaceChildren();
    $("note-box").hidden = true;
    $("clip-alert").hidden = true;
    renderPlayer();
    return;
  }
  const idx = view.clips.indexOf(clip);
  $("clip-title").textContent = `${clockOf(clip.start_local) || fmtPos(clip.start_s)} · ${fmtDur(clip.duration_s, ui.lang)}`;
  // aria-live: rewrite only on change, or every 2 s poll would be read out again;
  // the save spinner sits outside the live region, so it never re-reads the same text
  const sub = `клип ${idx + 1} из ${view.clips.length} · ${statusText(clip)}`;
  setKids($("clip-sub"), sub, () => [sub]);
  $("clip-saving").hidden = !ui.pending.has(clip.clip_id);
  const s = status(clip);
  for (const b of $("decisions").children) {
    const on = s.value === b.dataset.value && s.kind !== "conflict";
    b.setAttribute("aria-pressed", String(on));
    if (b.dataset.value === KEEP) {
      if (on) b.dataset.state = "kept";
      else delete b.dataset.state;
    }
  }
  const own = ownNote(clip);
  $("note-label").textContent = own ? "заметка" : "+ заметка";
  $("note-box").hidden = ui.noteClipId !== clip.clip_id;
  const showOwn = own && ui.noteClipId !== clip.clip_id;
  setKids($("note-view"), JSON.stringify([clip.clip_id, showOwn && own.value, ui.expanded]), () => {
    if (!showOwn) return [];
    const parts = [el("p", own.value, `note-text${ui.expanded ? "" : " clamped"}`)];
    if (own.value.length > 160 || own.value.split("\n").length > 2) {
      parts.push(button(ui.expanded ? "свернуть" : "ещё", () => {
        ui.expanded = !ui.expanded;
        render();
      }, "link", "sm"));
    }
    return parts;
  });
  const others = clip.notes.filter((n) => !n.own);
  setKids($("other-notes"), JSON.stringify(others.map((n) => n.value)), () => others.map((n) => {
    const box = el("div", null, "other-note");
    box.append(el("p", "с другого прогона", "muted small"), el("p", n.value, "note-text"));
    return box;
  }));
  $("clip-alert").hidden = !(ui.audio === "error" && playingId === clip.clip_id);
  $("clip-alert-text").textContent = ui.audioError;
  renderPlayer();
}

function renderPlayer() {
  const p = $("player");
  const mine = playingId === ui.selectedId;
  const dur = mine && Number.isFinite(p.duration) ? p.duration : 0;
  const t = mine ? p.currentTime : 0;
  const pct = dur ? Math.min(100, (t / dur) * 100) : 0;
  $("bar-fill").style.width = `${pct}%`;
  $("bar").setAttribute("aria-valuenow", String(Math.round(pct)));
  const clip = selected();
  $("pos").textContent = clip ? `${fmtPos(t)} / ${fmtPos(dur || clip.duration_s)}` : "";
  if (clip) $("bar").setAttribute("aria-valuetext", `${fmtPos(t)} из ${fmtPos(dur || clip.duration_s)}`);
  else $("bar").removeAttribute("aria-valuetext");
  $("spinner").hidden = !(mine && ui.audio === "loading");
  $("play-icon").hidden = mine && !p.paused;
  $("pause-icon").hidden = !$("play-icon").hidden;
}

function cssVar(name, fallback) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}

function drawTimeline() {
  const cv = $("timeline");
  if (!view || cv.hidden) return;
  const w = cv.clientWidth * devicePixelRatio;
  const h = cv.clientHeight * devicePixelRatio;
  cv.width = w;
  cv.height = h;
  const g = cv.getContext("2d");
  g.clearRect(0, 0, w, h);
  const total = view.duration_s || Math.max(1, ...view.clips.map((c) => c.end_s));
  const colors = { kept: cssVar("--kept", "green"), rejected: cssVar("--muted-foreground", "gray"),
    undecided: cssVar("--chart-2", cssVar("--ring", "blue")) };
  let cur = null;
  for (const c of view.clips) {
    const k = status(c).kind;
    const x = (c.start_s / total) * w;
    const cw = Math.max(devicePixelRatio, ((c.end_s - c.start_s) / total) * w);
    g.globalAlpha = k === "rejected" ? 0.45 : 1;
    g.fillStyle = colors[k] || colors.undecided;
    g.fillRect(x, h * 0.2, cw, h * 0.6);
    if (c.clip_id === ui.selectedId) cur = x;
  }
  g.globalAlpha = 1;
  if (cur !== null) {
    g.fillStyle = cssVar("--destructive", "red");
    g.fillRect(cur, 0, Math.max(2, devicePixelRatio * 2), h);
  }
}

function renderLibrary() {
  const st = serverState;
  if (!st) return;
  const runs = sid && view ? [sid, runId, view.run_id, view.runs] : null;
  setKids($("sessions"), JSON.stringify([st.sessions, runs]), () => (st.sessions.length ? st.sessions.map((s) => {
    const row = el("div", null, "lib-row");
    const head = el("div", null, "row");
    head.append(el("strong", sessionLabel(s, ui.lang)),
      el("span", `${STATES[s.state] || s.state}${s.n_clips !== null ? `, клипов ${s.n_clips}` : ""}`, "muted"));
    row.append(head);
    const acts = el("div", null, "row");
    if (s.n_clips) {
      acts.append(button(s.session_id === sid ? "Открыта" : "Открыть", () => {
        ui.screen = "session";
        openSession(s.session_id).catch(fail);
        render();
      }, s.session_id === sid ? "secondary" : undefined));
    }
    if (s.can_process && s.state !== "clips") {
      acts.append(button("Обработать", () => submit("process", { session_id: s.session_id }), "outline", "sm", "cog"));
    } else if (!s.can_process && s.state === "clips") {
      acts.append(el("span", "только прослушивание (light)", "muted small"));
    }
    row.append(acts);
    if (s.session_id === sid && view && view.runs.length > 1) {
      const runs = el("div", null, "row run-switch");
      const list = el("div", null, "row");  // wrapped buttons line up under the first, not the label
      runs.append(el("span", "Прогон:", "muted small"), list);
      list.append(...view.runs.map((r) => {
        const active = (runId || view.run_id) === r.run_id;
        const b = button(`${r.name} (${r.created})`, () => openSession(sid, r.run_id).catch(fail),
          active ? "secondary" : "outline");
        b.setAttribute("aria-pressed", String(active));
        return b;
      }));
      row.append(runs);
    }
    if (s.error) row.append(el("div", errText(ui.lang, s.error), "error"));
    return row;
  }) : [el("p", "Сессий пока нет.", "muted")]));

  // Inputs being typed into are kept: re-render inbox only when its content changed.
  const inbox = $("inbox");
  const sig = JSON.stringify(st.inbox);
  if (inbox.dataset.sig !== sig) {
    inbox.dataset.sig = sig;
    const parts = [];
    for (const src of st.inbox.sources) {
      const box = el("div", null, "lib-row");
      box.append(el("strong", src.source === "" ? "inbox/ (корень)" : `inbox/${src.source}/`));
      if (src.error) box.append(el("div", errText(ui.lang, src.error), "error"));
      for (const ch of src.chains) {
        box.append(el("div", `цепочка ${ch.chain}: ${ch.files.length} файлов, начало ${ch.start_local}, `
          + `${(ch.duration_s / 60).toFixed(1)} мин${ch.imported ? ", уже импортирована" : ""}`));
        const input = el("input", null, "input");
        input.type = "text";
        input.placeholder = "начало YYYY-MM-DD HH:MM (если часы сбиты)";
        input.setAttribute("aria-label", "Начало записи, если часы диктофона сбиты");
        const row = el("div", null, "row");
        row.append(input, button("Импортировать", () => submit("import", {
          source: src.source, chain: ch.chain, files: ch.files, start: input.value.trim() || null,
        }), undefined, "sm", "folder-input"));
        box.append(row);
      }
      parts.push(box);
    }
    for (const name of st.inbox.backups) {
      const row = el("div", null, "lib-row");
      const line = el("div", null, "row");
      line.append(el("span", name), button("Восстановить", () => submit("restore", { name }), "outline"));
      row.append(line);
      parts.push(row);
    }
    if (!parts.length) parts.push(el("p", "Пусто: положите файлы в папку inbox продукта.", "muted"));
    inbox.replaceChildren(...parts);
  }

  setKids($("jobs"), JSON.stringify(st.jobs), () => (st.jobs.length ? st.jobs.slice().reverse().map((j) => {
    const box = el("div", null, "lib-row");
    box.append(el("strong", `${NAMES[j.kind] || j.kind}: ${JOB_STATES[j.state] || j.state}`
      + `${j.stage ? `, этап ${j.stage}` : ""}`));
    if (j.error) box.append(el("div", errText(ui.lang, j.error), "error"));
    if (j.log_tail.length) box.append(el("pre", j.log_tail.join("\n")));
    if (j.state === "queued" || j.state === "running") {
      box.append(button("Отменить", () => api("POST", `/api/jobs/${j.job_id}/cancel`, {})
        .then(poll).catch(fail), "outline"));
    }
    return box;
  }) : [el("p", "Задач нет.", "muted")]));

  $("backup-info").textContent = st.last_backup
    ? `Последний бэкап: ${backupTime(st.last_backup, ui.lang)}` : "Бэкапов ещё не было.";
  renderIdeas();
}

// D56-D58: one list in two places (the card and the expanded window), newest first.
function renderIdeas() {
  const ideas = serverState ? serverState.ideas : [];
  if (ui.ideaDelete && !ideas.some((i) => i.event_id === ui.ideaDelete)) ui.ideaDelete = null;
  const sig = JSON.stringify([ideas.map((i) => i.event_id), ui.ideaDelete, ui.ideaDeleting]);
  for (const ul of [$("ideas"), $("ideas-full")]) {
    setKids(ul, sig, () => ideas.slice().reverse().map(ideaRow));
  }
}

function ideaRow(idea) {
  const li = el("li");
  li.dataset.id = idea.event_id;
  const head = el("div", null, "idea-head");
  head.append(el("time", ideaTime(idea.ts, ui.lang)));
  if (ui.ideaDelete === idea.event_id) {
    const ask = el("span", null, "idea-ask");
    const yes = button("Да", () => deleteIdea(idea.event_id), "destructive");
    yes.classList.add("idea-yes");
    yes.disabled = ui.ideaDeleting;  // a double click must not send a second delete
    const no = button("Нет", () => askDeleteIdea(null), "outline");
    no.classList.add("idea-no");
    ask.append(el("span", "Удалить идею?"), yes, no);
    head.append(ask);
  } else {
    const x = button("", () => askDeleteIdea(idea.event_id), "ghost", "icon-sm", "x");
    x.classList.add("idea-del");
    x.setAttribute("aria-label", "Удалить идею");
    head.append(x);
  }
  li.append(head, el("p", idea.value));
  return li;
}

function askDeleteIdea(id) {
  ui.ideaDelete = id;
  renderIdeas();
}

async function deleteIdea(id) {
  if (ui.ideaDeleting) return;
  ui.ideaDeleting = true;
  renderIdeas();
  try {
    await api("POST", "/api/journal", { event_id: newId(), kind: "idea_deleted", value: id });
    ui.ideaDelete = null;
    // Gone at once: a poll already in flight makes poll() return before fresh state arrives.
    serverState.ideas = serverState.ideas.filter((i) => i.event_id !== id);
    await poll();
  } catch (e) {
    toast("Идея не удалена", e.message, true);
  } finally {
    ui.ideaDeleting = false;
    renderIdeas();
  }
}

async function submit(kind, params) {
  try {
    await api("POST", "/api/jobs", { job_id: newId(), kind, params });
    toast("Задача поставлена", NAMES[kind] || kind);
    await poll();
  } catch (e) {
    fail(e);
  }
}

// --- audio ---

function stopAudio() {
  if (audioCtl) audioCtl.abort();
  audioCtl = null;
  const p = $("player");
  p.pause();
  p.removeAttribute("src");
  if (audioUrl) URL.revokeObjectURL(audioUrl);
  audioUrl = null;
  playingId = null;
  ui.audio = "idle";
  ui.playWhenReady = false;
}

async function loadAudio(clip, autoplay) {
  if (!clip) return;
  const p = $("player");
  if (playingId === clip.clip_id && ui.audio !== "error") {
    if (autoplay && ui.audio === "ready") await p.play().catch(() => {});
    if (autoplay && ui.audio === "loading") ui.playWhenReady = true;  // Space while the clip loads
    return;
  }
  stopAudio();
  const ctl = new AbortController();
  audioCtl = ctl;
  playingId = clip.clip_id;
  ui.playWhenReady = autoplay;
  ui.audio = "loading";
  ui.audioError = "";
  renderPlayer();
  try {
    const r = await fetch(`/api/clip/${clip.clip_id}`, {
      headers: { Authorization: `Bearer ${KEY}` }, signal: ctl.signal,
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const blob = await r.blob();
    if (ctl !== audioCtl) return;  // a newer clip was selected meanwhile
    audioUrl = URL.createObjectURL(blob);
    p.src = audioUrl;
    ui.audio = "ready";
    if (ui.playWhenReady) await p.play().catch(() => {});
  } catch (e) {
    if (e.name === "AbortError" || ctl !== audioCtl) return;
    ui.audio = "error";
    ui.audioError = e.message;
  }
  if (playingId === ui.selectedId) render();
}

function togglePlay() {
  const clip = selected();
  if (!clipOnScreen()) return;
  const p = $("player");
  if (playingId === clip.clip_id && ui.audio === "loading") {
    ui.playWhenReady = !ui.playWhenReady;
    return;
  }
  if (playingId !== clip.clip_id || ui.audio !== "ready") {
    loadAudio(clip, true);
    return;
  }
  if (p.paused) {
    if (p.ended || p.currentTime >= p.duration) p.currentTime = 0;
    p.play().catch(() => {});
  } else p.pause();
}

function seek(delta) {
  if (!clipOnScreen()) return;
  const p = $("player");
  if (playingId !== ui.selectedId || !Number.isFinite(p.duration)) return;
  p.currentTime = Math.max(0, Math.min(p.duration, p.currentTime + delta));
  renderPlayer();
}

// --- actions ---

function select(clipId, { play = false } = {}) {
  if (clipId !== ui.selectedId) {
    ui.rechoose = null;
    closeNote();
    $("note-status").textContent = "";
    ui.expanded = false;
    ui.selectedId = clipId;
    $("player").pause();  // the old clip must not end (and become «heard») as the new one
  }
  render();
  loadAudio(selected(), play);
}

function step(delta) {
  const list = shown();
  if (!list.length || !ui.loaded || summaryShown()) return;
  const pos = list.findIndex((c) => c.clip_id === ui.selectedId);
  const next = list[pos < 0 ? 0 : Math.max(0, Math.min(list.length - 1, pos + delta))];
  select(next.clip_id, { play: true });  // D53
}

function journal(kind, value, clip) {
  writes++;
  const body = { event_id: newId(), kind, value };
  if (clip) body.clip_id = clip.clip_id;
  const p = queue.then(async () => {
    const r = await api("POST", "/api/journal", body);
    if (!r.saved) throw codedError({ code: "not_saved", params: {} }, ui.lang);
    return r;
  });
  queue = p.catch(() => {});
  return p;
}

// Optimistic: the clip shows the new decision at once; a failed write puts the old one back.
function setMark(clip, value) {
  const before = { own_mark: clip.own_mark, marks: clip.marks.slice(), heard: clip.heard };
  const others = clip.marks.filter((m) => !(before.own_mark && m === before.own_mark));
  clip.own_mark = value;
  clip.marks = [...new Set([...others, value].filter(Boolean))];
  clip.heard = true;
  ui.pending.add(clip.clip_id);
  const writes = [journal("mark", value, clip)];
  if (!before.heard) writes.push(journal("heard", "", clip));
  return Promise.all(writes).then(() => {
    ui.pending.delete(clip.clip_id);
    reloadView().catch(fail);
  }, (e) => {
    ui.pending.delete(clip.clip_id);
    Object.assign(clip, before);
    toast("Не сохранено", e.message, true);
    render();
    throw e;
  });
}

function decide(value) {
  const clip = selected();
  if (!clipOnScreen()) return;
  const entry = undoEntry(clip);  // before setMark: it records the previous own decision
  ui.undo.push(entry);
  $("player").pause();
  ui.playWhenReady = false;  // D53: a decided clip still loading must not start later
  const list = shown();
  const pos = list.indexOf(clip);
  setMark(clip, value).catch(() => {
    const i = ui.undo.lastIndexOf(entry);
    if (i >= 0) ui.undo.splice(i, 1);
  });
  let next = clip.clip_id;
  if (ui.tab === "review") {
    const i = nextUndecided(view.clips, view.clips.indexOf(clip));
    next = i >= 0 ? view.clips[i].clip_id : clip.clip_id;
    if (i >= 0 && !shown().some((c) => c.clip_id === next)) ui.filter = "all";
  } else if (value !== KEEP) {
    const after = shown();  // the rejected clip has left the list
    next = after.length ? after[Math.min(pos, after.length - 1)].clip_id : null;
  }
  select(next, { play: !!next && next !== clip.clip_id });  // D53: nothing left → silence
  $("card").focus({ preventScroll: true });
}

function openRechoose() {
  if (ui.tab !== "mine" || !clipOnScreen()) return;
  ui.rechoose = "menu";
  render();
}

// Every clip in «Мои клипы» is kept, so «Оставить» is the same choice: no question, no write.
function pickRechoose(value) {
  if (ui.rechoose !== "menu" || !clipOnScreen()) return;
  ui.rechoose = value === KEEP ? null : value;
  render();
}

function closeRechoose(confirmed) {
  const value = ui.rechoose;
  ui.rechoose = null;
  if (confirmed && value && value !== "menu") decide(value);
  else render();
}

function undo() {
  if (ui.screen !== "session" || !view || !ui.loaded) return;
  const t = undoTarget(ui.undo, view.clips);
  if (!t) {
    toast("Отменять нечего");
    return;
  }
  if (t.index < 0) {
    toast("Клип не из этого прогона", "Отмена пропущена.");
    return;
  }
  const clip = view.clips[t.index];
  setMark(clip, t.entry.prev).catch(() => ui.undo.push(t.entry));
  if (ui.tab === "review" && !shown().includes(clip)) ui.filter = "all";
  if (shown().includes(clip)) select(clip.clip_id, { play: false });
  else render();
}

function setTab(tab) {
  ui.tab = tab;
  ui.rechoose = null;
  ui.ideaDelete = null;
  ui.screen = "session";
  if (view && !shown().some((c) => c.clip_id === ui.selectedId)) {
    ui.selectedId = null;
    closeNote();
    stopAudio();
    keepSelection();
  }
  render();
}

function toggleLibrary() {
  ui.rechoose = null;
  ui.ideaDelete = null;
  if (ui.screen === "library") {
    if (sid) ui.screen = "session";
  } else ui.screen = "library";
  render();
}

// --- note, idea ---

function openNote() {
  const clip = selected();
  if (!clip) return;
  if (ui.noteClipId !== clip.clip_id) {
    ui.noteClipId = clip.clip_id;
    const own = ownNote(clip);
    const draft = ui.drafts.get(clip.clip_id);
    $("note").value = draft !== undefined ? draft : (own ? own.value : "");
  }
  $("note-box").hidden = false;
  render();
  $("note").focus();
}

// Closing never drops typed text: what differs from the saved note stays as this clip's draft.
function closeNote() {
  const id = ui.noteClipId;
  const clip = id && view && view.clips.find((c) => c.clip_id === id);
  const own = clip && ownNote(clip);
  if (id && $("note").value !== (own ? own.value : "")) ui.drafts.set(id, $("note").value);
  ui.noteClipId = null;
  $("note-box").hidden = true;
}

let noteTimer = null;
async function saveNote() {
  const id = ui.noteClipId;
  if (!id || ui.noteSaving) return;
  const clip = view && view.clips.find((c) => c.clip_id === id);
  const text = $("note").value;
  const own = clip && ownNote(clip);
  if (!clip || text === (own ? own.value : "")) {
    if (clip) ui.drafts.delete(id);
    closeNote();
    render();
    return;
  }
  ui.noteSaving = true;
  const st = $("note-status");
  clearTimeout(noteTimer);
  st.textContent = "сохраняю…";
  try {
    const r = await journal("note", text, clip);
    clip.notes = clip.notes.filter((n) => !n.own);
    if (text) clip.notes.unshift({ event_id: r.event.event_id, value: text, own: true });
    if (ui.drafts.get(id) === text) ui.drafts.delete(id);
    // typed on while saving: keep the field open, the next Esc saves the rest
    if (ui.noteClipId === id && $("note").value === text) closeNote();
    st.textContent = "сохранено";
    noteTimer = setTimeout(() => { st.textContent = ""; }, 2000);
    render();
    reloadView().catch(fail);
  } catch (e) {
    if (ui.noteClipId !== id) ui.drafts.set(id, text);  // left the clip: N on it brings the text back
    else st.textContent = `не сохранено: ${e.message}`;  // the text stays in the field; Esc retries
    toast("Заметка не сохранена", `${e.message}. Текст сохранён в поле заметки этого клипа.`, true);
  } finally {
    ui.noteSaving = false;
  }
}

function openIdea() {
  $("idea-status").textContent = "";
  if (!$("idea-dialog").open) $("idea-dialog").showModal();
  $("idea").focus();
}

async function saveIdea() {
  const text = $("idea").value.trim();
  if (!text) return;
  $("idea-status").textContent = "сохраняю…";
  try {
    const body = { event_id: newId(), kind: "idea", value: text };
    if (sid) body.session_id = sid;
    await api("POST", "/api/journal", body);
    $("idea").value = "";
    $("idea-dialog").close();
    toast("Идея записана");
    await poll();
  } catch (e) {
    $("idea-status").textContent = `не сохранено: ${e.message}`;
  }
}

function escape(target) {
  // The idea question takes Esc only when nothing else is open over it.
  if (ui.ideaDelete && !ui.ideaDeleting && ui.screen === "library"
      && !document.querySelector("dialog[open]:not(#ideas-dialog)")) return askDeleteIdea(null);
  const open = document.querySelector("dialog[open]");
  if (open) {
    open.close();
    $("card").focus({ preventScroll: true });
  } else if (target === $("note")) {
    saveNote();
    $("card").focus({ preventScroll: true });
  } else if (target instanceof HTMLElement && target.matches("input")) {
    target.blur();
  }
}

// --- keys, events ---

function run(action, target) {
  if (action.startsWith("mark:")) return decide(action.slice(5));
  if (action.startsWith("pick:")) return pickRechoose(action.slice(5));
  if (action.startsWith("seek:")) return seek(Number(action.slice(5)));
  const onSession = ui.screen === "session" && view && view.clips.length;
  const acts = {
    play: () => onSession && togglePlay(),
    prev: () => onSession && step(-1),
    next: () => onSession && step(1),
    undo,
    note: () => onSession && openNote(),
    idea: openIdea,
    tab: () => setTab(ui.tab === "review" ? "mine" : "review"),
    search: () => $("search").focus(),
    library: toggleLibrary,
    help: () => $("help-dialog").showModal(),
    escape: () => escape(target),
    "save-idea": saveIdea,
    rechoose: openRechoose,
    confirm: () => closeRechoose(true),
    cancel: () => closeRechoose(false),
    noop: () => {},
  };
  return acts[action] && acts[action]();
}

function onKey(e) {
  if (ui.fatal) return;
  const t = e.target;
  const inField = t instanceof HTMLElement
    && (t.tagName === "TEXTAREA" || (t.tagName === "INPUT" && t.type !== "checkbox"));
  const dialogOpen = !!document.querySelector("dialog[open]");
  const screen = ui.screen === "library" ? "library" : ui.tab;
  const action = keyAction(e, { inField, dialogOpen, screen, rechoose: ui.rechoose });
  if (!action) return;
  e.preventDefault();  // Space and digits must not press a focused button
  e.stopPropagation();
  run(action, t);
}

function bind() {
  document.addEventListener("keydown", onKey, true);
  $("decisions").replaceChildren(...DECISIONS.map((value, i) => {
    const b = button(value === KEEP ? "Оставить" : value[0].toUpperCase() + value.slice(1), () => {
      if (ui.tab === "mine") pickRechoose(value);
      else decide(value);
    }, value === KEEP ? undefined : "outline", "default", DECISION_ICONS[value]);
    b.dataset.value = value;
    b.append(el("kbd", String(i + 1), "kbd"));
    return b;
  }));
  for (const b of $("tabs").querySelectorAll("[role=tab]")) {
    b.addEventListener("click", () => setTab(b.dataset.tab));
  }
  $("library-btn").addEventListener("click", toggleLibrary);
  $("help-btn").addEventListener("click", () => $("help-dialog").showModal());
  $("help-close").addEventListener("click", () => $("help-dialog").close());
  $("theme").addEventListener("click", () => {
    ui.theme = document.documentElement.classList.contains("dark") ? "light" : "dark";
    applyTheme();
  });
  $("job-badge").addEventListener("click", () => {
    ui.screen = "library";
    render();
  });
  $("session-empty-action").addEventListener("click", () => {
    ui.screen = "library";
    render();
  });
  $("open-mine").addEventListener("click", () => setTab("mine"));
  $("hide-summary").addEventListener("click", () => {
    ui.hideSummary = true;
    render();
  });
  $("play-btn").addEventListener("click", () => {
    togglePlay();
    $("card").focus({ preventScroll: true });
  });
  $("note-btn").addEventListener("click", openNote);
  $("rechoose-btn").addEventListener("click", openRechoose);
  $("ideas-expand").addEventListener("click", () => $("ideas-dialog").showModal());
  $("ideas-close").addEventListener("click", () => $("ideas-dialog").close());
  $("inbox-open").addEventListener("click", () => {  // D49
    api("POST", "/api/inbox/open", {}).catch((e) => toast("Проводник не открылся", e.message, true));
  });
  $("rechoose-yes").addEventListener("click", () => closeRechoose(true));
  $("rechoose-no").addEventListener("click", () => closeRechoose(false));
  $("note").addEventListener("blur", saveNote);
  $("search").addEventListener("input", (e) => {
    ui.query = e.target.value;
    render();
  });
  $("in-order").addEventListener("change", (e) => {
    ui.inOrder = e.target.checked;
  });
  $("idea-save").addEventListener("click", saveIdea);
  $("idea-cancel").addEventListener("click", () => $("idea-dialog").close());
  for (const d of document.querySelectorAll("dialog")) {
    d.addEventListener("click", (e) => { if (e.target === d) d.close(); });
  }
  $("backup-full").addEventListener("click", () => submit("backup", { mode: "full" }));
  $("backup-light").addEventListener("click", () => submit("backup", { mode: "light" }));
  $("bar").addEventListener("click", (e) => {
    const p = $("player");
    if (playingId !== ui.selectedId || !Number.isFinite(p.duration)) return;
    p.currentTime = (e.offsetX / e.currentTarget.clientWidth) * p.duration;
    renderPlayer();
  });
  $("timeline").addEventListener("click", (e) => {
    const list = ui.tab === "mine" ? shown() : view && view.clips;
    if (!list || !list.length) return;
    const total = view.duration_s || Math.max(...view.clips.map((c) => c.end_s));
    const t = (e.offsetX / e.currentTarget.clientWidth) * total;
    const mid = (c) => Math.abs((c.start_s + c.end_s) / 2 - t);
    const best = list.reduce((a, c) => (mid(c) < mid(a) ? c : a));
    if (!shown().includes(best)) ui.filter = "all";
    select(best.clip_id, { play: false });  // D53, owner: the timeline only moves to a clip
  });
  const p = $("player");
  for (const ev of ["timeupdate", "play", "pause", "loadedmetadata"]) p.addEventListener(ev, renderPlayer);
  p.addEventListener("ended", () => {
    const clip = view && view.clips.find((x) => x.clip_id === playingId);
    if (clip && !clip.heard) {
      clip.heard = true;
      journal("heard", "", clip).then(() => reloadView()).catch(fail);
    }
    renderPlayer();
    if (ui.tab === "mine" && ui.inOrder && clip && clip.clip_id === ui.selectedId) {
      const list = shown();
      const i = list.indexOf(clip);
      if (i >= 0 && i + 1 < list.length) select(list[i + 1].clip_id, { play: true });
    }
  });
  window.addEventListener("resize", () => view && drawTimeline());
}

if (typeof module === "object") {
  module.exports = { parseSessionName, sessionLabel, clockOf, fmtDur, fmtPos, KEEP, REASONS, status, isDecided,
    counts, nextUndecided, visibleClips, gapBefore, gapLabel, backupTime, ideaTime, undoEntry, undoTarget, DECISIONS, keyAction, launchRect, autoOpenTarget,
    afterOpenError, retryAfter, codedError };
} else {
  start();
}
