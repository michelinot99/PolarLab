"""Registrazione delle sessioni: log dei pacchetti grezzi + esportazione CSV.

Il file `raw.plab` contiene ogni pacchetto esattamente come è arrivato dalla fascia
(più i comandi che abbiamo scritto). È la fonte di verità: le CSV si rigenerano
rielaborando il log con il decoder e il motore attuali, quindi un miglioramento degli
algoritmi si può riapplicare a tutte le corse già registrate.

Formato raw.plab:
    intestazione  b"PLAB\\x01\\n"
    record        <f8 host_t> <u1 canale> <u2 lunghezza> <byte...>   (little-endian)
"""

from __future__ import annotations

import csv
import json
import struct
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from polarlab import __version__
from polarlab.protocol.channels import Channel, Packet

MAGIC = b"PLAB\x01\n"
_REC = struct.Struct("<dBH")
RAW_NAME = "raw.plab"


def new_session_dir(root: Path) -> Path:
    d = Path(root) / datetime.now().strftime("%Y-%m-%d_%H%M%S")
    d.mkdir(parents=True, exist_ok=False)
    return d


class SessionWriter:
    def __init__(self, directory: Path, meta: dict | None = None):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._f = open(self.dir / RAW_NAME, "wb")
        self._f.write(MAGIC)
        self.started = time.time()
        self.packets = 0
        self._span: tuple[float, float] | None = None   # (primo, ultimo) host_t scritto
        self.meta = {"app": "PolarLab", "version": __version__,
                     "started": datetime.now().isoformat(timespec="seconds"), **(meta or {})}
        self._write_meta()

    def _write_meta(self) -> None:
        (self.dir / "meta.json").write_text(json.dumps(self.meta, indent=2, ensure_ascii=False))

    def write(self, p: Packet) -> None:
        self._f.write(_REC.pack(p.host_t, int(p.channel), len(p.data)))
        self._f.write(p.data)
        self.packets += 1
        self._span = (p.host_t, p.host_t) if self._span is None else (self._span[0], p.host_t)

    def close(self) -> Path:
        self._f.close()
        self.meta["ended"] = datetime.now().isoformat(timespec="seconds")
        # Dagli istanti dei pacchetti: vale anche quando il log si scrive tutto alla fine (probe_h10)
        span = self._span[1] - self._span[0] if self._span else time.time() - self.started
        self.meta["duration_s"] = round(span, 1)
        self.meta["packets"] = self.packets
        self._write_meta()
        return self.dir


def read_packets(path: Path) -> Iterator[Packet]:
    path = Path(path)
    if path.is_dir():
        path = path / RAW_NAME
    with open(path, "rb") as f:
        if f.read(len(MAGIC)) != MAGIC:
            raise ValueError(f"{path} non è un file PolarLab")
        while True:
            head = f.read(_REC.size)
            if len(head) < _REC.size:
                return
            host_t, ch, n = _REC.unpack(head)
            data = f.read(n)
            if len(data) < n:
                return   # registrazione interrotta a metà record
            yield Packet(host_t, Channel(ch), data)


@dataclass
class Processed:
    """Risultato di un passaggio completo decoder + motore su raw.plab (tempi relativi a t0)."""
    ecg_t: np.ndarray
    ecg_uv: np.ndarray
    ecg_fs: float
    acc_t: np.ndarray
    acc_mg: np.ndarray
    acc_fs: float
    hr_rows: list[tuple]            # (t, bpm, "rr;rr", contatto)
    beat_log: list[dict]
    metrics_log: list[dict]
    rpeaks: np.ndarray              # (n, 2): istante del picco R, ampiezza
    gaps: list[tuple[float, int]]   # (istante, campioni ECG mancanti)
    stats: object
    t0: float


def process_session(directory: Path) -> Processed:
    """Rielabora raw.plab con il decoder e il motore attuali, tutto in memoria."""
    from polarlab.core.engine import Engine
    from polarlab.protocol.decoder import StreamDecoder
    from polarlab.protocol.events import AccChunk, EcgChunk, HrEvent

    dec, eng = StreamDecoder(), Engine(keep_log=True)
    ecg_t, ecg_v, acc_t, acc_v, hr_rows, gaps = [], [], [], [], [], []
    for p in read_packets(directory):
        for ev in dec.feed(p):
            eng.handle(ev)
            if isinstance(ev, EcgChunk):
                ecg_t.append(ev.t)
                ecg_v.append(ev.uv)
                if ev.missing_before:
                    gaps.append((float(ev.t[0]), int(ev.missing_before)))
            elif isinstance(ev, AccChunk):
                acc_t.append(ev.t)
                acc_v.append(ev.mg)
            elif isinstance(ev, HrEvent):
                hr_rows.append((ev.device_t, ev.bpm, ";".join(f"{r:.1f}" for r in ev.rr_ms), ev.contact))
    eng.finish()
    t0 = eng.t0 or 0.0

    def cat(chunks, shape):
        return np.concatenate(chunks) if chunks else np.empty(shape)

    return Processed(
        ecg_t=cat(ecg_t, 0) - t0, ecg_uv=cat(ecg_v, 0), ecg_fs=eng.ecg_fs,
        acc_t=cat(acc_t, 0) - t0, acc_mg=cat(acc_v, (0, 3)), acc_fs=eng.acc_fs,
        hr_rows=[(a - t0, b, c, d) for a, b, c, d in hr_rows], beat_log=eng.beat_log, metrics_log=eng.metrics_log,
        rpeaks=np.asarray(eng.rpeak_log, dtype=float).reshape(-1, 2), gaps=[(t - t0, n) for t, n in gaps],
        stats=dec.stats, t0=t0)


def export_session(directory: Path, processed: Processed | None = None) -> list[Path]:
    """Rielabora raw.plab e scrive ecg.csv, acc.csv, hr.csv, beats.csv, metrics.csv."""
    directory = Path(directory)
    pr = processed or process_session(directory)
    out: list[Path] = []

    def write(name: str, header: list[str], rows) -> None:
        path = directory / name
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        out.append(path)

    if len(pr.ecg_t):
        write("ecg.csv", ["t_s", "ecg_uv"], zip(np.round(pr.ecg_t, 4), pr.ecg_uv.astype(int)))
    if len(pr.acc_t):
        write("acc.csv", ["t_s", "x_mg", "y_mg", "z_mg"],
              ((round(a, 4), *b) for a, b in zip(pr.acc_t, pr.acc_mg.astype(int))))
    if pr.hr_rows:
        write("hr.csv", ["t_s", "hr_bpm", "rr_ms", "contact"], ((round(a, 3), b, c, d) for a, b, c, d in pr.hr_rows))
    if pr.beat_log:
        keys = list(pr.beat_log[0])
        write("beats.csv", keys, ([r[k] for k in keys] for r in pr.beat_log))
    if pr.metrics_log:
        keys = ["t"] + [k for k in pr.metrics_log[0] if k != "t"]
        write("metrics.csv", keys, ([_fmt(r[k]) for k in keys] for r in pr.metrics_log))
    stats = pr.stats
    meta_path = directory / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    meta["decoder"] = {"packets": stats.packets, "gaps": stats.gaps, "missing_samples": stats.missing_samples,
                       "errors": stats.errors}
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    return out


def _fmt(v):
    if isinstance(v, float):
        return "" if not np.isfinite(v) else round(v, 3)
    return v
