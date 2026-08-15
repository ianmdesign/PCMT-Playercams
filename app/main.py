from __future__ import annotations

from pathlib import Path
from typing import Any

import socketio
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .storage import AppConfig, OverrideStore, SessionStore, normalize_group_code, stream_id_for_riot_id
from .session_identity import SessionRiotIdStore
from .vdo import build_preview_url, build_publish_url


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

config = AppConfig.load()
sessions = SessionStore(config=config)
overrides = OverrideStore()
session_riot_ids = SessionRiotIdStore()

sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
app = FastAPI(title="PCMT Playercams", version="0.2.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Current Spectra rosters are transient. The frontend republishes them whenever
# match data changes or its Socket.IO connection is re-established.
rosters: dict[str, dict[str, Any]] = {}
frontend_groups: dict[str, str] = {}
frontend_sessions: dict[str, str | None] = {}


class SessionRequest(BaseModel):
    groupCode: str = Field(min_length=1, max_length=64)


class RebindRequest(BaseModel):
    groupCode: str = Field(min_length=1, max_length=64)


class EnabledRequest(BaseModel):
    enabled: bool


class OverrideRequest(BaseModel):
    riotId: str = Field(min_length=3, max_length=128)
    displayName: str = Field(min_length=1, max_length=64)


class JoinRequest(BaseModel):
    riotId: str = Field(min_length=3, max_length=128)
    shareType: str


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


def get_latest_roster(session: dict[str, Any]) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for alias in sessions.list_aliases(session["sessionId"]):
        roster = rosters.get(normalize_group_code(alias))
        if roster:
            candidates.append(roster)
    if not candidates:
        return None
    return max(candidates, key=lambda item: int(item.get("updatedAt", 0)))


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
            "identifier": session["roomId"],
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
        # Preserve the same array-of-tuples shape Spectra serializes for Map.
        "nameOverrides": overrides.as_pairs(),
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


async def emit_state_to_all_frontends() -> None:
    if not frontend_groups:
        return
    for sid in list(frontend_groups.keys()):
        try:
            state = await frontend_state_for_sid(sid)
            if state:
                await sio.emit("pcmt_tools_data", state, to=sid)
        except Exception:
            # A disconnect event will clean the normal path. This keeps one stale
            # SID from blocking an update to all other connected overlays.
            pass


@app.get("/")
async def producer_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "producer.html")


@app.get("/producer/{session_id}")
async def producer_session_page(session_id: str) -> FileResponse:
    # The session identifier is not authentication. Serve the producer shell and
    # let the token-protected API decide whether the URL fragment is valid.
    return FileResponse(STATIC_DIR / "producer.html")


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


@app.post("/api/producer/session")
async def create_session(payload: SessionRequest) -> JSONResponse:
    try:
        session, created, producer_token = sessions.create_or_reuse(payload.groupCode)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if not created or not producer_token:
        raise HTTPException(
            status_code=409,
            detail="An active playercam session already exists for this group code. Open its private producer link instead.",
        )

    await emit_state_to_all_frontends()
    return JSONResponse(
        {
            "created": True,
            "producerToken": producer_token,
            "session": build_producer_state(session),
        }
    )


@app.get("/api/producer/session/{session_id}")
async def producer_session(
    session_id: str,
    x_producer_token: str | None = Header(default=None),
) -> dict[str, Any]:
    session = require_session_token(session_id, x_producer_token)
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
    if not group_code:
        return
    frontend_groups[sid] = group_code
    resolved = sessions.resolve_group(group_code)
    frontend_sessions[sid] = resolved["sessionId"] if resolved else None
    await sio.emit("pcmt_tools_data", build_frontend_state(group_code, resolved), to=sid)


@sio.on("frontend_roster")
async def frontend_roster(sid: str, data: Any) -> None:
    if not isinstance(data, dict):
        return
    group_code = frontend_groups.get(sid)
    if not group_code:
        return
    teams = data.get("teams")
    if not isinstance(teams, list):
        return
    rosters[group_code] = {
        "teams": teams,
        "updatedAt": int(data.get("updatedAt") or 0),
    }


application = socketio.ASGIApp(sio, other_asgi_app=app)
