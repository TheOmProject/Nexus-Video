"""Framebuffer display detection.

Primary path: FBIOGET_VSCREENINFO ioctl on /dev/fb0 — reads depth AND channel
offsets reliably on all Raspberry Pi kernels (BCM2708, VC4), including those
where the per-channel sysfs files (red/green/blue) are absent.

Fallback: sysfs virtual_size, bits_per_pixel, stride — available on all Pi
kernels but provides depth only (no channel layout).
"""

import logging
import struct
import fcntl
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

from .config import (
    DEFAULT_FB_PATH,
    FALLBACK_BPP, FALLBACK_HEIGHT, FALLBACK_PIX_FMT, FALLBACK_STRIDE, FALLBACK_WIDTH,
    SYSFS_FB0_DIR, SUPPORTED_BPP, WARN_BPP,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pixel-format map: (bpp, r_off, r_len, g_off, g_len, b_off, b_len) → FFmpeg pix_fmt
#
# Channel offsets are Linux framebuffer bit-positions within a little-endian
# 32-bit integer.  On LE (ARM/x86) the mapping to memory byte order is:
#   bits 0-7  → byte[0] (lowest address)
#   bits 8-15 → byte[1]
#   …
#
# Example — BCM2708 (r@16, g@8, b@0):
#   byte[0]=B, byte[1]=G, byte[2]=R, byte[3]=A  →  FFmpeg "bgra"
# ---------------------------------------------------------------------------

_FMT_MAP: dict[tuple, str] = {
    (16, 11, 5,  5, 6,  0, 5): "rgb565le",   # 16bpp RGB565 (Micron, Pi 4 default)
    (32, 16, 8,  8, 8,  0, 8): "bgra",        # BCM2708/Voron StealthChanger
    (32,  0, 8,  8, 8, 16, 8): "rgba",
    (32,  8, 8, 16, 8, 24, 8): "argb",
    (32, 24, 8, 16, 8,  8, 8): "abgr",
}

# FBIOGET_VSCREENINFO ioctl and struct offsets (Linux <linux/fb.h>)
_FBIOGET_VSCREENINFO = 0x4600
_VSCREENINFO_BPP_OFFSET    = 24   # bits_per_pixel
_VSCREENINFO_R_OFF_OFFSET  = 32   # red.offset
_VSCREENINFO_R_LEN_OFFSET  = 36   # red.length
_VSCREENINFO_G_OFF_OFFSET  = 44   # green.offset
_VSCREENINFO_G_LEN_OFFSET  = 48   # green.length
_VSCREENINFO_B_OFF_OFFSET  = 56   # blue.offset
_VSCREENINFO_B_LEN_OFFSET  = 60   # blue.length


@dataclass
class DisplayInfo:
    width:    int
    height:   int
    bpp:      int
    stride:   int      # bytes per row
    fb_path:  str = DEFAULT_FB_PATH
    # Channel layout (populated from ioctl; -1 = unknown)
    r_off:    int = -1
    r_len:    int = 0
    g_off:    int = -1
    g_len:    int = 0
    b_off:    int = -1
    b_len:    int = 0

    @property
    def bytes_per_pixel(self) -> int:
        return self.bpp // 8

    @property
    def frame_bytes(self) -> int:
        return self.width * self.height * self.bytes_per_pixel

    @property
    def pix_fmt(self) -> Optional[str]:
        """FFmpeg pixel format string, or None when channel layout is unknown.

        For 16bpp with unknown channels, returns FALLBACK_PIX_FMT ("rgb565le")
        because all Raspberry Pi 16bpp displays use RGB565.
        """
        key = (self.bpp, self.r_off, self.r_len, self.g_off, self.g_len, self.b_off, self.b_len)
        fmt = _FMT_MAP.get(key)
        if fmt is None and self.bpp == 16:
            return FALLBACK_PIX_FMT   # safe assumption for 16bpp
        return fmt

    @property
    def is_supported(self) -> bool:
        """True when the pipeline can generate correct output for this display.

        * 16bpp: always supported (rgb565le assumed when channels unknown).
        * 32bpp: supported only when channel layout is known (via ioctl).
        """
        if self.bpp == 16:
            return True
        if self.bpp == 32:
            return self.pix_fmt is not None
        return False

    @property
    def needs_rgb32_pipeline(self) -> bool:
        """True for 32bpp displays (uses ARGB/RGBA/BGRA/ABGR path, not RGB565)."""
        return self.bpp == 32


class DisplayDetectionError(RuntimeError):
    pass


def _read_sysfs(sysfs_dir: Path, name: str) -> str:
    return (sysfs_dir / name).read_text().strip()


def _detect_via_ioctl(fb_path: str) -> Optional[Tuple[int, int, int, int, int, int, int]]:
    """Read fb_var_screeninfo via FBIOGET_VSCREENINFO ioctl.

    Returns (bpp, r_off, r_len, g_off, g_len, b_off, b_len) or None on failure.
    Silently returns None on any OS error (no /dev/fb0, permission denied, etc.)
    so callers on non-Pi hosts do not crash.
    """
    try:
        with open(fb_path, "rb") as f:
            buf = bytearray(160)
            fcntl.ioctl(f, _FBIOGET_VSCREENINFO, buf)
        bpp   = struct.unpack_from("<I", buf, _VSCREENINFO_BPP_OFFSET)[0]
        r_off = struct.unpack_from("<I", buf, _VSCREENINFO_R_OFF_OFFSET)[0]
        r_len = struct.unpack_from("<I", buf, _VSCREENINFO_R_LEN_OFFSET)[0]
        g_off = struct.unpack_from("<I", buf, _VSCREENINFO_G_OFF_OFFSET)[0]
        g_len = struct.unpack_from("<I", buf, _VSCREENINFO_G_LEN_OFFSET)[0]
        b_off = struct.unpack_from("<I", buf, _VSCREENINFO_B_OFF_OFFSET)[0]
        b_len = struct.unpack_from("<I", buf, _VSCREENINFO_B_LEN_OFFSET)[0]
        return bpp, r_off, r_len, g_off, g_len, b_off, b_len
    except (OSError, ImportError):
        return None


def detect_display(
    fb_path: str = DEFAULT_FB_PATH,
    sysfs_dir: Optional[Path] = None,
) -> DisplayInfo:
    """Detect display geometry and pixel format.

    1. Reads width, height, bpp, stride from sysfs (reliable on all Pi kernels).
    2. Reads channel offsets/lengths via FBIOGET_VSCREENINFO ioctl on fb_path
       (more reliable than sysfs per-channel files which are absent on BCM2708).

    Returns a DisplayInfo populated from sysfs + ioctl.  Falls back to
    FALLBACK_* constants (and logs a warning) when sysfs is unavailable so
    callers on non-Pi development hosts still work.

    Raises DisplayDetectionError only on malformed (non-numeric) sysfs values.
    """
    sysfs = sysfs_dir if sysfs_dir is not None else SYSFS_FB0_DIR

    if not sysfs.exists():
        log.warning(
            "sysfs %s not found — using fallback %dx%d bpp=%d",
            sysfs, FALLBACK_WIDTH, FALLBACK_HEIGHT, FALLBACK_BPP,
        )
        return DisplayInfo(
            width=FALLBACK_WIDTH, height=FALLBACK_HEIGHT,
            bpp=FALLBACK_BPP, stride=FALLBACK_STRIDE,
            fb_path=fb_path,
        )

    try:
        raw_size   = _read_sysfs(sysfs, "virtual_size")   # "800,480"
        raw_bpp    = _read_sysfs(sysfs, "bits_per_pixel")  # "16" or "32"
        raw_stride = _read_sysfs(sysfs, "stride")          # "1600" or "3200"
    except OSError as exc:
        raise DisplayDetectionError(f"Cannot read sysfs {sysfs}: {exc}") from exc

    try:
        w_str, h_str = raw_size.split(",")
        width  = int(w_str)
        height = int(h_str)
        bpp    = int(raw_bpp)
        stride = int(raw_stride)
    except ValueError as exc:
        raise DisplayDetectionError(
            f"Malformed sysfs values in {sysfs}: size={raw_size!r} "
            f"bpp={raw_bpp!r} stride={raw_stride!r}"
        ) from exc

    # Try ioctl for channel offsets (works on BCM2708 and VC4; sysfs red/green/blue absent on BCM2708)
    r_off, r_len, g_off, g_len, b_off, b_len = -1, 0, -1, 0, -1, 0
    ioctl_result = _detect_via_ioctl(fb_path)
    if ioctl_result is not None:
        ioctl_bpp, r_off, r_len, g_off, g_len, b_off, b_len = ioctl_result
        if ioctl_bpp != bpp:
            log.warning(
                "ioctl bpp=%d differs from sysfs bpp=%d — trusting sysfs",
                ioctl_bpp, bpp,
            )

    info = DisplayInfo(
        width=width, height=height, bpp=bpp, stride=stride, fb_path=fb_path,
        r_off=r_off, r_len=r_len, g_off=g_off, g_len=g_len, b_off=b_off, b_len=b_len,
    )

    if info.is_supported:
        log.info(
            "Display detected: %dx%d bpp=%d stride=%d pix_fmt=%s (%s)",
            width, height, bpp, stride, info.pix_fmt, fb_path,
        )
    elif info.bpp == 32:
        log.warning(
            "32bpp display detected but channel layout unknown (ioctl unavailable). "
            "Run as root or ensure /dev/fb0 is readable for format detection."
        )
    else:
        log.warning("Unsupported bpp=%d on %dx%d display.", bpp, width, height)

    return info
