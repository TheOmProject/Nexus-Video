"""Tests for nexus_video.detect — display detection from sysfs and ioctl."""

from pathlib import Path
from unittest.mock import patch, mock_open

import pytest

from nexus_video.detect import (
    DisplayInfo, DisplayDetectionError, detect_display, _FMT_MAP,
)
from nexus_video.config import (
    FALLBACK_BPP, FALLBACK_HEIGHT, FALLBACK_STRIDE, FALLBACK_WIDTH,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sysfs(tmp_path: Path, virtual_size: str, bpp: str, stride: str) -> Path:
    sysfs = tmp_path / "fb0"
    sysfs.mkdir()
    (sysfs / "virtual_size").write_text(virtual_size)
    (sysfs / "bits_per_pixel").write_text(bpp)
    (sysfs / "stride").write_text(stride)
    return sysfs


# ---------------------------------------------------------------------------
# Normal detection
# ---------------------------------------------------------------------------

def test_detect_reads_800x480_16bpp(tmp_path):
    sysfs = _make_sysfs(tmp_path, "800,480", "16", "1600")
    info = detect_display(sysfs_dir=sysfs)
    assert info.width == 800
    assert info.height == 480
    assert info.bpp == 16
    assert info.stride == 1600


def test_detect_reads_1024x600_16bpp(tmp_path):
    sysfs = _make_sysfs(tmp_path, "1024,600", "16", "2048")
    info = detect_display(sysfs_dir=sysfs)
    assert info.width == 1024
    assert info.height == 600
    assert info.bpp == 16


def test_detect_reads_32bpp(tmp_path):
    sysfs = _make_sysfs(tmp_path, "800,480", "32", "3200")
    info = detect_display(sysfs_dir=sysfs)
    assert info.bpp == 32
    assert info.needs_rgb32_pipeline is True
    assert info.is_supported is False


def test_detect_uses_fb_path_argument(tmp_path):
    sysfs = _make_sysfs(tmp_path, "800,480", "16", "1600")
    info = detect_display(fb_path="/dev/fb1", sysfs_dir=sysfs)
    assert info.fb_path == "/dev/fb1"


# ---------------------------------------------------------------------------
# Fallback when sysfs is absent
# ---------------------------------------------------------------------------

def test_detect_fallback_when_sysfs_missing(tmp_path):
    absent = tmp_path / "nonexistent_fb"
    info = detect_display(sysfs_dir=absent)
    assert info.width  == FALLBACK_WIDTH
    assert info.height == FALLBACK_HEIGHT
    assert info.bpp    == FALLBACK_BPP
    assert info.stride == FALLBACK_STRIDE


# ---------------------------------------------------------------------------
# DisplayInfo computed properties
# ---------------------------------------------------------------------------

def test_display_info_frame_bytes_16bpp():
    info = DisplayInfo(width=800, height=480, bpp=16, stride=1600)
    assert info.frame_bytes == 800 * 480 * 2


def test_display_info_frame_bytes_32bpp():
    info = DisplayInfo(width=800, height=480, bpp=32, stride=3200)
    assert info.frame_bytes == 800 * 480 * 4


def test_display_info_is_supported_16bpp():
    info = DisplayInfo(width=800, height=480, bpp=16, stride=1600)
    assert info.is_supported is True


def test_display_info_is_not_supported_32bpp():
    # 32bpp without channel info (no ioctl available in test env) → unsupported
    info = DisplayInfo(width=800, height=480, bpp=32, stride=3200)
    assert info.is_supported is False
    assert info.needs_rgb32_pipeline is True
    assert info.pix_fmt is None


def test_display_info_is_supported_32bpp_bcm2708():
    # BCM2708 Voron StealthChanger: r@16/8, g@8/8, b@0/8
    info = DisplayInfo(
        width=800, height=480, bpp=32, stride=3200,
        r_off=16, r_len=8, g_off=8, g_len=8, b_off=0, b_len=8,
    )
    assert info.is_supported is True
    assert info.pix_fmt == "bgra"
    assert info.bytes_per_pixel == 4
    assert info.frame_bytes == 800 * 480 * 4


def test_display_info_pix_fmt_16bpp_default():
    # 16bpp without channel info → defaults to rgb565le
    info = DisplayInfo(width=800, height=480, bpp=16, stride=1600)
    assert info.pix_fmt == "rgb565le"
    assert info.is_supported is True


def test_display_info_pix_fmt_16bpp_explicit():
    info = DisplayInfo(
        width=800, height=480, bpp=16, stride=1600,
        r_off=11, r_len=5, g_off=5, g_len=6, b_off=0, b_len=5,
    )
    assert info.pix_fmt == "rgb565le"


def test_display_info_pix_fmt_32bpp_all_formats():
    cases = [
        ((16,  8,  8,  0, 8), "rgba"),   # r@0, g@8, b@16
        (( 8,  8, 16,  8, 24, 8), "argb"),
        ((24,  8, 16,  8,  8, 8), "abgr"),
    ]
    # BCM2708 (bgra) already tested above
    info_rgba = DisplayInfo(800, 480, 32, 3200, r_off=0, r_len=8, g_off=8, g_len=8, b_off=16, b_len=8)
    assert info_rgba.pix_fmt == "rgba"

    info_argb = DisplayInfo(800, 480, 32, 3200, r_off=8, r_len=8, g_off=16, g_len=8, b_off=24, b_len=8)
    assert info_argb.pix_fmt == "argb"

    info_abgr = DisplayInfo(800, 480, 32, 3200, r_off=24, r_len=8, g_off=16, g_len=8, b_off=8, b_len=8)
    assert info_abgr.pix_fmt == "abgr"


def test_fmt_map_bcm2708_is_bgra_not_argb():
    # Regression: BCM2708 (r@16, g@8, b@0) must map to FFmpeg "bgra".
    # Memory byte order on LE: byte[0]=B, byte[1]=G, byte[2]=R, byte[3]=A.
    # FFmpeg "bgra" = B[0]G[1]R[2]A[3]. "argb" would be wrong (A[0]R[1]G[2]B[3]).
    assert _FMT_MAP.get((32, 16, 8, 8, 8, 0, 8)) == "bgra"
    assert _FMT_MAP.get((32, 8, 8, 16, 8, 24, 8)) == "argb"


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

def test_detect_raises_on_malformed_virtual_size(tmp_path):
    sysfs = _make_sysfs(tmp_path, "800x480", "16", "1600")  # 'x' not ','
    with pytest.raises(DisplayDetectionError, match="Malformed sysfs"):
        detect_display(sysfs_dir=sysfs)


def test_detect_raises_on_non_numeric_bpp(tmp_path):
    sysfs = _make_sysfs(tmp_path, "800,480", "sixteen", "1600")
    with pytest.raises(DisplayDetectionError, match="Malformed sysfs"):
        detect_display(sysfs_dir=sysfs)


def test_detect_raises_on_missing_bits_per_pixel_file(tmp_path):
    sysfs = tmp_path / "fb0"
    sysfs.mkdir()
    (sysfs / "virtual_size").write_text("800,480")
    (sysfs / "stride").write_text("1600")
    # bits_per_pixel intentionally missing
    with pytest.raises(DisplayDetectionError, match="Cannot read sysfs"):
        detect_display(sysfs_dir=sysfs)
