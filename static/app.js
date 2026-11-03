"use strict";

const $ = (sel) => document.querySelector(sel);
const PAGES = ["cameras", "clips", "settings"];
const TITLES = { cameras: "Cameras", clips: "Clips", settings: "Settings" };
let currentView = "cameras";
let pollTimer = null;
let hls = null;
let liveKey = null;
let clipHours = 24;

// ---------- helpers ----------

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}

function icon(name, cls = "ic") {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("class", cls);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(ns, "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

let toastTimer = null;
function toast(message, isError = false) {
  const node = $("#toast");
  node.replaceChildren(icon(isError ? "alert" : "check"), document.createTextNode(message));
  node.className = isError ? "error" : "";
  node.hidden = false;
  node.style.animation = "none"; void node.offsetWidth; node.style.animation = "";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, isError ? 6000 : 2800);
}

async function busy(button, fn) {
  button.disabled = true;
  try { return await fn(); }
  catch (err) { toast(err.message, true); }
  finally { button.disabled = false; }
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v === true) node.setAttribute(k, "");
    else if (v !== false && v != null) node.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c != null && c !== false) node.append(c);
  return node;
}

function toMs(value) {
  if (!value) return NaN;
  return typeof value === "number" ? value * 1000 : Date.parse(value);
}

function timeAgo(value) {
  const t = toMs(value);
  if (Number.isNaN(t)) return "";
  const s = Math.round((Date.now() - t) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  const d = Math.round(s / 86400);
  return d === 1 ? "yesterday" : `${d} days ago`;
}

const timeFmt = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });
function dayLabel(t) {
  const d = new Date(t);
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const day = new Date(d); day.setHours(0, 0, 0, 0);
  const diff = Math.round((today - day) / 86400000);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
}

// ---------- navigation ----------

function show(view) {
  for (const v of ["login", "twofa", ...PAGES]) $(`#${v}-view`).hidden = v !== view;
  const signedIn = PAGES.includes(view);
  $("#tabs").hidden = !signedIn;
  $("#app").classList.toggle("signed-out", !signedIn);
  $("#navbar").hidden = !signedIn;
  $("#refresh-btn").hidden = view !== "cameras" && view !== "clips";
  $("#navbar-title").textContent = TITLES[view] || "";
  document.querySelectorAll(".tab").forEach((t) => {
    if (t.dataset.view === view) t.setAttribute("aria-current", "page");
    else t.removeAttribute("aria-current");
  });
  if (signedIn) {
    currentView = view;
    document.title = `${TITLES[view]} – FreeBlink`;
  } else {
    document.title = "FreeBlink";
  }
}

function openPage(view) {
  show(view);
  window.scrollTo({ top: 0 });
  if (view === "clips") loadClips();
  else if (view === "settings") loadSettings();
  else loadDevices();
}

document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => openPage(tab.dataset.view)));

// Collapse the large title into the bar, as iOS navigation bars do.
const onScroll = () => $("#navbar").classList.toggle("scrolled", window.scrollY > 30);
window.addEventListener("scroll", onScroll, { passive: true });

// ---------- auth ----------

async function boot() {
  const { state, error } = await api("/api/status");
  if (error) toast(error, true);
  route(state);
}

function route(state) {
  if (state === "ready") {
    openPage(currentView);
    clearInterval(pollTimer);
    pollTimer = setInterval(() => {
      if (document.hidden || $("#player").open) return;
      if (currentView === "cameras") loadDevices();
      else if (currentView === "clips") loadClips(true);
    }, 30000);
  } else {
    clearInterval(pollTimer);
    show(state === "needs_2fa" ? "twofa" : "login");
  }
}

$("#login-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const form = e.target;
  busy(form.querySelector("button"), async () => {
    const { state } = await api("/api/login", {
      method: "POST",
      body: { username: form.username.value, password: form.password.value },
    });
    form.password.value = "";
    route(state);
  });
});

$("#twofa-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const form = e.target;
  busy(form.querySelector("button"), async () => {
    const { state } = await api("/api/2fa", { method: "POST", body: { code: form.code.value } });
    form.code.value = "";
    route(state);
  });
});

$("#logout-btn").addEventListener("click", (e) =>
  busy(e.target, async () => route((await api("/api/logout", { method: "POST" })).state)));

