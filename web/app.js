/* Kidsout webpage */
"use strict";

const WEEKDAYS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"];
const LABELS = { sun: "SUN", mon: "MON", tue: "TUE", wed: "WED", thu: "THU", fri: "FRI", sat: "SAT" };

let state = null;          // last received apiState
let taPending = {};        // "device|weekday" -> accumulated delta (debounced)
let taTimers = {};

const STATUS_FLASH_MS = 1800;
let lastStatus = {};       // device -> last rendered deviceStatus (flash on change)
let statusFlashUntil = {}; // device -> timestamp until which the flash effect runs

const MINICHART_SLOTS = 20; // matches backend maxStateHistory, fixes dot spacing regardless of sample count

// ---- helpers ----

function fmtMin(min) {
  if (min <= 0) return "0m";
  const h = Math.floor(min / 60), m = min % 60;
  if (h === 0) return m + "m";
  if (m === 0) return h + "h";
  return h + "h" + String(m).padStart(2, "0") + "m";
}

function weekdayOrder(today) {
  const i = WEEKDAYS.indexOf(today);
  return [...WEEKDAYS.slice(i), ...WEEKDAYS.slice(0, i)];
}

async function post(path, body) {
  try {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) console.warn("POST", path, res.status);
  } catch (e) {
    console.warn("POST failed", path, e);
  }
}

// ---- TA plus/minus with client-side debounce/batch (~500ms) ----

function bumpTA(device, weekday, delta) {
  const key = device + "|" + weekday;
  taPending[key] = (taPending[key] || 0) + delta;

  // optimistic UI
  const d = state.devices[device].days[weekday];
  d.taMinutes = Math.max(0, Math.min(24 * 60, d.taMinutes + delta));
  d.trMinutes = Math.max(0, d.taMinutes - d.tuMinutes);
  render();

  clearTimeout(taTimers[key]);
  taTimers[key] = setTimeout(() => {
    const deltaSum = taPending[key];
    delete taPending[key];
    if (deltaSum) post(`/api/device/${device}/ta`, { weekday, deltaMinutes: deltaSum });
  }, 500);
}

// ---- rendering ----

// ---- device row order (remembered per-browser) ----

function deviceOrder(names) {
  let saved = [];
  try { saved = JSON.parse(localStorage.getItem("kidsout-device-order")) || []; } catch { /* ignore */ }
  const known = saved.filter((n) => names.includes(n));
  const rest = names.filter((n) => !known.includes(n)).sort();
  return [...known, ...rest];
}

function moveDevice(name, dir) {
  const names = deviceOrder(Object.keys(state.devices));
  const i = names.indexOf(name), j = i + dir;
  if (j < 0 || j >= names.length) return;
  [names[i], names[j]] = [names[j], names[i]];
  localStorage.setItem("kidsout-device-order", JSON.stringify(names));
  render();
}

function infoTextFor(dev) {
  if (dev.enforcementToggle === "enforcementOFF") return "FREE (ENFORCEMENTOFF)";
  if (dev.pauseToggle === "pauseON") return `BLOCK-PAUSED FOR ${dev.pauseMinutesRemaining}MIN`;
  return null;
}

function render() {
  if (!state) return;
  const order = weekdayOrder(state.today);
  const devices = deviceOrder(Object.keys(state.devices));

  // header
  const headrow = document.getElementById("headrow");
  while (headrow.children.length > 1) headrow.removeChild(headrow.lastChild);
  for (const wd of order) {
    const th = document.createElement("th");
    th.textContent = LABELS[wd];
    if (wd === state.today) th.classList.add("today");
    headrow.appendChild(th);
  }

  // body
  const tbody = document.getElementById("tbody");
  tbody.innerHTML = "";
  for (const name of devices) {
    const dev = state.devices[name];
    const tr = document.createElement("tr");
    tr.appendChild(renderDeviceHeader(name, dev));
    const info = infoTextFor(dev);
    for (const wd of order) {
      tr.appendChild(info !== null
        ? renderInfoCell(info, wd === state.today)
        : renderInteractiveCell(name, dev.days[wd], wd, wd === state.today));
    }
    tbody.appendChild(tr);
  }
}

