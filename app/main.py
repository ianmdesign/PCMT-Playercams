from __future__ import annotations

from pathlib import Path
from typing import Any

import socketio
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .admin_auth import AdminAuth
from .storage import DATA_DIR, AppConfig, OverrideStore, SessionStore, normalize_group_code, normalize_riot_id, now_ms, stream_id_for_riot_id, audio_stream_id_for_player
from .session_identity import SessionRiotIdStore
from .spectra_roster import SpectraRosterWatcher, select_unambiguous_roster
from .vdo import build_preview_url, build_publish_url, build_audio_publish_url


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

config = AppConfig.load()
sessions = SessionStore(config=config)
overrides = OverrideStore()
session_riot_ids = SessionRiotIdStore()
admin_auth = AdminAuth(DATA_DIR, "pcmt_playercams_admin")

sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
app = FastAPI(title="PCMT Playercams", version="0.2.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Trusted Spectra output rosters are transient. Unauthenticated browser-submitted
# frontend_roster events are intentionally ignored.
rosters: dict[tuple[str, str], dict[str, Any]] = {}
frontend_groups: dict[str, str] = {}
frontend_sessions: dict[str, str | None] = {}


class SessionRequest(BaseModel):
    groupCode: str = Field(min_length=1, max_length=64)


class AdminLoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class RebindRequest(BaseModel):
    groupCode: str = Field(min_length=1, max_length=64)


class EnabledRequest(BaseModel):
    enabled: bool


class ListenInRequest(BaseModel):
    listenIn: bool | str


class AudioEnabledRequest(BaseModel):
    riotId: str = Field(min_length=3, max_length=128)
    enabled: bool


class OverrideRequest(BaseModel):
    riotId: str = Field(min_length=3, max_length=128)
    displayName: str = Field(min_length=1, max_length=64)


class JoinRequest(BaseModel):
    riotId: str = Field(min_length=3, max_length=128)
    shareType: str
    audioEnabled: bool = False


class HeartbeatRequest(BaseModel):
    riotId: str = Field(min_length=3, max_length=128)


class CorrectPlayerRiotIdRequest(BaseModel):
    connectionRiotId: str = Field(min_length=3, max_length=128)
    riotId: str = Field(min_length=3, max_length=128)


def require_session_token(session_id: str, x_producer_token: str | None) -> dict[str, Any]:
    session = sessions.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Playercam session not found or expired")
    if not sessions.verify_producer_token(session_id, x_producer_token):
        raise HTTPException(status_code=401, detail="Invalid or missing producer link token")
    return session


ROSTER_TTL_MS = 120_000


def get_trusted_roster(group_code: str) -> dict[str, Any] | None:
    return select_unambiguous_roster(rosters, group_code, now_ms(), ROSTER_TTL_MS)


def get_latest_roster(session: dict[str, Any]) -> dict[str, Any] | None:
    candidates = [
        roster
        for alias in sessions.list_aliases(session["sessionId"])
        if (roster := get_trusted_roster(alias)) is not None
    ]
    return max(candidates, key=lambda item: item["updatedAt"]) if candidates else None


def scoped_name_overrides(group_code: str, session: dict[str, Any] | None) -> list[list[str]]:
    """Only disclose display names for the verified roster or joined players."""
    roster = get_latest_roster(session) if session else get_trusted_roster(group_code)
    visible_ids: set[str] = set()
    if roster:
        for team in roster["teams"]:
            visible_ids.update(normalize_riot_id(player["riotId"]) for player in team["players"])
    if session:
        visible_ids.update(normalize_riot_id(player["riotId"]) for player in effective_players(session["sessionId"]))
    return [
        [riot_id, display_name]
        for riot_id, display_name in overrides.as_pairs()
        if normalize_riot_id(riot_id) in visible_ids
    ]


def get_override_display_name(riot_id: str) -> str | None:
    normalized = (riot_id or "").strip().casefold()
    if not normalized:
        return None
    for entry in overrides.list_entries():
        if str(entry.get("normalizedRiotId", "")).casefold() == normalized:
            display_name = str(entry.get("displayName", "")).strip()
            return display_name or None
    return None


def effective_players(session_id: str) -> list[dict[str, Any]]:
    return session_riot_ids.apply_to_players(
        session_id,
        sessions.list_players(session_id),
    )


def build_producer_state(session: dict[str, Any]) -> dict[str, Any]:
    players = effective_players(session["sessionId"])
    for player in players:
        # The VDO stream remains keyed to the Riot ID originally entered by the
        # player. A session correction changes matching/display identity only.
        preview_url, stream_id = build_preview_url(
            session["roomId"],
            player["connectionRiotId"],
            config.producer_preview_bitrate_kbps,
        )
        player["streamId"] = stream_id
        player["previewUrl"] = preview_url
    return {
        **session,
        "joinUrl": f"{config.public_base_url}/join/{session['joinToken']}",
        # The private producer token is deliberately not included here. The browser
        # keeps it in the URL fragment and attaches it to authenticated API requests.
        "producerUrl": f"{config.public_base_url}/producer/{session['sessionId']}",
        "aliases": sessions.list_aliases(session["sessionId"]),
        "players": players,
        "producerPreviewBitrateKbps": config.producer_preview_bitrate_kbps,
        "nameOverrides": overrides.list_entries(),
        "roster": get_latest_roster(session),
    }


def build_frontend_state(group_code: str, session_override: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized = normalize_group_code(group_code)
    session = session_override or sessions.resolve_group(normalized)
    playercams_info: dict[str, Any] | None = None
    session_id: str | None = None
    if session:
        session_id = session["sessionId"]
        players = effective_players(session_id)
        playercams_info = {
            "enable": session["playercamsEnabled"],
            "listenIn": session["listenIn"],
            "identifier": session["roomId"],
            "listenInRoom": f"{session['roomId']}-listenin",
            "audioStreamMappings": build_listen_in_state(session)["audioStreamMappings"],
            "enabledPlayers": [player["riotId"] for player in players],
            # Effective Riot ID -> actual live VDO stream ID. This lets a producer
            # correct a mistyped Riot ID without asking the player to reconnect.
            "streamMappings": {
                player["riotId"]: stream_id_for_riot_id(player["connectionRiotId"])
                for player in players
            },
        }
    return {
        "groupCode": normalized,
        "sessionId": session_id,
        "playercamsInfo": playercams_info,
        # Preserve Spectra's array-of-tuples format without exposing the whole
        # persistent override database to anonymous frontend subscribers.
        "nameOverrides": scoped_name_overrides(normalized, session),
    }


async def frontend_state_for_sid(sid: str) -> dict[str, Any] | None:
    group_code = frontend_groups.get(sid)
    if not group_code:
        return None

    pinned_session_id = frontend_sessions.get(sid)
    pinned_session = sessions.get_session(pinned_session_id) if pinned_session_id else None
    if not pinned_session:
        pinned_session = sessions.resolve_group(group_code)
        frontend_sessions[sid] = pinned_session["sessionId"] if pinned_session else None

    return build_frontend_state(group_code, pinned_session)


def build_listen_in_state(session: dict[str, Any]) -> dict[str, Any]:
    side = session["listenIn"]
    result: dict[str, Any] = {"sessionId": session["sessionId"], "listenIn": side, "audioStreamMappings": {}}
    if side not in ("left", "right") or not session["playercamsEnabled"]:
        return result
    roster = get_latest_roster(session)
    teams = roster.get("teams", []) if roster else []
    team_index = 0 if side == "left" else 1
    if len(teams) <= team_index:
        return result
    permitted = {
        normalize_riot_id(p["riotId"])
        for p in teams[team_index].get("players", [])
        if isinstance(p, dict) and isinstance(p.get("riotId"), str)
    }
    result["audioStreamMappings"] = {
        p["riotId"]: audio_stream_id_for_player(session, p["connectionRiotId"])
        for p in effective_players(session["sessionId"])
        if p.get("audioEnabled") and normalize_riot_id(p["riotId"]) in permitted
    }
    return result


async def emit_state_to_all_frontends() -> None:
    for sid in list(frontend_groups.keys()):
        try:
            state = await frontend_state_for_sid(sid)
            if state:
                await sio.emit("pcmt_tools_data", state, to=sid)
        except Exception:
            # A disconnect event will clean the normal path.
            pass


async def receive_trusted_roster(
    endpoint: str, group_code: str, teams: list[dict[str, Any]]
) -> None:
    group_code = normalize_group_code(group_code)
    previous = get_trusted_roster(group_code)
    rosters[(endpoint, group_code)] = {
        "teams": teams, "updatedAt": now_ms(), "spectraEndpoint": endpoint
    }
    current = get_trusted_roster(group_code)
    if (previous is None) != (current is None) or (
        previous is not None and current is not None and previous["teams"] != current["teams"]
    ):
        await emit_state_to_all_frontends()


def roster_watch_needed(group_code: str) -> bool:
    return group_code in frontend_groups.values() or sessions.resolve_group(group_code) is not None


roster_watcher = SpectraRosterWatcher(
    config.spectra_output_endpoints or config.spectra_output_endpoint,
    receive_trusted_roster,
    roster_watch_needed,
)


@app.get("/")
async def producer_page() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "producer.html",
        headers={"Strict-Transport-Security": "max-age=31536000"},
    )


@app.get("/producer/{session_id}")
async def producer_session_page(session_id: str) -> FileResponse:
    # The session identifier is not authentication. Serve the producer shell and
    # let the token-protected API decide whether the URL fragment is valid.
    return FileResponse(
        STATIC_DIR / "producer.html",
        headers={"Strict-Transport-Security": "max-age=31536000"},
    )


@app.get("/join/{token}")
async def join_page(token: str) -> FileResponse:
    if not sessions.get_session_by_token(token):
        raise HTTPException(status_code=404, detail="Playercam session not found or expired")
    return FileResponse(STATIC_DIR / "join.html")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/status")
async def status() -> dict[str, Any]:
    return {"status": "UP", "version": "0.2.0"}


@app.get("/api/admin/session")
async def admin_session(request: Request, response: Response) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "Cookie"
    return admin_auth.status(request)


@app.post("/api/admin/login")
async def admin_login(payload: AdminLoginRequest, request: Request, response: Response) -> dict[str, Any]:
    return admin_auth.login(request, response, payload.password)


@app.post("/api/admin/logout")
async def admin_logout(request: Request, response: Response) -> dict[str, bool]:
    admin_auth.require(request)
    admin_auth.logout(response)
    return {"signedOut": True}


@app.post("/api/producer/session")
async def create_session(payload: SessionRequest, request: Request) -> JSONResponse:
    admin_auth.require(request)
    try:
        session, created, producer_token = sessions.create_or_reuse(payload.groupCode)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if not created or not producer_token:
        raise HTTPException(
            status_code=409,
            detail="An active playercam session already exists for this group code. Open its private producer link instead.",
        )

    roster_watcher.ensure_watch(session["groupCode"])
    await emit_state_to_all_frontends()
    return JSONResponse(
        {
            "created": True,
            "producerToken": producer_token,
            "session": build_producer_state(session),
        },
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/producer/session/{session_id}")
async def producer_session(
    session_id: str,
    x_producer_token: str | None = Header(default=None),
) -> dict[str, Any]:
    session = require_session_token(session_id, x_producer_token)
    roster_watcher.ensure_watch(session["groupCode"])
    return build_producer_state(session)


@app.post("/api/producer/session/{session_id}/producer-token/rotate")
async def rotate_producer_token(
    session_id: str,
    x_producer_token: str | None = Header(default=None),
) -> dict[str, Any]:
    session = require_session_token(session_id, x_producer_token)
    try:
        new_token = sessions.rotate_producer_token(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "producerToken": new_token,
        "session": build_producer_state(session),
    }


@app.put("/api/producer/session/{session_id}/group-code")
async def rebind_group_code(
    session_id: str,
    payload: RebindRequest,
    x_producer_token: str | None = Header(default=None),
) -> dict[str, Any]:
    require_session_token(session_id, x_producer_token)
    try:
        session = sessions.rebind_group(session_id, payload.groupCode)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    roster_watcher.ensure_watch(session["groupCode"])
    await emit_state_to_all_frontends()
    return build_producer_state(session)


@app.put("/api/producer/session/{session_id}/enabled")
async def set_enabled(
    session_id: str,
    payload: EnabledRequest,
    x_producer_token: str | None = Header(default=None),
) -> dict[str, Any]:
    require_session_token(session_id, x_producer_token)
    try:
        session = sessions.set_playercams_enabled(session_id, payload.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await emit_state_to_all_frontends()
    return build_producer_state(session)


@app.put("/api/producer/session/{session_id}/listen-in")
async def set_listen_in(
    session_id: str,
    payload: ListenInRequest,
    x_producer_token: str | None = Header(default=None),
) -> dict[str, Any]:
    require_session_token(session_id, x_producer_token)
    if payload.listenIn not in (False, "left", "right") or payload.listenIn is True:
        raise HTTPException(status_code=400, detail="Listen-In must be false, left or right")
    try:
        session = sessions.set_listen_in(session_id, payload.listenIn)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await emit_state_to_all_frontends()
    return build_producer_state(session)


@app.post("/api/producer/session/{session_id}/end")
async def end_session(
    session_id: str,
    x_producer_token: str | None = Header(default=None),
) -> dict[str, bool]:
    require_session_token(session_id, x_producer_token)
    sessions.end_session(session_id)
    session_riot_ids.clear_session(session_id)
    await emit_state_to_all_frontends()
    return {"ended": True}


@app.put("/api/producer/session/{session_id}/players/riot-id")
async def correct_player_riot_id(
    session_id: str,
    payload: CorrectPlayerRiotIdRequest,
    x_producer_token: str | None = Header(default=None),
) -> dict[str, Any]:
    session = require_session_token(session_id, x_producer_token)
    try:
        player = session_riot_ids.set_correction(
            session_id,
            payload.connectionRiotId,
            payload.riotId,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await emit_state_to_all_frontends()
    return {
        "player": player,
        "session": build_producer_state(session),
    }


@app.get("/api/producer/session/{session_id}/name-overrides")
async def get_name_overrides(
    session_id: str,
    x_producer_token: str | None = Header(default=None),
) -> dict[str, Any]:
    require_session_token(session_id, x_producer_token)
    return {"overrides": overrides.list_entries()}


@app.put("/api/producer/session/{session_id}/name-overrides")
async def put_name_override(
    session_id: str,
    payload: OverrideRequest,
    x_producer_token: str | None = Header(default=None),
) -> dict[str, Any]:
    require_session_token(session_id, x_producer_token)
    try:
        entry = overrides.upsert(payload.riotId, payload.displayName)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await emit_state_to_all_frontends()
    return entry


@app.delete("/api/producer/session/{session_id}/name-overrides")
async def delete_name_override(
    session_id: str,
    riotId: str,
    x_producer_token: str | None = Header(default=None),
) -> dict[str, bool]:
    require_session_token(session_id, x_producer_token)
    removed = overrides.remove(riotId)
    if removed:
        await emit_state_to_all_frontends()
    return {"removed": removed}


@app.post("/api/join/{token}/register")
async def register_player(token: str, payload: JoinRequest) -> dict[str, Any]:
    session = sessions.get_session_by_token(token)
    if not session:
        raise HTTPException(status_code=400, detail="Playercam session is not active")
    try:
        # If this player was already corrected in this session and reloads the
        # page using the corrected Riot ID, keep the original connection identity
        # so VDO.Ninja republishes to the same live stream ID.
        connection_riot_id = session_riot_ids.connection_riot_id_for_effective(
            session["sessionId"],
            payload.riotId,
        )
        session, player = sessions.register_player(
            token,
            connection_riot_id,
            payload.shareType,
            payload.audioEnabled,
        )
        vdo_url, stream_id = build_publish_url(
            session["roomId"],
            player["riotId"],
            player["shareType"],
            config.publisher_max_bitrate_kbps,
            config.publisher_total_bitrate_kbps,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    effective_player = session_riot_ids.apply_to_player(session["sessionId"], player)
    await emit_state_to_all_frontends()
    return {
        "sessionId": session["sessionId"],
        "roomId": session["roomId"],
        "connectionRiotId": player["riotId"],
        "riotId": effective_player["riotId"],
        "displayName": get_override_display_name(effective_player["riotId"]),
        "shareType": player["shareType"],
        "streamId": stream_id,
        "vdoUrl": vdo_url,
        "audioUrl": build_audio_publish_url(
            session["roomId"], player["riotId"],
            audio_stream_id_for_player(session, player["riotId"]),
        ) if payload.audioEnabled else None,
    }


@app.put("/api/join/{token}/audio")
async def set_player_audio(token: str, payload: AudioEnabledRequest) -> dict[str, Any]:
    session = sessions.get_session_by_token(token)
    if not session:
        raise HTTPException(status_code=400, detail="Playercam session is not active")
    try:
        sessions.set_audio_enabled(token, payload.riotId, payload.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await emit_state_to_all_frontends()
    return {
        "audioEnabled": payload.enabled,
        "audioUrl": build_audio_publish_url(
            session["roomId"], payload.riotId,
            audio_stream_id_for_player(session, payload.riotId),
        ) if payload.enabled else None,
    }


@app.post("/api/join/{token}/heartbeat")
async def player_heartbeat(token: str, payload: HeartbeatRequest) -> dict[str, Any]:
    session = sessions.get_session_by_token(token)
    if not session:
        raise HTTPException(status_code=400, detail="Playercam session is not active")
    try:
        sessions.heartbeat_player(token, payload.riotId)
        effective_riot_id = session_riot_ids.get_effective_riot_id(
            session["sessionId"],
            payload.riotId,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "connectionRiotId": payload.riotId,
        "riotId": effective_riot_id,
        "displayName": get_override_display_name(effective_riot_id),
    }


@sio.event
async def connect(sid: str, environ: dict[str, Any], auth: Any = None) -> None:
    return None


@sio.event
async def disconnect(sid: str) -> None:
    frontend_groups.pop(sid, None)
    frontend_sessions.pop(sid, None)


@sio.on("frontend_logon")
async def frontend_logon(sid: str, data: Any) -> None:
    if not isinstance(data, dict):
        return
    group_code = normalize_group_code(str(data.get("groupCode", "")))
    if not group_code or len(group_code) > 64:
        return
    frontend_groups[sid] = group_code
    resolved = sessions.resolve_group(group_code)
    frontend_sessions[sid] = resolved["sessionId"] if resolved else None
    roster_watcher.ensure_watch(group_code)
    await sio.emit("pcmt_tools_data", build_frontend_state(group_code, resolved), to=sid)


# Legacy frontend_roster messages are deliberately ignored: only trusted
# Spectra output can populate the producer's match roster.


application = socketio.ASGIApp(sio, other_asgi_app=app)
