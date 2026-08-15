from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.session_identity import SessionRiotIdStore
from app.storage import AppConfig, SessionStore, stream_id_for_riot_id


class SessionRiotIdStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.db_path = root / "playercams.sqlite3"
        self.sessions = SessionStore(
            self.db_path,
            AppConfig(public_base_url="http://localhost:5400"),
        )
        self.identities = SessionRiotIdStore(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def _session_with_player(self, group: str, riot_id: str):
        session, _, _ = self.sessions.create_or_reuse(group)
        self.sessions.register_player(session["joinToken"], riot_id, "video")
        return session

    def test_correction_is_scoped_to_one_session(self):
        first = self._session_with_player("GROUP1", "Typo#NA1")
        second = self._session_with_player("GROUP2", "Typo#NA1")

        self.identities.set_correction(first["sessionId"], "Typo#NA1", "Correct#NA1")

        first_player = self.identities.apply_to_players(
            first["sessionId"],
            self.sessions.list_players(first["sessionId"]),
        )[0]
        second_player = self.identities.apply_to_players(
            second["sessionId"],
            self.sessions.list_players(second["sessionId"]),
        )[0]

        self.assertEqual("Correct#NA1", first_player["riotId"])
        self.assertEqual("Typo#NA1", second_player["riotId"])

    def test_correction_preserves_connection_identity_and_stream_id(self):
        session = self._session_with_player("GROUP1", "Typo Name#NA1")
        original_stream = stream_id_for_riot_id("Typo Name#NA1")

        self.identities.set_correction(
            session["sessionId"],
            "Typo Name#NA1",
            "Correct Name#NA1",
        )
        player = self.identities.apply_to_players(
            session["sessionId"],
            self.sessions.list_players(session["sessionId"]),
        )[0]

        self.assertEqual("Correct Name#NA1", player["riotId"])
        self.assertEqual("Typo Name#NA1", player["connectionRiotId"])
        self.assertEqual(
            original_stream,
            stream_id_for_riot_id(player["connectionRiotId"]),
        )

    def test_correction_survives_store_restart(self):
        session = self._session_with_player("GROUP1", "Typo#NA1")
        self.identities.set_correction(session["sessionId"], "Typo#NA1", "Correct#NA1")

        reopened = SessionRiotIdStore(self.db_path)
        self.assertEqual(
            "Correct#NA1",
            reopened.get_effective_riot_id(session["sessionId"], "Typo#NA1"),
        )

    def test_corrected_id_rejoin_resolves_to_original_connection_id(self):
        session = self._session_with_player("GROUP1", "Typo#NA1")
        self.identities.set_correction(session["sessionId"], "Typo#NA1", "Correct#NA1")

        self.assertEqual(
            "Typo#NA1",
            self.identities.connection_riot_id_for_effective(
                session["sessionId"],
                "Correct#NA1",
            ),
        )

    def test_setting_original_id_removes_correction(self):
        session = self._session_with_player("GROUP1", "Typo#NA1")
        self.identities.set_correction(session["sessionId"], "Typo#NA1", "Correct#NA1")
        result = self.identities.set_correction(session["sessionId"], "Typo#NA1", "Typo#NA1")

        self.assertFalse(result["riotIdCorrected"])
        self.assertEqual(
            "Typo#NA1",
            self.identities.get_effective_riot_id(session["sessionId"], "Typo#NA1"),
        )

    def test_duplicate_effective_riot_id_is_rejected(self):
        session = self._session_with_player("GROUP1", "Wrong#NA1")
        self.sessions.register_player(session["joinToken"], "Other#NA1", "video")

        with self.assertRaises(ValueError):
            self.identities.set_correction(
                session["sessionId"],
                "Wrong#NA1",
                "Other#NA1",
            )


if __name__ == "__main__":
    unittest.main()
