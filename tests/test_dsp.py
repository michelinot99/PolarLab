import numpy as np
import pytest

from polarlab.core.ringbuffer import Ring
from polarlab.dsp import dfa, hrv
from polarlab.dsp.filters import EcgFilter, SosFilter, UniformResampler, bandpass_sos
from polarlab.dsp.motion import MotionAnalyzer
from polarlab.dsp.qrs import detect_offline
from polarlab.dsp.respiration import RespirationEstimator
from polarlab.dsp.rr_clean import ECTOPIC, EXTRA, LONGSHORT, MISSED, NORMAL, RRCleaner
from polarlab.protocol.decoder import StreamDecoder
from polarlab.protocol.events import EcgChunk
from polarlab.sources.simulator import H10Simulator

FS = 130.0


@pytest.fixture(scope="module")
def sim_ecg():
    """10 minuti di scenario simulato (riposo → corsa → soglia), decodificati dai byte."""
    sim = H10Simulator(seed=3, host_t0=0.0)
    dec = StreamDecoder()
    for p in sim.startup_packets():
        dec.feed(p)
    ts, xs = [], []
    for _ in range(1200):
        for p in sim.advance(0.5):
            for e in dec.feed(p):
                if isinstance(e, EcgChunk):
                    ts.append(e.t)
                    xs.append(e.uv)
    return sim, np.concatenate(ts), np.concatenate(xs)


def match(detected, truth, tol=0.15):
    j = np.searchsorted(detected, truth)
    errs = []
    for k, tr in enumerate(truth):
        cands = [detected[i] for i in (j[k] - 1, j[k]) if 0 <= i < len(detected)]
        d = min(cands, key=lambda v: abs(v - tr))
        if abs(d - tr) < tol:
            errs.append(d - tr)
    return np.asarray(errs)


def beat_times(t, x):
    bp, _ = EcgFilter(FS).process(x)
    beats = detect_offline(bp, FS)
    return np.interp([b.index for b in beats], np.arange(len(t)), t)


def test_qrs_sensitivity_and_timing(sim_ecg):
    sim, t, x = sim_ecg
    bt = beat_times(t, x)
    lo, hi = t[0] + 3, t[-1] - 1
    truth = sim.true_beats_between(lo, hi)
    det = bt[(bt > lo) & (bt < hi)]
    errs = match(det, truth)
    se, ppv = len(errs) / len(truth), len(errs) / len(det)
    assert se >= 0.995 and ppv >= 0.995
    # jitter robusto (MAD) sotto il millisecondo grazie all'interpolazione sub-campione
    jitter = 1.4826 * np.median(np.abs(errs - np.median(errs))) * 1000
    assert jitter < 1.0


def test_qrs_inverted_polarity(sim_ecg):
    sim, t, x = sim_ecg
    n = int(120 * FS)
    bt = beat_times(t[:n], -x[:n])
    truth = sim.true_beats_between(t[0] + 3, t[n - 1] - 1)
    assert len(match(bt, truth)) >= 0.99 * len(truth)


def test_qrs_recovers_after_contact_loss(sim_ecg):
    sim, t, x = sim_ecg
    n = int(60 * FS)
    x2 = x[:n].copy()
    x2[int(20 * FS):int(26 * FS)] = 0.0             # 6 s di fascia staccata
    bt = beat_times(t[:n], x2)
    truth = sim.true_beats_between(t[0] + 30, t[n - 1] - 1)
    assert len(match(bt[bt > t[0] + 29], truth)) >= 0.98 * len(truth)


def _rr_series(n=400, seed=0):
    rng = np.random.default_rng(seed)
    return 800 + 30 * np.sin(np.arange(n) / 4) + rng.normal(0, 8, n)


def _clean(rr):
    c = RRCleaner()
    out = []
    t = np.cumsum(rr) / 1000
    for ti, r in zip(t, rr):
        out += c.push(ti, r)
    return out + c.flush()


def test_rr_cleaner_keeps_normal_series():
    out = _clean(_rr_series())
    assert sum(o.kind != NORMAL for o in out) / len(out) < 0.01


def test_rr_cleaner_missed_extra_ectopic():
    rr = list(_rr_series())
    rr[100:102] = [rr[100] + rr[101]]                       # battito mancato
    rr[200:201] = [rr[200] * 0.35, rr[200] * 0.65]          # battito in più
    rr[300], rr[301] = rr[300] * 0.7, rr[301] * 1.3         # ectopico + pausa compensatoria
    out = _clean(np.asarray(rr))
    kinds = [o.kind for o in out]
    assert kinds.count(MISSED) == 2
    assert kinds.count(EXTRA) == 1
    assert sum(k in (ECTOPIC, LONGSHORT) for k in kinds) >= 1
    nn = np.array([o.nn for o in out])
    assert np.all(np.abs(nn - 800) < 120)                   # serie corretta senza salti


