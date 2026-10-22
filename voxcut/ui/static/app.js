// VoxCut screen A (U7): a working draft of the form; the design session may replace it.
// Security (S2, S3, S7): the key lives only in this module, all data goes out via textContent.
"use strict";

// --- pure functions (no DOM; tested by node --test, D48) ---

const MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];

function parseSessionName(name) {
  const m = /^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})_(?:(\d+)h)?(?:(\d+)m)?$/.exec(name);
  if (!m || (!m[6] && !m[7]) || !MONTHS[+m[2] - 1]) return null;
  const length = [m[6] && `${+m[6]} ч`, m[7] && `${+m[7]} мин`].filter(Boolean).join(" ");
  return { date: `${+m[3]} ${MONTHS[+m[2] - 1]}`, time: `${m[4]}:${m[5]}`, length };
}

const clockOf = (startLocal) => (startLocal ? startLocal.slice(11, 19) : "");

function fmtDur(s) {
  const t = Math.round(s);
  return t < 60 ? `${t} с` : `${Math.floor(t / 60)} мин ${t % 60} с`;
}

function fmtPos(s) {
  const t = Math.floor(s);
  const mm = Math.floor((t % 3600) / 60);
  const ss = String(t % 60).padStart(2, "0");
  return t >= 3600 ? `${Math.floor(t / 3600)}:${String(mm).padStart(2, "0")}:${ss}` : `${mm}:${ss}`;
}

// --- screen ---

let KEY = null;
let state = null;          // GET /api/state
let view = null;           // GET /api/session/{sid}
let sid = null;
let runId = null;
let current = -1;          // index into view.clips
let currentId = null;      // clip_id of the current clip: indexes change when a run changes
let playingId = null;      // clip_id whose audio is in the player
let openSeq = 0;           // only the newest openSession response is applied
let queue = Promise.resolve();  // journal writes in key-press order
let audioCtl = null;       // AbortController of the clip fetch in flight
let audioUrl = null;       // object URL of the clip in the player

const MARKS = { "1": "моё", "2": "вокал", "3": "шум", "0": "" };
const COLORS = { "моё": "#2e7d32", "вокал": "#ef6c00", "шум": "#757575" };
const $ = (id) => document.getElementById(id);
const newId = () => crypto.randomUUID().replaceAll("-", "");

function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined && text !== null) e.textContent = String(text);
  if (cls) e.className = cls;
  return e;
}

function button(text, onClick) {
  const b = el("button", text);
  b.type = "button";
  b.addEventListener("click", onClick);
  return b;
}

function say(text) {
  $("msg").textContent = text;
}

