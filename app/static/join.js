const $ = (id) => document.getElementById(id);
const token = decodeURIComponent(location.pathname.split("/").filter(Boolean).pop() || "");
let activeConnectionRiotId = "";
let activeShareType = "";
let micEnabled = false;
let micSwitching = false;
let heartbeatTimer = null;
const rememberedRiotIdKey = "pcmt-playercams-riot-id";

function getRememberedRiotId() {
  try {
    return (localStorage.getItem(rememberedRiotIdKey) || "").trim();
  } catch {
    return "";
  }
}

function rememberRiotId(riotId) {
  const value = String(riotId || "").trim();
  if (!value) return;
  try {
    localStorage.setItem(rememberedRiotIdKey, value);
  } catch {
    // Browser storage can be unavailable in private/restricted contexts.
  }
  if ($("riotId").value !== value) {
    $("riotId").value = value;
  }
}

function renderEmbedIdentity(riotId, displayName, shareType) {
  const root = $("embedIdentity");
  root.replaceChildren();

  const primary = document.createElement("strong");
  primary.textContent = displayName || riotId;

  const secondary = document.createElement("span");
  const details = [];
  if (displayName) details.push(riotId);
  details.push(shareType === "media" ? "Media" : "Camera");
  secondary.textContent = details.join(" · ");

  root.append(primary, secondary);
}

async function register(shareType) {
  $("joinError").textContent = "";
  const riotId = $("riotId").value.trim();
  if (!riotId.includes("#") || riotId.startsWith("#") || riotId.endsWith("#")) {
    $("joinError").textContent = "Enter your full Riot ID including # and tagline.";
    return;
  }
  try {
    const response = await fetch(`/api/join/${encodeURIComponent(token)}/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ riotId, shareType, audioEnabled: $("shareMic").checked }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data?.detail || "Could not join the playercam session.");

    activeConnectionRiotId = data.connectionRiotId || riotId;
    activeShareType = shareType;
    rememberRiotId(data.riotId || riotId);
    renderEmbedIdentity(data.riotId, data.displayName || "", shareType);

    $("vdoFrame").src = data.vdoUrl;
    setMicView(Boolean(data.audioUrl), data.audioUrl || "");
    $("joinSetup").classList.add("hidden");
    $("embedMode").classList.remove("hidden");
    document.body.classList.add("embed-mode");
    startHeartbeat();
  } catch (error) {
    $("joinError").textContent = error.message;
  }
}

async function heartbeat() {
  if (!activeConnectionRiotId) return;
  try {
    const response = await fetch(`/api/join/${encodeURIComponent(token)}/heartbeat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ riotId: activeConnectionRiotId }),
      keepalive: true,
    });
    if (!response.ok) return;
    const data = await response.json();
    const effectiveRiotId = data.riotId || activeConnectionRiotId;
    // A producer correction changes only the session identity; the live VDO
    // connection remains on activeConnectionRiotId. Persist the corrected Riot
    // ID locally so this browser pre-fills it for future playercam sessions.
    rememberRiotId(effectiveRiotId);
    renderEmbedIdentity(
      effectiveRiotId,
      data.displayName || "",
      activeShareType,
    );
  } catch { /* status is best effort */ }
}

function setMicView(enabled, audioUrl = "") {
  // Clearing the iframe ends local capture immediately. The publisher is
  // always deafened and has no incoming room media, even if a player unmutes.
  micEnabled = enabled;
  $("micFrame").src = enabled && audioUrl ? audioUrl : "about:blank";
  $("toggleMic").textContent = enabled ? "Stop sharing microphone" : "Enable microphone";
  $("micStatus").textContent = enabled
    ? "Microphone requested. Browser permission may be required; you cannot hear other players."
    : "Microphone off. No audio sent to production.";
}

async function toggleMicrophone() {
  if (micSwitching || !activeConnectionRiotId) return;
  micSwitching = true;
  $("toggleMic").disabled = true;
  const nextEnabled = !micEnabled;
  // Mute immediately, even if the API request takes time or fails.
  if (!nextEnabled) setMicView(false);
  try {
    const response = await fetch(`/api/join/${encodeURIComponent(token)}/audio`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ riotId: activeConnectionRiotId, enabled: nextEnabled }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data?.detail || "Could not change microphone setting.");
    setMicView(Boolean(data.audioEnabled), data.audioUrl || "");
  } catch (error) {
    $("micStatus").textContent = error.message;
    if (!nextEnabled) setMicView(false);
  } finally {
    micSwitching = false;
    $("toggleMic").disabled = false;
  }
}

function startHeartbeat() {
  if (heartbeatTimer) clearInterval(heartbeatTimer);
  heartbeat();
  heartbeatTimer = setInterval(heartbeat, 20000);
}

const rememberedRiotId = getRememberedRiotId();
if (rememberedRiotId) {
  $("riotId").value = rememberedRiotId;
}

$("shareVideo").addEventListener("click", () => register("video"));
$("shareMedia").addEventListener("click", () => register("media"));
$("toggleMic").addEventListener("click", toggleMicrophone);
$("riotId").addEventListener("keydown", (event) => { if (event.key === "Enter") register("video"); });
$("backButton").addEventListener("click", () => {
  if (heartbeatTimer) clearInterval(heartbeatTimer);
  heartbeatTimer = null;
  const previousRiotId = activeConnectionRiotId;
  activeConnectionRiotId = "";
  activeShareType = "";
  setMicView(false);
  if (previousRiotId) fetch(`/api/join/${encodeURIComponent(token)}/audio`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ riotId: previousRiotId, enabled: false }),
    keepalive: true,
  }).catch(() => {});
  $("vdoFrame").src = "about:blank";
  $("embedIdentity").replaceChildren();
  $("embedMode").classList.add("hidden");
  $("joinSetup").classList.remove("hidden");
  document.body.classList.remove("embed-mode");
});
