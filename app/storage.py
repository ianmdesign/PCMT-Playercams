from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import string
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", Path(__file__).resolve().parent.parent / "config"))
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
CONFIG_PATH = CONFIG_DIR / "config.json"
OVERRIDES_PATH = CONFIG_DIR / "name-overrides.json"
DB_PATH = DATA_DIR / "playercams.sqlite3"
SESSION_LIFETIME_HOURS = 48


def now_ms() -> int:
    return int(time.time() * 1000)


def hash_token(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def normalize_group_code(value: str) -> str:
    return (value or "").strip().upper()


def normalize_riot_id(value: str) -> str:
    return (value or "").strip().casefold()


def validate_riot_id(value: str) -> str:
    riot_id = (value or "").strip()
    separator = riot_id.rfind("#")
    if separator <= 0 or separator >= len(riot_id) - 1:
        raise ValueError("Riot ID must include a name and tagline, for example Player Name#NA1")
    return riot_id


def stream_id_for_riot_id(riot_id: str) -> str:
    riot_id = validate_riot_id(riot_id)
    separator = riot_id.rfind("#")
    name = riot_id[:separator].replace(" ", "_")
    tagline = riot_id[separator + 1 :]
    return f"{name}_H_{tagline}"


@dataclass(frozen=True)
class AppConfig:
    public_base_url: str = "http://localhost:5400"
    group_alias_grace_minutes: int = 30
    room_prefix: str = "pcmtplayercams"
    room_random_digits: int = 12
    producer_preview_bitrate_kbps: int = 500
    publisher_max_bitrate_kbps: int = 2500
    publisher_total_bitrate_kbps: int = 3000

    @classmethod
    def load(cls) -> "AppConfig":
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if not CONFIG_PATH.exists():
            return cls()
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return cls(
            public_base_url=str(raw.get("publicBaseUrl", cls.public_base_url)).rstrip("/"),
            group_alias_grace_minutes=max(
                0, int(raw.get("groupAliasGraceMinutes", cls.group_alias_grace_minutes))
            ),
            room_prefix=str(raw.get("roomPrefix", cls.room_prefix)),
            room_random_digits=max(6, int(raw.get("roomRandomDigits", cls.room_random_digits))),
            producer_preview_bitrate_kbps=max(50, int(raw.get("producerPreviewBitrateKbps", cls.producer_preview_bitrate_kbps))),
            publisher_max_bitrate_kbps=max(100, int(raw.get("publisherMaxBitrateKbps", cls.publisher_max_bitrate_kbps))),
            publisher_total_bitrate_kbps=max(100, int(raw.get("publisherTotalBitrateKbps", cls.publisher_total_bitrate_kbps))),
        )


class OverrideStore:
    def __init__(self, path: Path = OVERRIDES_PATH):
        self.path = path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"overrides": {}})

    def _read(self) -> dict[str, Any]:
        with self._lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                data = {"overrides": {}}
            if not isinstance(data.get("overrides"), dict):
                data["overrides"] = {}
            return data

    def _write(self, data: dict[str, Any]) -> None:
        with self._lock:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            tmp.replace(self.path)

    def list_entries(self) -> list[dict[str, str]]:
        data = self._read()
        entries: list[dict[str, str]] = []
        for norm, value in data["overrides"].items():
            if isinstance(value, str):
                entries.append({"normalizedRiotId": norm, "riotId": norm, "displayName": value})
            elif isinstance(value, dict):
                display_name = str(value.get("displayName", "")).strip()
                riot_id = str(value.get("riotId", norm)).strip()
                if display_name:
                    entries.append(
                        {
                            "normalizedRiotId": norm,
                            "riotId": riot_id,
                            "displayName": display_name,
                        }
                    )
        entries.sort(key=lambda item: item["riotId"].casefold())
        return entries

    def as_pairs(self) -> list[list[str]]:
        return [[entry["riotId"], entry["displayName"]] for entry in self.list_entries()]

    def upsert(self, riot_id: str, display_name: str) -> dict[str, str]:
        riot_id = validate_riot_id(riot_id)
        display_name = (display_name or "").strip()
        if not display_name:
            raise ValueError("Display name cannot be blank")
        norm = normalize_riot_id(riot_id)
        with self._lock:
            data = self._read()
            data["overrides"][norm] = {"riotId": riot_id, "displayName": display_name}
            self._write(data)
        return {"normalizedRiotId": norm, "riotId": riot_id, "displayName": display_name}

    def remove(self, riot_id: str) -> bool:
        norm = normalize_riot_id(riot_id)
        with self._lock:
            data = self._read()
            existed = norm in data["overrides"]
            if existed:
                del data["overrides"][norm]
                self._write(data)
            return existed


