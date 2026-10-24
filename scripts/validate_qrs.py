"""Benchmark del rilevatore QRS sul MIT-BIH Arrhythmia Database (PhysioNet).

I tracciati (360 Hz, derivazione MLII) vengono ricampionati a 130 Hz come la H10 e passati
al rilevatore in blocchi da 73 campioni, esattamente come in tempo reale. Si confrontano
i battiti con le annotazioni (tolleranza 150 ms, standard ANSI/AAMI EC57) e si riportano
sensibilità (Se), valore predittivo positivo (+P) ed errore di posizione.

    venv.nosync/bin/pip install wfdb
    venv.nosync/bin/python scripts/validate_qrs.py              # tutti i 48 record
    venv.nosync/bin/python scripts/validate_qrs.py 100 105 203
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
from scipy import signal  # noqa: E402

from polarlab.dsp.filters import EcgFilter  # noqa: E402
from polarlab.dsp.qrs import detect_offline  # noqa: E402

FS = 130.0
RECORDS = [100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 111, 112, 113, 114, 115, 116, 117, 118, 119, 121,
           122, 123, 124, 200, 201, 202, 203, 205, 207, 208, 209, 210, 212, 213, 214, 215, 217, 219, 220, 221,
           222, 223, 228, 230, 231, 232, 233, 234]
BEAT_SYMBOLS = set("NLRBAaJSVrFejnE/fQ")
TOL_S = 0.150
SKIP_S = 10.0   # apprendimento iniziale delle soglie


def evaluate(record: int, cache: Path) -> tuple[int, int, int, list[float]]:
    import wfdb
    name = str(record)
    if not (cache / f"{name}.dat").exists():
        wfdb.dl_database("mitdb", str(cache), records=[name])
    # Formato 212 a 2 canali: 3 byte per campione doppio. Un download interrotto si usa fin dove arriva.
    frames = (cache / f"{name}.dat").stat().st_size // 3
    header = wfdb.rdheader(str(cache / name))
    sampto = min(header.sig_len, frames)
    rec = wfdb.rdrecord(str(cache / name), channels=[0], sampto=sampto)
    ann = wfdb.rdann(str(cache / name), "atr", sampto=sampto)
    x = signal.resample_poly(rec.p_signal[:, 0] * 1000.0, 13, 36)    # mV → µV, 360 → 130 Hz
    truth = np.array([s / rec.fs for s, sym in zip(ann.sample, ann.symbol) if sym in BEAT_SYMBOLS])
    bp, _ = EcgFilter(FS).process(x)
    det = np.array([b.index / FS for b in detect_offline(bp, FS)])
    end = len(x) / FS
    truth = truth[(truth > SKIP_S) & (truth < end - 1)]
    det = det[(det > SKIP_S) & (det < end - 1)]
    used = np.zeros(len(det), bool)
    tp, errs = 0, []
    j = np.searchsorted(det, truth)
    for k, tr in enumerate(truth):
        best = None
        for i in (j[k] - 1, j[k]):
            if 0 <= i < len(det) and not used[i] and abs(det[i] - tr) <= TOL_S:
                if best is None or abs(det[i] - tr) < abs(det[best] - tr):
                    best = i
        if best is not None:
            used[best] = True
            tp += 1
            errs.append(det[best] - tr)
    return tp, len(truth) - tp, len(det) - tp, errs


def main(argv: list[str]) -> int:
    records = [int(a) for a in argv] or RECORDS
    cache = Path(__file__).resolve().parents[1] / ".cache" / "mitdb"
    cache.mkdir(parents=True, exist_ok=True)
    TP = FN = FP = 0
    all_err: list[float] = []
    print(f"{'record':>6} {'battiti':>8} {'Se %':>7} {'+P %':>7}")
    for r in records:
        tp, fn, fp, errs = evaluate(r, cache)
        TP, FN, FP = TP + tp, FN + fn, FP + fp
        all_err += errs
        print(f"{r:>6} {tp + fn:>8} {100 * tp / max(1, tp + fn):7.2f} {100 * tp / max(1, tp + fp):7.2f}")
    e = np.asarray(all_err) * 1000
    print("-" * 32)
    print(f"{'totale':>6} {TP + FN:>8} {100 * TP / (TP + FN):7.2f} {100 * TP / (TP + FP):7.2f}")
    print(f"errore di posizione: mediana {np.median(e):+.1f} ms, IQR {np.subtract(*np.percentile(e, [75, 25])):.1f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
