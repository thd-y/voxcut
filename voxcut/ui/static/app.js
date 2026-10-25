// VoxCut screen (redesign R1–R10): review, my clips, library. Vanilla JS, no build step.
// Security (S2, S3, S7): the key lives only in this module, all data goes out via textContent.
"use strict";
const { translate, errText, pickLang, label } = typeof module === "object" ? require("./i18n.js") : globalThis.i18n;

// --- pure functions (no DOM; tested by node --test, D48) ---

const LOCALE = { en: "en-GB", ru: "ru" };  // D71: English dates day first, 24 h

// D95: "full" in lists and the job log, "short" (digits) in the header
const NAME_DATE = { full: { day: "numeric", month: "short", year: "numeric" },
  short: { day: "2-digit", month: "2-digit", year: "2-digit" } };

function parseSessionName(name, lang, form = "full") {
  const m = /^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})_(?:(\d+)h)?(?:(\d+)m)?$/.exec(name);
  if (!m || (!m[6] && !m[7]) || +m[2] < 1 || +m[2] > 12 || +m[4] > 23 || +m[5] > 59) return null;
  const [h, min] = [+m[6], +m[7]];
  const length = m[6] && m[7] ? translate(lang, "dur.hm", { h, m: min })
    : m[6] ? translate(lang, "dur.h", { h }) : translate(lang, "dur.m", { m: min });
  const day = new Date(+m[1], +m[2] - 1, +m[3]);
  if (day.getDate() !== +m[3]) return null;  // 02-31 would roll over into March
  const parts = new Intl.DateTimeFormat(LOCALE[lang], NAME_DATE[form]).formatToParts(day);  // D68
  const end = parts.findLastIndex((x) => x.type !== "literal");  // D95: ru appends a year suffix
  const date = parts.slice(0, end + 1).map((x) => x.value).join("");
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

// The status line of a clip (D67): whole templates, a mark through its label or as is (L14).
function statusText(clip, lang) {
  const s = status(clip);
  const mark = (v) => label(lang, "mark.", v);
  if (s.kind === "kept") return translate(lang, s.others ? "status.kept_others" : "status.kept");
  if (s.kind === "rejected") {
    return translate(lang, s.others ? "status.rejected_others" : "status.rejected", { mark: mark(s.value) });
  }
  if (s.kind === "conflict") return translate(lang, "status.conflict", { marks: clip.marks.map(mark) });
  return translate(lang, "status.undecided");
}

