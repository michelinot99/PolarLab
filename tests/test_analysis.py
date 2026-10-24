import asyncio
import os

import numpy as np
import pytest

from polarlab.analysis import report as R
from polarlab.core.session import SessionWriter
from polarlab.sources.replay import ReplaySource
from polarlab.sources.simulator import H10Simulator


@pytest.fixture(scope="module")
def sim_session(tmp_path_factory):
    """Scenario completo (riposo → corsa → defaticamento) con ~1% di battiti ectopici veri."""
    sim = H10Simulator(host_t0=0.0, ectopic_rate=0.01, loop=False)
    w = SessionWriter(tmp_path_factory.mktemp("sess") / "s", {"source": "test"})
    for p in sim.startup_packets():
        w.write(p)
    for _ in range(int(640 / 0.5)):
        for p in sim.advance(0.5):
            w.write(p)
    return sim, w.close()


def test_report_finds_true_premature_beats(sim_session):
    sim, d = sim_session
    rep = R.analyze(d, use_cache=False)
    ev = R.events(rep)
    premature = [e for e in ev if e.kind == "premature"]
    true_n = sum(1 for b in sim.beats if b[2] and b[0] < 638)
    assert abs(len(premature) - true_n) <= 1
    assert not [e for e in ev if e.kind == "artifact"]
    # I battiti ectopici del simulatore sono "ventricolari": QRS largo, forma diversa
    assert all("forma diversa" in e.detail for e in premature)
    assert np.nanmedian(rep.ect_corr) < 0.7
    # Il ritmo regolare della corsa non deve generare falsi allarmi
    assert not [e for e in ev if e.kind in ("irregular", "pause", "high_hr", "hr_jump")]


def test_report_metrics(sim_session):
    sim, d = sim_session
    rep = R.analyze(d)
    true_hr = 60.0 / np.mean([b[1] for b in sim.beats])
    assert R.hr_stats(rep)["avg"] == pytest.approx(true_hr, rel=0.05)
    assert R.hr_stats(rep)["max"] == pytest.approx(168, abs=6)              # "Ritmo soglia"
    act = R.activity(rep)
    assert act["active_s"] == pytest.approx(640 - 90, abs=20)                # si muove dopo i 90 s di riposo
    # Passi attesi dalle cadenze dello scenario: 60 s a 112, 240 a 166, 150 a 178, 100 a 120
    expected = (60 * 112 + 240 * 166 + 150 * 178 + 100 * 120) / 60
    assert act["steps"] == pytest.approx(expected, rel=0.08)
    rest = R.rest_hrv(rep)
    assert rest is not None and rest[0].hr == pytest.approx(62, abs=4)
    dq = R.data_quality(rep)
    assert dq["good_pct"] > 90 and rep.stats["gaps"] == 0
    zones = R.hr_zones(rep, R.Profile(hr_max=190, hr_rest=50))
    assert sum(z[3] for z in zones) == pytest.approx(np.isfinite(R.hr_series(rep)).sum())
    assert R.hr_zones(rep, R.Profile()) is None
    assert sum(R.alpha1_zones(rep).values()) > 300


