from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.session_identity import SessionRiotIdStore
from app.storage import AppConfig, SessionStore, stream_id_for_riot_id, audio_stream_id_for_player


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

    def test_correction_routes_both_camera_and_listen_in_without_changing_publishers(self):
        """One producer correction must repair both viewer mappings, not mic IDs."""
        from app import main
        from app.vdo import build_audio_publish_url, build_publish_url

        session, _, producer_token = self.sessions.create_or_reuse("GROUP1")
        self.sessions.register_player(
            session["joinToken"], "Typo Name#NA1", "video", audio_enabled=True
        )
        self.sessions.register_player(
            session["joinToken"], "Other#NA1", "video", audio_enabled=True
        )
        session = self.sessions.set_listen_in(session["sessionId"], "left")
        roster = {
            "teams": [
                {"players": [{"riotId": "Correct Name#NA1"}]},
                {"players": [{"riotId": "Other#NA1"}]},
            ]
        }
        original_camera = stream_id_for_riot_id("Typo Name#NA1")
        original_mic = audio_stream_id_for_player(session, "Typo Name#NA1")
        original_publish_url, _ = build_publish_url(session["roomId"], "Typo Name#NA1", "video")
        original_audio_url = build_audio_publish_url(
            session["roomId"], "Typo Name#NA1", original_mic
        )

        with (
            patch.object(main, "sessions", self.sessions),
            patch.object(main, "session_riot_ids", self.identities),
            patch.object(main, "get_latest_roster", return_value=roster),
            patch.object(main, "scoped_name_overrides", return_value=[]),
            patch.object(main, "emit_state_to_all_frontends", new_callable=AsyncMock) as broadcast,
        ):
            before = main.build_frontend_state("GROUP1")["playercamsInfo"]
            self.assertEqual({}, before["audioStreamMappings"])
            self.assertEqual(original_camera, before["streamMappings"]["Typo Name#NA1"])

            result = asyncio.run(main.correct_player_riot_id(
                session["sessionId"],
                main.CorrectPlayerRiotIdRequest(
                    connectionRiotId="Typo Name#NA1", riotId="Correct Name#NA1"
                ),
                x_producer_token=producer_token,
            ))
            self.assertEqual("Correct Name#NA1", result["player"]["riotId"])
            broadcast.assert_awaited_once()
            after = main.build_frontend_state("GROUP1")["playercamsInfo"]
            self.assertEqual(
                original_camera, after["streamMappings"]["Correct Name#NA1"]
            )
            self.assertEqual(
                {"Correct Name#NA1": original_mic}, after["audioStreamMappings"]
            )
            self.assertNotIn("Typo Name#NA1", after["streamMappings"])
            self.assertEqual(session["roomId"] + "_li", after["listenInRoom"])

            preview = main.build_producer_state(session)["players"]
            corrected = next(p for p in preview if p["riotId"] == "Correct Name#NA1")
            self.assertEqual("Typo Name#NA1", corrected["connectionRiotId"])
            self.assertEqual(original_camera, corrected["streamId"])
            self.assertIn("view=" + original_camera, corrected["previewUrl"])
            self.assertEqual(
                original_publish_url,
                build_publish_url(session["roomId"], corrected["connectionRiotId"], "video")[0],
            )
            self.assertEqual(
                original_audio_url,
                build_audio_publish_url(
                    session["roomId"],
                    corrected["connectionRiotId"],
                    audio_stream_id_for_player(session, corrected["connectionRiotId"]),
                ),
            )
            self.assertEqual(
                "Typo Name#NA1",
                self.identities.connection_riot_id_for_effective(
                    session["sessionId"], "Correct Name#NA1"
                ),
            )

            # Team gating still applies after the correction.
            session = self.sessions.set_listen_in(session["sessionId"], "right")
            self.assertEqual(
                {"Other#NA1": audio_stream_id_for_player(session, "Other#NA1")},
                main.build_frontend_state("GROUP1")["playercamsInfo"]["audioStreamMappings"],
            )
            session = self.sessions.set_listen_in(session["sessionId"], False)
            self.assertEqual(
                {}, main.build_frontend_state("GROUP1")["playercamsInfo"]["audioStreamMappings"]
            )

            # Undo keeps both original live publisher IDs stable as well.
            self.identities.set_correction(
                session["sessionId"], "Typo Name#NA1", "Typo Name#NA1"
            )
            restored = main.build_frontend_state("GROUP1")["playercamsInfo"]
            self.assertEqual(original_camera, restored["streamMappings"]["Typo Name#NA1"])
            self.assertNotIn("Correct Name#NA1", restored["streamMappings"])

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
