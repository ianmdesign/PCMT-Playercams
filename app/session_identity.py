from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .storage import DB_PATH, normalize_riot_id, now_ms, validate_riot_id


class SessionRiotIdStore:
    """Persist producer corrections without changing a live VDO.Ninja stream ID.

    The players table keeps the Riot ID a player originally entered. That original
    identity is the connection identity and therefore remains the source of the
    VDO.Ninja stream ID for the lifetime of the connection.

    This store overlays a corrected Riot ID for matching/display purposes only.
    Corrections are scoped to one playercam session and are never global.
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS player_identity_corrections (
                    session_id TEXT NOT NULL,
                    connection_riot_norm TEXT NOT NULL,
                    corrected_riot_id TEXT NOT NULL,
                    corrected_riot_norm TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (session_id, connection_riot_norm),
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_player_identity_corrections_effective
                ON player_identity_corrections (session_id, corrected_riot_norm);
                """
            )

    def list_corrections(self, session_id: str) -> dict[str, dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT connection_riot_norm, corrected_riot_id, corrected_riot_norm
                FROM player_identity_corrections
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchall()
        return {
            str(row["connection_riot_norm"]): {
                "riotId": str(row["corrected_riot_id"]),
                "normalizedRiotId": str(row["corrected_riot_norm"]),
            }
            for row in rows
        }

    def apply_to_player(self, session_id: str, player: dict[str, Any]) -> dict[str, Any]:
        result = dict(player)
        connection_riot_id = str(player["riotId"])
        connection_norm = str(player["normalizedRiotId"])

        result["connectionRiotId"] = connection_riot_id
        result["connectionNormalizedRiotId"] = connection_norm
        result["riotIdCorrected"] = False

        correction = self.list_corrections(session_id).get(connection_norm)
        if correction:
            result["riotId"] = correction["riotId"]
            result["normalizedRiotId"] = correction["normalizedRiotId"]
            result["riotIdCorrected"] = True

        return result

    def apply_to_players(
        self,
        session_id: str,
        players: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        corrections = self.list_corrections(session_id)
        results: list[dict[str, Any]] = []

        for player in players:
            result = dict(player)
            connection_riot_id = str(player["riotId"])
            connection_norm = str(player["normalizedRiotId"])

            result["connectionRiotId"] = connection_riot_id
            result["connectionNormalizedRiotId"] = connection_norm
            result["riotIdCorrected"] = False

            correction = corrections.get(connection_norm)
            if correction:
                result["riotId"] = correction["riotId"]
                result["normalizedRiotId"] = correction["normalizedRiotId"]
                result["riotIdCorrected"] = True

            results.append(result)

        return results

    def set_correction(
        self,
        session_id: str,
        connection_riot_id: str,
        corrected_riot_id: str,
    ) -> dict[str, Any]:
        connection_riot_id = validate_riot_id(connection_riot_id)
        corrected_riot_id = validate_riot_id(corrected_riot_id)
        connection_norm = normalize_riot_id(connection_riot_id)
        corrected_norm = normalize_riot_id(corrected_riot_id)

        with self._connect() as conn:
            source = conn.execute(
                """
                SELECT riot_id, riot_norm
                FROM players
                WHERE session_id = ? AND riot_norm = ?
                """,
                (session_id, connection_norm),
            ).fetchone()
            if not source:
                raise ValueError("That player is not registered in this session")

            # Entering the original connection identity removes the correction.
            if corrected_norm == connection_norm:
                conn.execute(
                    """
                    DELETE FROM player_identity_corrections
                    WHERE session_id = ? AND connection_riot_norm = ?
                    """,
                    (session_id, connection_norm),
                )
                return {
                    "connectionRiotId": str(source["riot_id"]),
                    "riotId": str(source["riot_id"]),
                    "normalizedRiotId": str(source["riot_norm"]),
                    "riotIdCorrected": False,
                }

            # A corrected Riot ID must uniquely identify one connected player in
            # this session. Compare against each other player's effective ID.
            rows = conn.execute(
                """
                SELECT
                    p.riot_norm AS connection_riot_norm,
                    COALESCE(c.corrected_riot_norm, p.riot_norm) AS effective_riot_norm
                FROM players p
                LEFT JOIN player_identity_corrections c
                  ON c.session_id = p.session_id
                 AND c.connection_riot_norm = p.riot_norm
                WHERE p.session_id = ? AND p.riot_norm != ?
                """,
                (session_id, connection_norm),
            ).fetchall()
            if any(str(row["effective_riot_norm"]) == corrected_norm for row in rows):
                raise ValueError("That Riot ID is already assigned to another player in this session")

            conn.execute(
                """
                INSERT INTO player_identity_corrections (
                    session_id,
                    connection_riot_norm,
                    corrected_riot_id,
                    corrected_riot_norm,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id, connection_riot_norm) DO UPDATE SET
                    corrected_riot_id = excluded.corrected_riot_id,
                    corrected_riot_norm = excluded.corrected_riot_norm,
                    updated_at = excluded.updated_at
                """,
                (
                    session_id,
                    connection_norm,
                    corrected_riot_id,
                    corrected_norm,
                    now_ms(),
                ),
            )

        return {
            "connectionRiotId": str(source["riot_id"]),
            "riotId": corrected_riot_id,
            "normalizedRiotId": corrected_norm,
            "riotIdCorrected": True,
        }


    def connection_riot_id_for_effective(
        self,
        session_id: str,
        riot_id: str,
    ) -> str:
        """Return the immutable connection identity behind an effective Riot ID.

        This allows a corrected player to reload the player page, type the now-
        correct Riot ID, and still republish to the same VDO stream ID.
        """
        riot_id = validate_riot_id(riot_id)
        normalized = normalize_riot_id(riot_id)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT p.riot_id
                FROM player_identity_corrections c
                JOIN players p
                  ON p.session_id = c.session_id
                 AND p.riot_norm = c.connection_riot_norm
                WHERE c.session_id = ? AND c.corrected_riot_norm = ?
                LIMIT 1
                """,
                (session_id, normalized),
            ).fetchone()
        return str(row["riot_id"]) if row else riot_id

    def get_effective_riot_id(self, session_id: str, connection_riot_id: str) -> str:
        connection_riot_id = validate_riot_id(connection_riot_id)
        connection_norm = normalize_riot_id(connection_riot_id)
        correction = self.list_corrections(session_id).get(connection_norm)
        return correction["riotId"] if correction else connection_riot_id

    def clear_session(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM player_identity_corrections WHERE session_id = ?",
                (session_id,),
            )