// D101: a failed decision write names the clip by its list-row time, and the decision ("" = back to undecided).
function notSavedTitle(clip, value, lang) {
  const decision = value === KEEP ? translate(lang, "status.kept")
    : value ? label(lang, "mark.", value) : translate(lang, "status.undecided");
  return translate(lang, "toast.not_saved_clip", { time: clockOf(clip.start_local) || fmtPos(clip.start_s), decision });
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

function summaryText(c, lang) {
  const reasons = REASONS.map((r) => translate(lang, "summary.reason", { mark: label(lang, "mark.", r), n: c.reasons[r] }));
  return translate(lang, "summary.text", { kept: c.kept, rejected: c.rejected, reasons });
}

function chainText(ch, lang) {
  return translate(lang, ch.imported ? "inbox.chain_imported" : "inbox.chain", { chain: ch.chain,
    n: ch.files.length, start: ch.start_local, minutes: (ch.duration_s / 60).toFixed(1) });
}

// D79, D95: what the job is about; a session through its label, a gone one (a delete's trace) by its id
function jobSubject(j, sessions, lang) {
  const s = j.subject || {};
  if (j.kind === "process" || j.kind === "delete") {
    const row = sessions.find((r) => r.session_id === s.session_id)
      || (s.session_id && { name: s.session_id.split(".")[0] });  // the name is the id without its hash
    return row ? sessionLabel(row, lang) : "";
  }
  if (j.kind === "import") return s.source === "" ? translate(lang, "inbox.root") : s.source || "";
  return (j.kind === "backup" ? s.mode : j.kind === "restore" ? s.name : "") || "";
}

// D79: «{kind} {subject} — {state}»; the stage while running and where it failed (refines D75)
function jobLine(j, sessions, lang) {
  const params = { kind: label(lang, "job.kind.", j.kind), subject: jobSubject(j, sessions, lang),
    state: j.state === "running" && j.stage ? label(lang, "job.stage.", j.stage) : label(lang, "job.state.", j.state),
    stage: label(lang, "job.stage_at.", j.stage) };
  const key = j.state === "failed" && j.stage ? "jobs.line_failed_at" : "jobs.line";
  return translate(lang, key, params).replace(/ {2}—/, " —");  // a job without a subject
}

const jobClock = (j, lang) => ideaTime(j.started || j.submitted || "", lang);

function fmtSpan(seconds, lang) {
  const t = Math.max(0, Math.floor(seconds));
  const [h, m] = [Math.floor(t / 3600), Math.floor((t % 3600) / 60)];
  if (h) return translate(lang, m ? "dur.hm" : "dur.h", { h, m });
  return m ? translate(lang, "dur.m", { m }) : translate(lang, "dur.s", { s: t });
}

function jobDuration(j, now, lang) {
  const from = Date.parse(j.started);
  if (Number.isNaN(from)) return "";
  if (j.state === "running") return translate(lang, "jobs.running_for", { dur: fmtSpan((now - from) / 1000, lang) });
  const to = Date.parse(j.finished);
  return Number.isNaN(to) ? "" : fmtSpan((to - from) / 1000, lang);
}

// D96: a job whose state changed since the last poll is read out (aria-live); new jobs have a toast
function jobAnnouncements(prev, jobs, sessions, lang) {
  if (!prev) return [];
  const was = new Map(prev.map((j) => [j.job_id, j.state]));
  return jobs.filter((j) => was.has(j.job_id) && was.get(j.job_id) !== j.state)
    .map((j) => jobLine(j, sessions, lang));
}

// D96: the log redraws every second while a job is in its first minute («running 5 s»), else every minute
function jobTick(jobs, now) {
  const young = jobs.some((j) => j.state === "running" && now - Date.parse(j.started) < 60000);
  return young ? `s${Math.floor(now / 1000)}` : `m${Math.floor(now / 60000)}`;
}

// D81: newest first; two or more done backups in a row become one group
function journalRows(jobs) {
  const rows = [];
  for (const j of jobs.slice().reverse()) {
    const prev = rows[rows.length - 1];
    if (j.kind === "backup" && j.state === "done" && prev && prev.run) prev.run.push(j);
    else rows.push(j.kind === "backup" && j.state === "done" ? { run: [j] } : { job: j });
  }
  return rows.map((r) => (!r.run ? r : r.run.length > 1 ? { group: r.run } : { job: r.run[0] }));
}

const groupKey = (group) => group[group.length - 1].job_id;  // the oldest: stays when a newer backup joins

const groupLine = (group, lang) => translate(lang, "jobs.backup_group",
  { n: group.length, time: ideaTime(group[0].finished || group[0].started || "", lang) });

// D84: the newest open failure, and how many more are open
function failedBadge(jobs) {
  const open = jobs.filter((j) => j.failure === "open");
  return open.length ? { job: open[open.length - 1], more: open.length - 1 } : null;
}

// D85: the session row's mark; a queued or running process outranks an open failure
// D91: hidden sessions leave the list for «Hidden: N»; the order is kept
function splitHidden(sessions) {
  return { shown: sessions.filter((s) => !s.hidden), hidden: sessions.filter((s) => s.hidden) };
}

// The text of an empty session list: none at all, or every one hidden (D91); null when one shows.
function emptySessionsKey(sessions) {
  return sessions.some((s) => !s.hidden) ? null : sessions.length ? "all_hidden" : "no_sessions";
}

// D94: no hide or delete while a job of the session is queued or running
const sessionBusy = (jobs, sessionId) => jobs.some((j) => (j.state === "queued" || j.state === "running")
  && j.subject && j.subject.session_id === sessionId);

function sessionJobMark(jobs, sessionId) {
  const mine = jobs.filter((j) => j.kind === "process" && j.subject && j.subject.session_id === sessionId);
  const active = mine.find((j) => j.state === "queued" || j.state === "running");
  if (active) return { kind: "running", job: active };
  const failed = mine.filter((j) => j.failure === "open").pop();
  return failed ? { kind: "failed", job: failed } : null;
}

// D80: «== stage» lines stand out; a failed job's last non-empty line is the error
function logLines(tail, failed) {
  let last = -1;
  if (failed) tail.forEach((t, i) => { if (t.trim()) last = i; });
  return tail.map((text, i) => ({ text, cls: text.startsWith("== ") ? "stage" : i === last ? "error" : "" }));
}

function nextUndecided(clips, from) {
  const after = clips.findIndex((c, i) => i > from && !isDecided(c));
  return after >= 0 ? after : clips.findIndex((c) => !isDecided(c));
}

// D97: after a decision on Review. Inside «отброшены»/«оставлены» the clip stays selected and silent
// (the filter goes to «все» if the clip has left it); in «все»/«не разобраны» the next undecided plays.
function afterDecision(clips, clip, filter) {
  const inFilter = (c) => visibleClips(clips, { tab: "review", filter }).includes(c);
  if (filter === "rejected" || filter === "kept") {
    return { next: clip.clip_id, filter: inFilter(clip) ? filter : "all", play: false };
  }
  const i = nextUndecided(clips, clips.indexOf(clip));
  const next = i >= 0 ? clips[i].clip_id : clip.clip_id;
  return { next, filter: i >= 0 && !inFilter(clips[i]) ? "all" : filter, play: next !== clip.clip_id };
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

// D100: the clip a tab remembers, while it is still in that tab's list; null → the entry rule.
const tabReturn = (list, savedId) => (savedId && list.some((c) => c.clip_id === savedId) ? savedId : null);

// D102(2): a pause separates neighbours in the recording; a filtered list has neighbours by filter only.
const showGaps = (tab, filter) => tab === "review" && filter === "all";

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
  return new Intl.DateTimeFormat(LOCALE[lang], { day: "2-digit", month: "2-digit",
    ...(withYear ? { year: "numeric" } : {}), hour: "2-digit", minute: "2-digit" }).format(d);
}

const ideaTime = (ts, lang) => localTime(ts, false, lang);

const backupTime = (value, lang) => localTime(value, true, lang) || value;  // D61; anything unparsable as is

const gapLabel = (seconds, lang) => translate(lang, "card.gap", { t: fmtPos(seconds) });  // D54

// D66: the state keeps the descriptor and renders it in the current language (D67).
function codedError(desc) {  // the message is the code, for the console; the UI renders errDesc(e)
  const e = new Error(desc.code);
  e.descriptor = desc;
  return e;
}

// Any caught error as a descriptor; a JS error without one shows its text as is.
const errDesc = (e) => (e && e.descriptor) || { code: "legacy_error", params: { detail: String(e?.message ?? e) } };

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
let lastJobs = null;       // the jobs of the previous poll, for the aria-live line (D96)
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
  selectedId: null, tabSel: { review: null, mine: null }, undo: [], pending: new Set(), drafts: new Map(), hideSummary: false, theme: null, fatal: false,  // tabSel: each tab's own clip (D100)
  lang: "en",  // D69: page memory only, every launch starts in English
  loaded: false, openError: null, audio: "idle", audioError: null, playWhenReady: false,
  noteClipId: null, noteSaving: false, expanded: false,
  rechoose: null,  // D52: null | "menu" | the decision awaiting confirmation
  ideaDelete: null, ideaDeleting: false,  // D56: the idea asked about, a delete in flight
  logOpen: new Map(), logPinned: new Map(), logScroll: new Map(),  // D80: by job_id, window memory only
  groupOpen: new Set(), cancelAsk: null,  // D81: open backup groups; D83: the job asked about
  hiddenOpen: false, deleteAsk: null,  // D91: the hidden list is open; the hidden session asked about
  noteStatus: null, ideaStatus: null,  // null | {key, params}: drawn in the current language (L13)
};
const GAP_MIN_S = 120;  // one recording: 131 gaps >= 30 s (noise), 12 >= 120 s (real breaks)
const $ = (id) => document.getElementById(id);
const tr = (key, params) => translate(ui.lang, key, params);
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

