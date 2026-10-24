import asyncio
import time

import numpy as np
import pytest

from polarlab.core.engine import Engine
from polarlab.core.pipeline import Pipeline
from polarlab.core.session import SessionWriter, export_session, read_packets
from polarlab.protocol.channels import Channel, Packet
from polarlab.protocol.decoder import StreamDecoder
from polarlab.protocol.pmd import MeasurementType
from polarlab.sources.replay import ReplaySource
from polarlab.sources.simulator import H10Simulator, SimulatedSource


def run_sim(seconds, **kw):
    sim = H10Simulator(host_t0=0.0, **kw)
    dec, eng = StreamDecoder(), Engine(keep_log=True)
    packets = list(sim.startup_packets())
    for _ in range(int(seconds / 0.5)):
        packets += sim.advance(0.5)
    for p in packets:
        for e in dec.feed(p):
            eng.handle(e)
    eng.finish()
    return sim, dec, eng, packets


def test_engine_end_to_end_metrics():
    sim, dec, eng, _ = run_sim(240)
    m = eng.metrics
    assert dec.stats.errors == 0 and dec.stats.gaps == 0
    true_hr = 60.0 / np.mean([b[1] for b in sim.beats if 230 < b[0] < 240])
    assert m.hr_ecg == pytest.approx(true_hr, rel=0.05)
    assert m.quality > 80 and m.strap_mae_ms < 3.0
    assert np.isfinite(m.rmssd) and np.isfinite(m.dfa_a1)
    assert m.cadence_spm == pytest.approx(166, abs=4)       # "Corsa facile"
    assert m.battery == 87 and m.contact is True


def test_packet_loss_is_detected_and_flagged():
    _, dec, eng, _ = run_sim(180, drop_rate=0.02)
    assert dec.stats.gaps > 0 and dec.stats.missing_samples >= 73 * dec.stats.gaps
    kinds = {r["kind"] for r in eng.beat_log}
    assert "invalid" in kinds
    assert eng.metrics.beats > 150


def test_session_roundtrip_and_export(tmp_path):
    _, _, eng, packets = run_sim(90)
    w = SessionWriter(tmp_path / "s", {"source": "test"})
    for p in packets:
        w.write(p)
    d = w.close()
    back = list(read_packets(d))
    assert back == packets
    files = {p.name for p in export_session(d)}
    assert {"ecg.csv", "acc.csv", "hr.csv", "beats.csv", "metrics.csv"} <= files
    lines = (d / "beats.csv").read_text().splitlines()
    assert len(lines) - 1 == len(eng.beat_log)              # stessa elaborazione del tempo reale


def test_truncated_log_is_read_up_to_last_full_record(tmp_path):
    w = SessionWriter(tmp_path / "s")
    w.write(Packet(1.0, Channel.HR, bytes.fromhex("163c0004")))
    w.write(Packet(2.0, Channel.BATTERY, bytes([90])))
    d = w.close()
    raw = d / "raw.plab"
    raw.write_bytes(raw.read_bytes()[:-1])
    assert len(list(read_packets(d))) == 1


def test_realtime_pipeline_with_recording_and_replay(tmp_path):
    p = Pipeline(SimulatedSource(speed=20.0), sessions_dir=tmp_path)
    p.start()
    time.sleep(1.0)
    p.start_recording()
    time.sleep(2.5)
    p.stop_recording()
    time.sleep(0.3)
    p.stop()
    assert p.engine.metrics.beats > 30
    sessions = list(tmp_path.iterdir())
    assert len(sessions) == 1
    for _ in range(50):
        if p.last_export:
            break
        time.sleep(0.1)
    assert (sessions[0] / "ecg.csv").exists()

    # Il replay della sessione produce gli stessi pacchetti, decodificabili da zero
    got = []
    stop = asyncio.Event()
    asyncio.run(ReplaySource(sessions[0], speed=1000.0).run(got.append, lambda s: None, stop))
    assert got == list(read_packets(sessions[0]))
    dec = StreamDecoder()
    events = [e for pk in got for e in dec.feed(pk)]
    assert dec.stats.errors == 0 and any(type(e).__name__ == "EcgChunk" for e in events)


def test_acc_metrics_follow_real_strap_clock():
    # La H10 reale a "100 Hz" di accelerometro ne produce ~101.9: cadenza e respiro non devono sbagliare del 2%
    _, dec, eng, _ = run_sim(240, acc_true_fs=101.9)
    assert dec.sample_rate(MeasurementType.ACC) == pytest.approx(101.9, rel=0.002)
    assert eng.metrics.cadence_spm == pytest.approx(166, abs=1.5)