async function api(method, path, body) {
  const opts = { method, headers: { Authorization: `Bearer ${KEY}` } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

async function start() {
  const code = location.hash.slice(1);
  history.replaceState(null, "", "/");
  try {
    const r = await fetch("/api/exchange", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
    if (!r.ok) throw new Error();
    KEY = (await r.json()).key;
  } catch {
    say("Откройте окно заново: VoxCut.cmd или make ui");
    return;
  }
  bind();
  await refresh();
  setInterval(() => refresh().catch((e) => say(e.message)), 2000);
}

// --- state, panel ---

async function refresh() {
  state = await api("GET", "/api/state");
  renderPanel();
  const sessions = state.sessions;
  if (sid === null && sessions.length) {
    const ready = sessions.filter((s) => s.state === "clips");
    await openSession((ready[ready.length - 1] || sessions[0]).session_id);
  } else if (sid !== null) {
    const row = sessions.find((s) => s.session_id === sid);
    if (row && view && row.n_clips !== null && view.clips.length !== row.n_clips && !runId) {
      await openSession(sid);  // processing finished: the newest run appeared
    }
  }
}

function renderPanel() {
  const sel = $("session");
  const keep = sel.value;
  sel.replaceChildren(...state.sessions.map((s) => {
    const o = el("option", `${s.name} (${s.state})`);
    o.value = s.session_id;
    return o;
  }));
  sel.value = sid || keep;

  $("sessions").replaceChildren(...state.sessions.map((s) => {
    const li = el("li");
    li.append(el("span", `${s.name}: ${s.state}`));
    if (s.n_clips !== null) li.append(el("span", `, клипов ${s.n_clips}`));
    if (s.can_process && s.state !== "clips") {
      li.append(" ", button("Обработать", () => submit("process", { session_id: s.session_id })));
    } else if (!s.can_process && s.state === "clips") {
      li.append(el("div", "только прослушивание (light)", "note"));
    }
    if (s.error) li.append(el("div", s.error, "error"));
    return li;
  }));

  const inbox = $("inbox");
  const parts = [];
  for (const src of state.inbox.sources) {
    const box = el("div", null, "source");
    box.append(el("div", src.source === "" ? "inbox/ (корень)" : `inbox/${src.source}/`, "name"));
    if (src.error) box.append(el("div", src.error, "error"));
    for (const ch of src.chains) {
      const row = el("div", null, "chain");
      row.append(el("div", `цепочка ${ch.chain}: ${ch.files.length} файлов, начало ${ch.start_local}, `
        + `${(ch.duration_s / 60).toFixed(1)} мин${ch.imported ? ", уже импортирована" : ""}`));
      const startInput = el("input");
      startInput.placeholder = "начало YYYY-MM-DD HH:MM (если часы сбиты)";
      row.append(startInput, button("Импортировать", () => submit("import", {
        source: src.source, chain: ch.chain, start: startInput.value.trim() || null,
      })));
      box.append(row);
    }
    parts.push(box);
  }
  for (const name of state.inbox.backups) {
    const row = el("div", null, "source");
    row.append(el("span", name), " ", button("Восстановить", () => submit("restore", { name })));
    parts.push(row);
  }
  if (!parts.length) parts.push(el("div", "пусто: положите файлы в папку inbox продукта", "note"));
  // Inputs being typed into are kept: re-render inbox only when its content changed.
  const sig = JSON.stringify(state.inbox);
  if (inbox.dataset.sig !== sig) {
    inbox.dataset.sig = sig;
    inbox.replaceChildren(...parts);
  }

  $("jobs").replaceChildren(...(state.jobs.length ? state.jobs.slice().reverse().map((j) => {
    const box = el("div", null, `job ${j.state}`);
    box.append(el("div", `${j.kind}: ${j.state}${j.stage ? `, этап ${j.stage}` : ""}`));
    if (j.error) box.append(el("div", j.error, "error"));
    if (j.log_tail.length) box.append(el("pre", j.log_tail.join("\n")));
    if (j.state === "queued" || j.state === "running") {
      box.append(button("Отменить", () => api("POST", `/api/jobs/${j.job_id}/cancel`, {})
        .then(refresh).catch((e) => say(e.message))));
    }
    return box;
  }) : [el("div", "задач нет", "note")]));

  $("backup-info").textContent = state.last_backup
    ? `последний бэкап: ${state.last_backup}` : "бэкапов ещё не было";
  $("ideas").replaceChildren(...state.ideas.slice().reverse().map((i) => el("li", i.value)));
  if (state.journal_warnings.length) say(state.journal_warnings.join("; "));
}

async function submit(kind, params) {
  try {
    await api("POST", "/api/jobs", { job_id: newId(), kind, params });
    say(`задача ${kind} поставлена`);
    await refresh();
  } catch (e) {
    say(e.message);
  }
}

// --- session view ---

async function openSession(id, run) {
  const seq = ++openSeq;
  const v = await api("GET", `/api/session/${encodeURIComponent(id)}${run ? `?run=${run}` : ""}`);
  if (seq !== openSeq) return;  // a newer openSession was started meanwhile
  sid = id;
  runId = run || null;
  view = v;
  $("session").value = id;
  const runBox = $("run-box");
  runBox.hidden = view.runs.length < 2;
  $("run").replaceChildren(...view.runs.map((r) => {
    const o = el("option", `${r.name} (${r.created})`);
    o.value = r.run_id;
    return o;
  }));
  $("run").value = view.run_id || "";
  const row = state && state.sessions.find((s) => s.session_id === id);
  $("session-info").textContent = view.clips.length
    ? `${id}: начало ${view.start_local || "?"}, клипов ${view.clips.length}`
    : `${id}: клипов пока нет${row ? ` (${row.state})` : ""}`;
  current = view.clips.findIndex((c) => c.clip_id === currentId);
  if (current < 0 && currentId !== null) {  // the clip is not in this run: nothing is current
    currentId = null;
    $("player").pause();
    $("note").value = "";
  }
  render();
}

function visible() {
  const f = $("filter").value;
  let rows = view.clips.map((c, i) => ({ c, i }));
  if (f === "unheard") rows = rows.filter((r) => !r.c.heard);
  if (f === "mine") rows = rows.filter((r) => r.c.marks.includes("моё"));
  if ($("sort").value === "p_max") rows.sort((a, b) => (b.c.p_max ?? 0) - (a.c.p_max ?? 0));
  return rows;
}

function render() {
  if (!view) return;
  const rows = visible();
  $("clips").tBodies[0].replaceChildren(...rows.map(({ c, i }) => {
    const tr = el("tr", null, i === current ? "current" : (c.heard ? "heard" : ""));
    tr.append(el("td", c.n), el("td", c.start_local || c.start_s.toFixed(1)),
      el("td", c.duration_s.toFixed(1)), el("td", c.p_max === null ? "" : c.p_max.toFixed(2)),
      el("td", c.marks.join(", ")));
    tr.addEventListener("click", () => select(i));
    return tr;
  }));
  const heard = view.clips.filter((c) => c.heard).length;
  $("counter").textContent = `прослушано ${heard} из ${view.clips.length}`;
  drawTimeline();
  const c = view.clips[current];
  $("clip-title").textContent = c ? `клип ${c.n}: ${c.start_local || ""}, ${c.duration_s.toFixed(1)} с`
    : "клип не выбран";
  $("clip-marks").textContent = c ? `отметки: ${c.marks.join(", ") || "нет"}` : "";
}

function drawTimeline() {
  const cv = $("timeline");
  const w = cv.clientWidth * devicePixelRatio;
  const h = cv.clientHeight * devicePixelRatio;
  cv.width = w;
  cv.height = h;
  const g = cv.getContext("2d");
  g.clearRect(0, 0, w, h);
  const total = view.duration_s || Math.max(1, ...view.clips.map((c) => c.end_s));
  view.clips.forEach((c, i) => {
    const x = (c.start_s / total) * w;
    const cw = Math.max(1, ((c.end_s - c.start_s) / total) * w);
    const mark = c.marks[0];
    g.fillStyle = mark ? COLORS[mark] || "#5e35b1" : (c.heard ? "#90a4ae" : "#29b6f6");
    g.fillRect(x, c.heard || mark ? h * 0.3 : h * 0.1, cw, c.heard || mark ? h * 0.6 : h * 0.8);
    if (i === current) {
      g.fillStyle = "#d32f2f";
      g.fillRect(x, 0, Math.max(2, devicePixelRatio * 2), h);
    }
  });
}

async function select(i) {
  if (!view || i < 0 || i >= view.clips.length) return;
  current = i;
  currentId = view.clips[i].clip_id;
  $("player").pause();  // the old clip must not end (and become «heard») as the new one
  $("note").value = (view.clips[i].notes[0] || {}).value || "";
  $("note-status").textContent = "";
  render();
  await play(view.clips[i]);
}

async function play(clip) {
  if (audioCtl) audioCtl.abort();
  audioCtl = new AbortController();
  try {
    const r = await fetch(`/api/clip/${clip.clip_id}`, {
      headers: { Authorization: `Bearer ${KEY}` }, signal: audioCtl.signal,
    });
    if (!r.ok) throw new Error(`клип недоступен (HTTP ${r.status})`);
    const url = URL.createObjectURL(await r.blob());
    if (audioUrl) URL.revokeObjectURL(audioUrl);
    audioUrl = url;
    playingId = clip.clip_id;
    $("player").src = url;
    await $("player").play().catch(() => {});
  } catch (e) {
    if (e.name !== "AbortError") say(e.message);
  }
}

function journal(kind, value, clip) {
  const body = { event_id: newId(), kind, value };
  if (clip) body.clip_id = clip.clip_id;
  const p = queue.then(async () => {
    const r = await api("POST", "/api/journal", body);
    if (!r.saved) throw new Error("не сохранено");
    return r;
  });
  queue = p.catch(() => {});
  return p;
}

async function mark(value) {
  const c = view && view.clips[current];
  if (!c) return;
  try {
    await journal("mark", value, c);
    await journal("heard", "", c);
    await openSession(sid, runId);
    say(value ? `отметка «${value}» сохранена` : "отметка снята");
  } catch (e) {
    say(e.message);
  }
}

function bind() {
  $("session").addEventListener("change", (e) => { currentId = null; openSession(e.target.value); });
  $("run").addEventListener("change", (e) => { currentId = null; openSession(sid, e.target.value); });
  $("sort").addEventListener("change", render);
  $("filter").addEventListener("change", render);
  $("timeline").addEventListener("click", (e) => {
    if (!view || !view.clips.length) return;
    const total = view.duration_s || Math.max(...view.clips.map((c) => c.end_s));
    const t = (e.offsetX / e.target.clientWidth) * total;
    let best = 0;
    view.clips.forEach((c, i) => {
      const d = Math.abs((c.start_s + c.end_s) / 2 - t);
      if (d < Math.abs((view.clips[best].start_s + view.clips[best].end_s) / 2 - t)) best = i;
    });
    select(best);
  });
  $("player").addEventListener("ended", () => {
    const c = view && view.clips.find((x) => x.clip_id === playingId);
    if (c && !c.heard) journal("heard", "", c).then(() => openSession(sid, runId))
      .catch((e) => say(e.message));
  });
  $("note-save").addEventListener("click", async () => {
    const c = view && view.clips[current];
    if (!c) return;
    $("note-status").textContent = "сохраняю…";
    try {
      await journal("note", $("note").value, c);
      $("note-status").textContent = "сохранено";
      await openSession(sid, runId);
    } catch (e) {
      $("note-status").textContent = `не сохранено: ${e.message}`;
    }
  });
  $("idea-save").addEventListener("click", async () => {
    const text = $("idea").value.trim();
    if (!text) return;
    try {
      const body = { event_id: newId(), kind: "idea", value: text };
      if (sid) body.session_id = sid;
      await api("POST", "/api/journal", body);
      $("idea").value = "";
      await refresh();
    } catch (e) {
      say(e.message);
    }
  });
  $("backup-full").addEventListener("click", () => submit("backup", { mode: "full" }));
  $("backup-light").addEventListener("click", () => submit("backup", { mode: "light" }));
  document.addEventListener("keydown", (e) => {
    if (["INPUT", "TEXTAREA"].includes(e.target.tagName) || e.ctrlKey || e.altKey) return;
    if (e.key in MARKS) { e.preventDefault(); mark(MARKS[e.key]); return; }
    if (!view) return;
    const order = visible().map((r) => r.i);
    const pos = order.indexOf(current);
    if (e.key === "ArrowRight") { e.preventDefault(); select(order[pos + 1] ?? order[0]); }
    if (e.key === "ArrowLeft") { e.preventDefault(); select(order[Math.max(0, pos - 1)]); }
  });
  window.addEventListener("resize", () => view && drawTimeline());
}

if (typeof module === "object") {
  module.exports = { parseSessionName, clockOf, fmtDur, fmtPos };
} else {
  start();
}