function renderDeviceHeader(name, dev) {
  const th = document.createElement("th");
  th.className = "devcol";

  const nameEl = document.createElement("span");
  nameEl.className = "devname";
  if (dev.deviceStatus === "inUse") nameEl.classList.add("inUse");
  else if (dev.deviceStatus === "blockedPauseON") nameEl.classList.add("paused");
  else if (dev.deviceStatus.startsWith("blocked")) nameEl.classList.add("blocked");
  else if (dev.deviceStatus === "notInUse") nameEl.classList.add("notInUse");
  else if (dev.deviceStatus === "enforcementOFF") nameEl.classList.add("enforcementOFF");
  nameEl.textContent = name;
  th.appendChild(nameEl);

  const toggles = document.createElement("div");
  toggles.className = "togglerow";

  toggles.appendChild(renderMiniChart(dev.stateHistory));

  const btns = document.createElement("div");
  btns.className = "togglebtns";

  // enforcement-toggle: show the OPPOSITE state's emoji (the action); glow when enforcementOFF
  const enfBtn = document.createElement("button");
  enfBtn.className = "toggle";
  const enfOn = dev.enforcementToggle === "enforcementON";
  enfBtn.textContent = enfOn ? "\u{1F512}" : "\u{1F513}";
  enfBtn.title = enfOn ? "Set free-mode (never block)" : "Re-enable enforcement";
  if (!enfOn) enfBtn.classList.add("glow");
  // ghost while paused: pause and enforcement modes are mutually exclusive (own button stays live)
  if (enfOn && dev.pauseToggle === "pauseON") enfBtn.classList.add("ghost");
  enfBtn.onclick = () =>
    post(`/api/device/${name}/enforcement`, { toggle: enfOn ? "enforcementOFF" : "enforcementON" });
  btns.appendChild(enfBtn);

  // pause-toggle: same emoji both states; glow when pauseON; ghost when TF/TR block
  const pauseBtn = document.createElement("button");
  pauseBtn.className = "toggle";
  pauseBtn.textContent = "\u23EF\uFE0F";
  pauseBtn.title = dev.pauseToggle === "pauseON" ? "Unpause device" : "Pause-block device for 20min";
  if (dev.pauseToggle === "pauseON") pauseBtn.classList.add("glow");
  if (dev.deviceStatus === "blockedNoTime" || dev.deviceStatus === "blockedNotInTimeframe")
    pauseBtn.classList.add("ghost");
  // ghost while free-mode: pausing is meaningless when enforcement is off (own button stays live)
  if (dev.enforcementToggle === "enforcementOFF" && dev.pauseToggle !== "pauseON")
    pauseBtn.classList.add("ghost");
  pauseBtn.onclick = () =>
    post(`/api/device/${name}/pause`, { toggle: dev.pauseToggle === "pauseON" ? "pauseOFF" : "pauseON" });
  btns.appendChild(pauseBtn);

  toggles.appendChild(btns);
  th.appendChild(toggles);

  if (dev.pauseToggle === "pauseON") {
    const left = document.createElement("span");
    left.className = "pauseleft";
    left.textContent = dev.pauseMinutesRemaining + "min left";
    left.title = left.textContent; // full text on hover/long-press since it may be truncated
    th.appendChild(left);
  }

  const st = document.createElement("span");
  st.className = "devstatus";
  st.textContent = dev.deviceStatus;
  st.title = dev.deviceStatus; // full text on hover/long-press since it may be truncated
  // glowing flash on status change (skip first paint)
  if (lastStatus[name] !== undefined && lastStatus[name] !== dev.deviceStatus)
    statusFlashUntil[name] = Date.now() + STATUS_FLASH_MS;
  lastStatus[name] = dev.deviceStatus;
  const flashLeft = (statusFlashUntil[name] || 0) - Date.now();
  if (flashLeft > 0) {
    st.classList.add("flash");
    // resume mid-animation across re-renders instead of restarting
    st.style.animationDelay = `-${STATUS_FLASH_MS - flashLeft}ms`;
  }
  th.appendChild(st);

  const orderRow = document.createElement("div");
  orderRow.className = "orderrow";
  const upBtn = document.createElement("button");
  upBtn.className = "orderbtn";
  upBtn.textContent = "\u25B2";
  upBtn.title = "Move row up";
  upBtn.onclick = () => moveDevice(name, -1);
  const downBtn = document.createElement("button");
  downBtn.className = "orderbtn";
  downBtn.textContent = "\u25BC";
  downBtn.title = "Move row down";
  downBtn.onclick = () => moveDevice(name, 1);
  orderRow.append(upBtn, downBtn);
  th.appendChild(orderRow);
  return th;
}