def test_time_domain_known_values():
    nn = np.array([800.0, 860.0, 790.0, 800.0, 810.0, 700.0])
    td = hrv.time_domain(nn)
    d = np.diff(nn)
    assert td.rmssd == pytest.approx(np.sqrt(np.mean(d ** 2)))
    assert td.sdnn == pytest.approx(np.std(nn, ddof=1))
    assert td.pnn50 == pytest.approx(100 * np.mean(np.abs(d) > 50))
    assert td.sd1 ** 2 == pytest.approx(0.5 * np.var(d, ddof=1))


def test_frequency_domain_finds_rsa():
    t = np.cumsum(np.full(400, 0.8))
    nn = 800 + 40 * np.sin(2 * np.pi * 0.25 * t)
    fd = hrv.frequency_domain(t, nn)
    assert fd.hf > 10 * fd.lf
    assert fd.resp_peak_hz == pytest.approx(0.25, abs=0.02)


def _powerlaw_noise(n, beta, seed):
    rng = np.random.default_rng(seed)
    f = np.fft.rfftfreq(n)
    spec = rng.normal(size=len(f)) + 1j * rng.normal(size=len(f))
    spec[1:] /= f[1:] ** (beta / 2)
    spec[0] = 0
    return np.fft.irfft(spec, n)


@pytest.mark.parametrize("beta,expected", [(0.0, 0.5), (1.0, 1.0), (2.0, 1.5)])
def test_dfa_alpha1_on_known_noise(beta, expected):
    vals = [dfa.dfa_alpha(_powerlaw_noise(4096, beta, s)) for s in range(8)]
    assert np.mean(vals) == pytest.approx(expected, abs=0.12)


def test_dfa_needs_enough_beats():
    assert np.isnan(dfa.dfa_alpha(np.ones(20)))


def test_streaming_filter_equals_one_shot():
    rng = np.random.default_rng(2)
    x = rng.normal(size=2000)
    one = SosFilter(bandpass_sos(FS, 0.5, 40)).process(x)
    f = SosFilter(bandpass_sos(FS, 0.5, 40))
    chunks = np.concatenate([f.process(x[i:i + 73]) for i in range(0, len(x), 73)])
    assert np.allclose(one, chunks)


def test_resampler_maps_real_clock_to_nominal_grid():
    # Quarzo a 101.9 Hz, griglia a 100 Hz: blocchi arbitrari danno lo stesso risultato del segnale intero
    t = np.arange(2000) / 101.9
    x = np.column_stack([np.sin(2 * np.pi * 2.0 * t), np.cos(2 * np.pi * 0.3 * t)])
    rs, out_t, out_x = UniformResampler(100.0), [], []
    for a, b in zip([0, 36, 72, 500, 1300], [36, 72, 500, 1300, 2000]):
        gt, gx = rs.push(t[a:b], x[a:b])
        out_t.append(gt)
        out_x.append(gx)
    gt, gx = np.concatenate(out_t), np.concatenate(out_x)
    assert np.allclose(np.diff(gt), 0.01) and gt[-1] <= t[-1]
    assert len(gt) == int(t[-1] * 100) + 1
    assert np.max(np.abs(gx[:, 0] - np.sin(2 * np.pi * 2.0 * gt))) < 0.01


def _running_acc(fs, cadence, seconds, up=(0.3, -0.9, 0.3)):
    up = np.asarray(up) / np.linalg.norm(up)
    t = np.arange(int(seconds * fs)) / fs
    ph = 2 * np.pi * cadence / 60 * t
    vert = 700 * (0.9 * np.cos(ph) + 0.35 * np.cos(2 * ph + 0.5)) + 150 * np.sin(ph / 2)
    return 1000 * up + vert[:, None] * up


@pytest.mark.parametrize("cadence", [110.0, 165.0, 182.0])
def test_cadence_any_orientation(cadence):
    m = MotionAnalyzer(100.0)
    m.push(_running_acc(100.0, cadence, 10))
    st = m.update()
    assert st.cadence_spm == pytest.approx(cadence, abs=2.0)


def test_cadence_nan_when_still():
    m = MotionAnalyzer(100.0)
    m.push(np.tile([0.0, -1000.0, 0.0], (1000, 1)) + np.random.default_rng(0).normal(0, 4, (1000, 3)))
    assert np.isnan(m.update().cadence_spm)


def test_respiration_from_rsa_and_amplitude():
    r = RespirationEstimator()
    t, f = 0.0, 15 / 60
    while t < 70:
        rr = 0.75 + 0.04 * np.sin(2 * np.pi * f * t)
        t += rr
        r.push_beat(t, 1000 * (1 + 0.1 * np.sin(2 * np.pi * f * t)), rr * 1000)
    est = r.estimate()
    assert est.brpm == pytest.approx(15, abs=1.0) and est.source == "ecg"


def test_ring_absolute_indexing():
    r = Ring(5)
    r.extend(np.arange(3))
    r.extend(np.arange(3, 9))
    assert (r.start, r.end) == (4, 9)
    assert r.get(6, 9).tolist() == [6, 7, 8]
    with pytest.raises(IndexError):
        r.get(2, 5)