class SessionStore:
    def __init__(self, db_path: Path = DB_PATH, config: AppConfig | None = None):
        self.db_path = db_path
        self.config = config or AppConfig.load()
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
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    room_id TEXT NOT NULL UNIQUE,
                    join_token TEXT NOT NULL UNIQUE,
                    producer_token_hash TEXT,
                    current_group_code TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    playercams_enabled INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS group_aliases (
                    group_code TEXT PRIMARY KEY COLLATE NOCASE,
                    session_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    resolve_until INTEGER,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS players (
                    session_id TEXT NOT NULL,
                    riot_norm TEXT NOT NULL,
                    riot_id TEXT NOT NULL,
                    share_type TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    last_seen INTEGER NOT NULL,
                    PRIMARY KEY (session_id, riot_norm),
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
                );
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
            if "producer_token_hash" not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN producer_token_hash TEXT")
            # Sessions created by pre-capability-link development builds have no
            # recoverable producer secret. Retire them rather than leave an
            # unauthenticated or permanently stuck producer session active.
            conn.execute(
                "UPDATE sessions SET active = 0 WHERE producer_token_hash IS NULL OR producer_token_hash = ''"
            )

    def _generate_session_id(self) -> str:
        alphabet = string.ascii_uppercase + string.digits
        return "PC-" + "".join(secrets.choice(alphabet) for _ in range(10))

    def _generate_room_id(self) -> str:
        alphabet = string.digits
        suffix = "".join(secrets.choice(alphabet) for _ in range(self.config.room_random_digits))
        return f"{self.config.room_prefix}{suffix}"

    def _generate_producer_token(self) -> str:
        return secrets.token_urlsafe(32)

    def _row_to_session(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "sessionId": row["session_id"],
            "roomId": row["room_id"],
            "joinToken": row["join_token"],
            "groupCode": row["current_group_code"],
            "createdAt": row["created_at"],
            "expiresAt": row["expires_at"],
            "active": bool(row["active"]),
            "playercamsEnabled": bool(row["playercams_enabled"]),
        }

    def _is_live(self, row: sqlite3.Row | None, at: int | None = None) -> bool:
        if row is None:
            return False
        at = now_ms() if at is None else at
        return bool(row["active"]) and int(row["expires_at"]) > at

    def get_session(self, session_id: str, include_expired: bool = False) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if not row:
            return None
        if not include_expired and not self._is_live(row):
            return None
        return self._row_to_session(row)

    def get_session_by_token(self, token: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE join_token = ?", (token,)).fetchone()
        if not self._is_live(row):
            return None
        return self._row_to_session(row)

    def resolve_group(self, group_code: str) -> dict[str, Any] | None:
        group_code = normalize_group_code(group_code)
        if not group_code:
            return None
        at = now_ms()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT s.*, a.resolve_until
                FROM group_aliases a
                JOIN sessions s ON s.session_id = a.session_id
                WHERE a.group_code = ? COLLATE NOCASE
                """,
                (group_code,),
            ).fetchone()
        if not self._is_live(row, at):
            return None
        if row["current_group_code"].casefold() == group_code.casefold():
            return self._row_to_session(row)
        resolve_until = row["resolve_until"]
        if resolve_until is None or int(resolve_until) > at:
            return self._row_to_session(row)
        return None

    def create_or_reuse(self, group_code: str) -> tuple[dict[str, Any], bool, str | None]:
        group_code = normalize_group_code(group_code)
        if not group_code:
            raise ValueError("Group code is required")
        existing = self.resolve_group(group_code)
        if existing:
            return existing, False, None

        created = now_ms()
        # Playercam sessions are intentionally fixed at 48 hours from creation.
        # Reloading the producer page, reconnecting players, or rebinding the
        # Spectra group code never extends this deadline.
        expires = created + SESSION_LIFETIME_HOURS * 60 * 60 * 1000
        session_id = self._generate_session_id()
        room_id = self._generate_room_id()
        join_token = secrets.token_urlsafe(32)
        producer_token = self._generate_producer_token()
        producer_token_hash = hash_token(producer_token)

        with self._connect() as conn:
            stale = conn.execute(
                """
                SELECT s.*, a.resolve_until FROM group_aliases a
                JOIN sessions s ON s.session_id = a.session_id
                WHERE a.group_code = ? COLLATE NOCASE
                """,
                (group_code,),
            ).fetchone()
            if stale and self._is_live(stale):
                alias_is_current = stale["current_group_code"].casefold() == group_code.casefold()
                alias_is_live = stale["resolve_until"] is None or int(stale["resolve_until"]) > created
                if alias_is_current or alias_is_live:
                    raise ValueError(f"Group code {group_code} is already associated with an active session")
            conn.execute("DELETE FROM group_aliases WHERE group_code = ? COLLATE NOCASE", (group_code,))
            conn.execute(
                """
                INSERT INTO sessions (
                    session_id, room_id, join_token, producer_token_hash, current_group_code,
                    created_at, expires_at, active, playercams_enabled
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1)
                """,
                (session_id, room_id, join_token, producer_token_hash, group_code, created, expires),
            )
            conn.execute(
                "INSERT INTO group_aliases (group_code, session_id, created_at, resolve_until) VALUES (?, ?, ?, NULL)",
                (group_code, session_id, created),
            )

        return self.get_session(session_id) or {}, True, producer_token

    def verify_producer_token(self, session_id: str, token: str | None) -> bool:
        if not token:
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if not self._is_live(row):
            return False
        stored_hash = str(row["producer_token_hash"] or "")
        if not stored_hash:
            return False
        return hmac.compare_digest(stored_hash, hash_token(token))

    def rotate_producer_token(self, session_id: str) -> str:
        session = self.get_session(session_id)
        if not session:
            raise ValueError("Playercam session is not active")
        token = self._generate_producer_token()
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET producer_token_hash = ? WHERE session_id = ?",
                (hash_token(token), session_id),
            )
        return token

    def rebind_group(self, session_id: str, new_group_code: str) -> dict[str, Any]:
        new_group_code = normalize_group_code(new_group_code)
        if not new_group_code:
            raise ValueError("New group code is required")
        session = self.get_session(session_id)
        if not session:
            raise ValueError("Playercam session is not active")
        if session["groupCode"].casefold() == new_group_code.casefold():
            return session

        at = now_ms()
        grace_until = at + self.config.group_alias_grace_minutes * 60 * 1000
        with self._connect() as conn:
            conflict = conn.execute(
                """
                SELECT s.*, a.resolve_until FROM group_aliases a
                JOIN sessions s ON s.session_id = a.session_id
                WHERE a.group_code = ? COLLATE NOCASE AND s.session_id != ?
                """,
                (new_group_code, session_id),
            ).fetchone()
            if conflict and self._is_live(conflict, at):
                alias_is_current = conflict["current_group_code"].casefold() == new_group_code.casefold()
                alias_is_live = conflict["resolve_until"] is None or int(conflict["resolve_until"]) > at
                if alias_is_current or alias_is_live:
                    raise ValueError(f"Group code {new_group_code} is already associated with another active session")

            conn.execute(
                "UPDATE group_aliases SET resolve_until = ? WHERE session_id = ? AND group_code = ? COLLATE NOCASE",
                (grace_until, session_id, session["groupCode"]),
            )
            conn.execute("DELETE FROM group_aliases WHERE group_code = ? COLLATE NOCASE", (new_group_code,))
            conn.execute(
                "INSERT INTO group_aliases (group_code, session_id, created_at, resolve_until) VALUES (?, ?, ?, NULL)",
                (new_group_code, session_id, at),
            )
            conn.execute(
                "UPDATE sessions SET current_group_code = ? WHERE session_id = ?",
                (new_group_code, session_id),
            )
        return self.get_session(session_id) or {}

    def list_aliases(self, session_id: str) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT group_code FROM group_aliases WHERE session_id = ? ORDER BY created_at",
                (session_id,),
            ).fetchall()
        return [str(row["group_code"]) for row in rows]

    def set_playercams_enabled(self, session_id: str, enabled: bool) -> dict[str, Any]:
        session = self.get_session(session_id)
        if not session:
            raise ValueError("Playercam session is not active")
        with self._connect() as conn:
            conn.execute(
                "UPDATE sessions SET playercams_enabled = ? WHERE session_id = ?",
                (1 if enabled else 0, session_id),
            )
        return self.get_session(session_id) or {}

    def end_session(self, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE sessions SET active = 0 WHERE session_id = ?", (session_id,))

    def register_player(self, token: str, riot_id: str, share_type: str) -> tuple[dict[str, Any], dict[str, Any]]:
        session = self.get_session_by_token(token)
        if not session:
            raise ValueError("Playercam session is not active")
        riot_id = validate_riot_id(riot_id)
        share_type = (share_type or "").strip().lower()
        if share_type not in {"video", "media"}:
            raise ValueError("Share type must be video or media")
        at = now_ms()
        norm = normalize_riot_id(riot_id)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO players (session_id, riot_norm, riot_id, share_type, created_at, last_seen)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, riot_norm) DO UPDATE SET
                  riot_id = excluded.riot_id,
                  share_type = excluded.share_type,
                  last_seen = excluded.last_seen
                """,
                (session["sessionId"], norm, riot_id, share_type, at, at),
            )
        player = {
            "riotId": riot_id,
            "normalizedRiotId": norm,
            "shareType": share_type,
            "lastSeen": at,
        }
        return session, player

    def heartbeat_player(self, token: str, riot_id: str) -> None:
        session = self.get_session_by_token(token)
        if not session:
            raise ValueError("Playercam session is not active")
        norm = normalize_riot_id(validate_riot_id(riot_id))
        with self._connect() as conn:
            conn.execute(
                "UPDATE players SET last_seen = ? WHERE session_id = ? AND riot_norm = ?",
                (now_ms(), session["sessionId"], norm),
            )

    def list_players(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT riot_id, riot_norm, share_type, created_at, last_seen FROM players WHERE session_id = ? ORDER BY created_at",
                (session_id,),
            ).fetchall()
        return [
            {
                "riotId": row["riot_id"],
                "normalizedRiotId": row["riot_norm"],
                "shareType": row["share_type"],
                "createdAt": row["created_at"],
                "lastSeen": row["last_seen"],
            }
            for row in rows
        ]
