"""Window tiling for headed multi-screen runs (app/screens.py)."""
from app import screens

AREA = screens.Rect(0, 0, 1920, 1032)


def test_tiles_cover_the_area_without_overlap():
    for n in (1, 2, 3, 4, 6, 8):
        rects = screens.tile(n, AREA)
        assert len(rects) == n
        for i, a in enumerate(rects):
            assert a.x >= 0 and a.y >= 0 and a.x + a.width <= 1920 and a.y + a.height <= 1032
            for b in rects[i + 1:]:
                assert a.x + a.width <= b.x or b.x + b.width <= a.x or \
                       a.y + a.height <= b.y or b.y + b.height <= a.y


def test_every_tile_shows_at_least_a_desktop_sized_page():
    for n in (2, 3, 4, 6, 8):
        for r in screens.tile(n, AREA):
            scale = screens.tile_scale(r)
            assert r.width / scale >= screens.MIN_PAGE[0] - 1
            assert r.height / scale >= screens.MIN_PAGE[1] - 1


def test_headless_needs_no_window_arguments():
    assert screens.launch_args(0, 3, headless=True) == []


def test_single_headed_window_is_maximised_at_full_resolution():
    args = screens.launch_args(None, 1, headless=False)
    assert "--start-maximized" in args
    assert any(a.startswith("--force-device-scale-factor=") for a in args)
