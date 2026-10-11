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


def test_video_keeps_the_page_shape_and_never_enlarges():
    # A tile at scale 0.7: frames are ~960x430 device px -> recorded at that size.
    assert screens.video_size(958, 432) == {"width": 958, "height": 432}
    # A maximised 1920-wide page is reduced to 1280, same shape.
    assert screens.video_size(1920, 930) == {"width": 1280, "height": 620}
    # Odd sizes are made even (video encoders require it); junk falls back.
    assert screens.video_size(1001, 601) == {"width": 1000, "height": 600}
    assert screens.video_size(0, 0) == screens.DEFAULT_VIDEO
