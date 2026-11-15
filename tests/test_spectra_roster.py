from __future__ import annotations

import json
import unittest

from app.spectra_roster import parse_spectra_roster, select_unambiguous_roster


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
        for data in (packet("B"), {}, [], "{}", "not json", {"groupCode": "A", "teams": [1]}, {"groupCode": 4, "teams": []}):
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

    def test_independent_sources_for_different_groups(self) -> None:
        now = 10_000
        roster_na = {"teams": [{"teamName": "NA"}], "updatedAt": now}
        roster_eu = {"teams": [{"teamName": "EU"}], "updatedAt": now}
        snapshots = {("https://na:5200", "A"): roster_na, ("https://eu:5200", "B"): roster_eu}
        self.assertIs(select_unambiguous_roster(snapshots, "A", now, 120_000), roster_na)
        self.assertIs(select_unambiguous_roster(snapshots, "B", now, 120_000), roster_eu)

    def test_same_group_collision_fails_closed_until_one_is_stale(self) -> None:
        now = 150_000
        roster_na = {"teams": [{"teamName": "NA"}], "updatedAt": now}
        roster_eu = {"teams": [{"teamName": "EU"}], "updatedAt": now}
        snapshots = {("https://na:5200", "A"): roster_na, ("https://eu:5200", "A"): roster_eu}
        self.assertIsNone(select_unambiguous_roster(snapshots, "A", now, 120_000))
        roster_eu["updatedAt"] = now - 120_001
        self.assertIs(select_unambiguous_roster(snapshots, "A", now, 120_000), roster_na)


if __name__ == "__main__":
    unittest.main()
