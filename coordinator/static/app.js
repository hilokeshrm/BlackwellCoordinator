"use strict";
const API = "/api/v1";
const $ = (s, r = document) => r.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const REDUCED = matchMedia("(prefers-reduced-motion: reduce)").matches;

let snap = null;
let history = [];
let rangeMin = 30;
let adminToken = ssGet("bw_admin");
let schedTarget = null;
let showDesktop = false;
let firstSnap = true;
const seenEvents = new Set();

if (new URLSearchParams(location.search).has("app")) document.body.classList.add("app");

function ssGet(k) { try { return sessionStorage.getItem(k); } catch { return null; } }
function ssSet(k, v) { try { v == null ? sessionStorage.removeItem(k) : sessionStorage.setItem(k, v); } catch {} }

// ---------- motion helpers ----------
const easeOut = (t) => 1 - Math.pow(1 - t, 3);
function tween(el, to, { decimals = 0, dur = 900, fmt } = {}) {
  if (!el) return;
  const from = el._v ?? 0;
  el._v = to;
  const render = (v) => { el.textContent = fmt ? fmt(v) : v.toFixed(decimals); };
  if (REDUCED || Math.abs(to - from) < Math.pow(10, -decimals) / 2) { render(to); return; }
  cancelAnimationFrame(el._raf);
  const t0 = performance.now();
  const step = (now) => {
    const t = Math.min(1, (now - t0) / dur);
    render(from + (to - from) * easeOut(t));
    if (t < 1) el._raf = requestAnimationFrame(step);
  };
  el._raf = requestAnimationFrame(step);
}
function setText(el, text, animate = true) {
  if (!el || el.textContent === text) return;
  el.textContent = text;
  if (animate && !REDUCED) { el.classList.remove("swap"); void el.offsetWidth; el.classList.add("swap"); }
}
const STOPS = [[0, [45, 212, 191]], [45, [255, 195, 77]], [75, [255, 122, 61]], [100, [255, 77, 109]]];
function heat(p) {
  p = Math.max(0, Math.min(100, p));
  for (let i = 1; i < STOPS.length; i++) {
    const [b, cb] = STOPS[i], [a, ca] = STOPS[i - 1];
    if (p <= b) {
      const t = (p - a) / (b - a);
      return `rgb(${ca.map((c, k) => Math.round(c + (cb[k] - c) * t)).join(",")})`;
    }
  }
  return "rgb(255,77,109)";
}
function heatWord(u) { return u < 10 ? "Idle" : u < 40 ? "Warm" : u < 75 ? "Busy" : "Flat out"; }

// ---------- formatting ----------
function fmtDur(min) {
  if (min == null || !isFinite(min)) return "—";
  min = Math.max(0, min);
  if (min < 1) return "under 1m";
  if (min < 60) return `${Math.round(min)}m`;
  const h = Math.floor(min / 60), m = Math.round(min % 60);
  if (h < 48) return `${h}h ${String(m).padStart(2, "0")}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}
function fmtWhen(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000), day = new Date(d), today = new Date();
  day.setHours(0, 0, 0, 0); today.setHours(0, 0, 0, 0);
  const diff = Math.round((day - today) / 86400000);
  const t = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (diff === 0) return `Today ${t}`;
  if (diff === 1) return `Tomorrow ${t}`;
  if (diff === -1) return `Yesterday ${t}`;
  return `${d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })}, ${t}`;
}
function fmtAgo(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  return s < 60 ? `${Math.round(s)}s ago` : `${fmtDur(s / 60)} ago`;
}
const gb = (mb) => (mb / 1024).toFixed(mb >= 10240 ? 0 : 1);
function hue(str) { let h = 0; for (const c of str) h = (h * 31 + c.charCodeAt(0)) % 360; return h; }
function initials(n) { return n.split(/[\s._-]+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("") || "?"; }
const STATUS_LABEL = { registered: "Ready", queued: "Waiting for GPU", scheduled: "Scheduled", running: "Running", stopping: "Checkpointing", paused: "Paused", completed: "Completed", failed: "Failed", cancelled: "Cancelled" };
const FLASH = { running: "#FF7A3D", completed: "#2DD4BF", failed: "#FF4D6D", paused: "#A78BFA", queued: "#FFC34D", scheduled: "#FFC34D" };
const GROUP = {
  job: { label: "Training jobs", cls: "g-job", hint: "run by the coordinator" },
  service: { label: "Deployed services", cls: "g-service", hint: "always on" },
  vm: { label: "WSL / Docker VM", cls: "g-vm", hint: "containers and WSL workloads" },
  untracked: { label: "Untracked", cls: "g-untracked", hint: "started outside the coordinator" },
  desktop: { label: "Desktop & apps", cls: "g-desktop", hint: "Windows, browsers, editors" },
};

// ---------- network ----------
async function call(method, path, body, headers = {}) {
  const r = await fetch(API + path, { method, headers: { "Content-Type": "application/json", ...headers }, body: body ? JSON.stringify(body) : undefined });
  let data = null;
  try { data = await r.json(); } catch {}
  if (!r.ok) throw new Error((data && data.detail) || `${r.status} ${r.statusText}`);
  return data;
}
function toast(msg, bad = false) {
  const el = document.createElement("div");
  el.className = "toast" + (bad ? " bad" : "");
  el.textContent = msg;
  $("#toasts").append(el);
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 400); }, 4500);
}
let ws, wsRetry = 0, pollTimer;
function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${API}/stream`);
  ws.onopen = () => { wsRetry = 0; setConn(true); clearInterval(pollTimer); };
  ws.onmessage = (e) => onSnap(JSON.parse(e.data));
  ws.onclose = () => {
    setConn(false);
    clearInterval(pollTimer);
    pollTimer = setInterval(() => call("GET", "/state").then(onSnap).catch(() => {}), 3000);
    setTimeout(connect, Math.min(15000, 1000 * 2 ** wsRetry++));
  };
}
function setConn(live) {
  $("#connPill").classList.toggle("live", live);
  $("#connPill span").textContent = live ? "Live" : "Reconnecting";
}
async function loadHistory(draw = false) {
  try {
    const rows = await call("GET", `/gpu/history?minutes=${rangeMin}`);
    history = rows.map((r) => ({ ts: r.ts, util: r.util, mem: r.mem_total_mb ? (100 * r.mem_used_mb) / r.mem_total_mb : 0 }));
    const wrap = $(".chart-wrap");
    wrap.classList.remove("fade");
    if (draw && !REDUCED) { wrap.classList.add("draw"); setTimeout(() => wrap.classList.remove("draw"), 2000); }
    drawChart();
  } catch {}
}
async function refresh() { try { onSnap(await call("GET", "/state")); } catch {} }

