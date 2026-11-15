from __future__ import annotations

import json
import unittest

from app.spectra_roster import parse_spectra_roster


def packet(group: str = "A", full_name: str = "Player One#NA1") -> dict:
    return {
        "groupCode": group,
        "teams": [
            {
                "teamName": "Team One",
                "teamTricode": "ONE",
                "players": [{"fullName": full_name, "name": "Player One"}],
            },
            {"teamName": "Team Two", "teamTricode": "TWO", "players": []},
        ],
    }


class SpectraRosterTests(unittest.TestCase):
    def test_accepts_real_spectra_packet_as_json_or_dict(self) -> None:
        for data in (packet(), json.dumps(packet())):
            with self.subTest(data_type=type(data).__name__):
                roster = parse_spectra_roster(data, "a")
                self.assertIsNotNone(roster)
                assert roster is not None
                self.assertEqual(roster[0]["players"][0]["riotId"], "Player One#NA1")
                self.assertEqual(roster[0]["players"][0]["fallbackName"], "Player One")
                self.assertEqual(roster[1]["players"], [])

    def test_rejects_other_groups_and_malformed_packets(self) -> None:
        for data in (packet("B"), {}, [], "{}", "not json", {"groupCode": "A", "teams": [1]}):
            with self.subTest(data=data):
                self.assertIsNone(parse_spectra_roster(data, "A"))

    def test_rejects_invalid_team_and_oversized_roster(self) -> None:
        oversized = packet()
        oversized["teams"][0]["players"] = [{"fullName": "Name#NA1"}] * 11
        self.assertIsNone(parse_spectra_roster(oversized, "A"))
        broken = packet()
        broken["teams"][0]["players"] = ["not a player"]
        self.assertIsNone(parse_spectra_roster(broken, "A"))

    def test_skips_invalid_riot_ids_and_ignores_extra_fields(self) -> None:
        data = packet(full_name="invalid")
        data["teams"][0]["players"].append({"fullName": "Valid#NA1", "name": "Valid"})
        data["tools"] = {"somethingPrivate": "not copied"}
        roster = parse_spectra_roster(data, "A")
        self.assertIsNotNone(roster)
        assert roster is not None
        self.assertEqual(roster[0]["players"], [{"riotId": "Valid#NA1", "fallbackName": "Valid"}])
        self.assertNotIn("tools", roster[0])


if __name__ == "__main__":
    unittest.main()
