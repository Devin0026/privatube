"use strict";
// Player: custom controls on a native <video>. Adds playback speed (0.5x-3x,
// remembered), keyboard shortcuts, double-tap seek on phones, and fullscreen that keeps
// the viewer watermark (we fullscreen the wrapper, not the <video>).
(function () {
  const wrap = document.querySelector(".player");
  const video = document.getElementById("player");
  if (!wrap || !video) return;

  const SPEEDS = [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2, 2.5, 3];
  const ZH = document.documentElement.lang.startsWith("zh");
  const T = ZH ? {
    seek: "进度", play: "播放 (k)", back: "后退 10 秒 (j)", fwd: "前进 10 秒 (l)", mute: "静音 (m)",
    volume: "音量", speed: "播放速度 (< / >)", normal: "正常", fs: "全屏 (f)", playLabel: "播放",
    error: "视频无法加载，链接可能已过期，请刷新页面。",
    errorFormat: "视频无法播放：链接已过期或服务器拒绝访问，也可能是此设备不支持该视频格式。请刷新页面重试。", cc: "字幕 (c)", off: "关闭",
  } : {
    seek: "Seek", play: "Play (k)", back: "Back 10 s (j)", fwd: "Forward 10 s (l)", mute: "Mute (m)",
    volume: "Volume", speed: "Playback speed (&lt; / &gt;)", normal: "Normal", fs: "Full screen (f)", playLabel: "Play",
    error: "This video could not be loaded. The link may have expired: reload the page.",
    errorFormat: "This video can't be played: the link expired or was refused, or this device can't decode the format. Reload the page to try again.", cc: "Subtitles (c)", off: "Off",
  };
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
  };
  const svg = (d) => `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${d}"/></svg>`;
  const ICON = {
    play: svg("M8 5.14v13.72a1 1 0 0 0 1.52.85l10.6-6.86a1 1 0 0 0 0-1.7L9.52 4.29A1 1 0 0 0 8 5.14z"),
    pause: svg("M6 5h4v14H6zM14 5h4v14h-4z"),
    back: svg("M12 5V2L7 6l5 4V7a6 6 0 1 1-6 6H4a8 8 0 1 0 8-8zm-1.1 11H10v-3.3l-1 .3v-.7l1.8-.6h.1V16zm4.3-1.5c0 .5-.1.9-.3 1.2-.2.3-.5.4-.9.4s-.7-.1-.9-.4c-.2-.3-.3-.7-.3-1.2v-.7c0-.5.1-.9.3-1.2.2-.3.5-.4.9-.4s.7.1.9.4c.2.3.3.7.3 1.2v.7zm-.9-.8c0-.3 0-.5-.1-.7 0-.2-.1-.3-.3-.3s-.2.1-.3.3l-.1.7v.9c0 .3 0 .5.1.7.1.1.2.2.3.2s.2-.1.3-.2l.1-.7v-.9z"),
    fwd: svg("M12 5V2l5 4-5 4V7a6 6 0 1 0 6 6h2a8 8 0 1 1-8-8zm-1.1 11H10v-3.3l-1 .3v-.7l1.8-.6h.1V16zm4.3-1.5c0 .5-.1.9-.3 1.2-.2.3-.5.4-.9.4s-.7-.1-.9-.4c-.2-.3-.3-.7-.3-1.2v-.7c0-.5.1-.9.3-1.2.2-.3.5-.4.9-.4s.7.1.9.4c.2.3.3.7.3 1.2v.7zm-.9-.8c0-.3 0-.5-.1-.7 0-.2-.1-.3-.3-.3s-.2.1-.3.3l-.1.7v.9c0 .3 0 .5.1.7.1.1.2.2.3.2s.2-.1.3-.2l.1-.7v-.9z"),
    vol: svg("M3 10v4h4l5 5V5L7 10H3zm13.5 2A4.5 4.5 0 0 0 14 8v8a4.5 4.5 0 0 0 2.5-4zM14 3.2v2.1a7 7 0 0 1 0 13.4v2.1a9 9 0 0 0 0-17.6z"),
    mute: svg("M16.5 12A4.5 4.5 0 0 0 14 8v2.2l2.4 2.4.1-.6zm2.5 0c0 .9-.2 1.8-.5 2.6l1.5 1.5A8.8 8.8 0 0 0 21 12a9 9 0 0 0-7-8.8v2.1a7 7 0 0 1 5 6.7zM4.3 3 3 4.3 7.7 9H3v6h4l5 5v-6.7l4.3 4.3c-.7.5-1.4.9-2.3 1.1v2.1a9 9 0 0 0 3.7-1.8l2 2 1.3-1.3L4.3 3zM12 4 9.9 6.1 12 8.2V4z"),
    fs: svg("M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z"),
    cc: svg("M19 4H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2zm-8 7H9.5v-.5h-2v3h2V13H11v1a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1v-4a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v1zm7 0h-1.5v-.5h-2v3h2V13H18v1a1 1 0 0 1-1 1h-3a1 1 0 0 1-1-1v-4a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v1z"),
    fsExit: svg("M5 16h3v3h2v-5H5v2zm3-8H5v2h5V5H8v3zm6 11h2v-3h3v-2h-5v5zm2-11V5h-2v5h5V8h-3z"),
  };

  const fmt = (s) => {
    s = Math.max(0, Math.floor(s || 0));
    const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, sec = String(s % 60).padStart(2, "0");
    return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
  };

  // ---- build UI (progressive enhancement: without JS the native controls stay)
  video.controls = false;
  wrap.classList.add("pt-player", "paused");
  const ui = document.createElement("div");
  ui.className = "pc";
  ui.innerHTML = `
    <div class="pc-seek" role="slider" aria-label="${T.seek}" tabindex="0">
      <div class="pc-track"><div class="pc-buf"></div><div class="pc-played"></div><div class="pc-knob"></div></div>
      <div class="pc-tip"></div>
    </div>
    <div class="pc-row">
      <button type="button" class="pc-btn pc-play" title="${T.play}">${ICON.play}</button>
      <button type="button" class="pc-btn pc-back" title="${T.back}">${ICON.back}</button>
      <button type="button" class="pc-btn pc-fwd" title="${T.fwd}">${ICON.fwd}</button>
      <button type="button" class="pc-btn pc-mute" title="${T.mute}">${ICON.vol}</button>
      <input class="pc-vol" type="range" min="0" max="1" step="0.05" aria-label="${T.volume}">
      <span class="pc-time">0:00 / 0:00</span>
      <span class="pc-gap"></span>
      <div class="pc-ccwrap" hidden>
        <button type="button" class="pc-btn pc-cc" title="${T.cc}">${ICON.cc}</button>
        <div class="pc-menu pc-ccmenu" hidden></div>
      </div>
      <div class="pc-speedwrap">
        <button type="button" class="pc-btn pc-speed" title="${T.speed}">1×</button>
        <div class="pc-menu" hidden>${SPEEDS.map((r) => `<button type="button" data-rate="${r}">${r === 1 ? T.normal : r + "×"}</button>`).join("")}</div>
      </div>
      <button type="button" class="pc-btn pc-fs" title="${T.fs}">${ICON.fs}</button>
    </div>`;
  const big = document.createElement("button");
  big.type = "button";
  big.className = "pc-big";
  big.innerHTML = ICON.play;
  big.setAttribute("aria-label", T.playLabel);
  const toast = document.createElement("div");
  toast.className = "pc-toast";
  const spinner = document.createElement("div");
  spinner.className = "pc-spinner";
  const msg = document.createElement("div");
  msg.className = "pc-msg";
  wrap.append(spinner, big, toast, msg, ui);

  const $ = (sel) => ui.querySelector(sel);
  const seek = $(".pc-seek"), track = $(".pc-track"), played = $(".pc-played"), buf = $(".pc-buf"),
    knob = $(".pc-knob"), tip = $(".pc-tip"), playBtn = $(".pc-play"), muteBtn = $(".pc-mute"),
    vol = $(".pc-vol"), time = $(".pc-time"), speedBtn = $(".pc-speed"), menu = $(".pc-speedwrap .pc-menu"), fsBtn = $(".pc-fs");

  // ---- state helpers
  let toastTimer;
  function flash(text) {
    toast.textContent = text;
    toast.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove("show"), 700);
  }
  function togglePlay() { if (video.paused || video.ended) video.play().catch(() => {}); else video.pause(); }
  function skip(sec) {
    if (!isFinite(video.duration)) return;
    video.currentTime = Math.min(video.duration, Math.max(0, video.currentTime + sec));
    flash((sec > 0 ? "+" : "−") + Math.abs(sec) + " s");
  }
  function setRate(r) {
    r = Math.min(3, Math.max(0.5, r));
    video.playbackRate = r;
    video.defaultPlaybackRate = r;
    store.set("pt.rate", r);
  }
  function stepRate(dir) {
    const cur = video.playbackRate;
    const i = SPEEDS.findIndex((s) => s >= cur - 0.001);
    const next = SPEEDS[Math.min(SPEEDS.length - 1, Math.max(0, (i < 0 ? 2 : i) + dir))];
    setRate(next);
    flash(next + "×");
  }
  function setVolume(v) {
    video.volume = Math.min(1, Math.max(0, v));
    video.muted = video.volume === 0;
    store.set("pt.vol", video.volume);
  }
  const isFs = () => document.fullscreenElement || document.webkitFullscreenElement;
  function toggleFs() {
    if (isFs()) {
      (document.exitFullscreen || document.webkitExitFullscreen).call(document);
    } else if (wrap.requestFullscreen) {
      wrap.requestFullscreen().then(() => {
        if (screen.orientation && screen.orientation.lock && video.videoWidth > video.videoHeight) {
          screen.orientation.lock("landscape").catch(() => {});
        }
      }).catch(() => {});
    } else if (wrap.webkitRequestFullscreen) {
      wrap.webkitRequestFullscreen();
    } else if (video.webkitEnterFullscreen) {
      video.webkitEnterFullscreen();   // iPhone: native fullscreen only (watermark hidden there)
    }
  }

  // ---- render
  function paintTime() {
    const d = video.duration || 0, t = video.currentTime || 0;
    const pct = d ? (t / d) * 100 : 0;
    played.style.width = pct + "%";
    knob.style.left = pct + "%";
    time.textContent = `${fmt(t)} / ${fmt(d)}`;
    seek.setAttribute("aria-valuenow", String(Math.floor(t)));
    let end = 0;
    for (let i = 0; i < video.buffered.length; i++) {
      if (video.buffered.start(i) <= t + 0.5) end = Math.max(end, video.buffered.end(i));
    }
    buf.style.width = (d ? (end / d) * 100 : 0) + "%";
  }
  function paintPlay() {
    const paused = video.paused || video.ended;
    wrap.classList.toggle("paused", paused);
    playBtn.innerHTML = paused ? ICON.play : ICON.pause;
    big.innerHTML = paused ? ICON.play : ICON.pause;
    if (paused) showControls();
  }
  function paintVolume() {
    muteBtn.innerHTML = video.muted || video.volume === 0 ? ICON.mute : ICON.vol;
    vol.value = video.muted ? 0 : video.volume;
  }
  function paintRate() {
    const r = video.playbackRate;
    speedBtn.textContent = (Math.round(r * 100) / 100) + "×";
    menu.querySelectorAll("button").forEach((b) => b.classList.toggle("on", Math.abs(+b.dataset.rate - r) < 0.001));
  }

  // ---- auto-hide controls while playing
  let idleTimer;
  function showControls() {
    wrap.classList.remove("idle");
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => {
      if (!video.paused && menu.hidden && !dragging) wrap.classList.add("idle");
    }, 2500);
  }
  wrap.addEventListener("pointermove", (e) => { if (e.pointerType === "mouse") showControls(); });
  wrap.addEventListener("mouseleave", () => { if (!video.paused) wrap.classList.add("idle"); });

  // ---- seeking (mouse, touch, pen)
  let dragging = false;
  function fracAt(x) {
    const r = track.getBoundingClientRect();
    return Math.min(1, Math.max(0, (x - r.left) / r.width));
  }
  function preview(x) {
    const f = fracAt(x);
    tip.textContent = fmt(f * (video.duration || 0));
    tip.style.left = f * 100 + "%";
    return f;
  }
  seek.addEventListener("pointerdown", (e) => {
    if (!isFinite(video.duration)) return;
    dragging = true;
    seek.setPointerCapture(e.pointerId);
    seek.classList.add("drag");
    const f = preview(e.clientX);
    played.style.width = knob.style.left = f * 100 + "%";
  });
  seek.addEventListener("pointermove", (e) => {
    const f = preview(e.clientX);
    if (dragging) played.style.width = knob.style.left = f * 100 + "%";
  });
  function endDrag(e) {
    if (!dragging) return;
    dragging = false;
    seek.classList.remove("drag");
    video.currentTime = fracAt(e.clientX) * video.duration;
    showControls();
  }
  seek.addEventListener("pointerup", endDrag);
  seek.addEventListener("pointercancel", () => { dragging = false; seek.classList.remove("drag"); });
  seek.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") { e.preventDefault(); e.stopPropagation(); skip(e.key === "ArrowLeft" ? -5 : 5); }
  });

  // ---- buttons
  playBtn.addEventListener("click", togglePlay);
  big.addEventListener("click", (e) => { e.stopPropagation(); togglePlay(); });
  $(".pc-back").addEventListener("click", () => skip(-10));
  $(".pc-fwd").addEventListener("click", () => skip(10));
  muteBtn.addEventListener("click", () => {
    if (video.muted || video.volume === 0) { video.muted = false; if (video.volume === 0) setVolume(0.5); }
    else video.muted = true;
  });
  vol.addEventListener("input", () => setVolume(+vol.value));
  fsBtn.addEventListener("click", toggleFs);
  speedBtn.addEventListener("click", (e) => { e.stopPropagation(); menu.hidden = !menu.hidden; showControls(); });
  menu.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-rate]");
    if (!b) return;
    setRate(+b.dataset.rate);
    menu.hidden = true;
  });
  document.addEventListener("click", (e) => { if (!menu.hidden && !e.target.closest(".pc-speedwrap")) menu.hidden = true; });

  // ---- subtitles: <track> elements from the page; choice remembered per language family
  const tracks = Array.from(video.textTracks || []).filter((t) => t.kind === "subtitles");
  const ccWrap = $(".pc-ccwrap"), ccBtn = $(".pc-cc"), ccMenu = $(".pc-ccmenu");
  let ccLast = null;
  function setCC(track, remember) {
    tracks.forEach((t) => (t.mode = t === track ? "showing" : "disabled"));
    if (track) ccLast = track;
    ccBtn.classList.toggle("on", !!track);
    ccMenu.querySelectorAll("button").forEach((b) => b.classList.toggle("on", (b.dataset.i === "off" && !track) || tracks[+b.dataset.i] === track));
    if (remember) store.set("pt.cc", track ? track.language.slice(0, 2) : "off");
  }
  // keep subtitles clear of the control bar: 3 lines up from the bottom
  const lift = (t) => { for (const c of Array.from(t.cues || [])) if (c.line === "auto") c.line = -3; };
  video.querySelectorAll("track").forEach((el) => el.addEventListener("load", () => lift(el.track)));
  tracks.forEach(lift);
  if (tracks.length) {
    ccWrap.hidden = false;
    ccMenu.innerHTML = `<button type="button" data-i="off">${T.off}</button>` +
      tracks.map((t, i) => `<button type="button" data-i="${i}"></button>`).join("");
    tracks.forEach((t, i) => (ccMenu.querySelector(`[data-i="${i}"]`).textContent = t.label));
    const pref = store.get("pt.cc", "zh");          // Chinese on by default; "off" if turned off
    setCC(pref === "off" ? null : tracks.find((t) => t.language.startsWith(pref)) || null, false);
    ccBtn.addEventListener("click", (e) => { e.stopPropagation(); ccMenu.hidden = !ccMenu.hidden; menu.hidden = true; showControls(); });
    ccMenu.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-i]");
      if (!b) return;
      setCC(b.dataset.i === "off" ? null : tracks[+b.dataset.i], true);
      ccMenu.hidden = true;
    });
    document.addEventListener("click", (e) => { if (!ccMenu.hidden && !e.target.closest(".pc-ccwrap")) ccMenu.hidden = true; });
  }
  function toggleCC() {
    if (!tracks.length) return;
    const on = tracks.find((t) => t.mode === "showing");
    setCC(on ? null : ccLast || tracks[0], true);
    flash(on ? T.off : (ccLast || tracks[0]).label);
  }

  // ---- clicks / taps on the picture
  let lastTap = 0, lastTapX = 0;
  video.addEventListener("pointerup", (e) => {
    if (e.pointerType === "mouse") { if (e.button === 0) togglePlay(); return; }
    const now = Date.now(), r = video.getBoundingClientRect(), x = e.clientX - r.left;
    if (now - lastTap < 300 && Math.abs(x - lastTapX) < 80) {          // double tap: seek by side
      if (x < r.width / 3) skip(-10); else if (x > (r.width * 2) / 3) skip(10); else toggleFs();
      lastTap = 0;
      return;
    }
    lastTap = now; lastTapX = x;
    if (wrap.classList.contains("idle") || video.paused) showControls(); else wrap.classList.add("idle");
  });
  video.addEventListener("dblclick", toggleFs);
  wrap.addEventListener("contextmenu", (e) => e.preventDefault());

  // ---- keyboard (ignored while typing in a form field)
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select" || e.target.isContentEditable) return;
    let handled = true;
    switch (e.key) {
      case " ": case "k": case "K": togglePlay(); break;
      case "j": case "J": skip(-10); break;
      case "l": case "L": skip(10); break;
      case "ArrowLeft": skip(-5); break;
      case "ArrowRight": skip(5); break;
      case "ArrowUp": setVolume(video.volume + 0.1); flash(Math.round(video.volume * 100) + "%"); break;
      case "ArrowDown": setVolume(video.volume - 0.1); flash(Math.round(video.volume * 100) + "%"); break;
      case "m": case "M": muteBtn.click(); break;
      case "f": case "F": toggleFs(); break;
      case "c": case "C": toggleCC(); break;
      case "<": case ",": if (e.key === "<" || e.shiftKey) stepRate(-1); else handled = false; break;
      case ">": case ".": if (e.key === ">" || e.shiftKey) stepRate(1); else handled = false; break;
      default:
        if (/^[0-9]$/.test(e.key) && isFinite(video.duration)) video.currentTime = (+e.key / 10) * video.duration;
        else handled = false;
    }
    if (handled) { e.preventDefault(); showControls(); }
  });

  // ---- media events
  video.addEventListener("timeupdate", () => { if (!dragging) paintTime(); });
  video.addEventListener("progress", paintTime);
  video.addEventListener("durationchange", paintTime);
  video.addEventListener("play", paintPlay);
  video.addEventListener("pause", paintPlay);
  video.addEventListener("ended", paintPlay);
  video.addEventListener("volumechange", paintVolume);
  video.addEventListener("ratechange", paintRate);
  video.addEventListener("waiting", () => wrap.classList.add("loading"));
  ["playing", "canplay", "seeked"].forEach((ev) => video.addEventListener(ev, () => wrap.classList.remove("loading")));
  video.addEventListener("error", () => {
    const code = video.error ? video.error.code : 0;      // 2 network, 3 decode, 4 not supported / no access
    msg.textContent = (code === 3 || code === 4 ? T.errorFormat : T.error) + (code ? ` (E${code})` : "");
    wrap.classList.add("failed");
  });

  // ---- source: a signed, expiring link that nginx serves (set here, not in the HTML, so the
  // browser doesn't start fetching before the player is ready)
  if (wrap.dataset.src) video.src = wrap.dataset.src;
  ["fullscreenchange", "webkitfullscreenchange"].forEach((ev) => document.addEventListener(ev, () => {
    fsBtn.innerHTML = isFs() ? ICON.fsExit : ICON.fs;
    wrap.classList.toggle("fs", !!isFs());
  }));

  // ---- restore preferences + resume position
  // Computers: 25% by default (their speakers start loud), remembered per browser.
  // Phones/tablets: always full; the device's volume buttons control loudness (no slider there).
  video.volume = matchMedia("(hover: none)").matches ? 1 : store.get("pt.vol", 0.25);
  const start = parseFloat(wrap.dataset.position || "0");
  video.addEventListener("loadedmetadata", () => {
    setRate(store.get("pt.rate", 1));
    if (start > 5 && start < video.duration - 10) video.currentTime = start;
    paintTime();
  }, { once: true });
  paintVolume(); paintRate(); paintPlay();
})();
