"use strict";
(function () {
  const csrf = document.body.dataset.csrf;

  // Confirmation prompts on forms / buttons with data-confirm.
  document.addEventListener("submit", (e) => {
    const f = e.target;
    const msg = f.dataset.confirm || (e.submitter && e.submitter.dataset.confirm);
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  // Search box over a list (channel page): hides rows whose title doesn't contain every word.
  document.querySelectorAll("input[data-filter-rows]").forEach((box) => {
    const rows = Array.from(document.querySelectorAll(box.dataset.filterRows));
    const count = box.parentElement.querySelector(".rowcount");
    // Enter must not submit the surrounding "Download selected" form
    box.addEventListener("keydown", (e) => { if (e.key === "Enter") e.preventDefault(); });
    box.addEventListener("input", () => {
      const words = box.value.toLowerCase().split(/\s+/).filter(Boolean);
      let shown = 0;
      rows.forEach((r) => {
        const t = r.dataset.title.toLowerCase();
        r.hidden = !words.every((w) => t.includes(w));
        shown += !r.hidden;
      });
      if (count) count.textContent = words.length ? `${shown} of ${rows.length}` : "";
    });
  });

  // "Select all" helper on admin pick lists (only the rows the search box shows).
  document.querySelectorAll("[data-select-all]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const boxes = Array.from(btn.form.querySelectorAll(`input[type=checkbox][name="${btn.dataset.selectAll}"]`))
        .filter((b) => !b.closest("tr")?.hidden);
      const all = Array.from(boxes).every((b) => b.checked);
      boxes.forEach((b) => (b.checked = !all));
    });
  });

  // Channel page: keyword matches, computed live from the keyword boxes (same rules as
  // keywords.py: any include matches, no exclude matches; empty include = everything).
  const kwIn = document.querySelector("textarea[name=include_keywords]");
  const kwEx = document.querySelector("textarea[name=exclude_keywords]");
  const kwBtn = document.querySelector("[data-select-matching]");
  if (kwIn && kwEx && kwBtn) {
    const parse = (s) => s.split(/[,\n，、;；]+/).map((k) => k.trim().toLowerCase()).filter(Boolean);
    const rows = Array.from(document.querySelectorAll("tr[data-title]"));
    function matches(title) {
      const t = title.toLowerCase(), inc = parse(kwIn.value), exc = parse(kwEx.value);
      if (exc.some((k) => t.includes(k))) return false;
      return inc.length === 0 || inc.some((k) => t.includes(k));
    }
    function paintMatches() {
      const active = parse(kwIn.value).length + parse(kwEx.value).length > 0;
      rows.forEach((r) => {
        const cell = r.querySelector(".kwm");
        if (cell) cell.textContent = active ? (matches(r.dataset.title) ? "✓ match" : "—") : "";
      });
      kwBtn.disabled = !active;
    }
    kwIn.addEventListener("input", paintMatches);
    kwEx.addEventListener("input", paintMatches);
    paintMatches();
    kwBtn.addEventListener("click", () => {
      // Tick exactly the matching, not-yet-downloaded videos; press again to clear.
      const boxes = rows.map((r) => [r.querySelector(`input[name="${kwBtn.dataset.selectMatching}"]`), r])
        .filter(([b]) => b);
      const want = boxes.map(([, r]) => matches(r.dataset.title));
      const already = boxes.every(([b], i) => b.checked === want[i]) && want.some(Boolean);
      boxes.forEach(([b], i) => (b.checked = already ? false : want[i]));
    });
  }

  // <dialog> openers (share options for admins) and copy buttons.
  document.querySelectorAll("[data-dialog]").forEach((btn) => {
    const dlg = document.getElementById(btn.dataset.dialog);
    if (!dlg || !dlg.showModal) return;
    btn.addEventListener("click", () => dlg.showModal());
    dlg.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", () => dlg.close()));
  });
  document.querySelectorAll("[data-copy]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const input = document.getElementById(btn.dataset.copy);
      input.select();
      try { await navigator.clipboard.writeText(input.value); } catch (e) { document.execCommand("copy"); }
      const old = btn.textContent;
      btn.textContent = btn.dataset.copied || "Copied";
      setTimeout(() => (btn.textContent = old), 1500);
    });
  });

  // Watched-progress bars (set via JS: the CSP forbids inline styles).
  function paintWatched(root) {
    root.querySelectorAll(".watched i[data-w]").forEach((i) => (i.style.width = i.dataset.w + "%"));
  }
  paintWatched(document);

  // Infinite scroll for card grids.
  function watchMore(el) {
    const io = new IntersectionObserver(async (entries) => {
      if (!entries[0].isIntersecting) return;
      io.disconnect();
      const res = await fetch(el.dataset.more, { credentials: "same-origin" });
      if (!res.ok) return;
      const tmp = document.createElement("div");
      tmp.innerHTML = await res.text();
      paintWatched(tmp);
      const grid = el.parentNode;
      el.remove();
      Array.from(tmp.childNodes).forEach((n) => grid.appendChild(n));
      const next = grid.querySelector(".more");
      if (next) watchMore(next);
    }, { rootMargin: "600px" });
    io.observe(el);
  }
  const more = document.querySelector(".more");
  if (more) watchMore(more);

  // Save watch position (the player itself is in player.js) and move the watermark.
  const wrap = document.querySelector(".player");
  const video = document.getElementById("player");
  if (wrap && video) {
    let lastSent = -1;
    function save() {
      const pos = Math.floor(video.currentTime);
      if (pos === lastSent || !csrf) return;
      lastSent = pos;
      fetch("/api/progress", {
        method: "POST", keepalive: true, credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF": csrf },
        body: JSON.stringify({ video_id: wrap.dataset.videoId, position: pos }),
      }).catch(() => {});
    }
    setInterval(() => { if (!video.paused) save(); }, 15000);
    video.addEventListener("pause", save);
    video.addEventListener("ended", save);
    document.addEventListener("visibilitychange", () => { if (document.hidden) save(); });

    const wm = wrap.querySelector(".wm");
    function moveWm() {
      wm.style.left = 5 + Math.random() * 60 + "%";
      wm.style.top = 5 + Math.random() * 70 + "%";
    }
    if (wm) { moveWm(); setInterval(moveWm, 20000); }
  }
})();