// D101: an error toast that stays until its action or its cross; several stack in #toaster (column-reverse).
// A toast with the same key replaces the older one (the same clip failing again).
function stickyToast(title, text, action, key) {
  for (const old of $("toaster").querySelectorAll(".toast[data-key]")) if (old.dataset.key === key) old.remove();
  const t = el("div", null, "toast");
  if (key) t.dataset.key = key;
  t.setAttribute("role", "alert");
  t.setAttribute("aria-hidden", "false");
  t.dataset.category = "error";
  const close = () => {
    t.setAttribute("aria-hidden", "true");
    setTimeout(() => t.remove(), 400);
  };
  const content = el("div", null, "toast-content");
  const sec = el("section");
  sec.append(el("h2", title));
  if (text) sec.append(el("p", text));
  const foot = el("footer");
  if (action) {
    // closes only once the action worked: a failed open keeps the record of what was not saved
    const b = button(action.label, () => action.run().then(close, fail), "outline");
    b.dataset.toastAction = "";
    foot.append(b);
  }
  const x = button(null, close, "ghost", "icon-sm", "x");
  x.dataset.toastCancel = "";
  x.setAttribute("aria-label", tr("toast.close"));
  foot.append(x);
  for (const btn of foot.children) btn.addEventListener("mousedown", (e) => e.preventDefault());  // a note being typed keeps focus
  content.append(sec, foot);
  t.append(content);
  $("toaster").append(t);
  return t;
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
    const e = codedError({ code: "offline", params: {} });
    e.offline = true;
    throw e;
  }
  const data = await r.json().catch(() => ({}));
  if (r.status === 401 || r.status === 403) {  // the key is gone (server restarted): reopen the window
    ui.fatal = true;
    render();
  }
  if (!r.ok) throw codedError((data && data.error) || { code: "http_status", params: { status: r.status } });
  return data;
}

function applyTheme() {
  const dark = ui.theme ? ui.theme === "dark" : !!(darkQuery && darkQuery.matches);
  document.documentElement.classList.toggle("dark", dark);
  $("theme").setAttribute("aria-pressed", String(dark));
  if (view) drawTimeline();
}

// L11: static text, placeholders and labels marked data-i18n* take the current language.
function applyStatic() {
  for (const n of document.querySelectorAll("[data-i18n]")) n.textContent = tr(n.dataset.i18n);
  for (const attr of ["placeholder", "aria-label", "title"]) {
    for (const n of document.querySelectorAll(`[data-i18n-${attr}]`)) {
      n.setAttribute(attr, tr(n.getAttribute(`data-i18n-${attr}`)));
    }
  }
}

// D67, L13: a live switch; nothing typed, selected, open or playing is reset.
function setLang(value) {
  ui.lang = pickLang(value);
  document.documentElement.lang = ui.lang;
  applyStatic();
  render();
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
function autoOpenTarget(all, tried) {
  const sessions = all.filter((s) => !s.hidden);  // D91
  if (!sessions.length) return null;
  const ready = sessions.filter((s) => s.state === "clips");
  const id = (ready[ready.length - 1] || sessions[0]).session_id;
  return id === tried ? null : id;
}

// D94: the start a delete left in start.txt fills the field; what the owner typed, even "", wins
const startValue = (typed, key, hint) => (typed.has(key) ? typed.get(key) : hint || "");

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
  applyStatic();
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
    else if (!ui.fatal) toast(tr("toast.error"), errText(ui.lang, errDesc(e)), true);
    return;
  }
  for (const w of serverState.journal_warnings) {
    const sig = JSON.stringify(w);
    if (!shownWarnings.has(sig)) {
      shownWarnings.add(sig);
      toast(tr("toast.journal"), errText(ui.lang, w), true);
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
  } else if (sid !== null && !sessions.some((s) => s.session_id === sid && !s.hidden)) {
    closeSession();  // D91: hidden (or deleted) in this or another window
  } else if (sid !== null && view && !runId) {
    const row = sessions.find((s) => s.session_id === sid);
    if (row && row.n_clips !== null && row.n_clips !== view.clips.length) await reloadView();
  }
  render();
}

function closeSession() {
  openSeq++;  // an open still in flight must not bring it back
  const clip = ui.noteClipId && view && view.clips.find((c) => c.clip_id === ui.noteClipId);
  const own = clip && ownNote(clip);
  const text = $("note").value;
  if (clip && !ui.noteSaving && text !== (own ? own.value : "")) {  // hidden while typing: the note still goes
    const id = clip.clip_id;
    journal("note", text, clip).then(() => {
      if (ui.drafts.get(id) === text) ui.drafts.delete(id);  // saved: no stale draft on reopening
    }, (e) => toast(tr("toast.note_not_saved"), tr("toast.note_kept", { error: errDesc(e) }), true));
  }
  closeNote();
  stopAudio();
  sid = null;
  view = null;
  runId = null;
  autoTried = null;
  ui.selectedId = null;
  ui.rechoose = null;
  ui.undo = [];
  ui.loaded = true;
  ui.openError = null;
  retryOpen = null;  // a lost-link retry must not reopen it
}

function fail(e) {
  if (e.stale) return;  // a newer open replaced this one: its outcome is what counts
  if (e.offline) $("conn-alert").hidden = false;
  else if (!ui.fatal) toast(tr("toast.error"), errText(ui.lang, errDesc(e)), true);
}

const sessionPath = (id, run) =>
  `/api/session/${encodeURIComponent(id)}${run ? `?run=${encodeURIComponent(run)}` : ""}`;

function sessionRow(id) {
  return serverState && serverState.sessions.find((s) => s.session_id === id);
}

function sessionLabel(row, lang, form = "full") {
  const p = parseSessionName(row.name, lang, form);
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
    ui.tabSel = { review: null, mine: null };
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
    $("fatal-title").textContent = tr("fatal.title");
    $("fatal-text").textContent = tr("fatal.text");
    for (const id of ["top", "screen-session", "screen-library"]) $(id).hidden = true;
    return;
  }
  renderHeader();
  renderStatuses();
  const lib = ui.screen === "library";
  $("screen-library").hidden = !lib;
  $("screen-session").hidden = lib;
  if (lib) renderLibrary();
  else {
    if (serverState) lastJobs = serverState.jobs;  // back in the Library: only news is announced
    renderSession();
  }
}

function renderStatuses() {
  for (const [id, s] of [["note-status", ui.noteStatus], ["idea-status", ui.ideaStatus]]) {
    $(id).textContent = s ? tr(s.key, s.params) : "";
  }
}