// ---------- snapshot ----------
function onSnap(s) {
  snap = s;
  const g = s.gpu || {};
  if (g.ts) {
    const last = history[history.length - 1];
    if (!last || g.ts > last.ts) history.push({ ts: g.ts, util: g.utilization_percent, mem: g.memory_percent });
    const cutoff = Date.now() / 1000 - rangeMin * 60;
    while (history.length && history[0].ts < cutoff) history.shift();
  }
  renderHeader(s);
  renderGate(s);
  renderVram(s);
  drawChart();
  renderProcs(s);
  renderServices(s);
  renderTimeline(s);
  renderGrid(s);
  renderFeed(s);
  if (!$("#admin").hidden) fillProjectFilter();
  if (firstSnap) {
    firstSnap = false;
    requestAnimationFrame(() => document.body.classList.add("ready"));
    moveNavInd();
  }
}

function renderHeader(s) {
  const g = s.gpu || {};
  setText($("#gpuName"), g.name ? g.name + (s.mock_gpu ? " (test mode)" : "") : "Reading GPU…", false);
  const u = g.utilization_percent ?? 0;
  const col = heat(u);
  document.documentElement.style.setProperty("--heatc", col);
  document.documentElement.style.setProperty("--heat", col.replace("rgb", "rgba").replace(")", ",.55)"));
  document.documentElement.style.setProperty("--load", (u / 100).toFixed(3));
  document.body.classList.toggle("hot", u >= 60);
  tween($("#utilBig"), u);
  $("#gaugeArc").style.strokeDashoffset = 100 - Math.min(100, u);
  placeThreshold(s.threshold);
  setText($("#heatWord"), heatWord(u));
  $("#utilEff").innerHTML = s.service_util > 0.5 && s.exclude_service_load
    ? `Scheduling sees <b>${Math.round(s.util_effective)}%</b> · services' ${Math.round(s.service_util)}% excluded`
    : `${gb(g.memory_used_mb || 0)} of ${Math.round((g.memory_total_mb || 0) / 1024)} GB memory in use`;
  $("#thrVal").textContent = Math.round(s.threshold);
  tween($("#temp"), g.temperature_c ?? 0);
  tween($("#power"), g.power_w ?? 0);
  const waiting = s.projects.filter((p) => p.status === "queued" || p.status === "scheduled");
  tween($("#qCount"), waiting.length, { dur: 500 });
  const next = s.timeline.find((t) => t.status !== "running");
  $("#qNext").textContent = next ? `next ${fmtWhen(next.start).replace(/^Today /, "")}` : "";
  const night = s.night.active;
  $("#dayPill").classList.toggle("night", night);
  $("#dayText").textContent = night ? `Night window until ${String(s.night.end_hour).padStart(2, "0")}:00` : `Night window from ${String(s.night.start_hour).padStart(2, "0")}:00`;
  const j = s.jev || {}, jb = $("#jevStatus");
  jb.className = "jev" + (j.online ? " on" : "");
  const jhtml = s.decider === "heuristic" ? "<i></i>Rules only"
    : j.online ? "<i></i>Open-Jev online"
    : `<i></i><span title="${esc(j.last_error || "")}">Open-Jev ${j.online === false ? "offline" : "starting"}, rules in charge</span>`;
  if (jb._h !== jhtml) { jb.innerHTML = jhtml; jb._h = jhtml; }
  const visible = (s.gpu_procs || []).filter((p) => p.group !== "desktop");
  $("#navProcs").textContent = visible.length || "";
  const down = s.services.filter((x) => x.svc_state === "down").length;
  const nsv = $("#navSvc");
  nsv.textContent = down ? `${down} down` : s.services.length || "";
  nsv.classList.toggle("alert", !!down);
  const active = s.projects.filter((p) => ["running", "stopping", "queued", "scheduled"].includes(p.status)).length;
  $("#navJobs").textContent = s.projects.length ? (active ? `${active} active` : s.projects.length) : "";
}
function placeThreshold(thr) {
  const th = (225 - 270 * (thr / 100)) * Math.PI / 180;
  const l = $("#gaugeThr");
  const r1 = 42, r2 = 56;
  l.setAttribute("x1", 60 + r1 * Math.cos(th)); l.setAttribute("y1", 60 - r1 * Math.sin(th));
  l.setAttribute("x2", 60 + r2 * Math.cos(th)); l.setAttribute("y2", 60 - r2 * Math.sin(th));
}

function renderGate(s) {
  const gate = $("#gate");
  const running = s.projects.find((p) => p.status === "running" || p.status === "stopping");
  let state, why = "", fill = 0, cls = "";
  if (running) {
    cls = "running"; state = `${running.name} is training`;
    why = `<b>${esc(running.owner)}</b> · ${running.percent.toFixed(0)}% · finishes <b>${fmtWhen(running.projected_finish)}</b>`;
    fill = running.percent;
  } else if (!s.can_start_new_job) {
    cls = "busy"; state = "Not right now"; why = esc(s.reason_if_blocked || "");
  } else if (s.auto_candidate) {
    const left = Math.max(0, s.grace_seconds - s.idle_for);
    cls = "open"; state = left > 0 ? `Starting in ${left}s` : "Starting now";
    why = `<b>${esc(s.auto_candidate)}</b> starts once the GPU has been quiet for ${s.grace_seconds}s.`;
    fill = Math.min(100, (100 * s.idle_for) / Math.max(1, s.grace_seconds));
  } else {
    cls = "open"; state = "Yes, the GPU is free";
    const next = s.timeline.find((t) => t.status !== "running");
    why = next ? `Next planned: <b>${esc(next.name)}</b>, ${fmtWhen(next.start)} (${esc(next.reason)}).` : "Nothing queued. Start a job below.";
  }
  ["open", "busy", "running"].forEach((c) => gate.classList.toggle(c, c === cls));
  setText($("#gateState"), state);
  const w = $("#gateWhy"); if (w._h !== why) { w.innerHTML = why; w._h = why; }
  $("#gateFill").style.width = fill + "%";
  const d = s.last_decision, box = $("#decision");
  if (d) {
    box.hidden = false;
    const h = `Last decision: <b>${esc(d.project)}</b> <span class="src">(${esc(d.source)})</span> — ${esc(d.why)}`;
    if (box._h !== h) { box.innerHTML = h; box._h = h; }
  }
}