// builds a small SVG sparkline of stateHistory: -1 unknown (bottom), 0 down (mid), 1 up (top);
// fixed dot spacing (one dot-diameter apart), right-anchored so newest ticks push older ones left (snake scroll);
// the whole chart fades out towards the left (oldest ticks) via a shared mask
function renderMiniChart(history) {
  const svgNS = "http://www.w3.org/2000/svg";
  const w = 80, h = 18, pad = 3;
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("class", "minichart");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("preserveAspectRatio", "none");

  const g = document.createElementNS(svgNS, "g");
  g.setAttribute("mask", "url(#minichart-fade)");
  svg.appendChild(g);

  const yFor = (v) => h - pad - ((v + 1) / 2) * (h - 2 * pad);

  // faint reference gridlines for the 3 possible levels, drawn first so they sit behind the data
  for (const v of [1, 0, -1]) {
    const gl = document.createElementNS(svgNS, "line");
    const y = yFor(v);
    gl.setAttribute("x1", 0);
    gl.setAttribute("x2", w);
    gl.setAttribute("y1", y);
    gl.setAttribute("y2", y);
    gl.setAttribute("class", "minichart-grid");
    g.appendChild(gl);
  }

  const hist = history || [];
  if (hist.length === 0) return svg;

  const spacing = (w - 2 * pad) / (MINICHART_SLOTS - 1);
  const dotRadius = spacing / 2; // dot-to-dot separation equals one dot diameter
  const colorFor = (v) => (v === 1 ? "#00e676" : v === 0 ? "#ff1744" : "#607d8b");
  const n = hist.length;
  const xFor = (i) => w - pad - (n - 1 - i) * spacing;
  const points = hist.map((v, i) => [xFor(i), yFor(v)]);

  if (points.length > 1) {
    const poly = document.createElementNS(svgNS, "polyline");
    poly.setAttribute("points", points.map((p) => p.join(",")).join(" "));
    poly.setAttribute("class", "minichart-line");
    g.appendChild(poly);
  }
  points.forEach(([x, y], i) => {
    const c = document.createElementNS(svgNS, "circle");
    c.setAttribute("cx", x);
    c.setAttribute("cy", y);
    c.setAttribute("r", dotRadius);
    c.setAttribute("class", "minichart-dot");
    c.style.fill = colorFor(hist[i]);
    g.appendChild(c);
  });
  return svg;
}

function renderInfoCell(text, isToday) {
  const td = document.createElement("td");
  td.className = "cell info";
  if (isToday) td.classList.add("today");
  const wrap = document.createElement("div");
  wrap.className = "infotext";
  const span = document.createElement("span");
  span.textContent = text;
  wrap.appendChild(span);
  if (text.length > 14) wrap.classList.add("scroll"); // too big -> horizontal roll
  td.appendChild(wrap);
  return td;
}

