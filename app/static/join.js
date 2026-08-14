const $ = (id) => document.getElementById(id);
const token = decodeURIComponent(location.pathname.split("/").filter(Boolean).pop() || "");
let activeRiotId = "";
let activeShareType = "";
let heartbeatTimer = null;

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
      body: JSON.stringify({ riotId, shareType }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data?.detail || "Could not join the playercam session.");

    activeRiotId = data.riotId;
    activeShareType = shareType;
    renderEmbedIdentity(data.riotId, data.displayName || "", shareType);

    $("vdoFrame").src = data.vdoUrl;
    $("joinSetup").classList.add("hidden");
    $("embedMode").classList.remove("hidden");
    document.body.classList.add("embed-mode");
    startHeartbeat();
  } catch (error) {
    $("joinError").textContent = error.message;
  }
}

async function heartbeat() {
  if (!activeRiotId) return;
  try {
    const response = await fetch(`/api/join/${encodeURIComponent(token)}/heartbeat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ riotId: activeRiotId }),
      keepalive: true,
    });
    if (!response.ok) return;
    const data = await response.json();
    renderEmbedIdentity(
      data.riotId || activeRiotId,
      data.displayName || "",
      activeShareType,
    );
  } catch { /* status is best effort */ }
}

function startHeartbeat() {
  if (heartbeatTimer) clearInterval(heartbeatTimer);
  heartbeat();
  heartbeatTimer = setInterval(heartbeat, 20000);
}

$("shareVideo").addEventListener("click", () => register("video"));
$("shareMedia").addEventListener("click", () => register("media"));
$("riotId").addEventListener("keydown", (event) => { if (event.key === "Enter") register("video"); });
$("backButton").addEventListener("click", () => {
  if (heartbeatTimer) clearInterval(heartbeatTimer);
  heartbeatTimer = null;
  activeRiotId = "";
  activeShareType = "";
  $("vdoFrame").src = "about:blank";
  $("embedIdentity").replaceChildren();
  $("embedMode").classList.add("hidden");
  $("joinSetup").classList.remove("hidden");
  document.body.classList.remove("embed-mode");
});