function renderVram(s) {
  const g = s.gpu || {};
  const total = g.memory_total_mb || 1, used = g.memory_used_mb || 0;
  tween($("#vramUsed"), used / 1024, { decimals: used >= 10240 ? 0 : 1 });
  $("#vramTot").textContent = Math.round(total / 1024);
  const procs = s.gpu_procs || [];
  const segs = [];
  const add = (key, label, mb, cls, dim) => { if (mb > 1) segs.push({ key, label, mb, cls, dim }); };
  const byLabel = (grp) => {
    const m = new Map();
    procs.filter((p) => p.group === grp).forEach((p) => m.set(p.label, (m.get(p.label) || 0) + p.mem_mb));
    return m;
  };
  byLabel("job").forEach((mb, l) => add("j:" + l, `<b>${esc(l)}</b> training`, mb, "g-job"));
  byLabel("service").forEach((mb, l) => add("s:" + l, `<b>${esc(l)}</b>`, mb, "g-service"));
  const sum = (grp) => procs.filter((p) => p.group === grp).reduce((a, p) => a + p.mem_mb, 0);
  add("vm", "<b>WSL / Docker VM</b>", sum("vm"), "g-vm");
  const un = procs.filter((p) => p.group === "untracked");
  add("un", `<b>Untracked</b> ${un.length ? "· " + esc(un.slice(0, 2).map((p) => p.name).join(", ")) : ""}`, sum("untracked"), "g-untracked");
  add("dt", "<b>Desktop & apps</b>", sum("desktop"), "g-desktop");
  const attributed = segs.reduce((a, x) => a + x.mb, 0);
  if (procs.length) add("other", "<b>Other / driver</b>", used - attributed, "g-desktop", true);
  else add("used", "<b>In use</b>", used, "g-vm");
  const reserve = s.reserved_vram_mb || 0;
  if (reserve) segs.push({ key: "reserve", label: "<b>Reserved</b> for services that are down", mb: reserve, cls: "reserve" });
  const free = Math.max(0, total - used - reserve);
  // keyed segments so widths animate instead of snapping
  const bar = $("#vramBar");
  const keep = new Set(segs.map((x) => x.key));
  [...bar.children].forEach((el) => { if (!keep.has(el.dataset.k)) { el.style.width = "0"; setTimeout(() => el.remove(), 900); } });
  segs.forEach((x, i) => {
    let el = bar.querySelector(`[data-k="${CSS.escape(x.key)}"]`);
    if (!el) { el = document.createElement("span"); el.dataset.k = x.key; bar.append(el); }
    el.className = x.cls;
    el.style.opacity = x.dim ? .45 : 1;
    el.style.order = i;
    el.title = `${x.label.replace(/<[^>]+>/g, "")}: ${gb(x.mb)} GB`;
    requestAnimationFrame(() => { el.style.width = `${(100 * x.mb) / total}%`; });
  });
  const legend = [...segs].sort((a, b) => b.mb - a.mb).map((x) => `<li><i class="${x.cls}" style="opacity:${x.dim ? .45 : 1}"></i><span class="lbl">${x.label}</span><span class="gb">${gb(x.mb)} GB</span></li>`).join("")
    + `<li><i class="g-free"></i><span class="lbl"><b>Free</b> for new jobs</span><span class="gb">${gb(free)} GB</span></li>`;
  const l = $("#vramLegend"); if (l._h !== legend) { l.innerHTML = legend; l._h = legend; }
  const fit = $("#vramFit");
  fit.className = "fit" + (free < 16 * 1024 ? " tight" : "");
  if (!fit._b) { fit.innerHTML = `A new job can use up to <b></b> GB right now.`; fit._b = fit.querySelector("b"); }
  tween(fit._b, free / 1024, { decimals: free >= 10240 ? 0 : 1 });
}

