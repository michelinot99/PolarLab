"""Rapporto di una sessione: un solo passaggio sul log grezzo, poi metriche dell'intera corsa.

`analyze(dir)` rielabora raw.plab con il decoder e il motore attuali (gli stessi della
diretta), filtra l'ECG a fase zero, calcola battito medio, passi e tratti di corsa, e
mette tutto in cache (`analysis.npz` + `analysis.json`). La cache vale finché non cambia
raw.plab o `ANALYSIS_VERSION`.

Le grandezze che dipendono dal profilo dell'atleta (FC max, FC a riposo) si ricavano al
momento dagli array in cache: `hr_zones`, `events`, `summary`.

Gli "eventi da controllare" sono indicazioni per rivedere il tracciato, non diagnosi:
una fascia toracica dà una sola derivazione e PolarLab non è un dispositivo medico.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from scipy import signal

from polarlab.core.session import RAW_NAME, export_session, process_session
from polarlab.dsp import hrv
from polarlab.dsp.dfa import ANT_ALPHA1, AET_ALPHA1
from polarlab.dsp.filters import zero_phase_ecg
from polarlab.dsp.template import POST_S, PRE_S

ANALYSIS_VERSION = 2
CACHE_NPZ, CACHE_JSON = "analysis.npz", "analysis.json"

GOOD_QUALITY = 65.0          # stessa soglia dell'etichetta "buona"
DFA_MAX_ARTIFACTS = 5.0      # % oltre cui α1 non è affidabile
ACTIVE_RMS_MG = 120.0        # movimento (0.8–5 Hz) oltre cui si considera "in corsa/camminata"
PREMATURE_FRAC = 0.85        # RR < 85% della mediana locale = battito in anticipo
SIMILAR_SHAPE_R = 0.85       # correlazione del QRS col battito normale oltre cui la forma è "simile"
PAUSE_MS = 2000.0
JUMP_BPM = 30.0
IRREGULAR_DIFF = 0.08        # |ΔRR| / RR oltre cui due battiti consecutivi sono "diversi"
IRREGULAR_SHARE = 0.25
MIN_R2 = 0.3                 # sotto, la regressione α1 ↔ FC non dà soglie       # quota di coppie "diverse" che rende irregolare una finestra di 30 s

LEVELS = ("serious", "warning", "info")
FLAGGED = ("ectopic", "longshort")
_ARRAYS = ("m_t", "hr_ecg", "hr_strap", "dfa_a1", "quality", "artifact_pct", "cadence", "motion", "vosc",
           "rmssd", "resp", "battery", "strap_mae", "strap_match",
           "b_t", "b_rr", "b_nn", "b_kind", "b_med", "r_t", "r_amp", "ecg_t", "ecg",
           "step_t", "active", "tpl_t", "tpl_normal", "tpl_ectopic", "ect_t", "ect_corr", "gaps", "contact_lost")


@dataclass
class Profile:
    hr_max: float | None = None
    hr_rest: float | None = None


@dataclass
class Event:
    t: float
    end: float | None
    kind: str
    level: str          # "serious" | "warning" | "info"
    title: str
    detail: str


@dataclass
class SessionReport:
    dir: Path
    meta: dict
    duration_s: float
    ecg_fs: float
    stats: dict
    # serie a 1 Hz dal motore
    m_t: np.ndarray
    hr_ecg: np.ndarray
    hr_strap: np.ndarray
    dfa_a1: np.ndarray
    quality: np.ndarray
    artifact_pct: np.ndarray
    cadence: np.ndarray
    motion: np.ndarray
    vosc: np.ndarray
    rmssd: np.ndarray
    resp: np.ndarray
    battery: np.ndarray
    strap_mae: np.ndarray
    strap_match: np.ndarray
    # intervalli RR corretti (beats.csv) e mediana locale degli RR
    b_t: np.ndarray
    b_rr: np.ndarray
    b_nn: np.ndarray
    b_kind: np.ndarray
    b_med: np.ndarray
    # picchi R rilevati e ECG filtrato a fase zero
    r_t: np.ndarray
    r_amp: np.ndarray
    ecg_t: np.ndarray
    ecg: np.ndarray
    # movimento
    step_t: np.ndarray
    active: np.ndarray          # (n, 2) inizio/fine dei tratti in movimento
    # morfologia
    tpl_t: np.ndarray
    tpl_normal: np.ndarray
    tpl_ectopic: np.ndarray
    ect_t: np.ndarray
    ect_corr: np.ndarray
    # integrità dei dati
    gaps: np.ndarray            # (n, 2) istante, campioni mancanti
    contact_lost: np.ndarray    # (n, 2) inizio/fine
    extras: dict = field(default_factory=dict)

    @property
    def is_iphone(self) -> bool:
        return "iOS" in str(self.meta.get("app", ""))


# ---------------------------------------------------------------------- cache

def cache_key(directory: Path) -> str:
    st = (Path(directory) / RAW_NAME).stat()
    return f"{ANALYSIS_VERSION}-{st.st_size}-{st.st_mtime_ns}"


def read_meta(directory: Path) -> dict:
    p = Path(directory) / "meta.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def cached_summary(directory: Path) -> dict | None:
    """Riepilogo salvato da un'analisi precedente, se ancora valido (lettura leggera per l'elenco)."""
    try:
        d = json.loads((Path(directory) / CACHE_JSON).read_text())
        return d if d.get("key") == cache_key(directory) else None
    except (OSError, ValueError):
        return None


def clear_cache(directory: Path) -> None:
    for name in (CACHE_NPZ, CACHE_JSON):
        (Path(directory) / name).unlink(missing_ok=True)


def _load(directory: Path) -> SessionReport | None:
    directory = Path(directory)
    head = cached_summary(directory)
    if head is None:
        return None
    try:
        with np.load(directory / CACHE_NPZ, allow_pickle=False) as z:
            arrays = {k: z[k] for k in _ARRAYS}
    except (OSError, KeyError, ValueError):
        return None
    return SessionReport(dir=directory, meta=read_meta(directory), duration_s=head["duration_s"],
                         ecg_fs=head["ecg_fs"], stats=head["stats"], **arrays)


def _save(rep: SessionReport) -> None:
    try:
        with open(rep.dir / CACHE_NPZ, "wb") as f:
            np.savez(f, **{k: getattr(rep, k) for k in _ARRAYS})
        head = {"key": cache_key(rep.dir), "duration_s": rep.duration_s, "ecg_fs": rep.ecg_fs, "stats": rep.stats,
                "summary": summary(rep)}
        (rep.dir / CACHE_JSON).write_text(json.dumps(head, indent=1, ensure_ascii=False, default=_json_num))
    except OSError:
        pass   # cartella in sola lettura: si rianalizza la prossima volta


def _json_num(v):
    if isinstance(v, np.generic):
        return v.item()
    raise TypeError(type(v))


# ---------------------------------------------------------------------- analisi

def analyze(directory: Path, use_cache: bool = True) -> SessionReport:
    directory = Path(directory)
    if use_cache:
        rep = _load(directory)
        if rep is not None:
            return rep
    pr = process_session(directory)
    if not (directory / "metrics.csv").exists():
        try:
            export_session(directory, pr)
        except OSError:
            pass
    rep = _build(directory, pr)
    _save(rep)
    return rep


def reanalyze(directory: Path) -> SessionReport:
    """Rigenera le CSV con gli algoritmi attuali e ricalcola il rapporto."""
    directory = Path(directory)
    clear_cache(directory)
    pr = process_session(directory)
    export_session(directory, pr)
    rep = _build(directory, pr)
    _save(rep)
    return rep


def _col(rows: list[dict], key: str) -> np.ndarray:
    return np.array([np.nan if r.get(key) is None else r[key] for r in rows], dtype=float)


def _build(directory: Path, pr) -> SessionReport:
    ml, bl = pr.metrics_log, pr.beat_log
    fs = pr.ecg_fs
    ecg = zero_phase_ecg(pr.ecg_uv.astype(float), fs).astype(np.float32) if len(pr.ecg_uv) > 30 else \
        pr.ecg_uv.astype(np.float32)
    b_t = np.array([r["t"] for r in bl], dtype=float)
    b_rr = np.array([r["rr_ms"] for r in bl], dtype=float)
    b_kind = np.array([r["kind"] for r in bl], dtype="U9")
    step_t, active = _steps_and_activity(pr.acc_t, pr.acc_mg)
    rep = SessionReport(
        dir=directory, meta=read_meta(directory), ecg_fs=fs,
        duration_s=float(max(pr.ecg_t[-1] if len(pr.ecg_t) else 0.0, pr.acc_t[-1] if len(pr.acc_t) else 0.0)),
        stats={"packets": pr.stats.packets, "gaps": pr.stats.gaps, "missing_samples": pr.stats.missing_samples,
               "errors": pr.stats.errors},
        m_t=_col(ml, "t"), hr_ecg=_col(ml, "hr_ecg"), hr_strap=_col(ml, "hr_strap"), dfa_a1=_col(ml, "dfa_a1"),
        quality=_col(ml, "quality"), artifact_pct=_col(ml, "artifact_pct"), cadence=_col(ml, "cadence_spm"),
        motion=_col(ml, "motion_mg"), vosc=_col(ml, "vertical_osc_cm"), rmssd=_col(ml, "rmssd"),
        resp=_col(ml, "resp_brpm"), battery=_col(ml, "battery"), strap_mae=_col(ml, "strap_mae_ms"),
        strap_match=_col(ml, "strap_match_pct"),
        b_t=b_t, b_rr=b_rr, b_nn=np.array([r["nn_ms"] for r in bl], dtype=float), b_kind=b_kind,
        b_med=_local_median(b_rr, b_kind),
        r_t=pr.rpeaks[:, 0].copy(), r_amp=pr.rpeaks[:, 1].copy(), ecg_t=pr.ecg_t, ecg=ecg,
        step_t=step_t, active=active,
        tpl_t=np.empty(0), tpl_normal=np.empty(0), tpl_ectopic=np.empty(0), ect_t=np.empty(0), ect_corr=np.empty(0),
        gaps=np.asarray(pr.gaps, dtype=float).reshape(-1, 2),
        contact_lost=_contact_lost(pr.hr_rows))
    _morphology(rep)
    return rep


def _local_median(rr: np.ndarray, kind: np.ndarray, half: int = 5) -> np.ndarray:
    """Mediana degli RR normali vicini (±5 battiti), riferimento per dire "in anticipo"."""
    if len(rr) == 0:
        return np.empty(0)
    x = np.where(kind == "normal", rr, np.nan)
    pad = np.pad(x, half, mode="constant", constant_values=np.nan)
    win = sliding_window_view(pad, 2 * half + 1)
    with np.errstate(all="ignore"), _quiet():
        med = np.nanmedian(win, axis=1)
    return np.where(np.isfinite(med), med, np.nanmedian(rr) if np.isfinite(rr).any() else np.nan)


class _quiet:
    """Silenzia i RuntimeWarning di nanmedian/nanmean su finestre vuote (risultato nan voluto)."""

    def __enter__(self):
        import warnings
        self._w = warnings.catch_warnings()
        self._w.__enter__()
        warnings.simplefilter("ignore", RuntimeWarning)

    def __exit__(self, *a):
        self._w.__exit__(*a)


def _steps_and_activity(t: np.ndarray, mg: np.ndarray, fs: float = 100.0) -> tuple[np.ndarray, np.ndarray]:
    """Passi (picchi del modulo dell'accelerazione) e tratti in movimento, sull'intera sessione.

    Il modulo non dipende da come è indossata la fascia; ogni passo dà un picco tra 0.8 e 5 Hz.
    """
    empty = (np.empty(0), np.empty((0, 2)))
    if len(t) < 4 * fs:
        return empty
    tu = np.arange(t[0], t[-1], 1.0 / fs)
    mag = np.interp(tu, t, np.linalg.norm(mg, axis=1))
    x = signal.sosfiltfilt(signal.butter(2, [0.8, 5.0], btype="band", fs=fs, output="sos"), mag)
    # RMS per secondo, poi mediana su 5 s per non spezzare i tratti a ogni passo più leggero
    n_s = int(len(x) // fs)
    if n_s < 3:
        return empty
    rms = np.sqrt(np.mean(x[: int(n_s * fs)].reshape(n_s, int(fs)) ** 2, axis=1))
    rms = signal.medfilt(rms, 5) if n_s >= 5 else rms
    act = rms > ACTIVE_RMS_MG
    segs = _runs(act, tu[0], merge_gap=10.0, min_len=15.0)
    steps = []
    for a, b in segs:
        m = (tu >= a - 2) & (tu <= b + 2)
        seg = x[m]
        height = max(100.0, 0.35 * float(np.percentile(seg, 95)))
        pk, _ = signal.find_peaks(seg, height=height, distance=int(0.25 * fs))
        steps.append(tu[m][pk])
    return (np.concatenate(steps) if steps else np.empty(0)), np.asarray(segs, dtype=float).reshape(-1, 2)


def _runs(mask: np.ndarray, t0: float, merge_gap: float, min_len: float, step: float = 1.0) -> list[tuple]:
    """Tratti contigui di una maschera a passo `step` s, fondendo buchi brevi e scartando i tratti corti."""
    segs: list[list[float]] = []
    idx = np.flatnonzero(mask)
    if not len(idx):
        return []
    start = prev = idx[0]
    for i in idx[1:]:
        if i != prev + 1:
            segs.append([t0 + start * step, t0 + (prev + 1) * step])
            start = i
        prev = i
    segs.append([t0 + start * step, t0 + (prev + 1) * step])
    merged = [segs[0]]
    for a, b in segs[1:]:
        if a - merged[-1][1] <= merge_gap:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged if b - a >= min_len]


def _contact_lost(hr_rows: list[tuple]) -> np.ndarray:
    lost = [t for t, _, _, c in hr_rows if c is False and np.isfinite(t)]
    segs: list[list[float]] = []
    for t in lost:
        if segs and t - segs[-1][1] <= 3.0:
            segs[-1][1] = t
        else:
            segs.append([t, t])
    return np.asarray(segs, dtype=float).reshape(-1, 2)


def _segments(rep: SessionReport, times: np.ndarray) -> np.ndarray:
    off = np.arange(-int(round(PRE_S * rep.ecg_fs)), int(round(POST_S * rep.ecg_fs)) + 1) / rep.ecg_fs
    return np.interp(times[:, None] + off[None, :], rep.ecg_t, rep.ecg), off


def _morphology(rep: SessionReport) -> None:
    if len(rep.ecg_t) < rep.ecg_fs * 5 or not len(rep.b_t):
        return
    lo, hi = rep.ecg_t[0] + PRE_S, rep.ecg_t[-1] - POST_S
    inside = (rep.b_t > lo) & (rep.b_t < hi)
    normal = rep.b_t[(rep.b_kind == "normal") & inside]
    if len(normal) < 5:
        return
    if len(normal) > 1500:
        normal = normal[np.linspace(0, len(normal) - 1, 1500).astype(int)]
    segs, off = _segments(rep, normal)
    rep.tpl_t = off
    rep.tpl_normal = np.median(segs, axis=0)
    # Battiti in anticipo tra quelli corretti: si confronta la loro forma con il battito normale
    ect = np.isin(rep.b_kind, FLAGGED) & (rep.b_rr < PREMATURE_FRAC * rep.b_med) & inside
    if not ect.any():
        return
    rep.ect_t = rep.b_t[ect]
    esegs, _ = _segments(rep, rep.ect_t)
    rep.ect_corr = np.array([_qrs_corr(rep, s) for s in esegs])
    rep.tpl_ectopic = np.median(esegs, axis=0)


def _qrs_corr(rep: SessionReport, seg: np.ndarray) -> float:
    """Correlazione del complesso QRS (−100…+200 ms dal picco R) con quello del battito normale."""
    if not len(rep.tpl_normal):
        return np.nan
    qrs = (rep.tpl_t >= -0.10) & (rep.tpl_t <= 0.20)
    x = seg[qrs]
    return float(np.corrcoef(x, rep.tpl_normal[qrs])[0, 1]) if np.std(x) > 0 else np.nan


def beat_shape_corr(rep: SessionReport, t: float) -> float:
    if not len(rep.ecg_t) or not (rep.ecg_t[0] + PRE_S < t < rep.ecg_t[-1] - POST_S):
        return np.nan
    seg, _ = _segments(rep, np.array([t]))
    return _qrs_corr(rep, seg[0])


# ---------------------------------------------------------------------- metriche derivate

def _rolling_median(x: np.ndarray, n: int) -> np.ndarray:
    if len(x) < n:
        return x.copy()
    half = n // 2
    pad = np.pad(x, (half, n - 1 - half), mode="edge")
    with _quiet():
        return np.nanmedian(sliding_window_view(pad, n), axis=1)


def hr_series(rep: SessionReport) -> np.ndarray:
    """FC a 1 Hz dal nostro ECG, con la fascia dove il nostro rilevatore non ha battiti."""
    hr = rep.hr_ecg.copy()
    miss = ~np.isfinite(hr)
    hr[miss] = rep.hr_strap[miss]
    return hr


def hr_stats(rep: SessionReport) -> dict:
    hr = hr_series(rep)
    ok = np.isfinite(hr)
    if not ok.any():
        return {"avg": np.nan, "max": np.nan, "min": np.nan}
    smooth = _rolling_median(hr, 5)
    return {"avg": float(np.mean(hr[ok])), "max": float(np.nanmax(smooth)), "min": float(np.nanmin(smooth))}


def _at(rep: SessionReport, values: np.ndarray, t: float, half: float = 2.0) -> float:
    m = (rep.m_t >= t - half) & (rep.m_t <= t + half) & np.isfinite(values)
    return float(np.median(values[m])) if m.any() else np.nan


def recovery(rep: SessionReport) -> dict | None:
    """Recupero dopo l'ultimo tratto di movimento: calo della FC dopo 30, 60, 120 s dal picco."""
    long = [s for s in rep.active if s[1] - s[0] >= 20.0]
    if not long or not len(rep.m_t):
        return None
    t_end = float(long[-1][1])
    hr = _rolling_median(hr_series(rep), 3)
    w = (rep.m_t >= t_end - 5) & (rep.m_t <= t_end + 15) & np.isfinite(hr)
    if not w.any():
        return None
    i = np.flatnonzero(w)[np.argmax(hr[w])]
    t_peak, peak = float(rep.m_t[i]), float(hr[i])
    last = float(rep.m_t[np.isfinite(hr)][-1])
    drops = {}
    for dt in (30, 60, 120):
        if t_peak + dt <= last:
            v = _at(rep, hr, t_peak + dt)
            if np.isfinite(v):
                drops[dt] = peak - v
    return {"t_end": t_end, "t_peak": t_peak, "hr_peak": peak, "drops": drops}


def _dfa_mask(rep: SessionReport) -> np.ndarray:
    a = rep.artifact_pct
    return (np.isfinite(rep.dfa_a1) & (np.nan_to_num(a, nan=100.0) < DFA_MAX_ARTIFACTS)
            & (np.nan_to_num(rep.quality) >= GOOD_QUALITY))


def alpha1_zones(rep: SessionReport) -> dict[str, float]:
    m = _dfa_mask(rep)
    a = rep.dfa_a1[m]
    return {"aerobico": float(np.sum(a >= AET_ALPHA1)), "tra soglie": float(np.sum((a < AET_ALPHA1) & (a >= ANT_ALPHA1))),
            "sopra VT2": float(np.sum(a < ANT_ALPHA1))}


def thresholds(rep: SessionReport) -> dict | None:
    """FC a VT1 (α1 = 0.75) e VT2 (α1 = 0.5) per regressione lineare α1 ↔ FC (Rogers et al. 2021).

    α1 si aggiorna ogni 5 s: si prende un punto ogni 5 s. La stima è "affidabile" se il
    legame è netto (R² ≥ 0.5) e la soglia cade dentro i valori di α1 osservati.
    """
    m = _dfa_mask(rep)
    idx = np.flatnonzero(m)[::5]
    hr = hr_series(rep)[idx]
    a1 = rep.dfa_a1[idx]
    ok = np.isfinite(hr)
    hr, a1 = hr[ok], a1[ok]
    if len(hr) < 12 or np.ptp(hr) < 15.0:
        return None
    b, a = np.polyfit(hr, a1, 1)
    if b >= 0:
        return None
    pred = a + b * hr
    r2 = 1.0 - np.sum((a1 - pred) ** 2) / max(np.sum((a1 - a1.mean()) ** 2), 1e-12)
    out = {"hr": hr, "a1": a1, "slope": float(b), "intercept": float(a), "r2": float(r2)}
    for name, thr in (("vt1", AET_ALPHA1), ("vt2", ANT_ALPHA1)):
        h = (thr - a) / b
        inside = a1.min() - 0.1 <= thr <= a1.max() + 0.1
        # Con un legame debole la retta non dice nulla: niente stima
        plausible = r2 >= MIN_R2 and hr.min() - 20 <= h <= hr.max() + 20
        out[name] = float(h) if plausible else np.nan
        out[name + "_reliable"] = bool(plausible and inside and r2 >= 0.5)
    return out


def hr_zones(rep: SessionReport, profile: Profile) -> list[tuple[str, float, float, float]] | None:
    """Secondi in 5 zone (% FC max, o % riserva cardiaca se c'è la FC a riposo)."""
    if not profile.hr_max:
        return None
    hr = hr_series(rep)
    hr = hr[np.isfinite(hr)]
    rest = profile.hr_rest or 0.0
    def bpm(f):
        return rest + f * (profile.hr_max - rest)
    edges = [0.0, 0.6, 0.7, 0.8, 0.9, 10.0]
    out = []
    for i in range(5):
        lo, hi = bpm(edges[i]) if i else 0.0, bpm(edges[i + 1])
        out.append((f"Z{i + 1}", lo, hi if i < 4 else np.inf, float(np.sum((hr >= lo) & (hr < hi)))))
    return out


def drift(rep: SessionReport) -> dict | None:
    """Deriva cardiaca nel tratto di movimento più lungo (≥ 20 min): FC 2ª metà vs 1ª metà."""
    if not len(rep.active):
        return None
    a, b = max(rep.active, key=lambda s: s[1] - s[0])
    if b - a < 20 * 60:
        return None
    a += 0.1 * (b - a)        # si salta il riscaldamento iniziale
    mid = 0.5 * (a + b)
    hr = hr_series(rep)

    def mean(v, lo, hi):
        m = (rep.m_t >= lo) & (rep.m_t < hi) & np.isfinite(v)
        return float(np.mean(v[m])) if m.any() else np.nan
    h1, h2 = mean(hr, a, mid), mean(hr, mid, b)
    c1, c2 = mean(rep.cadence, a, mid), mean(rep.cadence, mid, b)
    return {"start": a, "end": b, "hr1": h1, "hr2": h2, "drift_pct": (h2 / h1 - 1) * 100.0,
            "cad1": c1, "cad2": c2}


def rest_hrv(rep: SessionReport):
    """HRV nei minuti da fermo prima del primo movimento (o dell'intera sessione se non ci si muove)."""
    end = float(rep.active[0][0]) - 10.0 if len(rep.active) else rep.duration_s
    start = max(0.0, end - 300.0)
    if end - start < 60.0:
        return None
    m = (rep.b_t >= start) & (rep.b_t <= end) & (rep.b_kind == "normal")
    td = hrv.time_domain(rep.b_nn[m])
    return (td, start, end) if td is not None else None


def activity(rep: SessionReport) -> dict:
    active_s = float(np.sum(rep.active[:, 1] - rep.active[:, 0])) if len(rep.active) else 0.0
    inside = np.zeros(len(rep.m_t), bool)
    for a, b in rep.active:
        inside |= (rep.m_t >= a) & (rep.m_t <= b)
    def mean(v):
        m = inside & np.isfinite(v)
        return float(np.mean(v[m])) if m.any() else np.nan
    return {"active_s": active_s, "steps": int(len(rep.step_t)),
            "cadence": 60.0 * len(rep.step_t) / active_s if active_s > 0 else np.nan,
            "vosc": mean(rep.vosc), "hr_active": mean(hr_series(rep)), "rmssd_active": mean(rep.rmssd)}


def data_quality(rep: SessionReport) -> dict:
    q = rep.quality[np.isfinite(rep.quality)]
    kinds = {k: int(np.sum(rep.b_kind == k)) for k in ("ectopic", "missed", "extra", "longshort", "invalid")}
    corrected = sum(kinds.values())
    batt = rep.battery[np.isfinite(rep.battery)]
    with _quiet():
        return {"good_pct": float(np.mean(q >= GOOD_QUALITY) * 100.0) if len(q) else np.nan,
                "quality_median": float(np.median(q)) if len(q) else np.nan,
                "artifact_pct": 100.0 * corrected / len(rep.b_kind) if len(rep.b_kind) else np.nan,
                "kinds": kinds, "beats": int(len(rep.r_t)),
                "strap_match": float(np.nanmedian(rep.strap_match)) if np.isfinite(rep.strap_match).any() else np.nan,
                "strap_mae": float(np.nanmedian(rep.strap_mae)) if np.isfinite(rep.strap_mae).any() else np.nan,
                "battery": (int(batt[0]), int(batt[-1])) if len(batt) else None,
                "contact_lost_s": float(np.sum(rep.contact_lost[:, 1] - rep.contact_lost[:, 0] + 1.0))
                if len(rep.contact_lost) else 0.0}


# ---------------------------------------------------------------------- eventi da controllare

def events(rep: SessionReport, profile: Profile | None = None) -> list[Event]:
    profile = profile or Profile()
    out: list[Event] = []
    out += _premature_events(rep)
    out += _pause_events(rep)
    out += _high_hr_events(rep, profile)
    out += _jump_events(rep)
    out += _irregular_events(rep)
    out += _signal_events(rep)
    out.sort(key=lambda e: e.t)
    return out


def _quality_at(rep: SessionReport, t: float) -> float:
    return _at(rep, rep.quality, t, half=1.0)


def _premature_events(rep: SessionReport) -> list[Event]:
    """Battiti prematuri dagli intervalli corretti, raggruppati in episodi di battiti adiacenti.

    Lipponen-Tarvainen marca l'intervallo corto (battito in anticipo) e la pausa
    compensatoria che segue, come "ectopic" o "longshort" a seconda del caso: un episodio è
    una sequenza di intervalli marcati consecutivi. È un battito prematuro se contiene un
    intervallo corto seguito da uno lungo, altrimenti è più probabilmente un errore di
    rilevamento (artefatto).
    """
    idx = np.flatnonzero(np.isin(rep.b_kind, FLAGGED))
    if not len(idx):
        return []
    groups, cur = [], [idx[0]]
    for i in idx[1:]:
        if i - cur[-1] <= 1:
            cur.append(i)
        else:
            groups.append(cur)
            cur = [i]
    groups.append(cur)
    n = len(rep.b_rr)
    out, n_premature, first = [], 0, None
    for g in groups:
        span = g + [g[-1] + 1] if g[-1] + 1 < n else g          # la pausa può cadere sul battito dopo
        ratio = rep.b_rr[span] / rep.b_med[span]
        short = [i for i, r in zip(span, ratio) if r < PREMATURE_FRAC]
        long_after = short and any(r > 1.05 for i, r in zip(span, ratio) if i > short[0])
        i0 = short[0] if short else g[0]
        t = float(rep.b_t[i0])
        q = _quality_at(rep, t)
        if not long_after or not np.isfinite(q) or q < GOOD_QUALITY:
            why = "segnale scarso" if np.isfinite(q) and q < GOOD_QUALITY else "senza la pausa tipica di un battito prematuro"
            out.append(Event(t, None, "artifact", "info", "Probabile artefatto", f"intervallo RR irregolare, {why}"))
            continue
        n_premature += len(short)
        first = t if first is None else first
        r = beat_shape_corr(rep, t)
        prem = 1.0 - rep.b_rr[i0] / rep.b_med[i0]
        shape = (f"forma simile ai battiti normali (r = {r:.2f})" if np.isfinite(r) and r >= SIMILAR_SHAPE_R
                 else f"forma diversa dai battiti normali (r = {r:.2f})" if np.isfinite(r) else "forma non valutabile")
        if len(short) >= 2:
            out.append(Event(t, float(rep.b_t[short[-1]]), "premature_run", "serious",
                             f"{len(short)} battiti prematuri ravvicinati", f"il primo in anticipo del {prem:.0%}, {shape}"))
        else:
            out.append(Event(t, None, "premature", "warning", "Battito prematuro",
                             f"in anticipo del {prem:.0%} sul ritmo, {shape}"))
    if n_premature >= 10 and n:
        burden = 100.0 * n_premature / n
        if burden >= 1.0:
            out.append(Event(first, None, "premature_burden", "serious" if burden >= 10 else "warning",
                             "Battiti prematuri frequenti",
                             f"{n_premature} su {n} battiti ({burden:.1f}%) nella sessione"))
    return out


def _pause_events(rep: SessionReport) -> list[Event]:
    out = []
    m = (rep.b_rr > PAUSE_MS) & (rep.b_kind != "invalid")
    for i in np.flatnonzero(m):
        t = float(rep.b_t[i])
        out.append(Event(t - rep.b_rr[i] / 1000.0, t, "pause", "serious", "Pausa tra due battiti",
                         f"{rep.b_rr[i] / 1000.0:.1f} s senza battiti (ritmo intorno: {60000.0 / rep.b_med[i]:.0f} bpm)"))
    return out


def _high_hr_events(rep: SessionReport, profile: Profile) -> list[Event]:
    thr = profile.hr_max + 5.0 if profile.hr_max else 200.0
    hr = _rolling_median(rep.hr_ecg, 5)
    ok = (hr > thr) & (np.nan_to_num(rep.quality) >= GOOD_QUALITY)
    out = []
    t0 = rep.m_t[0] if len(rep.m_t) else 0.0
    for a, b in _runs(ok, t0, merge_gap=3.0, min_len=10.0):
        m = (rep.m_t >= a) & (rep.m_t <= b)
        out.append(Event(a, b, "high_hr", "serious", "Frequenza molto alta",
                         f"fino a {np.nanmax(hr[m]):.0f} bpm per {b - a:.0f} s (soglia {thr:.0f})"))
    return out


def _jump_events(rep: SessionReport, lag: int = 5) -> list[Event]:
    """Salti di FC > 30 bpm in 5 s senza cambi di movimento (né subito dopo uno sforzo)."""
    if len(rep.m_t) <= lag:
        return []
    hr = _rolling_median(rep.hr_ecg, 3)
    mot = np.nan_to_num(rep.motion, nan=0.0) + 30.0
    q = np.nan_to_num(rep.quality)
    out, last_t = [], -1e9
    ends = rep.active[:, 1] if len(rep.active) else np.empty(0)
    for i in range(len(hr) - lag):
        d = hr[i + lag] - hr[i]
        if not np.isfinite(d) or abs(d) <= JUMP_BPM:
            continue
        if min(q[i], q[i + lag]) < GOOD_QUALITY or not 0.67 <= mot[i + lag] / mot[i] <= 1.5:
            continue
        t = float(rep.m_t[i])
        if d < 0 and np.any((t - ends >= -5) & (t - ends <= 120)):
            continue          # recupero dopo lo sforzo: calo fisiologico
        if t - last_t < 20:
            continue
        last_t = t
        out.append(Event(t, t + lag, "hr_jump", "warning", "Aumento brusco di FC" if d > 0 else "Calo brusco di FC",
                         f"da {hr[i]:.0f} a {hr[i + lag]:.0f} bpm in {lag} s, senza cambi di movimento"))
    return out


def _irregular_events(rep: SessionReport, win: float = 30.0, step: float = 15.0) -> list[Event]:
    """Finestre di 30 s sotto sforzo (FC > 100) con molti battiti consecutivi molto diversi tra loro.

    In corsa il ritmo sinusale è molto regolare (variazioni dell'1–2%); un'irregolarità
    diffusa, non dovuta a singoli battiti prematuri, merita di guardare il tracciato.
    """
    valid = rep.b_kind != "invalid"
    t, rr = rep.b_t[valid], rep.b_rr[valid]
    if len(t) < 30:
        return []
    hits: list[list[float]] = []
    shares: list[float] = []
    for a in np.arange(t[0], t[-1] - win + 1e-9, step):
        m = (t >= a) & (t < a + win)
        x = rr[m]
        if len(x) < 20 or 60000.0 / np.median(x) <= 100.0:
            continue
        qm = (rep.m_t >= a) & (rep.m_t < a + win) & np.isfinite(rep.quality)
        if not qm.any() or np.median(rep.quality[qm]) < GOOD_QUALITY:
            continue
        share = float(np.mean(np.abs(np.diff(x)) / x[:-1] > IRREGULAR_DIFF))
        if share >= IRREGULAR_SHARE:
            if hits and a <= hits[-1][1]:
                hits[-1][1] = a + win
                shares[-1] = max(shares[-1], share)
            else:
                hits.append([a, a + win])
                shares.append(share)
    return [Event(a, b, "irregular", "serious", "Ritmo irregolare sotto sforzo",
                  f"{s:.0%} dei battiti consecutivi differisce più dell'{IRREGULAR_DIFF:.0%}")
            for (a, b), s in zip(hits, shares)]


def _signal_events(rep: SessionReport) -> list[Event]:
    out = []
    for t, n in rep.gaps:
        out.append(Event(float(t), None, "gap", "info", "Dati mancanti",
                         f"{int(n)} campioni ECG persi ({n / rep.ecg_fs:.1f} s)"))
    for a, b in rep.contact_lost:
        out.append(Event(float(a), float(b), "contact", "info", "Contatto della fascia perso",
                         f"per circa {b - a + 1:.0f} s"))
    q = rep.quality
    t0 = rep.m_t[0] if len(rep.m_t) else 0.0
    for a, b in _runs(np.isfinite(q) & (q < 35.0), t0, merge_gap=5.0, min_len=10.0):
        out.append(Event(a, b, "poor_signal", "info", "Segnale scarso", f"per {b - a:.0f} s: tracciato poco leggibile"))
    return out


# ---------------------------------------------------------------------- riepilogo

def summary(rep: SessionReport, profile: Profile | None = None) -> dict:
    hs = hr_stats(rep)
    ev = events(rep, profile)
    act = activity(rep)
    dq = data_quality(rep)
    return {"duration_s": rep.duration_s, "avg_hr": hs["avg"], "max_hr": hs["max"], "steps": act["steps"],
            "active_s": act["active_s"], "good_pct": dq["good_pct"],
            "events": {lv: sum(e.level == lv for e in ev) for lv in LEVELS}}