function renderHeader() {
  const row = sid && sessionRow(sid);
  $("session-label").textContent = row ? sessionLabel(row, ui.lang, "short") : "";
  for (const b of $("tabs").querySelectorAll("[role=tab]")) {
    b.setAttribute("aria-selected", String(ui.screen === "session" && b.dataset.tab === ui.tab));
  }
  $("work").setAttribute("aria-labelledby", `tab-${ui.tab}`);
  const c = view && ui.loaded ? counts(view.clips) : null;
  $("progress-label").textContent = c && c.all ? tr("top.progress", { done: c.all - c.undecided, all: c.all }) : "";
  const badge = $("job-badge");
  const jobs = serverState ? serverState.jobs : [];
  const active = jobs.find((j) => j.state === "queued" || j.state === "running");
  badge.hidden = !active;
  if (active) {
    badge.dataset.variant = "secondary";
    badge.textContent = tr("top.job", { kind: label(ui.lang, "job.kind.", active.kind),
      state: active.stage ? label(ui.lang, "job.stage.", active.stage) : label(ui.lang, "job.state.", active.state) });
  }
  const fb = failedBadge(jobs);  // D84: a second, red badge while any failure is open
  $("fail-badge").hidden = !fb;
  if (fb) {
    $("fail-badge").textContent = tr(fb.more ? "top.job_failed_more" : "top.job_failed",
      { kind: label(ui.lang, "job.kind.", fb.job.kind), n: fb.more });
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
  const empty = serverState && emptySessionsKey(serverState.sessions);
  if (empty) {
    showSessionEmpty(tr(`empty.${empty}_title`), tr(`empty.${empty}_text`), true);
    return;
  }
  if (!view && ui.openError) {
    showSessionEmpty(tr("empty.open_failed_title"), tr("empty.open_failed_text", { error: ui.openError }), true);
    return;
  }
  if (!serverState || !ui.loaded || !view) {
    renderSkeleton();
    return;
  }
  if (!view.clips.length) {
    const row = sessionRow(sid);
    const active = serverState.jobs.find((j) => j.state === "queued" || j.state === "running");
    showSessionEmpty(tr("empty.no_clips_title"), active
      ? tr("empty.job_running", { kind: label(ui.lang, "job.kind.", active.kind),
        state: active.stage ? label(ui.lang, "job.stage.", active.stage) : label(ui.lang, "job.state.", active.state) })
      : row ? tr("empty.session_state", { state: label(ui.lang, "session.state.", row.state) })
        : tr("empty.session_no_row"), !active);
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
  const s = `${ui.lang}\n${sig}`;  // a language switch rebuilds every cached block (L13)
  if (node.dataset.sig === s) return;
  node.dataset.sig = s;
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
      chip(tr("filter.mine", { n: kept.length }), ui.mineFilter === "mine", set("mineFilter", "mine")),
      chip(tr("filter.noted", { n: noted }), ui.mineFilter === "noted", set("mineFilter", "noted"))]);
  } else {
    const f = [["all", c.all], ["undecided", c.undecided], ["kept", c.kept], ["rejected", c.rejected]];
    setKids($("filters"), JSON.stringify(["review", f, ui.filter]), () => f.map(([k, n]) =>
      chip(tr(`filter.${k}`, { n }), ui.filter === k, set("filter", k))));
  }
}

function statusBadge(clip) {
  const s = status(clip);
  const b = el("span", null, "badge");
  let text = "";  // undecided: no badge (D99), only the saving spinner while a write is in flight
  if (s.kind === "kept") {
    b.dataset.state = "kept";
    text = tr("status.kept");
  } else if (s.kind === "rejected") {
    b.dataset.variant = "secondary";
    text = label(ui.lang, "mark.", s.value);
  } else if (s.kind === "conflict") {
    b.dataset.variant = "outline";
    b.classList.add("conflict");
    text = tr("status.badge_conflict");
  } else {
    b.dataset.variant = "outline";
  }
  b.textContent = s.others ? tr("status.badge_others", { status: text }) : text;
  if (ui.pending.has(clip.clip_id)) b.append(" ", el("span", null, "spinner"));
  return b;
}

const firstLine = (clip) => {
  const n = ownNote(clip) || clip.notes[0];
  return n ? n.value.split("\n")[0] : "";
};

