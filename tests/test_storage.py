from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from app.storage import AppConfig, OverrideStore, SessionStore, stream_id_for_riot_id


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = AppConfig(
            public_base_url="http://localhost:5400",
            spectra_output_endpoints=(("NA", "https://na.valospectra.com:5200"), ("EU", "https://eu.valospectra.com:5200")),
            group_alias_grace_minutes=30,
            room_prefix="pcmtplayercams",
            room_random_digits=12,
            producer_preview_bitrate_kbps=800,
        )
        self.store = SessionStore(root / "playercams.sqlite3", self.config)

    def tearDown(self):
        self.temp.cleanup()

    def test_same_group_code_is_independent_between_servers(self):
        na, _, _ = self.store.create_or_reuse("SHARED", "https://na.valospectra.com:5200")
        eu, _, _ = self.store.create_or_reuse("SHARED", "https://eu.valospectra.com:5200")
        self.assertNotEqual(na["sessionId"], eu["sessionId"])
        self.assertEqual(na["sessionId"], self.store.resolve_group("SHARED", "https://na.valospectra.com:5200")["sessionId"])
        self.assertEqual(eu["sessionId"], self.store.resolve_group("SHARED", "https://eu.valospectra.com:5200")["sessionId"])

    def test_rebinding_does_not_collide_across_servers(self):
        na, _, _ = self.store.create_or_reuse("OLD", "https://na.valospectra.com:5200")
        eu, _, _ = self.store.create_or_reuse("NEW", "https://eu.valospectra.com:5200")
        result = self.store.rebind_group(na["sessionId"], "NEW")
        self.assertEqual(na["sessionId"], result["sessionId"])
        self.assertEqual(eu["sessionId"], self.store.resolve_group("NEW", "https://eu.valospectra.com:5200")["sessionId"])

    def test_create_and_reuse_group(self):
        first, created, producer_token = self.store.create_or_reuse("abc123", "https://na.valospectra.com:5200")
        second, created_again, reused_token = self.store.create_or_reuse("ABC123", "https://na.valospectra.com:5200")
        self.assertTrue(created)
        self.assertIsNotNone(producer_token)
        self.assertFalse(created_again)
        self.assertIsNone(reused_token)
        self.assertEqual(first["sessionId"], second["sessionId"])
        self.assertEqual(first["roomId"], second["roomId"])
        self.assertTrue(first["roomId"].startswith("pcmtplayercams"))
        suffix = first["roomId"][len("pcmtplayercams") :]
        self.assertEqual(12, len(suffix))
        self.assertTrue(suffix.isdigit())

    def test_rebind_preserves_room_and_join_token(self):
        first, _, _ = self.store.create_or_reuse("OLD123", "https://na.valospectra.com:5200")
        rebound = self.store.rebind_group(first["sessionId"], "NEW456")
        self.assertEqual(first["roomId"], rebound["roomId"])
        self.assertEqual(first["joinToken"], rebound["joinToken"])
        self.assertEqual("NEW456", rebound["groupCode"])
        self.assertEqual(first["sessionId"], self.store.resolve_group("OLD123", "https://na.valospectra.com:5200")["sessionId"])
        self.assertEqual(first["sessionId"], self.store.resolve_group("NEW456", "https://na.valospectra.com:5200")["sessionId"])

    def test_rebind_rejects_other_active_session(self):
        first, _, _ = self.store.create_or_reuse("GROUP1", "https://na.valospectra.com:5200")
        self.store.create_or_reuse("GROUP2", "https://na.valospectra.com:5200")
        with self.assertRaises(ValueError):
            self.store.rebind_group(first["sessionId"], "GROUP2")

    def test_producer_token_is_required_and_not_returned_in_session_state(self):
        session, created, producer_token = self.store.create_or_reuse("SECURE1", "https://na.valospectra.com:5200")
        self.assertTrue(created)
        self.assertTrue(producer_token)
        self.assertNotIn("producerToken", session)
        self.assertNotIn("producerTokenHash", session)
        self.assertTrue(self.store.verify_producer_token(session["sessionId"], producer_token))
        self.assertFalse(self.store.verify_producer_token(session["sessionId"], "wrong-token"))
        self.assertFalse(self.store.verify_producer_token(session["sessionId"], None))

    def test_rotating_producer_token_invalidates_old_link_only(self):
        session, _, old_token = self.store.create_or_reuse("ROTATE1", "https://na.valospectra.com:5200")
        room_id = session["roomId"]
        join_token = session["joinToken"]
        new_token = self.store.rotate_producer_token(session["sessionId"])
        self.assertNotEqual(old_token, new_token)
        self.assertFalse(self.store.verify_producer_token(session["sessionId"], old_token))
        self.assertTrue(self.store.verify_producer_token(session["sessionId"], new_token))
        refreshed = self.store.get_session(session["sessionId"])
        self.assertEqual(room_id, refreshed["roomId"])
        self.assertEqual(join_token, refreshed["joinToken"])

    def test_producer_token_is_stored_as_hash(self):
        session, _, producer_token = self.store.create_or_reuse("HASH1", "https://na.valospectra.com:5200")
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT producer_token_hash FROM sessions WHERE session_id = ?",
                (session["sessionId"],),
            ).fetchone()
        self.assertIsNotNone(row["producer_token_hash"])
        self.assertNotEqual(producer_token, row["producer_token_hash"])
        self.assertEqual(64, len(row["producer_token_hash"]))

    def test_session_expires_exactly_48_hours_after_creation(self):
        created_at = 1_800_000_000_000
        forty_eight_hours_ms = 48 * 60 * 60 * 1000
        with patch("app.storage.now_ms", return_value=created_at):
            session, _, producer_token = self.store.create_or_reuse("EXP48", "https://na.valospectra.com:5200")

        self.assertEqual(created_at + forty_eight_hours_ms, session["expiresAt"])

        with patch("app.storage.now_ms", return_value=created_at + forty_eight_hours_ms - 1):
            self.assertIsNotNone(self.store.get_session(session["sessionId"]))
            self.assertIsNotNone(self.store.get_session_by_token(session["joinToken"]))
            self.assertIsNotNone(self.store.resolve_group("EXP48", "https://na.valospectra.com:5200"))
            self.assertTrue(self.store.verify_producer_token(session["sessionId"], producer_token))

        with patch("app.storage.now_ms", return_value=created_at + forty_eight_hours_ms):
            self.assertIsNone(self.store.get_session(session["sessionId"]))
            self.assertIsNone(self.store.get_session_by_token(session["joinToken"]))
            self.assertIsNone(self.store.resolve_group("EXP48"))
            self.assertFalse(self.store.verify_producer_token(session["sessionId"], producer_token))

    def test_rebind_does_not_extend_48_hour_expiry(self):
        created_at = 1_800_000_000_000
        with patch("app.storage.now_ms", return_value=created_at):
            session, _, _ = self.store.create_or_reuse("EXPOLD", "https://na.valospectra.com:5200")
        original_expiry = session["expiresAt"]

        with patch("app.storage.now_ms", return_value=created_at + 24 * 60 * 60 * 1000):
            rebound = self.store.rebind_group(session["sessionId"], "EXPNEW")

        self.assertEqual(original_expiry, rebound["expiresAt"])

    def test_player_registration_is_case_insensitive_upsert(self):
        session, _, _ = self.store.create_or_reuse("GROUP1", "https://na.valospectra.com:5200")
        self.store.register_player(session["joinToken"], "Player Name#NA1", "video")
        self.store.register_player(session["joinToken"], "player name#na1", "media")
        players = self.store.list_players(session["sessionId"])
        self.assertEqual(1, len(players))
        self.assertEqual("media", players[0]["shareType"])


class OverrideStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "name-overrides.json"
        self.store = OverrideStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_existing_override_is_replaced_case_insensitively(self):
        self.store.upsert("Player Name#NA1", "OldName")
        self.store.upsert("player name#na1", "NewName")
        entries = self.store.list_entries()
        self.assertEqual(1, len(entries))
        self.assertEqual("NewName", entries[0]["displayName"])
        self.assertEqual("player name#na1", entries[0]["riotId"])

    def test_remove_override(self):
        self.store.upsert("Player#TAG", "Display")
        self.assertTrue(self.store.remove("PLAYER#tag"))
        self.assertEqual([], self.store.list_entries())


class RiotIdTests(unittest.TestCase):
    def test_stream_id_splits_at_final_hash(self):
        self.assertEqual("Player_Name_H_NA1", stream_id_for_riot_id("Player Name#NA1"))

    def test_invalid_riot_id_raises(self):
        with self.assertRaises(ValueError):
            stream_id_for_riot_id("PlayerName")


if __name__ == "__main__":
    unittest.main()