$("#refresh-btn").addEventListener("click", async () => {
  const btn = $("#refresh-btn");
  if (btn.disabled) return;
  btn.classList.add("spinning");
  await busy(btn, async () => {
    render(await api("/api/refresh", { method: "POST" }));
    if (currentView === "clips") await loadClips(true);
    toast("Up to date");
  });
  btn.classList.remove("spinning");
});

// ---------- cameras ----------

async function loadDevices() {
  try {
    render(await api("/api/devices"));
  } catch (err) {
    toast(err.message, true);
    if (/signed in/i.test(err.message)) boot();
  }
}

const KIND_LABEL = { sync_module: "Sync Module", mini: "Blink Mini", doorbell: "Doorbell", camera: "Outdoor camera" };

function render({ systems }) {
  const root = $("#systems");
  if (!systems.length) {
    root.replaceChildren(el("div", { class: "empty" }, icon("video"),
      el("strong", {}, "No cameras"), el("p", {}, "This Blink account has no systems yet. Add cameras in the Blink app, then refresh.")));
    return;
  }
  root.replaceChildren(...systems.map(renderSystem));
}

function segmented(options, value, label, onChange) {
  const group = el("div", { class: "segmented", role: "radiogroup", "aria-label": label });
  for (const [val, text] of options) {
    const btn = el("button", { role: "radio", "aria-checked": String(val === value) }, text);
    btn.addEventListener("click", () => {
      if (btn.getAttribute("aria-checked") === "true") return;
      group.querySelectorAll("button").forEach((b) => b.setAttribute("aria-checked", String(b === btn)));
      onChange(val, group);
    });
    group.append(btn);
  }
  return group;
}

function renderSystem(sys) {
  const armed = !!sys.armed;
  const arm = segmented([[false, "Disarmed"], [true, "Armed"]], armed, `Arm ${sys.name}`, (val, group) => {
    group.querySelectorAll("button").forEach((b) => { b.disabled = true; });
    api(`/api/systems/${encodeURIComponent(sys.name)}/arm`, { method: "POST", body: { enabled: val } })
      .then((data) => { render(data); toast(`${sys.name} ${val ? "armed" : "disarmed"}`); })
      .catch((err) => { toast(err.message, true); loadDevices(); });
  });

  const count = sys.cameras.length;
  const statusText = sys.online === false ? "Offline" : armed ? "Armed" : "Disarmed";
  return el("section", { class: "system" },
    el("div", { class: "system-head" },
      el("div", {},
        el("h2", {}, sys.name),
        el("p", {}, el("span", { class: `status-dot${sys.online === false ? " warn" : armed ? "" : " off"}` }),
          `${statusText}, ${KIND_LABEL[sys.kind] || "System"}, ${count} ${count === 1 ? "camera" : "cameras"}`)),
      arm),
    el("div", { class: "cam-grid" }, sys.cameras.map(renderCamera)));
}

function wifiChip(strength) {
  if (strength == null) return null;
  // Blink reports either bars (0-5) or dBm (negative).
  const bars = strength < 0 ? (strength > -60 ? 3 : strength > -72 ? 2 : 1) : Math.min(3, Math.ceil(strength / 5 * 3));
  const label = strength < 0 ? `${strength} dBm` : `${strength}/5`;
  const chip = el("span", { class: "chip", title: `Wi-Fi ${label}`, "aria-label": `Wi-Fi ${["weak", "weak", "fair", "good"][bars]}` }, icon("wifi"));
  if (bars < 2) chip.classList.add("low");
  return chip;
}

