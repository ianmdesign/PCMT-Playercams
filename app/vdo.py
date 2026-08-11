from __future__ import annotations

from urllib.parse import quote

from .storage import stream_id_for_riot_id


def _flag(name: str) -> str:
    return name


def build_publish_url(room_id: str, riot_id: str, share_type: str) -> tuple[str, str]:
    """Build the VDO.Ninja publisher URL used by a player's embedded call.

    Audio is intentionally disabled at multiple layers:
    - audiodevice=0 prevents VDO.Ninja from selecting/requesting a microphone.
    - audiogain=0 keeps any microphone track muted if one is later introduced.
    - noaudio/deafen prevent incoming room audio from being played to the player.
    - the embedding iframe itself does not grant microphone permission.
    """
    stream_id = stream_id_for_riot_id(riot_id)
    parts = [
        f"room={quote(room_id, safe='')}",
        f"push={quote(stream_id, safe='')}",
        f"label={quote(riot_id, safe='')}",
        "roombitrate=0",
        "audiodevice=0",
        "audiogain=0",
        _flag("noaudio"),
        _flag("deafen"),
        _flag("nomicbutton"),
        _flag("nospeakerbutton"),
        _flag("disablehotkeys"),
    ]
    if share_type == "media":
        parts.append("fileshare")
    else:
        # webcam2 shows a clear camera-sharing button before the device prompt,
        # which works well inside an iframe.
        parts.append("webcam2")
    return "https://vdo.ninja/?" + "&".join(parts), stream_id


def build_preview_url(room_id: str, riot_id: str, bitrate_kbps: int = 800) -> tuple[str, str]:
    """Build a low-bandwidth, video-only producer preview URL."""
    stream_id = stream_id_for_riot_id(riot_id)
    bitrate_kbps = max(50, int(bitrate_kbps))
    parts = [
        f"room={quote(room_id, safe='')}",
        f"view={quote(stream_id, safe='')}",
        "solo",
        f"videobitrate={bitrate_kbps}",
        _flag("noaudio"),
        _flag("deafen"),
        _flag("cleanoutput"),
        _flag("disablehotkeys"),
    ]
    return "https://vdo.ninja/?" + "&".join(parts), stream_id