function renderList() {
  const list = shown();
  const sig = JSON.stringify([ui.lang, ui.selectedId, [...ui.pending], list.map((c) =>
    [c.clip_id, c.own_mark, c.marks, c.heard, firstLine(c)])]);
  if (sig === listSig) return;  // polls must not rebuild the list (scroll, hover)
  listSig = sig;
  if (!list.length) {
    const box = el("div", null, "list-empty");
    box.append(el("p", tr("card.nothing_found")), button(tr("card.reset_filter"), () => {
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
    const gap = showGaps(ui.tab, ui.filter) ? gapBefore(list, i, GAP_MIN_S) : null;
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
      dot.title = tr("card.heard");
      aside.append(dot);
    }
    // in my clips every row is kept: only extra states earn a badge there
    const pending = ui.pending.has(c.clip_id);
    const s = status(c);
    if ((s.kind !== "undecided" || pending) && (ui.tab !== "mine" || s.others || pending)) aside.append(statusBadge(c));
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
  if (asking) $("rechoose-question").textContent = tr("rechoose.question", { mark: label(ui.lang, "mark.", ui.rechoose) });
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
    $("summary-text").textContent = summaryText(c, ui.lang);
    return;
  }
  const clip = selected();
  setDecisionsEnabled(!!clip);
  renderRechoose();  // also with no clip: a question must not outlive its clip
  if (!clip) {
    $("clip-title").textContent = tr(ui.tab === "mine" ? "card.no_kept" : "card.no_clip");
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
  const sub = tr("card.sub", { i: idx + 1, n: view.clips.length, status: statusText(clip, ui.lang) });
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
  $("note-label").textContent = tr(own ? "note.label_own" : "note.label_add");
  $("note-box").hidden = ui.noteClipId !== clip.clip_id;
  const showOwn = own && ui.noteClipId !== clip.clip_id;
  setKids($("note-view"), JSON.stringify([clip.clip_id, showOwn && own.value, ui.expanded]), () => {
    if (!showOwn) return [];
    const parts = [el("p", own.value, `note-text${ui.expanded ? "" : " clamped"}`)];
    if (own.value.length > 160 || own.value.split("\n").length > 2) {
      parts.push(button(tr(ui.expanded ? "note.collapse" : "note.more"), () => {
        ui.expanded = !ui.expanded;
        render();
      }, "link", "sm"));
    }
    return parts;
  });
  const others = clip.notes.filter((n) => !n.own);
  setKids($("other-notes"), JSON.stringify(others.map((n) => n.value)), () => others.map((n) => {
    const box = el("div", null, "other-note");
    box.append(el("p", tr("note.other_run"), "muted small"), el("p", n.value, "note-text"));
    return box;
  }));
  $("clip-alert").hidden = !(ui.audio === "error" && playingId === clip.clip_id);
  $("clip-alert-text").textContent = ui.audioError ? errText(ui.lang, ui.audioError) : "";
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
  if (clip) $("bar").setAttribute("aria-valuetext", tr("player.valuetext", { pos: fmtPos(t), dur: fmtPos(dur || clip.duration_s) }));
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
  const { shown, hidden } = splitHidden(st.sessions);
  if (ui.deleteAsk && !hidden.some((s) => s.session_id === ui.deleteAsk)) ui.deleteAsk = null;
  const marks = shown.map((s) => sessionJobMark(st.jobs, s.session_id));
  const markSig = marks.map((m) => m && [m.kind, m.job.job_id, m.job.state, m.job.stage]);  // not log_tail: no rebuild per poll
  const busy = st.sessions.map((s) => sessionBusy(st.jobs, s.session_id));
  const list = $("sessions");
  const had = document.activeElement;  // a rebuild by poll keeps focus, as in the job log
  const fkey = had && list.contains(had) ? had.dataset.focus : null;
  setKids(list, JSON.stringify([st.sessions, runs, markSig, busy, ui.hiddenOpen, ui.deleteAsk]), () => [
    ...(shown.length ? shown.map((s, i) => {
    const row = el("div", null, "lib-row");
    const head = el("div", null, "row");
    const busyRow = sessionBusy(st.jobs, s.session_id);  // not disabled: a click says why (D94)
    const hide = button("", () => (busyRow ? fail(codedError({ code: "http_session_busy", params: {} }))
      : setHidden(s.session_id, true)), "ghost", "icon-sm", "x");
    hide.classList.add("idea-del", "session-hide");
    hide.setAttribute("aria-label", `${tr("lib.hide")}: ${sessionLabel(s, ui.lang)}`);  // one name per row
    hide.title = tr("lib.hide");
    hide.setAttribute("aria-disabled", String(busyRow));
    hide.dataset.focus = `${s.session_id}:hide`;
    head.append(el("strong", sessionLabel(s, ui.lang)),
      el("span", s.n_clips !== null
        ? tr("lib.session_clips", { state: label(ui.lang, "session.state.", s.state), n: s.n_clips })
        : label(ui.lang, "session.state.", s.state), "muted"));
    const mark = marks[i];  // D85: the job state lives on the session row too; buttons stay in the log
    if (mark && mark.kind === "running") {
      head.append(el("span", tr("lib.session_running", { stage: mark.job.stage
        ? label(ui.lang, "job.stage.", mark.job.stage) : label(ui.lang, "job.state.", mark.job.state) }), "badge session-running"));
      head.lastChild.dataset.variant = "secondary";
    } else if (mark) {
      const b = el("button", tr("lib.session_failed"), "badge session-failed");
      b.type = "button";
      b.dataset.variant = "destructive";
      b.addEventListener("click", () => openJobLog(mark.job.job_id));
      head.append(b);
    }
    head.append(hide);  // last in the DOM as on screen: Tab meets the label first
    row.append(head);
    const acts = el("div", null, "row");
    if (s.n_clips) {
      acts.append(button(tr(s.session_id === sid ? "lib.opened" : "lib.open"), () => {
        ui.screen = "session";
        openSession(s.session_id).catch(fail);
        render();
      }, s.session_id === sid ? "secondary" : undefined));
    }
    if (s.can_process && s.state !== "clips") {
      acts.append(button(tr("lib.process"), () => submit("process", { session_id: s.session_id }), "outline", "sm", "cog"));
    } else if (!s.can_process && s.state === "clips") {
      acts.append(el("span", tr("lib.listen_only"), "muted small"));
    }
    row.append(acts);
    if (s.session_id === sid && view && view.runs.length > 1) {
      const runs = el("div", null, "row run-switch");
      const list = el("div", null, "row");  // wrapped buttons line up under the first, not the label
      runs.append(el("span", tr("lib.run"), "muted small"), list);
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
  }) : [el("p", tr(`lib.${emptySessionsKey(st.sessions)}`), "muted")]),
    ...hiddenRows(hidden, st)]);
  if (fkey && !list.contains(document.activeElement)) {  // its row moved away: the toggle or a cross
    const all = [...list.querySelectorAll("[data-focus]")];
    const again = all.find((e) => e.dataset.focus === fkey) || all.find((e) => e.dataset.focus === "hidden-toggle")
      || all.find((e) => !e.disabled);
    if (again) again.focus({ preventScroll: true });
  }

  // Inputs being typed into are kept: re-render inbox only when its content or the language changed,
  // and carry typed starts over by (source, chain files) (L13).
  const inbox = $("inbox");
  const sig = JSON.stringify([ui.lang, st.inbox]);
  if (inbox.dataset.sig !== sig) {
    inbox.dataset.sig = sig;
    const typed = new Map([...inbox.querySelectorAll("input[data-key]")].map((i) => [i.dataset.key, i.value]));
    const had = document.activeElement;  // the start being typed keeps focus and caret
    const focused = had && had.dataset && had.dataset.key !== undefined && inbox.contains(had)
      ? { key: had.dataset.key, from: had.selectionStart, to: had.selectionEnd } : null;
    const parts = [];
    for (const src of st.inbox.sources) {
      const box = el("div", null, "lib-row");
      box.append(el("strong", src.source === "" ? tr("inbox.root") : `inbox/${src.source}/`));
      if (src.error) box.append(el("div", errText(ui.lang, src.error), "error"));
      for (const ch of src.chains) {
        box.append(el("div", chainText(ch, ui.lang)));
        const input = el("input", null, "input");
        input.type = "text";
        input.dataset.key = `${src.source}\n${ch.files.join("\n")}`;  // chain numbers shift after an import
        input.value = startValue(typed, input.dataset.key, ch.start_hint);
        input.placeholder = tr("inbox.start_placeholder");
        input.setAttribute("aria-label", tr("inbox.start_aria"));
        const row = el("div", null, "row");
        row.append(input, button(tr("inbox.import"), () => submit("import", {
          source: src.source, chain: ch.chain, files: ch.files, start: input.value.trim() || null,
        }), undefined, "sm", "folder-input"));
        box.append(row);
      }
      parts.push(box);
    }
    for (const name of st.inbox.backups) {
      const row = el("div", null, "lib-row");
      const line = el("div", null, "row");
      line.append(el("span", name), button(tr("inbox.restore"), () => submit("restore", { name }), "outline"));
      row.append(line);
      parts.push(row);
    }
    if (!parts.length) parts.push(el("p", tr("inbox.empty"), "muted"));
    inbox.replaceChildren(...parts);
    const again = focused && [...inbox.querySelectorAll("input[data-key]")].find((i) => i.dataset.key === focused.key);
    if (again) {
      again.focus({ preventScroll: true });
      again.setSelectionRange(focused.from, focused.to);
    }
  }

  renderJobs(st);

  $("backup-info").textContent = st.last_backup
    ? tr("backup.last", { time: backupTime(st.last_backup, ui.lang) }) : tr("backup.none");
  renderIdeas();
}

// D79-D83: the job log, newest first. Open logs, their scroll and focus survive a rebuild by poll.
function renderJobs(st) {
  const box = $("jobs");
  if (ui.cancelAsk && !st.jobs.some((j) => j.job_id === ui.cancelAsk && (j.state === "queued" || j.state === "running"))) {
    ui.cancelAsk = null;  // the job ended while asked: Esc belongs to others again
  }
  for (const j of st.jobs) {  // a running log stays open after the job ends: its last line is the news
    if (j.state === "running" && !ui.logOpen.has(j.job_id)) ui.logOpen.set(j.job_id, true);
  }
  const had = document.activeElement;
  const fkey = had && box.contains(had) ? had.dataset.focus : null;
  const sig = JSON.stringify([st.jobs, st.sessions.map((s) => [s.session_id, s.name]), [...ui.logOpen],
    ui.cancelAsk, [...ui.groupOpen], jobTick(st.jobs, Date.now())]);
  setKids(box, sig, () => (st.jobs.length ? journalRows(st.jobs).map((r) => (r.group ? groupEl(r.group, st)
    : jobEl(r.job, st))) : [el("li", tr("jobs.none"), "muted jobs-none")]));
  const said = jobAnnouncements(lastJobs, st.jobs, st.sessions, ui.lang);
  if (said.length) $("jobs-live").textContent = said.join(". ");
  lastJobs = st.jobs;
  for (const pre of box.querySelectorAll(".job-log")) {
    const id = pre.dataset.job;
    pre.scrollTop = ui.logPinned.get(id) === false ? ui.logScroll.get(id) || 0 : pre.scrollHeight;
  }
  if (fkey && !box.contains(document.activeElement)) {
    const again = [...box.querySelectorAll("[data-focus]")].find((e) => e.dataset.focus === fkey);
    if (again) again.focus({ preventScroll: true });
  }
}

const logIsOpen = (j) => (ui.logOpen.has(j.job_id) ? ui.logOpen.get(j.job_id) : j.state === "running");

function jobEl(j, st) {
  const id = j.job_id;
  const cls = { open: "job-open", moot: "job-moot", retried: "job-moot", resolved: "job-resolved" }[j.failure]
    || (j.state === "done" ? "job-done" : "");
  const row = el("li", null, `lib-row job-row ${cls}`.trim());
  row.id = `job-${id}`;
  row.tabIndex = -1;  // D84: the red badge brings focus here
  row.dataset.focus = id;
  const head = el("div", null, "job-head");
  const line = el("span", jobLine(j, st.sessions, ui.lang), "job-line");
  const note = { resolved: "jobs.resolved", retried: "jobs.retried",
    moot: j.kind === "restore" ? "jobs.moot_archive" : "jobs.moot_source" }[j.failure];
  if (note) line.append(el("span", tr(note), "job-note"));
  head.append(el("time", jobClock(j, ui.lang), "job-time"), line,
    el("span", jobDuration(j, new Date(), ui.lang), "job-dur"));
  const acts = el("span", null, "job-acts");
  const open = logIsOpen(j) && j.log_tail.length > 0;
  const act = (b, cls) => {
    b.classList.add(cls);
    b.dataset.focus = `${id}:${cls}`;
    acts.append(b);
    return b;
  };
  if (j.log_tail.length) {
    const b = act(button(`${tr("jobs.log")} ${open ? "▾" : "▸"}`, () => {
      ui.logOpen.set(id, !open);
      renderLibrary();
    }, "ghost"), "job-log-btn");
    b.setAttribute("aria-expanded", String(open));
    if (open) b.setAttribute("aria-controls", `log-${id}`);  // only an element that exists
  }
  if (j.failure === "open" && !j.retrying) act(button(tr("jobs.retry"), (e) => retryJob(j, e.currentTarget), "outline"), "job-retry");
  if ((j.state === "queued" || j.state === "running") && j.kind !== "delete") {  // D94: no cancel
    if (ui.cancelAsk === id) {  // D83: two steps in the row, like deleting an idea
      acts.append(el("span", tr("jobs.cancel_question")));
      act(button(tr("common.yes"), (e) => cancelJob(id, e.currentTarget), "destructive"), "job-yes");
      act(button(tr("common.no"), () => askCancel(null), "outline"), "job-no");
    } else {
      act(button(tr("jobs.cancel"), () => askCancel(id), "outline"), "job-cancel");
    }
  }
  head.append(acts);
  row.append(head);
  if (j.error) row.append(el("div", errText(ui.lang, j.error), "error"));
  if (open) {
    const pre = el("pre", null, "job-log");
    pre.id = `log-${id}`;
    pre.dataset.job = id;
    pre.dataset.focus = `${id}:log`;
    pre.tabIndex = 0;  // a scrollable region is reachable by keyboard
    pre.setAttribute("aria-label", tr("jobs.log_aria", { kind: label(ui.lang, "job.kind.", j.kind),
      subject: jobSubject(j, st.sessions, ui.lang) }));
    for (const l of logLines(j.log_tail, j.state === "failed")) pre.append(el("span", l.text, l.cls || null), "\n");
    pre.addEventListener("scroll", () => {
      ui.logPinned.set(id, pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 2);
      ui.logScroll.set(id, pre.scrollTop);
    });
    row.append(pre);
  }
  return row;
}

// D81: done backups in a row
function groupEl(group, st) {
  const key = groupKey(group);
  const open = ui.groupOpen.has(key);
  const row = el("li", null, "lib-row job-group job-done");
  const head = el("div", null, "job-head");
  const b = button(`${groupLine(group, ui.lang)} ${open ? "▾" : "▸"}`, () => {
    if (open) ui.groupOpen.delete(key);
    else ui.groupOpen.add(key);
    renderLibrary();
  }, "ghost");
  b.setAttribute("aria-expanded", String(open));
  b.dataset.focus = `${key}:group`;
  head.append(el("time", jobClock(group[0], ui.lang), "job-time"), b);
  row.append(head);
  if (open) {
    const items = el("ul", null, "job-group-items");
    items.append(...group.map((j) => jobEl(j, st)));
    row.append(items);
  }
  return row;
}

// D84: open the library at a job, its log open, focus on its row
function openJobLog(jobId) {
  ui.logOpen.set(jobId, true);
  ui.rechoose = null;
  ui.ideaDelete = null;
  ui.screen = "library";
  render();
  const row = $(`job-${jobId}`);
  if (!row) return;
  row.scrollIntoView({ block: "center" });
  row.focus({ preventScroll: true });
}

function askCancel(id) {
  const was = ui.cancelAsk;
  ui.cancelAsk = id;
  renderLibrary();
  const back = id ? $(`job-${id}`).querySelector(".job-no") : was && $(`job-${was}`) && $(`job-${was}`).querySelector(".job-cancel");
  if (back) back.focus({ preventScroll: true });
}

async function cancelJob(id, b) {
  b.disabled = true;  // a double click must not send a second request
  ui.cancelAsk = null;
  try {
    await api("POST", `/api/jobs/${id}/cancel`, {});
    await poll();
  } catch (e) {
    fail(e);
  }
}

async function retryJob(j, b) {  // D82: the server takes kind and params from the failed job
  b.disabled = true;  // a double click must not send a second request
  try {
    await api("POST", `/api/jobs/${j.job_id}/retry`, { job_id: newId() });
    toast(tr("toast.job_submitted"), label(ui.lang, "job.kind.", j.kind));
    await poll();
  } catch (e) {
    b.disabled = false;  // D96: a failed request does not change the jobs, so no rebuild re-enables it
    fail(e);
  }
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
    const yes = button(tr("common.yes"), () => deleteIdea(idea.event_id), "destructive");
    yes.classList.add("idea-yes");
    yes.disabled = ui.ideaDeleting;  // a double click must not send a second delete
    const no = button(tr("common.no"), () => askDeleteIdea(null), "outline");
    no.classList.add("idea-no");
    ask.append(el("span", tr("idea.delete_question")), yes, no);
    head.append(ask);
  } else {
    const x = button("", () => askDeleteIdea(idea.event_id), "ghost", "icon-sm", "x");
    x.classList.add("idea-del");
    x.setAttribute("aria-label", tr("idea.delete"));
    head.append(x);
  }
  li.append(head, el("p", idea.value));
  return li;
}

// D91: «Hidden: N · show», then muted rows with «Restore» and a two-step «Delete…» (D83)
function hiddenRows(hidden, st) {
  if (!hidden.length) return [];
  const foot = el("div", null, "row hidden-foot muted small");
  const toggle = button(tr(ui.hiddenOpen ? "lib.hidden_hide" : "lib.hidden_show"), () => {
    ui.hiddenOpen = !ui.hiddenOpen;
    ui.deleteAsk = null;  // Esc must not go to a question nobody sees
    renderLibrary();
  }, "link");
  toggle.setAttribute("aria-expanded", String(ui.hiddenOpen));
  toggle.dataset.focus = "hidden-toggle";
  foot.append(el("span", tr("lib.hidden_count", { n: hidden.length })), toggle);
  if (!ui.hiddenOpen) return [foot];
  return [foot, ...hidden.map((s) => {
    const row = el("div", null, "lib-row session-hidden");
    const head = el("div", null, "row");
    head.append(el("strong", sessionLabel(s, ui.lang)));
    const busy = sessionBusy(st.jobs, s.session_id);
    const mark = (b, key) => {
      b.dataset.focus = `${s.session_id}:${key}`;
      b.disabled = busy;
      return b;
    };
    if (ui.deleteAsk === s.session_id) {
      const ask = el("span", null, "idea-ask");
      ask.append(el("span", tr("lib.delete_question")),
        mark(button(tr("common.yes"), (e) => deleteSession(s.session_id, e.currentTarget), "destructive"), "yes"),
        mark(button(tr("common.no"), () => askDelete(null), "outline"), "no"));
      head.append(ask);
    } else {
      head.append(mark(button(tr("lib.unhide"), () => setHidden(s.session_id, false), "outline"), "unhide"),
        mark(button(tr("lib.delete"), () => askDelete(s.session_id), "outline"), "delete"));
    }
    row.append(head);
    if (s.error) row.append(el("div", errText(ui.lang, s.error), "error"));
    return row;
  })];
}

async function setHidden(id, hidden) {  // D92: a journal event; the server refuses a busy session
  try {
    await api("POST", "/api/journal", { event_id: newId(), kind: hidden ? "session_hidden" : "session_shown",
      session_id: id });
    await poll();
  } catch (e) {
    fail(e);
  }
}

function askDelete(id) {
  const was = ui.deleteAsk;
  ui.deleteAsk = id;
  renderLibrary();
  const key = id ? `${id}:no` : was && `${was}:delete`;
  const back = key && [...$("sessions").querySelectorAll("[data-focus]")].find((e) => e.dataset.focus === key);
  if (back) back.focus({ preventScroll: true });
}

function deleteSession(id, b) {
  b.disabled = true;  // a double click must not send a second delete
  ui.deleteAsk = null;
  return submit("delete", { session_id: id });
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
    toast(tr("toast.idea_not_deleted"), errText(ui.lang, errDesc(e)), true);
  } finally {
    ui.ideaDeleting = false;
    renderIdeas();
  }
}

async function submit(kind, params) {
  try {
    await api("POST", "/api/jobs", { job_id: newId(), kind, params });
    toast(tr("toast.job_submitted"), label(ui.lang, "job.kind.", kind));
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
  ui.audioError = null;
  renderPlayer();
  try {
    const r = await fetch(`/api/clip/${clip.clip_id}`, {
      headers: { Authorization: `Bearer ${KEY}` }, signal: ctl.signal,
    });
    if (!r.ok) throw codedError({ code: "http_status", params: { status: r.status } });
    const blob = await r.blob();
    if (ctl !== audioCtl) return;  // a newer clip was selected meanwhile
    audioUrl = URL.createObjectURL(blob);
    p.src = audioUrl;
    ui.audio = "ready";
    if (ui.playWhenReady) await p.play().catch(() => {});
  } catch (e) {
    if (e.name === "AbortError" || ctl !== audioCtl) return;
    ui.audio = "error";
    ui.audioError = e.descriptor || { code: "offline", params: {} };  // fetch or blob failed
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
    ui.noteStatus = null;
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
    if (!r.saved) throw codedError({ code: "not_saved", params: {} });
    return r;
  });
  queue = p.catch(() => {});
  return p;
}

// Optimistic: the clip shows the new decision at once; a failed write puts the old one back.
function setMark(clip, value) {
  const [at, atRun] = [sid, runId];  // the owner may be elsewhere when the write fails (D101)
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
    stickyToast(notSavedTitle(clip, value, ui.lang), errText(ui.lang, errDesc(e)),
      { label: tr("toast.go_to_clip"), run: () => goToClip(at, atRun, clip.clip_id) }, clip.clip_id);
    render();
    throw e;
  });
}

