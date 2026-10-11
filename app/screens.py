"""Browser windows for a run: real screen size, and N windows tiled side by side.

Two problems this solves when watching a run (headed mode):

- **The page looked zoomed in.** Windows display scaling (150% on a 1920x1080
  laptop) gives a maximised browser only 1280x672 CSS pixels, so desktop ERP
  forms needed scrolling -- and two windows side by side would each fall into a
  phone-sized layout. Test browsers render at scale 1 by default
  (``QA_BROWSER_SCALE``), so the application sees the full physical screen.
- **Several windows piled on top of each other.** Each "screen" now gets its own
  browser window, placed in a grid across the work area, so every one is visible.

Headless runs are unaffected.
"""
from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int


def browser_scale() -> float:
    """Device scale factor for test browsers. 1 = full physical resolution."""
    try:
        value = float(os.getenv("QA_BROWSER_SCALE", "1") or 1)
    except ValueError:
        value = 1.0
    return min(max(value, 0.5), 3.0)


def work_area() -> Rect:
    """The usable desktop (screen minus taskbar) in physical pixels."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            try:
                user32.SetProcessDPIAware()   # report physical, not scaled, pixels
            except Exception:
                pass
            rect = wintypes.RECT()
            if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):  # SPI_GETWORKAREA
                return Rect(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
            return Rect(0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
        except Exception:
            pass
    return Rect(0, 0, 1920, 1040)


def tile(count: int, area: Rect | None = None) -> list[Rect]:
    """``count`` window rectangles filling ``area`` in a near-square grid."""
    count = max(1, count)
    area = area or work_area()
    cols = math.ceil(math.sqrt(count))
    rows = math.ceil(count / cols)
    width, height = area.width // cols, area.height // rows
    return [Rect(area.x + (i % cols) * width, area.y + (i // cols) * height, width, height)
            for i in range(count)]


#: The smallest page a tiled window should show. Below this, desktop
#: applications switch layout or push a dialog's dropdown off-screen -- on
#: Liberty a 960x516 tile made a Category click land on the dialog's mask,
#: which closed the form.
MIN_PAGE = (1366, 720)


def tile_scale(r: Rect) -> float:
    """Scale that shows at least MIN_PAGE CSS pixels inside a window of ``r``."""
    # Rounded down: rounding up would leave the page a pixel short of MIN_PAGE.
    return math.floor(min(browser_scale(), r.width / MIN_PAGE[0], r.height / MIN_PAGE[1]) * 1000) / 1000


def launch_args(slot: int | None, total: int, headless: bool) -> list[str]:
    """Chromium arguments for one window. ``slot`` None = a single maximised window."""
    if headless:
        return []
    if slot is None or total <= 1:
        return [f"--force-device-scale-factor={browser_scale():g}", "--start-maximized"]
    # A tile is smaller than the screen, so its content is scaled down rather
    # than squeezed: every window shows the full desktop layout, just smaller.
    # Position and size are in physical pixels; Chromium reads them in units of
    # the scale factor, so divide.
    r = tile(total)[slot]
    scale = tile_scale(r)
    return [f"--force-device-scale-factor={scale:g}",
            f"--window-position={int(r.x / scale)},{int(r.y / scale)}",
            f"--window-size={int(r.width / scale)},{int(r.height / scale)}"]