// ---------- chart ----------
function smooth(pts) {
  if (pts.length < 3) return pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("");
  let d = `M${pts[0][0].toFixed(1)},${pts[0][1].toFixed(1)}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[i - 1] || pts[i], p1 = pts[i], p2 = pts[i + 1], p3 = pts[i + 2] || p2;
    const c1 = [p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6];
    const c2 = [p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6];
    d += `C${c1[0].toFixed(1)},${c1[1].toFixed(1)} ${c2[0].toFixed(1)},${c2[1].toFixed(1)} ${p2[0].toFixed(1)},${p2[1].toFixed(1)}`;
  }
  return d;
}
function drawChart() {
  const svg = $("#chart"), wrap = $(".chart-wrap");
  const W = 1000, H = 250;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const now = Date.now() / 1000, t0 = now - rangeMin * 60;
  const x = (ts) => ((ts - t0) / (now - t0)) * W;
  const y = (v) => H - (Math.max(0, Math.min(100, v)) / 100) * (H - 4) - 2;
  const thr = snap ? snap.threshold : 10;
  const raw = history.filter((h) => h.ts >= t0);
  const buckets = Math.min(raw.length, 180);
  const pts = [];
  for (let b = 0; b < buckets; b++) {  // average into buckets so 2 s samples read as a trend, not noise
    const a = Math.floor((b * raw.length) / buckets), z = Math.max(a + 1, Math.floor(((b + 1) * raw.length) / buckets));
    const sl = raw.slice(a, z);
    pts.push({ ts: sl[sl.length - 1].ts, util: sl.reduce((s, p) => s + p.util, 0) / sl.length, mem: sl.reduce((s, p) => s + p.mem, 0) / sl.length });
  }
  const u = pts.map((p) => [x(p.ts), y(p.util)]);
  const m = pts.map((p) => [x(p.ts), y(p.mem)]);
  const utilPath = smooth(u);
  const area = u.length ? `${utilPath}L${u[u.length - 1][0].toFixed(1)},${H}L${u[0][0].toFixed(1)},${H}Z` : "";
  const stops = (op) => `<stop offset="0" stop-color="#2DD4BF" stop-opacity="${op[0]}"/><stop offset=".45" stop-color="#FFC34D" stop-opacity="${op[1]}"/><stop offset=".75" stop-color="#FF7A3D" stop-opacity="${op[2]}"/><stop offset="1" stop-color="#FF4D6D" stop-opacity="${op[3]}"/>`;
  svg.innerHTML = `
    <defs>
      <linearGradient id="thermalLine" gradientUnits="userSpaceOnUse" x1="0" y1="${H}" x2="0" y2="0">${stops([1, 1, 1, 1])}</linearGradient>
      <linearGradient id="thermalFill" gradientUnits="userSpaceOnUse" x1="0" y1="${H}" x2="0" y2="0">${stops([0.02, 0.16, 0.24, 0.3])}</linearGradient>
    </defs>
    <g class="grid">${[0, 25, 50, 75, 100].map((v) => `<line x1="0" x2="${W}" y1="${y(v)}" y2="${y(v)}"/>`).join("")}</g>
    <rect class="band" x="0" y="${y(thr)}" width="${W}" height="${H - y(thr)}"/>
    <line class="thr" x1="0" x2="${W}" y1="${y(thr)}" y2="${y(thr)}"/>
    <path class="area" d="${area}"/>
    ${$("#vramToggle").checked ? `<path class="vram" d="${smooth(m)}"/>` : ""}
    <path class="glow" d="${utilPath}"/>
    <path class="util" d="${utilPath}"/>
    <line class="cursor" id="cursorLine" x1="-10" x2="-10" y1="0" y2="${H}"/>`;
  wrap.querySelectorAll(".axis").forEach((a) => a.remove());
  const addAxis = (text, css, cls = "") => { const a = document.createElement("div"); a.className = "axis " + cls; a.style.cssText = css; a.textContent = text; wrap.append(a); };
  for (const v of [100, 50]) addAxis(v, `left:-2px;top:${(y(v) / H) * 100}%;transform:translate(-100%,-50%);padding-right:6px`);
  addAxis(`${Math.round(thr)}`, `left:-2px;top:${(y(thr) / H) * 100}%;transform:translate(-100%,-50%);padding-right:6px`, "thr-lbl");
  const labels = wrap.clientWidth < 500 ? 3 : 5;
  for (let i = 0; i <= labels; i++) {
    const ts = t0 + ((now - t0) * i) / labels;
    addAxis(i === labels ? "now" : new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }),
      `left:${(i / labels) * 100}%;bottom:-18px;transform:translateX(${i === labels ? "-100%" : i ? "-50%" : "0"})`);
  }
  let head = wrap.querySelector(".head");
  if (!head) { head = document.createElement("div"); head.className = "head"; wrap.append(head); }
  if (u.length) { head.hidden = false; head.style.left = `${(u[u.length - 1][0] / W) * 100}%`; head.style.top = `${(u[u.length - 1][1] / H) * 100}%`; }
  else head.hidden = true;
}
function chartHover(e) {
  const r = $(".chart-wrap").getBoundingClientRect();
  const fx = (e.clientX - r.left) / r.width;
  if (fx < 0 || fx > 1 || !history.length) return chartLeave();
  const now = Date.now() / 1000, t0 = now - rangeMin * 60, ts = t0 + fx * (now - t0);
  let best = history[0];
  for (const h of history) if (Math.abs(h.ts - ts) < Math.abs(best.ts - ts)) best = h;
  const tip = $("#tip");
  tip.hidden = false;
  tip.style.left = `${Math.min(90, Math.max(10, fx * 100))}%`;
  tip.innerHTML = `${new Date(best.ts * 1000).toLocaleTimeString()} · GPU <b style="color:${heat(best.util)}">${best.util.toFixed(0)}%</b> · VRAM ${best.mem.toFixed(0)}%`;
  const c = $("#cursorLine");
  if (c) { const px = ((best.ts - t0) / (now - t0)) * 1000; c.setAttribute("x1", px); c.setAttribute("x2", px); }
}
function chartLeave() { $("#tip").hidden = true; const c = $("#cursorLine"); if (c) { c.setAttribute("x1", -10); c.setAttribute("x2", -10); } }

// ---------- processes ----------
function ownerChip(p) {
  const g = GROUP[p.group] || GROUP.untracked;
  if (p.group === "job") return `<span class="owner-chip"><i class="g-job"></i>${esc(p.label)} <small>training · ${esc(p.owner)}</small></span>`;
  if (p.group === "service") return `<span class="owner-chip"><i class="g-service"></i>${esc(p.label)} <small>deployed service</small></span>`;
  return `<span class="owner-chip"><i class="${g.cls}"></i>${g.label} <small>${g.hint}</small></span>`;
}
function procRow(p, total) {
  const g = GROUP[p.group] || GROUP.untracked;
  return `<tr><td><span class="pname">${esc(p.name)}</span><span class="pid">${p.pid}</span>${p.cmd ? `<span class="cmd" title="${esc(p.cmd)}">${esc(p.cmd)}</span>` : ""}</td>
    <td>${ownerChip(p)}</td>
    <td class="num"><span class="membar"><span class="${g.cls}" style="width:${Math.min(100, (100 * p.mem_mb) / total)}%"></span></span>${gb(p.mem_mb)} GB</td>
    <td class="num" style="color:${p.util >= 0.5 ? heat(p.util) : "inherit"}">${p.util >= 0.5 ? p.util.toFixed(0) + "%" : "—"}</td></tr>`;
}
function renderProcs(s) {
  const procs = s.gpu_procs || [];
  const total = (s.gpu && s.gpu.memory_total_mb) || 1;
  const order = { job: 0, service: 1, untracked: 2, vm: 3, desktop: 4 };
  const main = procs.filter((p) => p.group !== "desktop").sort((a, b) => order[a.group] - order[b.group] || b.mem_mb - a.mem_mb);
  const desk = procs.filter((p) => p.group === "desktop");
  let html = main.map((p) => procRow(p, total)).join("");
  if (desk.length) {
    const mb = desk.reduce((a, p) => a + p.mem_mb, 0);
    html += `<tr class="fold"><td colspan="2"><span class="owner-chip"><i class="g-desktop"></i>Desktop &amp; apps <small>${desk.length} processes</small></span><button type="button" id="toggleDesk">${showDesktop ? "Hide" : "Show"}</button></td><td class="num">${gb(mb)} GB</td><td></td></tr>`;
    if (showDesktop) html += desk.map((p) => procRow(p, total)).join("");
  }
  if (!procs.length) html = `<tr class="empty-row"><td colspan="4">${s.mock_gpu ? "Per-process data isn't available in test mode." : "No process is holding GPU memory."}</td></tr>`;
  const tb = $("#procRows"); if (tb._h !== html) { tb.innerHTML = html; tb._h = html; }
}
$("#procRows").addEventListener("click", (e) => { if (e.target.id === "toggleDesk") { showDesktop = !showDesktop; $("#procRows")._h = ""; renderProcs(snap); } });

// ---------- services (keyed cards) ----------
function renderServices(s) {
  const grid = $("#svcGrid");
  if (!s.services.length) {
    const html = `<div class="empty"><h3>No deployed services registered</h3>
      <p>Register an always-on app (like AgentOS) so its health and GPU memory show up here: open its folder in Cursor and run <code>/activate</code> as a service.</p></div>`;
    if (grid._h !== html) { grid.innerHTML = html; grid._h = html; }
    return;
  }
  grid.querySelector(".empty")?.remove(); grid._h = "";
  const seen = new Set();
  s.services.forEach((v, i) => {
    seen.add(v.id);
    let card = grid.querySelector(`[data-sid="${v.id}"]`);
    if (!card) {
      card = document.createElement("article");
      card.dataset.sid = v.id;
      card.className = "panel svc card enter";
      card.addEventListener("animationend", (e) => { if (e.animationName === "cardin") card.classList.remove("enter"); });
      card.innerHTML = `<div class="svc-top"><div style="min-width:0"><h3 data-f="name"></h3><p data-f="path"></p></div><span class="health" data-f="health"><i></i><span></span></span></div>
        <dl class="svc-stats">
          <div><dt data-f="forLbl">Up for</dt><dd><span data-f="for">—</span><small data-f="checked"></small></dd></div>
          <div><dt>GPU memory</dt><dd><span data-f="mem">0</span> GB<small data-f="reserve"></small></dd></div>
          <div><dt>GPU load</dt><dd data-f="load">idle</dd></div>
        </dl>
        <div class="svc-check" data-f="check"></div><div class="svc-procs" data-f="procs"></div>`;
      grid.append(card);
    }
    if (grid.children[i] !== card) grid.insertBefore(card, grid.children[i] || null);
    const f = (k) => card.querySelector(`[data-f="${k}"]`);
    const state = v.svc_state || "unknown";
    if (card._state && card._state !== state && !REDUCED) { card.style.setProperty("--flash", state === "up" ? "#2DD4BF" : "#FF4D6D"); card.classList.remove("flash"); void card.offsetWidth; card.classList.add("flash"); }
    card._state = state;
    card.classList.remove("up", "down", "unknown"); card.classList.add(state);
    f("name").textContent = v.name;
    f("path").textContent = "‎" + v.path; f("path").title = v.path;
    const h = f("health"); h.className = "health " + state; setText(h.querySelector("span"), state === "up" ? "UP" : state === "down" ? "DOWN" : "UNKNOWN");
    f("forLbl").textContent = state === "down" ? "Down for" : "Up for";
    f("for").textContent = v.svc_since ? fmtDur((Date.now() / 1000 - v.svc_since) / 60) : "—";
    f("checked").textContent = `checked ${fmtAgo(v.svc_checked)}`;
    tween(f("mem"), v.vram_mb / 1024, { decimals: v.vram_mb >= 10240 ? 0 : 1 });
    f("reserve").textContent = v.est_vram_mb ? `reserves ${gb(v.est_vram_mb)} GB` : "learning";
    f("load").innerHTML = v.util >= 0.5 ? `<span style="color:${heat(v.util)}">${v.util.toFixed(0)}%</span><small>not counted for jobs</small>` : `idle<small>not counted for jobs</small>`;
    const chk = (v.health_url ? `<a href="${esc(v.health_url)}" target="_blank" rel="noopener">${esc(v.health_url)}</a>` : "process check") + ` <span>· ${esc(v.svc_detail || "")}</span>`;
    const c = f("check"); if (c._h !== chk) { c.innerHTML = chk; c._h = chk; }
    const procs = (v.processes.length ? v.processes.map((p) => `<span class="tag">${esc(p.name)} · ${gb(p.mem_mb)} GB</span>`).join("")
      : (v.process_match.length ? `<span class="tag">watching ${esc(v.process_match.join(", "))}</span>` : "")) + `<span class="tag">owner ${esc(v.owner)}</span>`;
    const pr = f("procs"); if (pr._h !== procs) { pr.innerHTML = procs; pr._h = procs; }
  });
  [...grid.querySelectorAll("[data-sid]")].forEach((c) => { if (!seen.has(c.dataset.sid)) c.remove(); });
}

// ---------- timeline ----------
function renderTimeline(s) {
  const el = $("#timeline");
  const now = Date.now() / 1000, t0 = now - 3600, t1 = now + 35 * 3600;
  const x = (ts) => ((Math.max(t0, Math.min(t1, ts)) - t0) / (t1 - t0)) * 100;
  let html = "";
  const d = new Date(t0 * 1000); d.setMinutes(0, 0, 0);
  const step = el.clientWidth < 700 ? 6 : 3;
  for (let h = 0; h <= 37; h++) {
    const ts = d.getTime() / 1000 + h * 3600;
    if (ts < t0 || ts > t1) continue;
    const hr = new Date(ts * 1000).getHours();
    if (hr % step === 0) html += `<div class="tl-tick" style="left:${x(ts).toFixed(2)}%"><span>${hr === 0 ? new Date(ts * 1000).toLocaleDateString([], { weekday: "short" }) : String(hr).padStart(2, "0") + ":00"}</span></div>`;
  }
  const { start_hour: sh, end_hour: eh } = s.night;
  const base = new Date(t0 * 1000); base.setHours(0, 0, 0, 0);
  for (let day = -1; day < 3; day++) {
    const ns = new Date(base); ns.setDate(ns.getDate() + day); ns.setHours(sh);
    const ne = new Date(ns); if (eh <= sh) ne.setDate(ne.getDate() + 1); ne.setHours(eh);
    const a = ns / 1000, b = ne / 1000;
    if (b < t0 || a > t1) continue;
    html += `<div class="tl-night" style="left:${x(a).toFixed(2)}%;width:${(x(b) - x(a)).toFixed(2)}%"><span>Night window</span></div>`;
  }
  html += `<div class="tl-now" style="left:${x(now).toFixed(2)}%" title="Now"></div>`;
  if (!s.timeline.length) html += `<div class="tl-empty">Nothing planned. Schedule a job and it appears here.</div>`;
  s.timeline.forEach((t, i) => {
    if (t.end < t0 || t.start > t1) return;
    const left = x(t.start), w = Math.max(0.6, x(t.end) - left);
    html += `<div class="tl-job ${t.status}" style="left:${left.toFixed(2)}%;width:${w.toFixed(2)}%;top:${30 + (i % 2) * 46}px;animation-delay:${i * 90}ms" title="${esc(t.name)} (${esc(t.owner)}) · ${fmtWhen(t.start)} → ${fmtWhen(t.end)} · ${esc(t.reason)}">${esc(t.name)} <small>${fmtDur(t.duration_min)}${t.end > t1 ? " →" : ""}</small></div>`;
  });
  // only rebuild when the plan changes (rounded), so bars don't replay their entrance every tick
  const key = JSON.stringify(s.timeline.map((t) => [t.project_id, t.status, Math.round(t.start / 60), Math.round(t.end / 60)])) + step + Math.round(now / 600);
  if (el._k !== key) { el.innerHTML = html; el._k = key; }
  else { const n = el.querySelector(".tl-now"); if (n) n.style.left = `${x(now).toFixed(2)}%`; }
  const long = s.timeline.find((t) => t.reason === "night window");
  const gap = s.timeline.find((t) => t.reason === "gap-fill");
  $("#planNote").textContent = gap && long ? `${gap.name} fits in before tonight; ${long.name} takes the night window.` : "Short jobs fill daytime gaps; long runs take the night.";
}

// ---------- job cards (keyed, persistent DOM so numbers and bars animate) ----------
const CARD_HTML = `
  <div class="card-top">
    <div class="avatar" data-f="av"></div>
    <div class="card-id"><h3 data-f="name"></h3><p data-f="path"></p></div>
    <span class="status" data-f="status"><i></i><span></span></span>
  </div>
  <div class="eta">
    <dl><dt data-f="etaLbl">Finishes</dt><dd><span data-f="eta">—</span><small data-f="etaSub"></small></dd></dl>
    <span class="pct"><span data-f="pct">0</span><small>%</small></span>
  </div>
  <div class="bar"><div data-f="bar"></div></div>
  <dl class="times">
    <div><dt>Remaining</dt><dd data-f="rem">—</dd></div>
    <div><dt>Elapsed on GPU</dt><dd data-f="el">—</dd></div>
  </dl>
  <div class="meta" data-f="tags"></div>
  <p class="msg" data-f="msg"></p>
  <p class="blocked" data-f="blocked"></p>
  <div class="actions" data-f="actions"></div>`;
function cardActions(p) {
  if (p.status === "running") return `<button class="warn" data-act="pause" data-id="${p.id}">Pause at checkpoint</button>`;
  if (p.status === "stopping") return `<button class="warn" disabled>Saving checkpoint…</button>`;
  const waiting = p.status === "queued" || p.status === "scheduled";
  const startLabel = p.percent > 0 && p.percent < 100 && p.supports_resume ? "Resume now" : "Start now";
  return `
    <button class="primary" data-act="start" data-id="${p.id}" ${p.can_start ? "" : "disabled"} title="${esc(p.blocked_reason || "")}">${startLabel}</button>
    <button class="ghost" data-act="schedule" data-id="${p.id}">${waiting ? "Reschedule" : "Schedule"}</button>
    ${waiting || p.status === "paused" ? `<button class="ghost" data-act="cancel" data-id="${p.id}">Remove from queue</button>` : ""}
    ${adminToken && !p.can_start ? `<button class="ghost" data-act="force" data-id="${p.id}" title="Admin override">Force start</button>` : ""}`;
}
function cardTags(p) {
  return `<span class="tag">${esc(p.owner)}</span>
    <span class="tag">${p.priority} priority</span>
    ${p.preferred_window !== "anytime" ? `<span class="tag">${p.preferred_window === "night_only" ? "night only" : "daytime only"}</span>` : p.is_long ? `<span class="tag">long · prefers night</span>` : ""}
    <span class="tag">~${Math.round(p.est_gpu_percent)}% GPU${p.est_vram_mb ? ` · ${gb(p.est_vram_mb)} GB` : ""}</span>
    ${p.supports_resume ? `<span class="tag ok">resumable</span>` : `<span class="tag warn">no resume yet · re-run /activate</span>`}
    ${p.resume_count ? `<span class="tag">resumed ${p.resume_count}×</span>` : ""}
    ${p.metrics && p.metrics.loss != null ? `<span class="tag">loss ${Number(p.metrics.loss).toFixed(4)}</span>` : ""}`;
}
function renderGrid(s) {
  const grid = $("#grid");
  if (!s.projects.length) {
    const html = `<div class="empty"><h3>No training jobs yet</h3>Jobs join the coordinator from Cursor:
      <ol><li>Open the training project in Cursor with the Blackwell MCP enabled.</li>
      <li>Type <code>/activate</code> and let the agent add checkpoint/resume.</li>
      <li>It appears here, ready to start now or schedule.</li></ol></div>`;
    if (grid._h !== html) { grid.innerHTML = html; grid._h = html; }
    return;
  }
  grid.querySelector(".empty")?.remove(); grid._h = "";
  const order = { running: 0, stopping: 0, queued: 1, scheduled: 2, paused: 3, registered: 4, failed: 5, completed: 6, cancelled: 7 };
  const projects = [...s.projects].sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9) || (a.projected_start || 9e12) - (b.projected_start || 9e12));
  const seen = new Set();
  projects.forEach((p, i) => {
    seen.add(p.id);
    let card = grid.querySelector(`[data-pid="${p.id}"]`);
    if (!card) {
      card = document.createElement("article");
      card.dataset.pid = p.id;
      card.innerHTML = CARD_HTML;
      card.style.animationDelay = `${i * 70}ms`;
      card.className = "panel card enter";
      card.addEventListener("animationend", (e) => { if (e.animationName === "cardin") card.classList.remove("enter"); });
    }
    if (grid.children[i] !== card) grid.insertBefore(card, grid.children[i] || null);
    const f = (k) => card.querySelector(`[data-f="${k}"]`);
    if (card._status && card._status !== p.status && !REDUCED) {
      card.style.setProperty("--flash", FLASH[p.status] || "#2DD4BF");
      card.classList.remove("flash"); void card.offsetWidth; card.classList.add("flash");
    }
    card._status = p.status;
    ["registered", "queued", "scheduled", "running", "stopping", "paused", "completed", "failed", "cancelled"].forEach((c) => card.classList.toggle(c, c === p.status));
    const av = f("av"); av.textContent = initials(p.owner); av.style.background = `hsl(${hue(p.owner)} 70% 72%)`; av.title = p.owner;
    f("name").textContent = p.name;
    f("path").textContent = "‎" + p.path; f("path").title = p.path;
    const st = f("status"); st.className = "status " + p.status; setText(st.querySelector("span"), STATUS_LABEL[p.status] || p.status);
    const running = p.status === "running" || p.status === "stopping", done = p.status === "completed";
    let lbl = "Finishes", eta = "Not scheduled", sub = "";
    if (done) { lbl = "Finished"; eta = fmtWhen(p.finished_at); }
    else if (p.projected_finish) {
      eta = fmtWhen(p.projected_finish);
      sub = running ? `in ${fmtDur(p.remaining_min)}` : p.projected_start ? `starts ${fmtWhen(p.projected_start).replace(/^Today /, "")}` : "";
    }
    f("etaLbl").textContent = lbl;
    setText(f("eta"), eta);
    f("etaSub").textContent = sub;
    const pct = p.percent || 0;
    tween(f("pct"), pct, { decimals: pct && pct < 10 ? 1 : 0 });
    f("bar").style.width = pct + "%";
    f("rem").textContent = done ? "0m" : (p.remaining_min < 1 ? "" : "~") + fmtDur(p.remaining_min);
    f("el").textContent = fmtDur((p.active_seconds || 0) / 60);
    const tags = cardTags(p); const tg = f("tags"); if (tg._h !== tags) { tg.innerHTML = tags; tg._h = tags; }
    f("msg").textContent = p.message || ""; f("msg").title = p.message || "";
    const blk = !p.can_start && !["running", "stopping", "completed"].includes(p.status) ? `Start is off: ${p.blocked_reason}` : "";
    f("blocked").textContent = blk; f("blocked").hidden = !blk;
    const acts = cardActions(p); const ac = f("actions"); if (ac._h !== acts) { ac.innerHTML = acts; ac._h = acts; }
  });
  [...grid.querySelectorAll("[data-pid]")].forEach((c) => { if (!seen.has(c.dataset.pid)) c.remove(); });
}

function renderFeed(s) {
  const feed = $("#feed");
  if (!s.events.length) {
    const h = `<li class="none">Nothing has happened since the coordinator started.</li>`;
    if (feed._h !== h) { feed.innerHTML = h; feed._h = h; }
    return;
  }
  const items = s.events.slice(0, 14);
  const html = items.map((e) => {
    const k = e.ts + e.message;
    const isNew = !firstSnap && !seenEvents.has(k);
    return `<li class="${e.level}${isNew ? " new" : ""}"><time>${new Date(e.ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}</time><span>${esc(e.message)}</span></li>`;
  }).join("");
  items.forEach((e) => seenEvents.add(e.ts + e.message));
  const key = items.map((e) => e.ts).join();
  if (feed._k !== key) { feed.innerHTML = html; feed._k = key; }
}

// ---------- nav indicator ----------
function moveNavInd() {
  const on = document.querySelector(".sections a.on"), ind = $("#navInd");
  if (!on || !ind) return;
  ind.style.left = on.offsetLeft + "px";
  ind.style.width = on.offsetWidth + "px";
}
const navLinks = [...document.querySelectorAll(".sections a")];
const spy = new IntersectionObserver((entries) => {
  entries.forEach((en) => {
    if (en.isIntersecting) {
      navLinks.forEach((a) => a.classList.toggle("on", a.getAttribute("href") === "#" + en.target.id));
      moveNavInd();
    }
  });
}, { rootMargin: "-40% 0px -55% 0px" });
document.querySelectorAll("main > section").forEach((sec) => spy.observe(sec));
const seenObs = new IntersectionObserver((entries) => entries.forEach((en) => { if (en.isIntersecting) en.target.classList.add("seen"); }), { threshold: .2 });
document.querySelectorAll(".block").forEach((b) => seenObs.observe(b));
addEventListener("resize", moveNavInd);

// ---------- actions ----------
$("#grid").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-act]");
  if (!b || b.disabled) return;
  const p = snap.projects.find((x) => x.id === b.dataset.id);
  try {
    if (b.dataset.act === "start") { await call("POST", `/projects/${p.id}/start`); toast(`Started ${p.name}`); }
    if (b.dataset.act === "force") { await call("POST", `/projects/${p.id}/start?force=true`, null, { "X-Admin-Token": adminToken }); toast(`Force-started ${p.name}`); }
    if (b.dataset.act === "pause") { await call("POST", `/projects/${p.id}/pause`); toast(`Pausing ${p.name} at its next checkpoint`); }
    if (b.dataset.act === "cancel") { await call("POST", `/projects/${p.id}/cancel`); toast(`Removed ${p.name} from the queue`); }
    if (b.dataset.act === "schedule") openSchedule(p);
    refresh();
  } catch (err) { toast(err.message, true); }
});
function nextAt(hour) {
  const d = new Date(); d.setHours(hour, 0, 0, 0);
  if (d <= new Date()) d.setDate(d.getDate() + 1);
  return d;
}
function openSchedule(p) {
  schedTarget = p;
  $("#schedName").textContent = p.name;
  $("#schedPri").value = p.priority;
  $("#schedWin").value = p.preferred_window;
  $("#tonightLbl").textContent = fmtWhen(nextAt(snap.night.start_hour) / 1000);
  $("#schedAt").value = new Date(Date.now() + 3600e3 - new Date().getTimezoneOffset() * 60e3).toISOString().slice(0, 16);
  $("#schedForm").elements.when.value = p.is_long ? "tonight" : "auto";
  updatePreview();
  $("#schedDlg").showModal();
}
function schedTime() {
  const w = $("#schedForm").elements.when.value;
  if (w === "tonight") return nextAt(snap.night.start_hour) / 1000;
  if (w === "morning") { const d = new Date(); d.setDate(d.getDate() + 1); d.setHours(snap.night.end_hour, 0, 0, 0); return d / 1000; }
  if (w === "custom") { const v = $("#schedAt").value; return v ? new Date(v) / 1000 : null; }
  return null;
}
function updatePreview() {
  if (!schedTarget) return;
  const at = schedTime(), p = schedTarget;
  $("#schedPreview").innerHTML = at
    ? `Starts ${fmtWhen(at)} if the GPU is free, finishes around <b>${fmtWhen(at + p.remaining_min * 60)}</b> (${fmtDur(p.remaining_min)}).`
    : `Runs ${fmtDur(p.remaining_min)} as soon as the planner finds a slot${p.projected_finish ? `; current plan finishes <b>${fmtWhen(p.projected_finish)}</b>` : ""}.`;
}
$("#schedForm").addEventListener("change", updatePreview);
$("#schedAt").addEventListener("focus", () => { $("#schedForm").elements.when.value = "custom"; updatePreview(); });
$("#schedDlg").addEventListener("close", async () => {
  if ($("#schedDlg").returnValue !== "ok" || !schedTarget) return;
  const p = schedTarget;
  try {
    await call("POST", `/projects/${p.id}/schedule`, { scheduled_at: schedTime(), priority: $("#schedPri").value, preferred_window: $("#schedWin").value });
    toast(`Scheduled ${p.name}`);
    refresh();
  } catch (err) { toast(err.message, true); }
});
document.querySelectorAll(".ranges button").forEach((b) => b.addEventListener("click", () => {
  if (b.classList.contains("on")) return;
  document.querySelectorAll(".ranges button").forEach((x) => x.classList.toggle("on", x === b));
  rangeMin = +b.dataset.min;
  $(".chart-wrap").classList.add("fade");
  setTimeout(() => loadHistory(true), 200);
}));
$("#vramToggle").addEventListener("change", drawChart);
$(".chart-wrap").addEventListener("mousemove", chartHover);
$(".chart-wrap").addEventListener("mouseleave", chartLeave);
document.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => b.closest("dialog").close("cancel")));

// ---------- admin (locked) ----------
let lastLogId = 0, logTimer = null;
$("#lockBtn").addEventListener("click", () => {
  if (adminToken) return openAdmin();
  $("#pw").value = ""; $("#pwErr").hidden = true;
  $("#loginDlg").showModal();
});
$("#loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const r = await call("POST", "/admin/login", { password: $("#pw").value });
    adminToken = r.token; ssSet("bw_admin", adminToken);
    $("#loginDlg").close();
    openAdmin();
  } catch {
    $("#pwErr").hidden = false;
    const f = $("#loginForm"); f.classList.remove("shake"); void f.offsetWidth; f.classList.add("shake");
    $("#pw").select();
  }
});
function fillProjectFilter() {
  const sel = $("#fProj"), cur = sel.value;
  const all = [...snap.projects, ...snap.services];
  const opts = `<option value="">All projects</option>` + all.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
  if (sel._o !== opts) { sel.innerHTML = opts; sel.value = cur; sel._o = opts; }
}
function openAdmin() {
  $("#admin").hidden = false;
  $("#lockBtn span").textContent = "Logs (unlocked)";
  if (snap) fillProjectFilter();
  reloadLogs();
  clearInterval(logTimer);
  logTimer = setInterval(() => { if ($("#fFollow").checked) pullLogs(); }, 2000);
}
function closeAdmin(lock = true) {
  $("#admin").hidden = true;
  clearInterval(logTimer);
  if (lock) { adminToken = null; ssSet("bw_admin", null); $("#lockBtn span").textContent = "Logs"; refresh(); }
}
$("#adminClose").addEventListener("click", () => closeAdmin(true));
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#admin").hidden) closeAdmin(false); });
function logQuery() {
  const q = new URLSearchParams();
  if ($("#fProj").value) q.set("project_id", $("#fProj").value);
  if ($("#fLvl").value) q.set("level", $("#fLvl").value);
  if ($("#fSearch").value) q.set("search", $("#fSearch").value);
  return q;
}
function reloadLogs() { lastLogId = 0; $("#logRows").innerHTML = ""; pullLogs(); }
async function pullLogs() {
  const q = logQuery(); q.set("after_id", lastLogId); q.set("limit", "1000");
  let rows;
  try { rows = await call("GET", `/admin/logs?${q}`, null, { "X-Admin-Token": adminToken }); }
  catch (err) { if (/admin/.test(err.message)) { closeAdmin(true); toast("Admin session expired. Unlock again.", true); } return; }
  if (!rows.length) return;
  lastLogId = rows[rows.length - 1].id;
  const names = Object.fromEntries([...(snap?.projects || []), ...(snap?.services || [])].map((p) => [p.id, p.name]));
  const raw = $("#fRaw").checked;
  $("#logRows").insertAdjacentHTML("beforeend", rows.map((r) => `<tr class="${r.level}"><td>${new Date(r.ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" })}</td><td>${esc(names[r.project_id] || (r.project_id ? r.project_id : "coordinator"))}</td><td>${r.level}</td><td>${raw ? esc(JSON.stringify(r)) : esc(r.message)}</td></tr>`).join(""));
  const body = $(".admin-body");
  if ($("#fFollow").checked) body.scrollTop = body.scrollHeight;
}
["#fProj", "#fLvl", "#fRaw"].forEach((s) => $(s).addEventListener("change", reloadLogs));
let searchT; $("#fSearch").addEventListener("input", () => { clearTimeout(searchT); searchT = setTimeout(reloadLogs, 300); });
$("#exportCsv").addEventListener("click", () => {
  const q = new URLSearchParams({ token: adminToken }); if ($("#fProj").value) q.set("project_id", $("#fProj").value);
  location.href = `${API}/admin/logs.csv?${q}`;
});
$("#adminRuns").addEventListener("click", async () => {
  const pane = $("#runsPane"), logs = $(".logs");
  if (!pane.hidden) { pane.hidden = true; logs.hidden = false; $("#adminRuns").textContent = "Runs"; return; }
  const runs = await call("GET", "/admin/runs", null, { "X-Admin-Token": adminToken });
  const names = Object.fromEntries(snap.projects.map((p) => [p.id, p.name]));
  pane.innerHTML = `<table><thead><tr><th>Project</th><th>Started</th><th>Duration</th><th>Status</th><th>Why started</th><th>Progress</th><th>Avg GPU</th><th>Exit</th></tr></thead><tbody>${runs.map((r) => `<tr><td>${esc(names[r.project_id] || r.project_id)}</td><td>${fmtWhen(r.started_at)}</td><td>${fmtDur(((r.finished_at || Date.now() / 1000) - r.started_at) / 60)}</td><td>${esc(r.status)}</td><td>${esc(r.reason)}</td><td>${(r.start_percent || 0).toFixed(0)}% → ${r.end_percent != null ? r.end_percent.toFixed(0) + "%" : "…"}</td><td>${r.avg_gpu_percent != null ? r.avg_gpu_percent.toFixed(0) + "%" : "—"}</td><td>${r.exit_code ?? ""}</td></tr>`).join("")}</tbody></table>`;
  pane.hidden = false; logs.hidden = true; $("#adminRuns").textContent = "Logs";
});

// ---------- boot ----------
loadHistory(true);
refresh();
connect();
setInterval(drawChart, 5000);
