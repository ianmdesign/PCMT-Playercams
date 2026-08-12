const $ = (id) => document.getElementById(id);

let session = null;
let producerToken = getProducerTokenFromHash();
let requestedSessionId = getSessionIdFromPath();
let pollTimer = null;
let lastRosterUpdated = null;
const previewCards = new Map();


function getSessionIdFromPath() {
  const match = location.pathname.match(/^\/producer\/([^/]+)\/?$/i);
  if (!match) return null;
  try { return decodeURIComponent(match[1]); } catch { return match[1]; }
}

function getProducerTokenFromHash() {
  const raw = location.hash.startsWith("#") ? location.hash.slice(1) : location.hash;
  if (!raw) return "";
  const params = new URLSearchParams(raw);
  return params.get("token") || "";
}

function producerFragment() {
  return producerToken ? `#token=${encodeURIComponent(producerToken)}` : "";
}

function setProducerPath(sessionId) {
  requestedSessionId = sessionId;
  const nextPath = `/producer/${encodeURIComponent(sessionId)}${producerFragment()}`;
  const current = `${location.pathname}${location.hash}`;
  if (current !== nextPath) history.replaceState({ sessionId }, "", nextPath);
}

function buildProducerUrl(state = session) {
  if (!state) return "";
  const base = state.producerUrl || `${location.origin}/producer/${encodeURIComponent(state.sessionId)}`;
  return `${base}${producerFragment()}`;
}

function showDashboard() {
  $("startCard").classList.add("hidden");
  $("dashboard").classList.remove("hidden");
}

function configureResumeCard(sessionId, message = "") {
  requestedSessionId = sessionId;
  $("startTitle").textContent = `Open session ${sessionId}`;
  $("groupCodeLabel").classList.add("hidden");
  $("openSession").classList.add("hidden");
  $("startError").textContent = message;
}

function headers() {
  const result = { "Content-Type": "application/json" };
  if (producerToken) result["X-Producer-Token"] = producerToken;
  return result;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { ...headers(), ...(options.headers || {}) },
  });
  let body = null;
  try { body = await response.json(); } catch { body = null; }
  if (!response.ok) {
    const message = body?.detail || `${response.status} ${response.statusText}`;
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return body;
}

function connectedPlayerMap(players) {
  const map = new Map();
  for (const player of players || []) map.set(player.normalizedRiotId, player);
  return map;
}

function overrideMap(entries) {
  const map = new Map();
  for (const entry of entries || []) map.set(entry.normalizedRiotId, entry);
  return map;
}

function norm(value) { return String(value || "").trim().toLocaleLowerCase(); }

function formatSeen(ms) {
  if (!ms) return "Registered";
  const age = Math.max(0, Date.now() - ms);
  if (age < 45000) return "Player page active";
  if (age < 120000) return "Seen about a minute ago";
  return `Last seen ${new Date(ms).toLocaleTimeString()}`;
}

function playerPageIsActive(player) {
  return !!player?.lastSeen && (Date.now() - player.lastSeen) < 60000;
}

function makePreviewCard(player) {
  const card = document.createElement("div");
  card.className = "preview-card";

  const frameWrap = document.createElement("div");
  frameWrap.className = "preview-frame-wrap";
  const frame = document.createElement("iframe");
  frame.className = "preview-frame";
  frame.allow = "autoplay; fullscreen";
  frame.referrerPolicy = "no-referrer";
  frame.loading = "eager";
  frameWrap.appendChild(frame);

  const info = document.createElement("div");
  info.className = "preview-info";
  const name = document.createElement("div");
  name.className = "preview-name";
  const mode = document.createElement("div");
  mode.className = "preview-mode";
  info.append(name, mode);
  card.append(frameWrap, info);

  return { card, frame, name, mode, url: "" };
}

