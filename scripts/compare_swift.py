"""Confronta PolarKit (Swift, app iPhone) con PolarLab (Python) sugli stessi dati.

    venv.nosync/bin/python scripts/compare_swift.py dsp
    venv.nosync/bin/python scripts/compare_swift.py session sessions/2026-10-24_161029
    venv.nosync/bin/python scripts/compare_swift.py sim --seconds 600

`dsp` confronta le primitive numeriche con scipy; `session` e `sim` rielaborano lo stesso
raw.plab con entrambe le implementazioni e confrontano beats.csv e metrics.csv.
Richiede il binario `plab`:
    cd ios/PolarKit && swift build -c release --scratch-path build.nosync --product plab
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from scipy import interpolate, signal  # noqa: E402

PLAB = ROOT / "ios" / "PolarKit" / "build.nosync" / "release" / "plab"


def plab(*args: str) -> str:
    return subprocess.run([str(PLAB), *args], check=True, capture_output=True, text=True).stdout


def report(name: str, ours, ref, tol: float) -> bool:
    ours, ref = np.asarray(ours, float), np.asarray(ref, float)
    if ours.shape != ref.shape:
        print(f"  ✗ {name:22s} forma {ours.shape} ≠ {ref.shape}")
        return False
    err = float(np.max(np.abs(ours - ref) / (np.abs(ref) + 1.0))) if ours.size else 0.0
    ok = err <= tol
    print(f"  {'✓' if ok else '✗'} {name:22s} errore relativo max {err:.1e}")
    return ok


def check_dsp() -> bool:
    sw = json.loads(plab("dsp"))
    i = np.arange(500.0)
    x = np.sin(0.05 * i) + 0.5 * np.sin(0.31 * i) + 0.2 * np.cos(1.7 * i) + 0.001 * i * i / 100
    ok = True
    filters = {
        "bp_ecg": signal.butter(2, [0.5, 40], "bandpass", fs=130, output="sos"),
        "bp_qrs": signal.butter(2, [5, 15], "bandpass", fs=130, output="sos"),
        "lp_grav": signal.butter(2, 0.3, fs=100, output="sos"),
        "bp_cad": signal.butter(2, [1, 6], "bandpass", fs=100, output="sos"),
        "lp_resp": signal.butter(2, 1.0, fs=10, output="sos"),
        "hp_osc": signal.butter(2, 0.5 / 0.36 * 0.5, "high", fs=100, output="sos"),
        "notch": signal.tf2sos(*signal.iirnotch(50.0, 30.0, fs=130)),
    }
    for name, sos in filters.items():
        # Le sezioni possono differire nell'ordine o nella ripartizione del guadagno: si confronta la risposta
        w = np.linspace(0, np.pi, 64)
        ok &= report("H(f) " + name, np.abs(signal.sosfreqz(sw["sos_" + name], w)[1]), np.abs(signal.sosfreqz(sos, w)[1]), 1e-9)
    bp = filters["bp_ecg"]
    ok &= report("sosfilt + zi", sw["sosfilt_bp_ecg"], signal.sosfilt(bp, x, zi=signal.sosfilt_zi(bp) * x[0])[0], 1e-9)
    ok &= report("sosfiltfilt bp", sw["sosfiltfilt_bp_cad"], signal.sosfiltfilt(filters["bp_cad"], x), 1e-9)
    ok &= report("sosfiltfilt lp", sw["sosfiltfilt_lp_resp"], signal.sosfiltfilt(filters["lp_resp"], x), 1e-9)
    ok &= report("detrend", sw["detrend"], signal.detrend(x), 1e-9)
    ok &= report("welch costante", sw["welch_const"], signal.welch(x, fs=4, nperseg=128, noverlap=64)[1], 1e-9)
    ok &= report("welch senza detrend", sw["welch_none"],
                 signal.welch(x, fs=4, window="hann", nperseg=256, noverlap=128, detrend=False)[1], 1e-9)
    ok &= report("welch dispari", sw["welch_odd"], signal.welch(x[:301], fs=10, nperseg=301, noverlap=150)[1], 1e-9)
    t = np.asarray(sw["spline_t"])
    y = 800 + 40 * np.sin(1.3 * t) + 10 * np.cos(0.2 * t)
    tu = np.arange(t[0], t[-1], 0.25)
    ok &= report("CubicSpline", sw["spline"], interpolate.CubicSpline(t, y)(tu), 1e-9)
    ok &= report("Pchip", sw["pchip"], interpolate.PchipInterpolator(t, y)(tu), 1e-9)
    ok &= report("percentili", sw["percentiles"],
                 [np.percentile(x, 25), np.percentile(x, 75), np.percentile(x, 99.5), np.percentile(x, 0.5), np.median(x)], 1e-12)
    from polarlab.dsp import dfa, hrv
    ok &= report("DFA α1", sw["dfa"], [dfa.dfa_alpha(y), dfa.dfa_alpha(x[:200])], 1e-9)
    ok &= report("find_peaks", sw["peaks"], signal.find_peaks(np.array([0, 1, 1, 0, 2, 3, 3, 3, 1, 4, 4, 5, 2, 2, 6.0]))[0], 0)
    cov = np.array([[4, 1, 0.5], [1, 3, 0.2], [0.5, 0.2, 1]])
    v = np.linalg.eigh(cov)[1][:, -1]
    ax = np.asarray(sw["axis"])
    ok &= report("asse principale", ax * np.sign(ax @ v), v, 1e-9)
    fd = hrv.frequency_domain(t * 1.5, y)
    ok &= report("LF/HF", sw["freq"], [fd.lf, fd.hf, fd.lf_hf, fd.resp_peak_hz], 1e-7)
    return ok


def _read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    import csv
    with open(path) as f:
        rows = list(csv.reader(f))
    return rows[0], rows[1:]


def _num(v: str) -> float:
    try:
        return float(v)
    except ValueError:
        return float("nan")


def check_session(raw_dir: Path) -> bool:
    from polarlab.core.session import export_session
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        py, sw = Path(tmp) / "py", Path(tmp) / "sw"
        py.mkdir()
        shutil.copy(raw_dir / "raw.plab", py / "raw.plab")
        export_session(py)
        print("  " + plab("analyze", str(raw_dir), str(sw)).strip())

        hb, rb_py = _read_csv(py / "beats.csv")
        _, rb_sw = _read_csv(sw / "beats.csv")
        n = min(len(rb_py), len(rb_sw))
        print(f"  battiti: Python {len(rb_py)}, Swift {len(rb_sw)}")
        ok &= len(rb_py) == len(rb_sw)
        a = np.array([[_num(r[0]), _num(r[1]), _num(r[2])] for r in rb_py[:n]])
        b = np.array([[_num(r[0]), _num(r[1]), _num(r[2])] for r in rb_sw[:n]])
        d_rr = np.abs(a[:, 1] - b[:, 1])
        kinds = sum(r1[3] != r2[3] for r1, r2 in zip(rb_py, rb_sw))
        print(f"  RR: differenza max {d_rr.max():.3f} ms, media {d_rr.mean():.4f} ms · classificazioni diverse {kinds}")
        ok &= d_rr.max() <= 0.5 and kinds == 0

        hm, rm_py = _read_csv(py / "metrics.csv")
        hm_sw, rm_sw = _read_csv(sw / "metrics.csv")
        ok &= hm == hm_sw and len(rm_py) == len(rm_sw)
        print(f"  righe metriche: Python {len(rm_py)}, Swift {len(rm_sw)}")
        n = min(len(rm_py), len(rm_sw))
        tol = {"lf": 1e-3, "hf": 1e-3, "lf_hf": 1e-3}
        for j, col in enumerate(hm):
            pa = np.array([_num(r[j]) for r in rm_py[:n]])
            sa = np.array([_num(r[j]) for r in rm_sw[:n]])
            if np.all(np.isnan(pa)) and np.all(np.isnan(sa)):
                if not all(r1[j] == r2[j] for r1, r2 in zip(rm_py[:n], rm_sw[:n])):
                    diff = sum(r1[j] != r2[j] for r1, r2 in zip(rm_py[:n], rm_sw[:n]))
                    print(f"  ✗ {col:16s} {diff} righe diverse")
                    ok = False
                continue
            nan_mismatch = int(np.sum(np.isnan(pa) != np.isnan(sa)))
            m = ~np.isnan(pa) & ~np.isnan(sa)
            rel = float(np.max(np.abs(pa[m] - sa[m]) / (np.abs(pa[m]) + 1.0))) if m.any() else 0.0
            good = nan_mismatch == 0 and rel <= tol.get(col, 2e-3)
            ok &= good
            print(f"  {'✓' if good else '✗'} {col:16s} errore relativo max {rel:.1e}"
                  + (f" · {nan_mismatch} valori mancanti diversi" if nan_mismatch else ""))
    return ok


def make_sim(seconds: float, out: Path) -> Path:
    from polarlab.core.session import SessionWriter
    from polarlab.sources.simulator import H10Simulator
    sim = H10Simulator(seed=11, host_t0=1_760_000_000.0, acc_true_fs=101.9, ectopic_rate=0.01)
    w = SessionWriter(out, {"source": "simulatore"})
    for p in sim.startup_packets():
        w.write(p)
    for _ in range(int(seconds / 0.5)):
        for p in sim.advance(0.5):
            w.write(p)
    w.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["dsp", "session", "sim"])
    ap.add_argument("path", nargs="?")
    ap.add_argument("--seconds", type=float, default=600)
    a = ap.parse_args()
    if not PLAB.exists():
        print(f"manca {PLAB}: compila prima con swift build")
        return 1
    if a.what == "dsp":
        ok = check_dsp()
    elif a.what == "session":
        ok = check_session(Path(a.path))
    else:
        with tempfile.TemporaryDirectory() as tmp:
            ok = check_session(make_sim(a.seconds, Path(tmp) / "sim"))
    print("TUTTO OK" if ok else "DIFFERENZE TROVATE")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
