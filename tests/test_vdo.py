from __future__ import annotations

import unittest

from app.vdo import build_preview_url, build_publish_url


class VdoUrlTests(unittest.TestCase):
    def test_camera_publish_url_disables_audio(self):
        url, stream_id = build_publish_url("pcmtplayercams123456789012", "Player Name#NA1", "video")
        self.assertEqual("Player_Name_H_NA1", stream_id)
        self.assertIn("audiodevice=0", url)
        self.assertIn("audiogain=0", url)
        self.assertIn("&noaudio", url)
        self.assertIn("&deafen", url)
        self.assertIn("&nomicbutton", url)
        self.assertIn("&nospeakerbutton", url)
        self.assertIn("&webcam2", url)

    def test_media_publish_url_keeps_audio_disabled(self):
        url, _ = build_publish_url("pcmtplayercams123456789012", "Player#TAG", "media")
        self.assertIn("audiodevice=0", url)
        self.assertIn("audiogain=0", url)
        self.assertIn("&noaudio", url)
        self.assertIn("&fileshare", url)

    def test_preview_is_low_bitrate_video_only(self):
        url, stream_id = build_preview_url("pcmtplayercams123456789012", "Player Name#NA1", 800)
        self.assertEqual("Player_Name_H_NA1", stream_id)
        self.assertIn("view=Player_Name_H_NA1", url)
        self.assertIn("videobitrate=800", url)
        self.assertIn("&solo", url)
        self.assertIn("&noaudio", url)
        self.assertIn("&deafen", url)
        self.assertIn("&cleanoutput", url)


if __name__ == "__main__":
    unittest.main()
