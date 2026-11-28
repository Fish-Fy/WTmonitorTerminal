"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const labels = JSON.parse($("mirror-copy").textContent);
  const label = (key, value) => value === undefined ? labels[key] : labels[key].replace("{0}", () => String(value));
  let image = $("map");
  const stage = $("stage");
  let source, snapshot = {}, newest = 0, loading = false, pending = 0, connected = false, generation = 0;
  let lastMessage = 0, lastFrame = 0, lastScene = null, frameTimer, scale = 1, pan = {x: 0, y: 0};
  let rosterSignature = "", hideLeftOverride = null, highlightOverride = null;
  const expandedPlayers = new Set(), collapsedGroups = new Set();
  const pointers = new Map();
  let frameSize = {width: 0, height: 0};
  function fittedImageSize() {
    const width = stage.clientWidth, height = stage.clientHeight;
    const sourceWidth = frameSize.width || snapshot.frame_width || width || 1;
    const sourceHeight = frameSize.height || snapshot.frame_height || height || 1;
    const ratio = Math.max(width / sourceWidth, height / sourceHeight);
    return {width: sourceWidth * ratio, height: sourceHeight * ratio};
  }
  function stagePoint(event) {
    const rect = stage.getBoundingClientRect();
    return document.body.classList.contains("landscape-fallback")
      ? {x: event.clientY - rect.top, y: rect.right - event.clientX}
      : {x: event.clientX - rect.left, y: event.clientY - rect.top};
  }
  function transform() {
    const fitted = fittedImageSize();
    const maxX = Math.max(0, (fitted.width * scale - stage.clientWidth) / 2);
    const maxY = Math.max(0, (fitted.height * scale - stage.clientHeight) / 2);
    pan.x = Math.max(-maxX, Math.min(maxX, pan.x));
    pan.y = Math.max(-maxY, Math.min(maxY, pan.y));
    image.style.width = `${fitted.width}px`;
    image.style.height = `${fitted.height}px`;
    image.style.transform = `translate(-50%,-50%) translate(${pan.x}px,${pan.y}px) scale(${scale})`;
  }
  function reset() { scale = 1; pan = {x: 0, y: 0}; transform(); }
  function zoom(factor, point) {
    const old = scale;
    scale = Math.max(1, Math.min(6, scale * factor));
    const x = point ? point.x - stage.clientWidth / 2 : 0;
    const y = point ? point.y - stage.clientHeight / 2 : 0;
    pan = {x: x - (x - pan.x) * scale / old, y: y - (y - pan.y) * scale / old};
    transform();
  }
  function geometry() {
    const values = [...pointers.values()];
    if (values.length < 2) return {point: values[0], distance: 0};
    return {point: {x: (values[0].x + values[1].x) / 2, y: (values[0].y + values[1].y) / 2},
            distance: Math.hypot(values[0].x - values[1].x, values[0].y - values[1].y)};
  }
  stage.addEventListener("pointerdown", event => {
    stage.setPointerCapture(event.pointerId);
    pointers.set(event.pointerId, stagePoint(event));
  });
  stage.addEventListener("pointermove", event => {
    if (!pointers.has(event.pointerId)) return;
    const before = geometry();
    pointers.set(event.pointerId, stagePoint(event));
    const after = geometry();
    if (before.distance && after.distance) zoom(after.distance / before.distance, before.point);
    pan.x += after.point.x - before.point.x; pan.y += after.point.y - before.point.y;
    transform();
  });
  for (const name of ["pointerup", "pointercancel", "lostpointercapture"]) {
    stage.addEventListener(name, event => pointers.delete(event.pointerId));
  }
  stage.addEventListener("wheel", event => { event.preventDefault(); zoom(event.deltaY < 0 ? 1.15 : 1 / 1.15, stagePoint(event)); }, {passive:false});
  $("zoom-in").onclick = () => zoom(1.3);
  $("zoom-out").onclick = () => zoom(1 / 1.3);
  $("reset").onclick = reset;
  const mapTools = $("map-tools");
  document.addEventListener("click", event => { if (!mapTools.contains(event.target)) mapTools.open = false; });
  document.addEventListener("keydown", event => { if (event.key === "Escape") mapTools.open = false; });
  $("focus").onclick = () => {
    document.body.classList.toggle("focus-map");
    $("focus").textContent = label(document.body.classList.contains("focus-map") ? "leaveFocus" : "focus");
    mapTools.open = false;
    transform();
  };
  const fullscreenButton = $("fullscreen");
  const standalone = window.matchMedia("(display-mode: standalone)").matches || Boolean(navigator.standalone);
  let standaloneLandscape = standalone, fullscreenActive = false, fullscreenBusy = false, orientationEpoch = 0, toastTimer;
  const fullscreenElement = () => document.fullscreenElement || document.webkitFullscreenElement;
  function fullscreenNotice(text) {
    $("fullscreen-notice").textContent = text;
    $("fullscreen-notice").hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { $("fullscreen-notice").hidden = true; }, 8000);
  }
  function layout() {
    const portrait = window.innerHeight > window.innerWidth;
    const mobile = window.matchMedia("(pointer: coarse)").matches || window.innerWidth <= 820;
    document.body.classList.toggle("landscape-fallback", fullscreenActive && portrait && mobile);
    pointers.clear();
    requestAnimationFrame(transform);
  }
  async function lockLandscape() {
    const epoch = ++orientationEpoch;
    try {
      if (screen.orientation?.lock) await screen.orientation.lock("landscape");
    } catch (_) { /* 不支持系统锁定时，由横屏页面布局接管。 */ }
    if (epoch === orientationEpoch) layout();
  }
  function syncFullscreen() {
    const native = Boolean(fullscreenElement());
    const active = native || standaloneLandscape;
    document.body.classList.toggle("fullscreen-app", active);
    fullscreenButton.textContent = label(native ? "exitFullscreen" : standalone ? (active ? "exitLandscape" : "landscape") : "fullscreen");
    fullscreenButton.setAttribute("aria-pressed", String(active));
    if (active !== fullscreenActive) {
      fullscreenActive = active;
      if (active) void lockLandscape();
      else {
        orientationEpoch += 1;
        try { screen.orientation?.unlock?.(); } catch (_) { /* 部分浏览器没有方向控制权限。 */ }
      }
    }
    layout();
  }
  fullscreenButton.onclick = async () => {
    if (fullscreenBusy) return;
    if (standalone && !fullscreenElement()) { standaloneLandscape = !standaloneLandscape; syncFullscreen(); return; }
    fullscreenBusy = true;
    try {
      if (fullscreenElement()) {
        const exit = document.exitFullscreen || document.webkitExitFullscreen;
        if (exit) await exit.call(document);
      } else {
        const root = document.documentElement;
        const request = root.requestFullscreen || root.webkitRequestFullscreen;
        if (!request) {
          fullscreenNotice(label("fullscreenUnavailable"));
          return;
        }
        await request.call(root, {navigationUI: "hide"});
      }
      syncFullscreen();
    } catch (_) {
      fullscreenNotice(label("fullscreenRejected"));
    } finally { fullscreenBusy = false; }
  };
  document.addEventListener("fullscreenchange", syncFullscreen);
  document.addEventListener("webkitfullscreenchange", syncFullscreen);
  window.addEventListener("resize", layout);
  screen.orientation?.addEventListener?.("change", layout);
  syncFullscreen();
  const rank = {alive: 0, killed: 1, unknown: 2, left: 3};
  const compareNames = (a, b) => {
    const first = a.name.toLocaleLowerCase(), second = b.name.toLocaleLowerCase();
    return first < second ? -1 : first > second ? 1 : 0;
  };
  function text(tag, className, value) {
    const element = document.createElement(tag); element.className = className; element.textContent = value;
    return element;
  }
  function playerItem(row, highlight) {
    const item = document.createElement("details");
    item.className = `player ${row.status_key || "unknown"}`;
    item.open = expandedPlayers.has(row.name);
    item.addEventListener("toggle", () => { if (item.open) expandedPlayers.add(row.name); else expandedPlayers.delete(row.name); });
    if (row.is_me) item.classList.add("self");
    if (highlight && !row.is_me && row.killed_me) item.classList.add("killed-me");
    const summary = document.createElement("summary");
    const name = text("strong", "player-name", row.display_name || row.name);
    const vehicle = text("span", "vehicle", row.vehicle || "?");
    name.title = row.name; vehicle.title = row.vehicle || "?";
    const status = row.status || label("unknownStatus"), seconds = Number(row.status_time) || 0;
    const timestamp = seconds ? ` ${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(Math.floor(seconds % 60)).padStart(2, "0")}` : "";
    summary.title = `${row.name} · ${status}${timestamp}${row.killed_me ? ` · ${label("killedMe", row.killed_me)}` : ""}`;
    const statusCell = text("span", "status", status);
    if (timestamp) statusCell.append(" ", text("span", "status-time", timestamp.trim()));
    summary.append(name, vehicle, text("span", "kills", row.kills || 0), statusCell);
    const history = document.createElement("div"); history.className = "player-history";
    history.append(text("div", "history-line", `${row.name} · ${status}${timestamp}`));
    for (const entry of row.vehicles || []) {
      const line = document.createElement("div"); line.className = "history-line";
      line.append(text("span", "", entry.vehicle), text("span", "", label("killCount", entry.kills || 0))); history.append(line);
    }
    item.append(summary, history); return item;
  }
  function renderRoster() {
    const root = $("roster"), needle = $("search").value.trim().toLocaleLowerCase();
    const options = snapshot.roster_options || {};
    const hideLeft = hideLeftOverride ?? Boolean(options.hide_left);
    const highlight = highlightOverride ?? Boolean(options.highlight_killers);
    $("hide-left").checked = hideLeft;
    $("highlight-killers").checked = highlight;
    const signature = JSON.stringify([snapshot.roster, options, hideLeft, highlight, needle]);
    if (signature === rosterSignature) return;
    rosterSignature = signature;
    const scrollTop = root.scrollTop;
    root.replaceChildren();
    let total = 0, shown = 0, hiddenLeft = 0;
    for (const side of ["enemy", "friend", "unknown"]) {
      const all = side === "friend" ? [...(snapshot.roster?.friend || []), ...(snapshot.roster?.me || [])] : snapshot.roster?.[side] || [];
      all.sort((a, b) => (rank[a.status_key] ?? 2) - (rank[b.status_key] ?? 2) || b.kills - a.kills || compareNames(a, b));
      total += all.length;
      hiddenLeft += all.filter(row => hideLeft && row.status_key === "left").length;
      const rows = all.filter(row => (!hideLeft || row.status_key !== "left") &&
        `${row.name} ${row.vehicle} ${row.search_text || ""}`.toLocaleLowerCase().includes(needle));
      if (!rows.length) continue;
      shown += rows.length;
      const group = document.createElement("details"); group.className = `group ${side}`;
      group.open = !collapsedGroups.has(side);
      group.addEventListener("toggle", () => { if (group.open) collapsedGroups.delete(side); else collapsedGroups.add(side); });
      group.append(text("summary", "", `${label(side)} (${rows.length})`));
      if (side === "unknown") {
        const statuses = ["alive", "killed", "left", "unknown"].filter(key => rows.some(row => row.status_key === key));
        for (const status of statuses) {
          const members = rows.filter(row => row.status_key === status).sort(compareNames);
          if (statuses.length > 1) group.append(text("div", "status-heading", `${members[0].status} (${members.length})`));
          for (const row of members) group.append(playerItem(row, highlight));
        }
      } else {
        for (const row of rows) group.append(playerItem(row, highlight));
      }
      root.append(group);
    }
    $("count").textContent = label("playerCount", needle || hiddenLeft ? `${shown}/${total}` : total);
    if (hiddenLeft) root.append(text("p", "hidden-count", label("hiddenCount", hiddenLeft)));
    if (!shown) root.append(text("p", "no-results", label(needle ? "noMatch" : total && hiddenLeft === total ? "everyoneLeft" : "waitingRoster")));
    root.scrollTop = scrollTop;
  }
  $("search").addEventListener("input", renderRoster);
  $("hide-left").addEventListener("change", event => { hideLeftOverride = event.target.checked; renderRoster(); });
  $("highlight-killers").addEventListener("change", event => { highlightOverride = event.target.checked; renderRoster(); });
  function connection(ok) {
    connected = ok;
    $("connection").textContent = label(ok ? "connected" : "reconnecting");
    $("connection").className = `badge ${ok ? "connected" : "disconnected"}`;
    document.body.classList.toggle("disconnected-map", !ok);
    if (!ok) $("frame-status").textContent = label("disconnected");
  }
  function loadFrame(revision) {
    if (revision <= newest) return;
    pending = Math.max(pending, revision);
    if (loading) return;
    loading = true;
    const requested = pending, currentGeneration = generation, next = new Image();
    const failed = () => {
      if (currentGeneration !== generation) return;
      clearTimeout(frameTimer);
      loading = false;
      next.onload = next.onerror = null;
      next.src = "";
      $("frame-status").textContent = label("retrying");
    };
    frameTimer = setTimeout(failed, 5000);
    next.onload = () => {
      if (currentGeneration !== generation) return;
      clearTimeout(frameTimer);
      lastFrame = Date.now();
      next.onload = next.onerror = null;
      if (requested > newest) {
        newest = requested;
        frameSize = {width: next.naturalWidth, height: next.naturalHeight};
        next.id = image.id;
        next.alt = image.alt;
        next.draggable = false;
        image.replaceWith(next);
        image = next;
        $("empty").hidden = true;
        transform();
      }
      loading = false;
      if (pending > requested) loadFrame(pending);
    };
    next.onerror = failed;
    next.src = `/frame.jpg?v=${requested}`;
  }
  function apply(message) {
    lastMessage = Date.now(); connection(true);
    if (message.kind === "snapshot" || message.kind === "frame") {
      snapshot = message;
      document.body.classList.toggle("stale-map", Boolean(snapshot.stale));
      if (lastScene !== snapshot.scene) { lastScene = snapshot.scene; reset(); }
      const sceneLabel = snapshot.scene_label || snapshot.scene || label("map");
      $("scene").textContent = sceneLabel;
      $("scene").title = sceneLabel;
      $("mode").hidden = !snapshot.planning;
      $("frame-status").textContent = label(lastFrame && Date.now() - lastFrame > 5000 ? "delayed" : snapshot.planning ? "planning" : snapshot.stale ? "cached" : "live");
      renderRoster();
      if (snapshot.frame_rev) loadFrame(snapshot.frame_rev);
    } else if (message.kind === "reset") { snapshot.roster = {}; snapshot.banner = ""; expandedPlayers.clear(); renderRoster(); }
    else if (message.kind === "threat") { snapshot.banner = message.banner; snapshot.banner_at = message.banner_at; }
    $("threat").textContent = snapshot.banner || "";
    $("threat").hidden = !snapshot.banner || Date.now() / 1000 - (snapshot.banner_at || 0) > 10;
  }
  function connect() {
    if (source) source.close();
    // 服务重开时帧编号会从零开始；新快照必须能替换旧画面。
    generation += 1; newest = 0; pending = 0; loading = false;
    clearTimeout(frameTimer);
    source = new EventSource("/api/stream");
    source.onmessage = event => { try { apply(JSON.parse(event.data)); } catch (error) { console.warn("Invalid mirror message", error); } };
    source.onerror = () => { connection(false); clearTimeout(frameTimer); generation += 1; newest = 0; pending = 0; loading = false; };
  }
  document.addEventListener("visibilitychange", () => {
    pointers.clear();
    if (document.hidden) source?.close(); else connect();
  });
  setInterval(() => {
    if (connected && Date.now() - lastMessage > 5000) connection(false);
    if (!document.hidden && connected && pending > newest && !loading) loadFrame(pending);
    if (connected && lastFrame && Date.now() - lastFrame > 5000) $("frame-status").textContent = label("delayedHold");
    if (snapshot.banner && Date.now() / 1000 - (snapshot.banner_at || 0) > 10) $("threat").hidden = true;
  }, 1000);
  connect();
})();