function renderPreviews(state) {
  const root = $("previewGrid");
  const activePlayers = (state.players || []).filter(playerPageIsActive);
  const wanted = new Set(activePlayers.map((player) => player.normalizedRiotId));

  for (const [key, refs] of previewCards.entries()) {
    if (!wanted.has(key)) {
      refs.frame.src = "about:blank";
      refs.card.remove();
      previewCards.delete(key);
    }
  }

  const oldEmpty = $("previewEmpty");
  if (oldEmpty) oldEmpty.remove();

  if (!activePlayers.length) {
    const empty = document.createElement("div");
    empty.id = "previewEmpty";
    empty.className = "empty";
    empty.textContent = "No active playercam pages yet. Previews appear here after a player enters the VDO.Ninja embed.";
    root.appendChild(empty);
  } else {
    for (const player of activePlayers) {
      let refs = previewCards.get(player.normalizedRiotId);
      if (!refs) {
        refs = makePreviewCard(player);
        previewCards.set(player.normalizedRiotId, refs);
        // Append a preview card only once. Moving/re-appending an iframe during
        // each producer-state poll can restart its browsing context and WebRTC
        // connection in some browsers, which looks like a periodic refresh.
        root.appendChild(refs.card);
      }
      refs.name.textContent = player.riotId;
      refs.mode.textContent = player.shareType === "media" ? "Media" : "Camera";
      if (player.previewUrl && refs.url !== player.previewUrl) {
        refs.url = player.previewUrl;
        refs.frame.src = player.previewUrl;
      }
    }
  }

  const bitrate = Number(state.producerPreviewBitrateKbps || 0);
  $("previewQuality").textContent = bitrate ? `${bitrate} kbps per preview · no audio` : "No audio";
}

function makeOverrideRow(riotId, currentOverride, player) {
  const row = document.createElement("div");
  row.className = "player-row";

  const identity = document.createElement("div");
  const name = document.createElement("div");
  name.className = "player-id";
  name.textContent = riotId;
  identity.appendChild(name);
  const status = document.createElement("div");
  status.className = "subtle";
  if (player) {
    status.textContent = `${player.shareType === "media" ? "Media" : "Video"} · ${formatSeen(player.lastSeen)}`;
  } else {
    status.textContent = "No playercam registration";
  }
  identity.appendChild(status);

  const input = document.createElement("input");
  input.value = currentOverride?.displayName || "";
  input.placeholder = "Use Spectra name";

  const actions = document.createElement("div");
  actions.className = "row";
  const save = document.createElement("button");
  save.className = "btn";
  save.textContent = "Save";
  save.addEventListener("click", async () => {
    const displayName = input.value.trim();
    try {
      if (!displayName) {
        await removeOverride(riotId);
      } else {
        await saveOverride(riotId, displayName);
      }
      await refresh();
    } catch (error) { showDashboardError(error.message); }
  });
  const remove = document.createElement("button");
  remove.className = "btn ghost";
  remove.textContent = "Remove";
  remove.disabled = !currentOverride;
  remove.addEventListener("click", async () => {
    try {
      await removeOverride(riotId);
      await refresh();
    } catch (error) { showDashboardError(error.message); }
  });
  actions.append(save, remove);
  row.append(identity, input, actions);
  return row;
}

function renderRoster(state) {
  const root = $("roster");
  root.replaceChildren();
  const roster = state.roster;
  const players = connectedPlayerMap(state.players);
  const overrides = overrideMap(state.nameOverrides);

  if (!roster?.teams?.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "Waiting for a PCMT Spectra frontend using this group code to report the current roster.";
    root.appendChild(empty);
    $("rosterUpdated").textContent = "";
    return;
  }

  lastRosterUpdated = roster.updatedAt;
  $("rosterUpdated").textContent = roster.updatedAt ? `Updated ${new Date(roster.updatedAt).toLocaleTimeString()}` : "";

  const wrap = document.createElement("div");
  wrap.className = "grid";
  roster.teams.forEach((team) => {
    const column = document.createElement("div");
    column.className = "col-6 stack";
    const title = document.createElement("div");
    const heading = document.createElement("h3");
    heading.textContent = team.teamName || team.teamTricode || "Team";
    title.appendChild(heading);
    if (team.teamTricode) {
      const tri = document.createElement("span");
      tri.className = "badge";
      tri.textContent = team.teamTricode;
      title.appendChild(tri);
    }
    column.appendChild(title);
    const list = document.createElement("div");
    list.className = "player-list";
    for (const item of team.players || []) {
      const riotId = item.riotId || item.fullName;
      if (!riotId) continue;
      const normalized = norm(riotId);
      list.appendChild(makeOverrideRow(riotId, overrides.get(normalized), players.get(normalized)));
    }
    if (!list.children.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "Waiting for player names.";
      list.appendChild(empty);
    }
    column.appendChild(list);
    wrap.appendChild(column);
  });
  root.appendChild(wrap);
}

