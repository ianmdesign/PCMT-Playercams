from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.storage import AppConfig, parse_spectra_output_endpoints


class SpectraEndpointConfigTests(unittest.TestCase):
    def test_multi_endpoint_allowlist_deduplicates_and_preserves_order(self) -> None:
        urls = parse_spectra_output_endpoints({
            "spectraOutputEndpoints": [
                "https://na.example.test:5200/",
                "https://eu.example.test:5200",
                "http://spectra-server:5200",
                "https://NA.EXAMPLE.TEST:5200",
            ]
        })
        self.assertEqual(urls, (
            "https://na.example.test:5200",
            "https://eu.example.test:5200",
            "http://spectra-server:5200",
        ))

    def test_single_legacy_endpoint_still_works(self) -> None:
        self.assertEqual(
            parse_spectra_output_endpoints({"spectraOutputEndpoint": "https://na.example.test:5200"}),
            ("https://na.example.test:5200",),
        )

    def test_explicit_empty_list_overrides_legacy_setting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps({
                "spectraOutputEndpoint": "https://na.example.test:5200",
                "spectraOutputEndpoints": [],
            }), encoding="utf-8")
            with patch("app.storage.CONFIG_DIR", Path(directory)), patch("app.storage.CONFIG_PATH", config_path):
                config = AppConfig.load()
            self.assertEqual(config.spectra_output_endpoints, ())
            self.assertEqual(config.spectra_output_endpoint, "")

    def test_invalid_endpoints_are_rejected(self) -> None:
        for value in (
            "https://user:pass@na.example.test:5200",
            "ftp://na.example.test:5200",
            "https://na.example.test",
            "https://na.example.test:5200/socket.io",
            "https://na.example.test:5200?token=secret",
            "https://na.example.test:bad",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_spectra_output_endpoints({"spectraOutputEndpoints": [value]})

    def test_invalid_list_or_too_many_endpoints_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_spectra_output_endpoints({"spectraOutputEndpoints": "https://na.example.test:5200"})
        with self.assertRaises(ValueError):
            parse_spectra_output_endpoints({
                "spectraOutputEndpoints": [f"https://spectra{i}.example.test:5200" for i in range(9)]
            })


if __name__ == "__main__":
    unittest.main()
