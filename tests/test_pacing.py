"""Test speed settings (app/pacing.py)."""
from app import executor, pacing


def test_unknown_or_empty_speed_falls_back_to_normal():
    assert pacing.pace_for("").name == "normal"
    assert pacing.pace_for("warp").name == "normal"
    assert pacing.pace_for(" FAST ").name == "fast"


def test_only_careful_keeps_fixed_pauses():
    assert pacing.SPEEDS["careful"].wait_steps_fixed
    assert pacing.SPEEDS["careful"].step_pause_ms > 0
    for name in ("fast", "normal"):
        assert not pacing.SPEEDS[name].wait_steps_fixed
        assert pacing.SPEEDS[name].step_pause_ms == 0


def test_faster_speeds_never_wait_longer():
    fast, normal, careful = (pacing.SPEEDS[k] for k in ("fast", "normal", "careful"))
    assert fast.settle_max_ms <= normal.settle_max_ms <= careful.settle_max_ms
    assert fast.slow_mo_headed <= normal.slow_mo_headed <= careful.slow_mo_headed
    assert fast.retry_backoff_ms <= normal.retry_backoff_ms <= careful.retry_backoff_ms


def test_run_config_defaults_to_normal():
    assert executor.RunConfig(project_name="p", flow_name="f").speed == "normal"


def test_custom_speed_caps_the_wait_and_keeps_smart_waits():
    pace = pacing.pace_for("custom:5")
    assert pace.settle_max_ms == 5000
    assert not pace.wait_steps_fixed and pace.step_pause_ms == 0
    assert pacing.is_valid("custom:5") and pacing.is_valid("fast")
    assert not pacing.is_valid("custom:abc") and not pacing.is_valid("warp")
    assert pacing.pace_for("custom:0").settle_max_ms == 500      # clamped low
    assert pacing.pace_for("custom:999").settle_max_ms == 120000  # clamped high