// D101 «к клипу»: the clip's session on Review, the clip selected and silent, filter reset if it hides it.
async function goToClip(sessionId, run, clipId) {
  if (sessionId !== sid || run !== runId || !ui.loaded) {
    await openSession(sessionId, run);
    if (sid !== sessionId || !view) return;  // a newer open replaced this one: it decides the screen
  }
  if (!view) return;
  ui.screen = "session";
  if (!view.clips.some((c) => c.clip_id === clipId)) {
    render();
    toast(tr("toast.clip_gone"));
    return;
  }
  if (!shown().some((c) => c.clip_id === clipId)) ui.tab = "review";
  if (!shown().some((c) => c.clip_id === clipId)) ui.filter = "all";
  ui.hideSummary = true;  // the owner asked for the clip, not the summary of a fully reviewed session
  select(clipId, { play: false });
  $("card").focus({ preventScroll: true });
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
  let play = false;
  if (ui.tab === "review") {
    const after = afterDecision(view.clips, clip, ui.filter);
    next = after.next;
    play = after.play;
    if (after.filter !== ui.filter) {
      ui.filter = after.filter;
      scrolledId = null;  // a new list: the row goes to the centre (D59)
    }
  } else if (value !== KEEP) {
    const after = shown();  // the rejected clip has left the list
    next = after.length ? after[Math.min(pos, after.length - 1)].clip_id : null;
    play = !!next && next !== clip.clip_id;
  }
  select(next, { play });  // D53: a decided clip never replays; nothing left → silence
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
    toast(tr("toast.nothing_to_undo"));
    return;
  }
  if (t.index < 0) {
    toast(tr("toast.undo_other_run"), tr("toast.undo_skipped"));
    return;
  }
  const clip = view.clips[t.index];
  setMark(clip, t.entry.prev).catch(() => ui.undo.push(t.entry));
  if (ui.tab === "review" && !shown().includes(clip)) ui.filter = "all";
  if (shown().includes(clip)) select(clip.clip_id, { play: false });
  else render();
}

