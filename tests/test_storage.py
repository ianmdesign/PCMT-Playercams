from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.storage import AppConfig, OverrideStore, SessionStore, stream_id_for_riot_id


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = AppConfig(
            public_base_url="http://localhost:5400",
            session_lifetime_hours=48,
            group_alias_grace_minutes=30,
            room_prefix="pcmtplayercams",
            room_random_digits=12,
            producer_access_key="",
        )
        self.store = SessionStore(root / "playercams.sqlite3", self.config)

    def tearDown(self):
        self.temp.cleanup()

    def test_create_and_reuse_group(self):
        first, created = self.store.create_or_reuse("abc123")
        second, created_again = self.store.create_or_reuse("ABC123")
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["sessionId"], second["sessionId"])
        self.assertEqual(first["roomId"], second["roomId"])
        self.assertTrue(first["roomId"].startswith("pcmtplayercams"))
        suffix = first["roomId"][len("pcmtplayercams") :]
        self.assertEqual(12, len(suffix))
        self.assertTrue(suffix.isdigit())

    def test_rebind_preserves_room_and_join_token(self):
        first, _ = self.store.create_or_reuse("OLD123")
        rebound = self.store.rebind_group(first["sessionId"], "NEW456")
        self.assertEqual(first["roomId"], rebound["roomId"])
        self.assertEqual(first["joinToken"], rebound["joinToken"])
        self.assertEqual("NEW456", rebound["groupCode"])
        self.assertEqual(first["sessionId"], self.store.resolve_group("OLD123")["sessionId"])
        self.assertEqual(first["sessionId"], self.store.resolve_group("NEW456")["sessionId"])

    def test_rebind_rejects_other_active_session(self):
        first, _ = self.store.create_or_reuse("GROUP1")
        self.store.create_or_reuse("GROUP2")
        with self.assertRaises(ValueError):
            self.store.rebind_group(first["sessionId"], "GROUP2")

    def test_player_registration_is_case_insensitive_upsert(self):
        session, _ = self.store.create_or_reuse("GROUP1")
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
