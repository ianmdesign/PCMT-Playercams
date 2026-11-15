"""Read-only, server-side Spectra roster subscriptions.

Browser overlays have no secret and may not author the current-match roster.
Only match_data received from the operator-configured Spectra output endpoint
can update a roster used by PCMT producer controls.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import socketio

from .storage import normalize_group_code

logger = logging.getLogger(__name__)
MAX_ROSTER_WATCHES = 48


def select_unambiguous_roster(
    rosters: dict[tuple[str, str], dict[str, Any]],
    group_code: str,
    timestamp_ms: int,
    ttl_ms: int,
) -> dict[str, Any] | None:
    """Return one verified source for this group, never mix two active sources."""
    normalized = normalize_group_code(group_code)
    candidates = [
        roster
        for (_endpoint, group), roster in rosters.items()
        if group == normalized
        and 0 <= timestamp_ms - int(roster.get("updatedAt", 0)) <= ttl_ms
    ]
    return candidates[0] if len(candidates) == 1 else None


def parse_spectra_roster(raw: Any, expected_group: str) -> list[dict[str, Any]] | None:
    """Normalize a real Spectra match_data packet; reject mismatched/malformed data."""
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("groupCode"), str):
        return None
    if normalize_group_code(data["groupCode"]) != normalize_group_code(expected_group):
        return None

    teams = data.get("teams")
    if not isinstance(teams, list) or len(teams) != 2:
        return None

    result: list[dict[str, Any]] = []
    for team in teams:
        if not isinstance(team, dict):
            return None
        players = team.get("players")
        if not isinstance(players, list) or len(players) > 10:
            return None
        normalized_players: list[dict[str, str]] = []
        for player in players:
            if not isinstance(player, dict):
                return None
            riot_id = player.get("fullName")
            if not isinstance(riot_id, str) or not (3 <= len(riot_id) <= 128):
                continue
            if "#" not in riot_id or riot_id.startswith("#") or riot_id.endswith("#"):
                continue
            fallback = player.get("name", "")
            normalized_players.append({
                "riotId": riot_id,
                "fallbackName": fallback[:128] if isinstance(fallback, str) else "",
            })
        name = team.get("teamName", "")
        tricode = team.get("teamTricode", "")
        result.append({
            "teamName": name[:128] if isinstance(name, str) else "",
            "teamTricode": tricode[:24] if isinstance(tricode, str) else "",
            "players": normalized_players,
        })
    return result


class SpectraRosterWatcher:
    """Bounded, read-only Socket.IO watches; endpoint cannot come from browsers."""

    def __init__(
        self,
        endpoints: tuple[str, ...] | str,
        on_roster: Callable[[str, str, list[dict[str, Any]]], Awaitable[None]],
        interested: Callable[[str], bool],
    ) -> None:
        self.endpoints = (endpoints,) if isinstance(endpoints, str) else tuple(endpoints)
        self.endpoints = tuple(endpoint for endpoint in self.endpoints if endpoint)
        self.on_roster = on_roster
        self.interested = interested
        self.tasks: dict[tuple[str, str], asyncio.Task[None]] = {}

    def ensure_watch(self, group_code: str) -> bool:
        group_code = normalize_group_code(group_code)
        if not self.endpoints or not group_code or len(group_code) > 64:
            return False
        for key, task in list(self.tasks.items()):
            if task.done():
                self.tasks.pop(key, None)
        started = False
        for endpoint in self.endpoints:
            key = (endpoint, group_code)
            if key in self.tasks:
                started = True
                continue
            if len(self.tasks) >= MAX_ROSTER_WATCHES:
                logger.warning("Reached maximum concurrent trusted Spectra roster watches")
                break
            task = asyncio.create_task(
                self._watch_loop(endpoint, group_code),
                name=f"spectra-roster-{len(self.tasks) + 1}",
            )
            self.tasks[key] = task
            started = True
        return started

    async def _watch_loop(self, endpoint: str, group_code: str) -> None:
        delay = 5
        try:
            while self.interested(group_code):
                client = socketio.AsyncClient(reconnection=False, request_timeout=5)

                @client.event
                async def connect() -> None:
                    await client.emit("logon", json.dumps({"groupCode": group_code}))

                @client.on("match_data")
                async def match_data(raw: Any) -> None:
                    roster = parse_spectra_roster(raw, group_code)
                    if roster is not None:
                        try:
                            await self.on_roster(endpoint, group_code, roster)
                        except Exception:
                            logger.exception("Unable to publish trusted Spectra roster for %s", group_code)

                try:
                    await asyncio.wait_for(client.connect(endpoint, wait_timeout=10), timeout=12)
                    delay = 5
                    while client.connected and self.interested(group_code):
                        await asyncio.sleep(10)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Spectra roster watch %s at %s disconnected: %s", group_code, endpoint, exc)
                finally:
                    if client.connected:
                        await client.disconnect()

                if self.interested(group_code):
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 60)
        finally:
            # A newer task may already have replaced an earlier completed watch.
            key = (endpoint, group_code)
            if self.tasks.get(key) is asyncio.current_task():
                self.tasks.pop(key, None)