function renderCamera(cam) {
  const media = el("button", { class: "cam-media", "aria-label": `Watch ${cam.name} live` });
  const img = el("img", { alt: "", loading: "lazy", src: `/api/cameras/${cam.key}/thumbnail?v=${cam.thumbnail_version}` });
  img.addEventListener("error", () => { img.replaceWith(el("span", { class: "no-image" }, "No picture yet")); });

  const chips = [];
  if (cam.temperature_f != null) chips.push(el("span", { class: "chip" }, `${cam.temperature_f}°`));
  const w = wifiChip(cam.wifi_strength);
  if (w) chips.push(w);
  if (cam.battery) {
    const low = cam.battery !== "ok";
    chips.push(el("span", { class: `chip${low ? " low" : ""}`, title: `Battery ${cam.battery}`, "aria-label": `Battery ${cam.battery}` },
      icon(low ? "battery-low" : "battery")));
  }

  const sub = KIND_LABEL[cam.kind] || cam.product_type || "Camera";
  const lastMotion = cam.last_record ? `Last motion ${timeAgo(cam.last_record)}` : "No recent motion";
  const play = el("span", { class: "play-badge", "aria-hidden": "true" }, icon("play"));
  media.append(...[img,
    cam.motion_detected && el("span", { class: "cam-motion chip red" }, "Motion"),
    chips.length && el("span", { class: "cam-chips" }, chips),
    el("span", { class: "cam-caption" }, el("strong", {}, cam.name), el("span", {}, sub)),
    play].filter(Boolean));
  media.addEventListener("click", () => {
    if (media.classList.contains("busy")) return;
    media.classList.add("busy");
    play.append(el("span", { class: "spinner" }));
    openLive(cam).catch((err) => toast(err.message, true)).finally(() => {
      media.classList.remove("busy");
      play.querySelector(".spinner")?.remove();
    });
  });

  const motionId = `motion-${cam.key}`;
  const motion = el("input", { type: "checkbox", class: "switch", id: motionId, role: "switch", checked: !!cam.motion_enabled });
  motion.addEventListener("change", () =>
    busy(motion, async () => {
      render(await api(`/api/cameras/${cam.key}/motion`, { method: "POST", body: { enabled: motion.checked } }));
      toast(`Motion detection ${motion.checked ? "on" : "off"} for ${cam.name}`);
    }).then(() => {}));

  const snap = el("button", { class: "btn-gray", "aria-label": `Take a new photo with ${cam.name}` }, icon("camera"), "New photo");
  snap.addEventListener("click", () =>
    busy(snap, async () => {
      snap.lastChild.textContent = "Taking…";
      try {
        render(await api(`/api/cameras/${cam.key}/snap`, { method: "POST" }));
        toast(`New photo from ${cam.name}`);
      } finally { snap.lastChild.textContent = "New photo"; }
    }));

  return el("article", { class: "cam" }, media,
    el("div", { class: "cam-controls" },
      el("div", { class: "row" },
        el("div", {},
          el("label", { class: "row-label", for: motionId }, "Motion detection"),
          el("div", { class: "row-sub" }, cam.motion_enabled ? "Records clips when armed" : "Off: no clips are recorded")),
        motion),
      el("div", { class: "row" },
        el("div", { class: "cam-meta" }, lastMotion),
        snap)));
}

// ---------- player ----------

function playUrl(url, isLive) {
  const video = $("#video");
  if (hls) { hls.destroy(); hls = null; }
  if (isLive && window.Hls && Hls.isSupported()) {
    hls = new Hls({ liveSyncDurationCount: 2, lowLatencyMode: false });
    hls.on(Hls.Events.ERROR, (_, data) => { if (data.fatal) setStatus("Stream ended. Close and start live view again."); });
    hls.loadSource(url);
    hls.attachMedia(video);
  } else {
    video.src = url; // Safari plays HLS natively; clips are plain MP4.
  }
  video.play().catch(() => {});
}

function setStatus(text, live = false) {
  const node = $("#player-status");
  node.replaceChildren(...(live ? [el("span", { class: "live-dot" })] : []), text);
}

function waiting(text, failed = false) {
  $("#video-wait").hidden = !text;
  $("#video-wait").classList.toggle("failed", failed);
  if (text) $("#video-wait-text").textContent = text;
}

function openSheet(title) {
  $("#player-title").textContent = title;
  $("#player-ai").hidden = true;
  $("#ptz").hidden = true;
  if (!$("#player").open) $("#player").showModal();
}

async function openLive(cam) {
  openSheet(cam.name);
  setStatus("Connecting");
  waiting("Waking up the camera. This takes 10 to 30 seconds.");
  liveKey = cam.key;
  try {
    const { playlist } = await api(`/api/cameras/${cam.key}/live`, { method: "POST" });
    if (liveKey !== cam.key) return; // closed while connecting
    waiting("");
    setStatus("Live", true);
    playUrl(playlist, true);
    if (cam.kind === "mini") detectMount(cam.key);
  } catch (err) {
    waiting("");
    if ($("#player").open) $("#player").close();
    throw err;
  }
}