function setTab(tab) {
  ui.tabSel[ui.tab] = ui.selectedId;
  ui.tab = tab;
  ui.rechoose = null;
  ui.ideaDelete = null;
  ui.screen = "session";
  const back = view && tabReturn(shown(), ui.tabSel[tab]);
  if (back) return select(back);  // silent: switching tabs never plays (D53)
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
  clearTimeout(noteTimer);
  ui.noteStatus = { key: "note.saving" };
  renderStatuses();
  try {
    const r = await journal("note", text, clip);
    clip.notes = clip.notes.filter((n) => !n.own);
    if (text) clip.notes.unshift({ event_id: r.event.event_id, value: text, own: true });
    if (ui.drafts.get(id) === text) ui.drafts.delete(id);
    // typed on while saving: keep the field open, the next Esc saves the rest
    if (ui.noteClipId === id && $("note").value === text) closeNote();
    ui.noteStatus = { key: "note.saved" };
    noteTimer = setTimeout(() => {
      ui.noteStatus = null;
      render();
    }, 2000);
    render();
    reloadView().catch(fail);
  } catch (e) {
    if (ui.noteClipId !== id) ui.drafts.set(id, text);  // left the clip: N on it brings the text back
    else {  // the text stays in the field; Esc retries
      ui.noteStatus = { key: "note.not_saved", params: { error: errDesc(e) } };
      renderStatuses();
    }
    toast(tr("toast.note_not_saved"), tr("toast.note_kept", { error: errDesc(e) }), true);
  } finally {
    ui.noteSaving = false;
  }
}

