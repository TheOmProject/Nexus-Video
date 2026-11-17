"""Nexus Video settings bridge: read/write video pack selection from Nexus Screen preferences.

Nexus Screen stores its own preferences at ~/.config/nexus-screen/preferences.json.
Nexus Video extends those preferences with two keys:
  startup_video_pack  — pack id string, e.g. "nexus_signature"
  startup_video_scale — scale mode string: "fit", "fill" or "stretch"

The service configuration at ~/nexus-video/config.json (or the dev path) is kept in
sync so the boot service reads it on the next reboot without touching Nexus Screen
internals.
"""
import json
import os
from pathlib import Path

PREFS_PATH = Path(os.path.expanduser("~/.config/nexus-screen/preferences.json"))

PREF_PACK  = "startup_video_pack"
PREF_SCALE = "startup_video_scale"

DEFAULT_PACK  = "nexus_signature"
DEFAULT_SCALE = "fit"
VALID_SCALES  = ("fit", "fill", "stretch")


def _read_prefs() -> dict:
    if PREFS_PATH.exists():
        try:
            with open(PREFS_PATH) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            pass
    return {}


def _write_prefs(prefs: dict) -> None:
    PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PREFS_PATH, "w") as fh:
        json.dump(prefs, fh, indent=2)


def get_selection() -> tuple[str, str]:
    """Return (pack_id, scale_mode) from preferences, with defaults."""
    prefs = _read_prefs()
    pack  = prefs.get(PREF_PACK, DEFAULT_PACK)
    scale = prefs.get(PREF_SCALE, DEFAULT_SCALE)
    if scale not in VALID_SCALES:
        scale = DEFAULT_SCALE
    return pack, scale


def set_selection(pack_id: str, scale_mode: str, service_config_path: Path | None = None) -> None:
    """Persist pack selection to preferences and optionally to the service config file."""
    if scale_mode not in VALID_SCALES:
        raise ValueError(f"Invalid scale mode: {scale_mode!r} (must be one of {VALID_SCALES})")
    prefs = _read_prefs()
    prefs[PREF_PACK]  = pack_id
    prefs[PREF_SCALE] = scale_mode
    _write_prefs(prefs)
    if service_config_path is not None:
        _write_service_config(pack_id, scale_mode, service_config_path)


def _write_service_config(pack_id: str, scale_mode: str, config_path: Path) -> None:
    """Write ~/nexus-video/config.json (read by the boot service on the next start)."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as fh:
        json.dump({"pack": pack_id, "scale": scale_mode}, fh, indent=2)


def list_raw_videos(build_dir: Path, custom_dir: Path) -> list[dict]:
    """Return raw video files in custom_dir with their build status.

    Each entry: {"filename", "path", "pack_id", "name", "built" (bool)}.
    """
    from .packs import scan_raw_videos
    items = scan_raw_videos(custom_dir)
    for item in items:
        item["built"] = build_is_ready(build_dir, item["pack_id"])
    return items


def build_raw_video_pack(video_path: Path, build_dir: Path) -> str:
    """Build a pack output directory from a single raw video file.

    The video is used as *both* intro and loop — the boot player handles
    scaling via ffmpeg at play time, so no re-encode is needed here.
    A still.rgb565 thumbnail is extracted from the first usable frame.

    Returns the pack_id (str).
    Raises OSError or subprocess.SubprocessError on failure.
    """
    import re
    import shutil
    import subprocess

    pack_id = "raw__" + re.sub(r"[^a-zA-Z0-9_-]", "_", video_path.stem)
    out_dir  = build_dir / pack_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # Copy MP4 as intro and loop (player scales on-the-fly via ffmpeg)
    shutil.copy2(video_path, out_dir / "intro.mp4")
    shutil.copy2(video_path, out_dir / "loop.mp4")

    # Detect display format for the still thumbnail (supports 16bpp and 32bpp displays)
    try:
        from nexus_video.detect import detect_display
        _display = detect_display()
        pix_fmt       = _display.pix_fmt or "rgb565le"
        expected_size = _display.frame_bytes
    except Exception:
        pix_fmt       = "rgb565le"
        expected_size = 800 * 480 * 2

    # Extract one frame as still.rgb565 thumbnail (fast — no encode)
    still_path = out_dir / "still.rgb565"
    cmd = [
        "ffmpeg", "-y", "-nostdin", "-v", "error",
        "-ss", "0.1",
        "-i", str(video_path),
        "-vframes", "1",
        "-vf", (
            "scale=800:480:force_original_aspect_ratio=decrease,"
            "pad=800:480:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
        ),
        "-f", "rawvideo", "-pix_fmt", pix_fmt,
        str(still_path),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=30)
        if not still_path.exists() or still_path.stat().st_size != expected_size:
            still_path.write_bytes(bytes(expected_size))   # fallback black frame
    except (subprocess.SubprocessError, OSError):
        still_path.write_bytes(bytes(expected_size))       # fallback black frame

    return pack_id


def build_is_ready(build_dir: Path, pack_id: str) -> bool:
    """True when the pack has a complete converted output (intro + loop + still)."""
    pack_build = build_dir / pack_id
    return all(
        (pack_build / name).exists()
        for name in ("intro.mp4", "loop.mp4", "still.rgb565")
    )


def active_pack_path(build_dir: Path, pack_id: str) -> Path:
    """Path to the converted output directory for pack_id."""
    return build_dir / pack_id