function openClip(clip) {
  liveKey = null;
  openSheet(clip.ai?.title || clip.camera);
  const when = new Date(toMs(clip.created_at));
  setStatus(`${clip.camera}, ${dayLabel(when)} at ${timeFmt.format(when)}`);
  waiting(clip.source === "usb" ? "Loading clip from the Sync Module…" : "Loading clip…");
  const video = $("#video");
  const done = () => { waiting(""); video.removeEventListener("loadeddata", done); };
  video.addEventListener("loadeddata", done);
  video.onerror = () => waiting("This clip couldn't be loaded. Try again in a minute.", true);
  if (clip.ai?.description) {
    $("#player-ai").replaceChildren(icon("sparkle"), el("span", {}, clip.ai.description));
    $("#player-ai").hidden = false;
  }
  playUrl(`/api/clips/${clip.id}/video`, false);
}

$("#player-close").addEventListener("click", () => $("#player").close());
$("#player").addEventListener("click", (e) => { if (e.target === $("#player")) $("#player").close(); }); // backdrop
$("#player").addEventListener("close", () => {
  $("#ptz").hidden = true;
  waiting("");
  const video = $("#video");
  video.onerror = null;
  if (hls) { hls.destroy(); hls = null; }
  video.pause();
  video.removeAttribute("src");
  video.load();
  if (liveKey) {
    api(`/api/cameras/${liveKey}/live`, { method: "DELETE" }).catch(() => {});
    liveKey = null;
  }
});

// ---------- pan-tilt mount ----------

function showPosition(pos) {
  $("#ptz-pos").textContent = pos ? `Pan ${pos.pan}°, tilt ${pos.tilt}°${pos.moving ? ", moving" : ""}` : "";
}

async function detectMount(key) {
  // The mount announces its angle on the live session; no report means no mount.
  const state = await api(`/api/cameras/${key}/pantilt?wait=8`).catch(() => null);
  if (liveKey !== key || !state || !state.available) return;
  showPosition(state.position);
  $("#ptz").hidden = false;
}

$("#ptz").addEventListener("click", (e) => {
  const button = e.target.closest("button[data-act]");
  if (!button || !liveKey) return;
  busy(button, async () => {
    const { position } = await api(`/api/cameras/${liveKey}/pantilt`, {
      method: "POST", body: { action: button.dataset.act },
    });
    showPosition(position);
    if (button.dataset.act === "set_home") toast("Saved as home position");
  });
});

document.addEventListener("keydown", (e) => {
  if ($("#ptz").hidden || !$("#player").open) return;
  const act = { ArrowLeft: "left", ArrowRight: "right", ArrowUp: "up", ArrowDown: "down" }[e.key];
  if (!act) return;
  e.preventDefault();
  $(`#ptz button[data-act="${act}"]`).click();
});

// ---------- clips ----------

$("#clip-range").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-hours]");
  if (!btn || btn.getAttribute("aria-checked") === "true") return;
  $("#clip-range").querySelectorAll("button").forEach((b) => b.setAttribute("aria-checked", String(b === btn)));
  clipHours = Number(btn.dataset.hours);
  loadClips();
});

let clipsRequest = 0;
async function loadClips(quiet = false) {
  const root = $("#clips");
  const request = ++clipsRequest;
  if (!quiet) root.replaceChildren(el("div", { class: "clip-list" }, [1, 2, 3, 4].map(() => el("div", { class: "skeleton" }))));
  try {
    const { clips } = await api(`/api/clips?hours=${clipHours}`);
    if (request !== clipsRequest) return;
    if (!clips.length) {
      root.replaceChildren(el("div", { class: "empty" }, icon("clips"),
        el("strong", {}, "No clips"), el("p", {}, "Nothing was recorded in this period. New motion clips show up here within a minute.")));
      return;
    }
    const groups = new Map();
    for (const clip of clips) {
      const label = dayLabel(toMs(clip.created_at));
      if (!groups.has(label)) groups.set(label, []);
      groups.get(label).push(clip);
    }
    root.replaceChildren(...[...groups].map(([label, items]) =>
      el("section", { class: "day" },
        el("h2", { class: "list-header prominent" }, label),
        el("div", { class: "clip-list" }, items.map(renderClip)))));
  } catch (err) {
    if (request !== clipsRequest) return;
    root.replaceChildren(el("div", { class: "empty" }, icon("alert"), el("strong", {}, "Couldn't load clips"), el("p", {}, err.message)));
  }
}

