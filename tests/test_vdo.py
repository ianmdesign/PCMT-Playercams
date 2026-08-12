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
        self.assertIn("roombitrate=0", url)
        self.assertIn("&novideo", url)
        self.assertIn("showlist=0", url)
        self.assertIn("chatbutton=0", url)
        self.assertIn("&cleanoutput", url)
        self.assertIn("&noheader", url)
        self.assertIn("&hidehome", url)
        self.assertIn("&nosettings", url)
        self.assertIn("&novideobutton", url)
        self.assertIn("&nohangupbutton", url)
        self.assertIn("maxvideobitrate=3000", url)
        self.assertIn("limittotalbitrate=4000", url)

    def test_publish_bandwidth_limits_are_configurable(self):
        url, _ = build_publish_url(
            "pcmtplayercams123456789012", "Player#TAG", "video", 2800, 3600
        )
        self.assertIn("maxvideobitrate=2800", url)
        self.assertIn("limittotalbitrate=3600", url)

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
