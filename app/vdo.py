from __future__ import annotations

from urllib.parse import quote

from .storage import stream_id_for_riot_id


def _flag(name: str) -> str:
    return name


def build_publish_url(
    room_id: str,
    riot_id: str,
    share_type: str,
    max_bitrate_kbps: int = 2500,
    total_bitrate_kbps: int = 3000,
) -> tuple[str, str]:
    """Build the locked-down VDO.Ninja publisher URL used by a player.

    The player link is deliberately publish-only from the user's perspective:
    - no microphone or incoming room audio
    - no incoming room video
    - no participant list, chat, settings, header, or normal VDO controls
    - room-to-room video delivery is disabled, while direct view/scene links still work
    - publisher bandwidth is capped per outbound stream and in aggregate
    """
    stream_id = stream_id_for_riot_id(riot_id)
    max_bitrate_kbps = max(100, int(max_bitrate_kbps))
    total_bitrate_kbps = max(100, int(total_bitrate_kbps))

    parts = [
        f"room={quote(room_id, safe='')}",
        f"push={quote(stream_id, safe='')}",
        f"label={quote(riot_id, safe='')}",

        # Do not send a player's feed to other ordinary room guests. This does
        # not prevent producer/overlay direct view links from receiving it.
        "roombitrate=0",

        # Hard publisher-side bandwidth limits. maxvideobitrate applies per
        # outbound stream; limittotalbitrate caps aggregate outbound video.
        f"maxvideobitrate={max_bitrate_kbps}",
        f"limittotalbitrate={total_bitrate_kbps}",

        # No audio capture or playback.
        "audiodevice=0",
        "audiogain=0",
        _flag("noaudio"),
        _flag("deafen"),

        # Do not form incoming room video connections.
        _flag("novideo"),

        # Lock down the VDO.Ninja room UI.
        "showlist=0",
        "chatbutton=0",
        _flag("cleanoutput"),
        _flag("noheader"),
        _flag("hidehome"),
        _flag("nosettings"),
        _flag("nomicbutton"),
        _flag("nospeakerbutton"),
        _flag("novideobutton"),
        _flag("nohangupbutton"),
        _flag("disablehotkeys"),
    ]

    if share_type == "media":
        # Keep fileshare available because this is the setup mode selected by
        # the outer PCMT page. The normal room file-share controls remain hidden
        # by cleanoutput and the other UI restrictions above.
        parts.append("fileshare")
    else:
        # webcam2 preserves the intentional camera-selection flow before publish.
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


def build_audio_publish_url(room_id: str, riot_id: str, audio_stream_id: str) -> str:
    """Separate microphone-only publisher. Never create incoming guest media.

    The visual stream ID and the existing camera URL are unchanged. The caller
    must only embed this URL after the participant explicitly enables their mic.
    """
    from urllib.parse import quote

    return "https://vdo.ninja/?" + "&".join([
        # Dedicated, separate microphone room. Disable room multicast:
        # only explicit direct-view subscriptions should receive mic audio.
        f"room={quote(room_id + '_li', safe='')}",
        f"push={quote(audio_stream_id, safe='')}",
        f"label={quote(riot_id + ' microphone', safe='')}",
        "miconly",
        "deafen",
        "novideo",
        "roombitrate=0",
        "showlist=0",
        "chatbutton=0",
        # Unlike the locked-down visual publisher, this must expose VDO's
        # native microphone setup and device picker to the participant.
        # In particular, do NOT add cleanoutput, nosettings, or nomicbutton.
        "nospeakerbutton",
        "novideobutton",
    ])