function renderOverrides(state) {
  const root = $("savedOverrides");
  root.replaceChildren();
  const rosterIds = new Set();
  for (const team of state.roster?.teams || []) {
    for (const player of team.players || []) {
      const riotId = player.riotId || player.fullName;
      if (riotId) rosterIds.add(norm(riotId));
    }
  }
  const players = connectedPlayerMap(state.players);
  const extras = (state.nameOverrides || []).filter((entry) => !rosterIds.has(entry.normalizedRiotId));
  if (!extras.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "No additional saved overrides outside the current roster.";
    root.appendChild(empty);
    return;
  }
  for (const entry of extras) {
    root.appendChild(makeOverrideRow(entry.riotId, entry, players.get(entry.normalizedRiotId)));
  }
}

function render(state) {
  session = state;
  setProducerPath(state.sessionId);
  $("sessionId").textContent = state.sessionId;
  $("roomId").textContent = state.roomId;
  $("producerUrl").textContent = buildProducerUrl(state);
  $("currentGroup").value = state.groupCode;
  $("joinUrl").textContent = state.joinUrl;
  $("camsEnabled").checked = !!state.playercamsEnabled;
  const otherAliases = (state.aliases || []).filter((value) => value.toLocaleUpperCase() !== state.groupCode.toLocaleUpperCase());
  $("aliases").textContent = otherAliases.length ? `Previous group code aliases: ${otherAliases.join(", ")}` : "";
  renderPreviews(state);
  renderRoster(state);
  renderOverrides(state);
}

function showDashboardError(message = "") { $("dashboardError").textContent = message; }

async function loadSessionById(sessionId, { silent = false } = {}) {
  if (!producerToken) {
    const message = "This producer URL is missing its private token. Use the full Producer URL that includes #token=...";
    configureResumeCard(sessionId, message);
    return false;
  }
  try {
    const state = await api(`/api/producer/session/${encodeURIComponent(sessionId)}`);
    showDashboard();
    render(state);
    startPolling();
    return true;
  } catch (error) {
    configureResumeCard(sessionId, error.message);
    if (!silent) $("startError").textContent = error.message;
    return false;
  }
}

async function openSession() {
  $("startError").textContent = "";
  if (requestedSessionId && !session) {
    await loadSessionById(requestedSessionId);
    return;
  }

  const groupCode = $("groupCode").value.trim();
  if (!groupCode) {
    $("startError").textContent = "Enter a Spectra group code.";
    return;
  }
  try {
    const result = await api("/api/producer/session", {
      method: "POST",
      body: JSON.stringify({ groupCode }),
    });
    producerToken = result.producerToken || "";
    if (!producerToken) throw new Error("Server did not return a producer token for the new session.");
    showDashboard();
    render(result.session);
    startPolling();
  } catch (error) {
    $("startError").textContent = error.message;
  }
}

async function refresh() {
  if (!session) return;
  try {
    const state = await api(`/api/producer/session/${encodeURIComponent(session.sessionId)}`);
    render(state);
    showDashboardError("");
  } catch (error) {
    showDashboardError(error.message);
    if (error.status === 401 || /not found|expired/i.test(error.message)) stopPolling();
  }
}

function startPolling() {
  stopPolling();
  pollTimer = setInterval(refresh, 2000);
}
function stopPolling() { if (pollTimer) clearInterval(pollTimer); pollTimer = null; }

