"""Tests for raw video auto-discovery (no pack.json required).

Covers scan_raw_videos(), list_raw_videos(), and build_raw_video_pack().
All tests use tmp_path and synthetic MP4 stubs — no ffprobe/ffmpeg needed
for the discovery tests; build tests mock subprocess.run.
"""
import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# --- helpers -----------------------------------------------------------------

def _make_fake_mp4(path: Path) -> Path:
    """Write a minimal valid-looking file at path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * 64)  # not real video; just needs to exist
    return path


# =============================================================================
# scan_raw_videos
# =============================================================================

class TestScanRawVideos:
    def test_empty_dir(self, tmp_path):
        from nexus_video.packs import scan_raw_videos
        custom = tmp_path / "custom"
        custom.mkdir()
        assert scan_raw_videos(custom) == []

    def test_nonexistent_dir(self, tmp_path):
        from nexus_video.packs import scan_raw_videos
        result = scan_raw_videos(tmp_path / "does_not_exist")
        assert result == []

    def test_detects_mp4_at_root(self, tmp_path):
        from nexus_video.packs import scan_raw_videos
        custom = tmp_path / "custom"
        _make_fake_mp4(custom / "my_video.mp4")
        result = scan_raw_videos(custom)
        assert len(result) == 1
        assert result[0]["filename"] == "my_video.mp4"
        assert result[0]["name"] == "my_video"
        assert result[0]["pack_id"].startswith("raw__")

    def test_detects_multiple_videos(self, tmp_path):
        from nexus_video.packs import scan_raw_videos
        custom = tmp_path / "custom"
        for name in ("alpha.mp4", "beta.mp4", "gamma.mkv"):
            _make_fake_mp4(custom / name)
        result = scan_raw_videos(custom)
        assert len(result) == 3
        names = [r["filename"] for r in result]
        assert "alpha.mp4" in names
        assert "beta.mp4"  in names
        assert "gamma.mkv" in names

    def test_ignores_subdirectory_packs(self, tmp_path):
        """Subdirs with pack.json should be handled by discover_packs, not here."""
        from nexus_video.packs import scan_raw_videos
        custom = tmp_path / "custom"
        pack_dir = custom / "my_pack"
        pack_dir.mkdir(parents=True)
        (pack_dir / "pack.json").write_text("{}")
        _make_fake_mp4(pack_dir / "intro.mp4")
        result = scan_raw_videos(custom)
        assert result == []

    def test_ignores_non_video_files(self, tmp_path):
        from nexus_video.packs import scan_raw_videos
        custom = tmp_path / "custom"
        custom.mkdir()
        (custom / "readme.txt").write_text("hello")
        (custom / "image.png").write_bytes(b"\x89PNG")
        result = scan_raw_videos(custom)
        assert result == []

    def test_pack_id_sanitizes_special_chars(self, tmp_path):
        from nexus_video.packs import scan_raw_videos
        custom = tmp_path / "custom"
        _make_fake_mp4(custom / "my video (1).mp4")
        result = scan_raw_videos(custom)
        assert len(result) == 1
        pack_id = result[0]["pack_id"]
        assert pack_id.startswith("raw__")
        # Must not contain spaces, parens
        assert " " not in pack_id
        assert "(" not in pack_id

    def test_pack_id_starts_with_raw_prefix(self, tmp_path):
        from nexus_video.packs import scan_raw_videos
        custom = tmp_path / "custom"
        _make_fake_mp4(custom / "intro.mp4")
        result = scan_raw_videos(custom)
        # Must not collide with built-in pack IDs like "nexus_signature"
        assert result[0]["pack_id"].startswith("raw__")

    def test_returns_sorted_by_filename(self, tmp_path):
        from nexus_video.packs import scan_raw_videos
        custom = tmp_path / "custom"
        for name in ("zz.mp4", "aa.mp4", "mm.mp4"):
            _make_fake_mp4(custom / name)
        result = scan_raw_videos(custom)
        filenames = [r["filename"] for r in result]
        assert filenames == sorted(filenames)

    def test_path_points_to_file(self, tmp_path):
        from nexus_video.packs import scan_raw_videos
        custom = tmp_path / "custom"
        f = _make_fake_mp4(custom / "clip.mp4")
        result = scan_raw_videos(custom)
        assert result[0]["path"] == f

    def test_all_supported_extensions(self, tmp_path):
        from nexus_video.packs import scan_raw_videos, SUPPORTED_VIDEO_EXTS
        custom = tmp_path / "custom"
        custom.mkdir()
        for i, ext in enumerate(sorted(SUPPORTED_VIDEO_EXTS)):
            _make_fake_mp4(custom / f"clip{i}{ext}")
        result = scan_raw_videos(custom)
        assert len(result) == len(SUPPORTED_VIDEO_EXTS)


# =============================================================================
# list_raw_videos
# =============================================================================

class TestListRawVideos:
    def test_empty_returns_empty(self, tmp_path):
        from nexus_video.settings_bridge import list_raw_videos
        custom = tmp_path / "custom"
        custom.mkdir()
        build  = tmp_path / "build"
        build.mkdir()
        result = list_raw_videos(build, custom)
        assert result == []

    def test_unbuilt_pack_shows_built_false(self, tmp_path):
        from nexus_video.settings_bridge import list_raw_videos
        custom = tmp_path / "custom"
        _make_fake_mp4(custom / "clip.mp4")
        build = tmp_path / "build"
        build.mkdir()
        result = list_raw_videos(build, custom)
        assert len(result) == 1
        assert result[0]["built"] is False

    def test_built_pack_shows_built_true(self, tmp_path):
        from nexus_video.settings_bridge import list_raw_videos
        from nexus_video.packs import scan_raw_videos
        custom = tmp_path / "custom"
        _make_fake_mp4(custom / "clip.mp4")
        # Determine the pack_id the function will generate
        items  = scan_raw_videos(custom)
        pid    = items[0]["pack_id"]
        build  = tmp_path / "build"
        # Create fake build output to satisfy build_is_ready
        pack_out = build / pid
        pack_out.mkdir(parents=True)
        for fname in ("intro.mp4", "loop.mp4", "still.rgb565"):
            (pack_out / fname).write_bytes(b"\x00")
        result = list_raw_videos(build, custom)
        assert result[0]["built"] is True


# =============================================================================
# build_raw_video_pack
# =============================================================================

class TestBuildRawVideoPack:
    def _mock_ffmpeg_ok(self, out_dir: Path) -> MagicMock:
        """Return a mock subprocess.run that creates a valid still.rgb565."""
        def _side_effect(cmd, **kwargs):
            # Simulate ffmpeg writing still.rgb565
            still = out_dir / "still.rgb565"
            still.write_bytes(bytes(800 * 480 * 2))
            m = MagicMock()
            m.returncode = 0
            return m
        m = MagicMock(side_effect=_side_effect)
        return m

    def test_creates_intro_and_loop(self, tmp_path):
        from nexus_video.settings_bridge import build_raw_video_pack
        from nexus_video.packs import scan_raw_videos
        src = _make_fake_mp4(tmp_path / "clip.mp4")
        pid = "raw__" + "clip"
        out_dir = tmp_path / "build" / pid

        with patch("subprocess.run", self._mock_ffmpeg_ok(out_dir)):
            build_raw_video_pack(src, tmp_path / "build")

        assert (out_dir / "intro.mp4").exists()
        assert (out_dir / "loop.mp4").exists()

    def test_creates_still_rgb565(self, tmp_path):
        from nexus_video.settings_bridge import build_raw_video_pack
        src = _make_fake_mp4(tmp_path / "clip.mp4")
        pid = "raw__clip"
        out_dir = tmp_path / "build" / pid

        with patch("subprocess.run", self._mock_ffmpeg_ok(out_dir)):
            build_raw_video_pack(src, tmp_path / "build")

        still = out_dir / "still.rgb565"
        assert still.exists()
        assert still.stat().st_size == 800 * 480 * 2

    def test_returns_correct_pack_id(self, tmp_path):
        from nexus_video.settings_bridge import build_raw_video_pack
        src = _make_fake_mp4(tmp_path / "my_clip.mp4")
        out_dir = tmp_path / "build" / "raw__my_clip"

        with patch("subprocess.run", self._mock_ffmpeg_ok(out_dir)):
            pid = build_raw_video_pack(src, tmp_path / "build")

        assert pid == "raw__my_clip"

    def test_fallback_still_on_ffmpeg_failure(self, tmp_path):
        from nexus_video.settings_bridge import build_raw_video_pack
        src = _make_fake_mp4(tmp_path / "clip.mp4")
        out_dir = tmp_path / "build" / "raw__clip"
        out_dir.mkdir(parents=True)

        def _fail(*args, **kwargs):
            raise subprocess.CalledProcessError(1, "ffmpeg")

        with patch("subprocess.run", _fail):
            build_raw_video_pack(src, tmp_path / "build")

        still = out_dir / "still.rgb565"
        assert still.exists()
        assert still.stat().st_size == 800 * 480 * 2   # black frame

    def test_intro_is_copy_of_source(self, tmp_path):
        from nexus_video.settings_bridge import build_raw_video_pack
        src = _make_fake_mp4(tmp_path / "clip.mp4")
        src.write_bytes(b"FAKE_MP4_CONTENT_XYZ")
        out_dir = tmp_path / "build" / "raw__clip"

        with patch("subprocess.run", self._mock_ffmpeg_ok(out_dir)):
            build_raw_video_pack(src, tmp_path / "build")

        assert (out_dir / "intro.mp4").read_bytes() == b"FAKE_MP4_CONTENT_XYZ"

    def test_build_makes_pack_ready(self, tmp_path):
        from nexus_video.settings_bridge import build_raw_video_pack, build_is_ready
        src = _make_fake_mp4(tmp_path / "clip.mp4")
        build_dir = tmp_path / "build"
        out_dir   = build_dir / "raw__clip"

        with patch("subprocess.run", self._mock_ffmpeg_ok(out_dir)):
            pid = build_raw_video_pack(src, build_dir)

        assert build_is_ready(build_dir, pid)
