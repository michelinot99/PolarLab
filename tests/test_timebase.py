import numpy as np
import pytest

from polarlab.protocol.timebase import ClockSync, StreamClock


def test_reconstructs_sample_times_backwards():
    c = StreamClock(130.0)
    t, missing = c.push(10_000_000_000, 73)
    assert missing == 0 and t[-1] == pytest.approx(10.0)
    assert np.allclose(np.diff(t), 1 / 130)


def test_tracks_device_clock_drift():
    c = StreamClock(130.0)
    true_dt = 1 / 130 * (1 + 0.004)   # quarzo 0.4% lento: entro la tolleranza di continuità
    t_last = 5.0
    for _ in range(200):
        t_last += 73 * true_dt
        _, missing = c.push(int(t_last * 1e9), 73)
        assert missing == 0
    assert c.dt == pytest.approx(true_dt, rel=1e-4)


def test_detects_lost_frames():
    c = StreamClock(130.0)
    c.push(int(1e9), 73)
    _, missing = c.push(int((1 + 3 * 73 / 130) * 1e9), 73)   # persi 2 frame
    assert missing == 146


def test_non_monotonic_timestamp_flags_reset():
    c = StreamClock(130.0)
    c.push(int(5e9), 73)
    _, missing = c.push(int(1e9), 73)
    assert missing == -1


def test_clock_sync_uses_minimum_latency():
    s = ClockSync()
    rng = np.random.default_rng(1)
    for k in range(200):
        dev = 1000.0 + k * 0.5
        s.update(dev + 1_700_000_000.0 + 0.010 + abs(rng.normal(0, 0.02)), dev)
    assert s.offset == pytest.approx(1_700_000_000.010, abs=0.002)
    assert s.jitter_ms > 0