async function saveOverride(riotId, displayName) {
  if (!session) throw new Error("No active producer session.");
  await api(`/api/producer/session/${encodeURIComponent(session.sessionId)}/name-overrides`, {
    method: "PUT",
    body: JSON.stringify({ riotId, displayName }),
  });
}

async function removeOverride(riotId) {
  if (!session) throw new Error("No active producer session.");
  await api(`/api/producer/session/${encodeURIComponent(session.sessionId)}/name-overrides?riotId=${encodeURIComponent(riotId)}`, { method: "DELETE" });
}

$("openSession").addEventListener("click", openSession);
$("groupCode").addEventListener("keydown", (event) => { if (event.key === "Enter") openSession(); });

$("copyProducer").addEventListener("click", async () => {
  if (!session) return;
  const producerUrl = buildProducerUrl(session);
  try {
    await navigator.clipboard.writeText(producerUrl);
    const original = $("copyProducer").textContent;
    $("copyProducer").textContent = "Copied";
    setTimeout(() => $("copyProducer").textContent = original, 1200);
  } catch {
    showDashboardError("Could not access the clipboard. Copy the producer link manually.");
  }
});

$("rotateProducer").addEventListener("click", async () => {
  if (!session) return;
  if (!confirm("Rotate the producer link? The current producer URL will stop working immediately on other machines.")) return;
  try {
    const result = await api(`/api/producer/session/${encodeURIComponent(session.sessionId)}/producer-token/rotate`, {
      method: "POST",
    });
    producerToken = result.producerToken || "";
    if (!producerToken) throw new Error("Server did not return a replacement producer token.");
    render(result.session);
    showDashboardError("Producer link rotated. The previous producer URL is now invalid.");
  } catch (error) {
    showDashboardError(error.message);
  }
});

$("copyJoin").addEventListener("click", async () => {
  if (!session) return;
  try {
    await navigator.clipboard.writeText(session.joinUrl);
    const original = $("copyJoin").textContent;
    $("copyJoin").textContent = "Copied";
    setTimeout(() => $("copyJoin").textContent = original, 1200);
  } catch {
    showDashboardError("Could not access the clipboard. Copy the link manually.");
  }
});

$("rebindGroup").addEventListener("click", async () => {
  if (!session) return;
  const groupCode = $("currentGroup").value.trim();
  if (!groupCode) return;
  try {
    const state = await api(`/api/producer/session/${encodeURIComponent(session.sessionId)}/group-code`, {
      method: "PUT",
      body: JSON.stringify({ groupCode }),
    });
    render(state);
    showDashboardError("");
  } catch (error) { showDashboardError(error.message); }
});

$("camsEnabled").addEventListener("change", async () => {
  if (!session) return;
  try {
    const state = await api(`/api/producer/session/${encodeURIComponent(session.sessionId)}/enabled`, {
      method: "PUT",
      body: JSON.stringify({ enabled: $("camsEnabled").checked }),
    });
    render(state);
  } catch (error) {
    $("camsEnabled").checked = !$("camsEnabled").checked;
    showDashboardError(error.message);
  }
});

$("manualSave").addEventListener("click", async () => {
  $("overrideError").textContent = "";
  const riotId = $("manualRiotId").value.trim();
  const displayName = $("manualDisplayName").value.trim();
  try {
    await saveOverride(riotId, displayName);
    $("manualRiotId").value = "";
    $("manualDisplayName").value = "";
    await refresh();
  } catch (error) { $("overrideError").textContent = error.message; }
});

$("endSession").addEventListener("click", async () => {
  if (!session) return;
  if (!confirm("End this playercam session? Existing player links will stop working.")) return;
  try {
    await api(`/api/producer/session/${encodeURIComponent(session.sessionId)}/end`, { method: "POST" });
    stopPolling();
    history.replaceState({}, "", "/");
    location.reload();
  } catch (error) { showDashboardError(error.message); }
});


if (requestedSessionId) {
  configureResumeCard(requestedSessionId);
  loadSessionById(requestedSessionId, { silent: true });
}