function openIdea() {
  ui.ideaStatus = null;
  renderStatuses();
  if (!$("idea-dialog").open) $("idea-dialog").showModal();
  $("idea").focus();
}

async function saveIdea() {
  const text = $("idea").value.trim();
  if (!text) return;
  ui.ideaStatus = { key: "note.saving" };
  renderStatuses();
  try {
    const body = { event_id: newId(), kind: "idea", value: text };
    if (sid) body.session_id = sid;
    await api("POST", "/api/journal", body);
    $("idea").value = "";
    $("idea-dialog").close();
    ui.ideaStatus = null;
    toast(tr("toast.idea_saved"));
    await poll();
  } catch (e) {
    ui.ideaStatus = { key: "note.not_saved", params: { error: errDesc(e) } };
    renderStatuses();
  }
}

function escape(target) {
  if ((ui.deleteAsk || ui.cancelAsk) && ui.screen === "library" && !document.querySelector("dialog[open]")) {
    const inCancel = ui.cancelAsk && $("jobs").contains(document.activeElement);
    const order = [ui.deleteAsk && (() => askDelete(null)), ui.cancelAsk && (() => askCancel(null))];
    for (const close of inCancel ? order : order.reverse()) if (close) close();  // one Esc closes both,
    return;  // the question the owner was in goes last and keeps the focus
  }
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
    const b = button(null, () => {
      if (ui.tab === "mine") pickRechoose(value);
      else decide(value);
    }, "outline", "default", DECISION_ICONS[value]);  // D98: all five alike until chosen
    b.dataset.value = value;
    const text = el("span", tr(`decision.${value}`));
    text.dataset.i18n = `decision.${value}`;  // applyStatic() relabels it on a language switch
    b.append(text, el("kbd", String(i + 1), "kbd"));
    return b;
  }));
  for (const b of $("tabs").querySelectorAll("[role=tab]")) {
    b.addEventListener("click", () => setTab(b.dataset.tab));
  }
  $("library-btn").addEventListener("click", toggleLibrary);
  $("help-btn").addEventListener("click", () => $("help-dialog").showModal());
  $("lang").addEventListener("click", () => setLang(ui.lang === "en" ? "ru" : "en"));
  $("lang").addEventListener("mousedown", (e) => e.preventDefault());  // L13: a note being typed keeps focus, no blur save
  $("help-close").addEventListener("click", () => $("help-dialog").close());
  $("theme").addEventListener("click", () => {
    ui.theme = document.documentElement.classList.contains("dark") ? "light" : "dark";
    applyTheme();
  });
  $("fail-badge").addEventListener("click", () => {
    const fb = serverState && failedBadge(serverState.jobs);
    if (fb) openJobLog(fb.job.job_id);
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
    api("POST", "/api/inbox/open", {}).catch((e) => toast(tr("toast.explorer_failed"), errText(ui.lang, errDesc(e)), true));
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
    counts, statusText, summaryText, chainText, splitHidden, emptySessionsKey, jobAnnouncements, jobTick, jobSubject, jobLine, jobClock, jobDuration, journalRows, groupKey, groupLine,
    failedBadge, sessionJobMark, logLines, nextUndecided, visibleClips, gapBefore, gapLabel, backupTime, ideaTime, undoEntry, undoTarget, DECISIONS, keyAction, launchRect, autoOpenTarget,
    afterOpenError, retryAfter, startValue, codedError, errDesc, notSavedTitle, afterDecision, tabReturn, showGaps };
} else {
  start();
}