def test_cache_roundtrip_and_invalidation(sim_session):
    _, d = sim_session
    rep = R.analyze(d)
    assert (d / R.CACHE_NPZ).exists() and R.cached_summary(d) is not None
    back = R.analyze(d)
    np.testing.assert_array_equal(back.ecg, rep.ecg)
    assert back.b_kind.tolist() == rep.b_kind.tolist()
    assert [e.title for e in R.events(back)] == [e.title for e in R.events(rep)]
    st = (d / "raw.plab").stat()
    os.utime(d / "raw.plab", ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert R.cached_summary(d) is None


def test_recovery_and_thresholds_on_synthetic_series():
    t = np.arange(0, 400.0)
    hr = np.where(t < 300, 100 + 0.2 * t, 160 - 0.5 * (t - 300))     # sale fino a 160, poi recupera 0.5 bpm/s
    a1 = 1.25 - (hr - 100) / 100                                       # α1 = 0.75 a 150 bpm, 0.5 a 175
    n = len(t)
    nan = np.full(n, np.nan)
    rep = R.SessionReport(
        dir=None, meta={}, duration_s=400.0, ecg_fs=130.0, stats={},
        m_t=t, hr_ecg=hr, hr_strap=nan, dfa_a1=np.where(t < 300, a1, np.nan), quality=np.full(n, 95.0),
        artifact_pct=np.zeros(n), cadence=nan, motion=nan, vosc=nan, rmssd=nan, resp=nan, battery=nan,
        strap_mae=nan, strap_match=nan, b_t=np.empty(0), b_rr=np.empty(0), b_nn=np.empty(0), b_kind=np.empty(0, "U9"),
        b_med=np.empty(0), r_t=np.empty(0), r_amp=np.empty(0), ecg_t=np.empty(0), ecg=np.empty(0),
        step_t=np.empty(0), active=np.array([[10.0, 300.0]]), tpl_t=np.empty(0), tpl_normal=np.empty(0),
        tpl_ectopic=np.empty(0), ect_t=np.empty(0), ect_corr=np.empty(0), gaps=np.empty((0, 2)),
        contact_lost=np.empty((0, 2)))
    rec = R.recovery(rep)
    assert rec["hr_peak"] == pytest.approx(160, abs=1)
    assert rec["drops"][30] == pytest.approx(15, abs=1.5)
    assert rec["drops"][60] == pytest.approx(30, abs=1.5)
    assert 120 not in rec["drops"] or rec["drops"][120] == pytest.approx(60, abs=1.5)
    thr = R.thresholds(rep)
    assert thr["vt1"] == pytest.approx(150, abs=1) and thr["vt1_reliable"]
    assert thr["vt2"] == pytest.approx(175, abs=1) and not thr["vt2_reliable"]   # oltre i valori osservati
    assert not R.events(rep)


def test_replay_starts_from_offset(sim_session):
    _, d = sim_session
    got = []
    src = ReplaySource(d, speed=1000.0, start_s=600.0)

    async def run():
        stop = asyncio.Event()
        t0 = asyncio.get_running_loop().time()
        await src.run(got.append, lambda s: None, stop)
        return asyncio.get_running_loop().time() - t0

    elapsed = asyncio.run(run())
    assert len(got) > 1000                     # anche i pacchetti prima di 600 s arrivano al motore
    assert elapsed < 1.0                       # ma senza attese: 40 s a ×1000 sono 0.04 s


def test_import_iphone_zip(sim_session, tmp_path):
    import zipfile

    from polarlab.ui.sessions_view import import_path, list_sessions, zip_session_name
    _, d = sim_session
    z = tmp_path / "PolarLab_2026-10-25_081500.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for f in ("raw.plab", "meta.json"):
            zf.write(d / f, f"2026-10-25_081500/{f}")
        zf.writestr("../evil.txt", "x")                      # percorsi fuori dalla sessione: ignorati
    assert zip_session_name(z) == "2026-10-25_081500"
    root = tmp_path / "sessions"
    dest = import_path(z, root)
    assert dest == root / "2026-10-25_081500" and (dest / "raw.plab").exists()
    assert not (tmp_path / "evil.txt").exists()
    assert import_path(z, root) == dest                      # stessa registrazione: nessun doppione
    assert list_sessions(root) == [dest]
    assert R.analyze(dest).duration_s > 600


def test_zero_phase_filter_does_not_cross_data_gaps():
    # Due tratti con livelli di base molto diversi separati da un buco: il secondo non deve
    # "vedere" il primo (il passa-alto lo farebbe oscillare per secondi attorno al buco)
    fs = 130.0
    t = np.r_[np.arange(0, 20, 1 / fs), 25 + np.arange(0, 20, 1 / fs)]
    uv = np.r_[np.full(int(20 * fs), 3000.0), np.zeros(int(20 * fs))]
    out = R._zero_phase_by_segment(t, uv, fs)
    assert np.max(np.abs(out)) < 1.0


def test_strap_rr_confirms_real_rhythm_changes_only():
    # Rallentamento brusco reale: la fascia (rilevatore indipendente) misura gli stessi RR
    rr = np.r_[np.full(30, 560.0), 730.0, 915.0, 835.0, np.full(30, 780.0)]
    b_t = np.cumsum(rr) / 1000.0
    hr_rows = [(float(b_t[i + 1]), 0, f"{rr[i]:.1f};{rr[i + 1]:.1f}", True) for i in range(0, len(rr) - 1, 2)]
    rep = R.SessionReport(
        dir=None, meta={}, duration_s=float(b_t[-1]), ecg_fs=130.0, stats={}, m_t=np.empty(0), hr_ecg=np.empty(0),
        hr_strap=np.empty(0), dfa_a1=np.empty(0), quality=np.empty(0), artifact_pct=np.empty(0), cadence=np.empty(0),
        motion=np.empty(0), vosc=np.empty(0), rmssd=np.empty(0), resp=np.empty(0), battery=np.empty(0),
        strap_mae=np.empty(0), strap_match=np.empty(0), b_t=b_t, b_rr=rr, b_nn=rr, b_kind=np.full(len(rr), "normal"),
        b_med=rr, r_t=b_t, r_amp=np.ones(len(rr)), ecg_t=np.empty(0), ecg=np.empty(0), step_t=np.empty(0),
        active=np.empty((0, 2)), tpl_t=np.empty(0), tpl_normal=np.empty(0), tpl_ectopic=np.empty(0),
        ect_t=np.empty(0), ect_corr=np.empty(0), gaps=np.empty((0, 2)), contact_lost=np.empty((0, 2)),
        **R._strap_rr(hr_rows))
    assert np.allclose(rep.s_rr, rr[: len(rep.s_rr)])
    assert np.allclose(rep.s_t, b_t[: len(rep.s_t)])
    assert R._confirmed_by_strap(rep, [30, 31, 32])
    # Un nostro falso battito spezza un intervallo in due: la fascia non ha quella sequenza
    rep.b_rr = rr.copy()
    rep.b_rr[31:33] = [400.0, 515.0]
    assert not R._confirmed_by_strap(rep, [31, 32])