function renderInteractiveCell(device, day, weekday, isToday) {
  const td = document.createElement("td");
  td.className = "cell";
  if (isToday) td.classList.add("today");

  const l1 = document.createElement("div");
  l1.className = "line1";
  const plus = document.createElement("button");
  plus.className = "pm";
  plus.textContent = "\u2795";
  plus.onclick = () => bumpTA(device, weekday, 10);
  const tr = document.createElement("span");
  tr.textContent = fmtMin(day.trMinutes);
  const minus = document.createElement("button");
  minus.className = "pm";
  minus.textContent = "\u2796";
  minus.onclick = () => bumpTA(device, weekday, -10);
  l1.append(plus, tr, minus);

  const l2 = document.createElement("div");
  l2.className = "line2";
  l2.textContent = `(${fmtMin(day.tuMinutes)})`;

  const l3 = document.createElement("div");
  l3.className = "line3";
  const tf = document.createElement("span");
  tf.textContent = `${day.tfStart}-${day.tfEnd} `;
  const cal = document.createElement("button");
  cal.className = "cal";
  cal.textContent = "\u{1F5D3}";
  cal.onclick = () => openTFModal(device, weekday, day);
  l3.append(tf, cal);

  td.append(l1, l2, l3);
  return td;
}

// ---- timeframe modal (24h HH:MM selects, no AM/PM) ----

const tfModal = document.getElementById("tfmodal");
const tfError = document.getElementById("tferror");
const tfConfirm = document.getElementById("tfconfirm");
let tfTarget = null;

function fillOptions(sel, max, step) {
  for (let v = 0; v < max; v += step) {
    const o = document.createElement("option");
    o.value = o.textContent = String(v).padStart(2, "0");
    sel.appendChild(o);
  }
}
const tfSel = {
  startH: document.getElementById("tfstart-h"),
  startM: document.getElementById("tfstart-m"),
  endH: document.getElementById("tfend-h"),
  endM: document.getElementById("tfend-m"),
};
fillOptions(tfSel.startH, 24, 1);
fillOptions(tfSel.endH, 24, 1);
fillOptions(tfSel.startM, 60, 5);
fillOptions(tfSel.endM, 60, 5);

function tfStartValue() { return `${tfSel.startH.value}:${tfSel.startM.value}`; }
function tfEndValue() { return `${tfSel.endH.value}:${tfSel.endM.value}`; }

function tfValidate() {
  const ok = tfStartValue() < tfEndValue();
  tfError.hidden = ok;
  tfConfirm.disabled = !ok;
}
for (const sel of Object.values(tfSel)) sel.oninput = tfValidate;

// setHM tolerates minutes not on the 5min grid (snaps down)
function setHM(hSel, mSel, hhmm) {
  const [h, m] = hhmm.split(":");
  hSel.value = h;
  mSel.value = String(Math.floor(parseInt(m, 10) / 5) * 5).padStart(2, "0");
}

function openTFModal(device, weekday, day) {
  tfTarget = { device, weekday };
  document.getElementById("tfmodal-title").textContent = `${device} \u2014 ${LABELS[weekday]} timeframe`;
  setHM(tfSel.startH, tfSel.startM, day.tfStart);
  setHM(tfSel.endH, tfSel.endM, day.tfEnd);
  tfValidate();
  tfModal.showModal();
}
document.getElementById("tfcancel").onclick = () => tfModal.close();
tfConfirm.onclick = () => {
  post(`/api/device/${tfTarget.device}/tf`, {
    weekday: tfTarget.weekday, tfStart: tfStartValue(), tfEnd: tfEndValue(),
  });
  tfModal.close();
};

// ---- help/legend modal ----

const helpModal = document.getElementById("helpmodal");
// non-modal show() so the table stays visible/peekable behind the corner panel
document.getElementById("helpbtn").onclick = () => helpModal.show();
document.getElementById("helpclose").onclick = () => helpModal.close();

// ---- SSE live updates (with reconnect) ----

function connect() {
  const es = new EventSource("/api/events");
  es.onmessage = (ev) => {
    state = JSON.parse(ev.data);
    render();
  };
  es.onerror = () => {
    es.close();
    setTimeout(connect, 3000);
  };
}
connect();
