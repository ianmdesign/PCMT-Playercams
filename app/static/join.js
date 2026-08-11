const $ = (id) => document.getElementById(id);
const token = decodeURIComponent(location.pathname.split("/").filter(Boolean).pop() || "");
let activeRiotId = "";
let heartbeatTimer = null;

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
    $("embedIdentity").textContent = `${data.riotId} · ${shareType === "media" ? "Media" : "Camera"}`;
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
    await fetch(`/api/join/${encodeURIComponent(token)}/heartbeat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ riotId: activeRiotId }),
      keepalive: true,
    });
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
  $("vdoFrame").src = "about:blank";
  $("embedMode").classList.add("hidden");
  $("joinSetup").classList.remove("hidden");
  document.body.classList.remove("embed-mode");
});