function renderClip(clip) {
  // USB clips have to be fetched from the Sync Module before a preview exists (a few seconds each).
  const thumb = el("div", { class: "clip-thumb" }, el("span", {}, clip.source === "usb" ? "Loading…" : ""));
  const img = el("img", { src: `/api/clips/${clip.id}/thumbnail`, alt: "", loading: "lazy" });
  img.addEventListener("load", () => thumb.classList.add("loaded"));
  img.addEventListener("error", () => { img.remove(); thumb.firstChild.textContent = "No preview"; });
  thumb.prepend(img);
  thumb.append(el("span", { class: "play-mini" }, icon("play")));

  const t = toMs(clip.created_at);
  const ai = clip.ai;
  return el("button", { class: "clip-row", onclick: () => openClip(clip), "aria-label": `${ai?.title || "Motion"}, ${clip.camera}, ${timeFmt.format(t)}` },
    thumb,
    el("div", { class: "clip-text" },
      el("span", { class: "clip-title" }, ai?.title || "Motion"),
      el("div", { class: "clip-meta" },
        el("span", { class: "clip-camera" }, clip.camera),
        el("span", { class: "clip-time" }, timeFmt.format(t))),
      ai?.description
        ? el("p", { class: "clip-desc" }, ai.description)
        : el("p", { class: "clip-desc pending" }, Date.now() - t < 10 * 60000 ? "Describing…" : "No description")));
}

// ---------- settings ----------

function renderSettings(s, status) {
  const keyStatus = $("#key-status");
  keyStatus.className = `row-value ${s.anthropic_key_set ? "ok" : "bad"}`;
  keyStatus.replaceChildren(icon(s.anthropic_key_set ? "check" : "alert"),
    s.anthropic_key_set ? `Saved, ends in ${(s.anthropic_key_hint || "").replace("…", "")}` : "Not set");
  $("#key-remove").hidden = s.anthropic_key_source !== "dashboard";
  $("#ai-footer").firstChild.textContent =
    `Every new motion clip is described by ${modelName(s.model)} in a sentence or two and sent to your phone. Create a key at `;
  $("#topic-form").topic.value = s.ntfy_topic || "";
  $("#ntfy-server").textContent = (s.ntfy_server || "").replace(/^https?:\/\//, "");

  if (status) {
    const node = $("#watch-status");
    if (!status.enabled) { node.className = "row-value"; node.textContent = "Off"; }
    else if (status.last_error) { node.className = "row-value bad"; node.replaceChildren(icon("alert"), "Needs attention"); node.title = status.last_error; }
    else { node.className = "row-value"; node.title = ""; node.textContent = status.last_check ? `Checked ${timeAgo(status.last_check)}` : "Starting"; }
  }
}

function modelName(id) {
  const m = /^claude-(\w+)-(\d+)-(\d+)/.exec(id || "");
  return m ? `Claude ${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2]}.${m[3]}` : "Claude";
}

async function loadSettings() {
  try {
    const [s, ev] = await Promise.all([api("/api/settings"), api("/api/events?limit=1").catch(() => null)]);
    renderSettings(s, ev?.status);
  } catch (err) { toast(err.message, true); }
}

async function saveSettings(button, body, message) {
  return busy(button, async () => {
    renderSettings(await api("/api/settings", { method: "POST", body }));
    toast(message);
    return true;
  });
}

$("#key-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = e.target.key;
  if (!input.value.trim()) { toast("Paste your API key first", true); input.focus(); return; }
  const btn = e.target.querySelector("button[type=submit]");
  btn.textContent = "Checking…";
  saveSettings(btn, { anthropic_api_key: input.value.trim() }, "API key checked and saved")
    .then((ok) => { if (ok) input.value = ""; })
    .finally(() => { btn.textContent = "Save"; });
});
$("#key-remove").addEventListener("click", (e) => saveSettings(e.target, { anthropic_api_key: "" }, "API key removed"));
$("#topic-form").addEventListener("submit", (e) => {
  e.preventDefault();
  saveSettings(e.target.querySelector("button[type=submit]"), { ntfy_topic: e.target.topic.value }, "Topic saved");
});
$("#notify-test").addEventListener("click", (e) => busy(e.target, async () => {
  await api("/api/notifications/test", { method: "POST" });
  toast("Test sent. Check your phone.");
}));

boot().catch((err) => toast(err.message, true));
