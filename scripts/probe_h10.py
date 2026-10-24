"""Diagnostica della Polar H10 senza interfaccia grafica: il primo test con la fascia vera.

    venv.nosync/bin/python scripts/probe_h10.py              # cerca la prima H10, 20 s di stream
    venv.nosync/bin/python scripts/probe_h10.py --seconds 60 --save
    venv.nosync/bin/python scripts/probe_h10.py --address <UUID-macOS>

Stampa: dispositivi trovati, servizi GATT, info dispositivo, MTU, funzioni PMD, impostazioni
disponibili, poi statistiche dello stream (campioni per frame, frequenza effettiva, buchi,
jitter BLE, battiti rilevati dal nostro QRS vs RR della fascia).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
from bleak import BleakClient  # noqa: E402

from polarlab.core.engine import Engine  # noqa: E402
from polarlab.core.pipeline import SESSIONS_DIR  # noqa: E402
from polarlab.core.session import SessionWriter, export_session, new_session_dir  # noqa: E402
from polarlab.protocol import pmd, uuids  # noqa: E402
from polarlab.protocol.channels import Channel, Packet  # noqa: E402
from polarlab.protocol.decoder import StreamDecoder, describe_settings  # noqa: E402
from polarlab.protocol.events import AccChunk, EcgChunk, HrEvent  # noqa: E402
from polarlab.protocol.pmd import MeasurementType as MT, SettingType as ST  # noqa: E402
from polarlab.sources.polar_ble import PmdController, PolarH10Source, scan  # noqa: E402


def h(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 60 - len(title)))


async def main(args) -> int:
    h("Scansione")
    devices = await scan(timeout=6.0)
    for d in devices:
        print(f"  {d.name:24s} {d.address}  {d.rssi} dBm")
    if args.address:
        address = args.address
    else:
        h10 = [d for d in devices if d.name.startswith("Polar H10")]
        if not h10:
            print("  Nessuna Polar H10 trovata: indossala (elettrodi umidi) e chiudi le app Polar sul telefono.")
            return 1
        address = h10[0].address

    packets: list[Packet] = []
    sink = packets.append
    async with BleakClient(address, timeout=20.0) as client:
        h("Connessione")
        print(f"  indirizzo {address}  ·  MTU {getattr(client, 'mtu_size', '?')}")
        info = await PolarH10Source._device_info(client)
        for k, v in info.items():
            print(f"  {k:13s} {v}")
        battery = bytes(await client.read_gatt_char(uuids.BATTERY_LEVEL))
        sink(Packet(time.time(), Channel.BATTERY, battery))
        print(f"  batteria      {battery[0]} %")

        h("Servizi GATT")
        for s in client.services:
            print(f"  {s.uuid}  {s.description}")
            for c in s.characteristics:
                print(f"      {c.uuid}  [{', '.join(c.properties)}]")

        h("PMD")
        ctl = PmdController(client, sink)
        await client.start_notify(uuids.PMD_CONTROL, ctl.on_control)
        await client.start_notify(uuids.PMD_DATA, lambda _c, d: sink(Packet(time.time(), Channel.PMD_DATA, bytes(d))))
        await client.start_notify(uuids.HR_MEASUREMENT, lambda _c, d: sink(Packet(time.time(), Channel.HR, bytes(d))))
        features = await ctl.read_features()
        print(f"  funzioni: {', '.join(sorted(f.name for f in features))}")
        wanted = {MT.ECG: {ST.SAMPLE_RATE: 130, ST.RESOLUTION: 14},
                  MT.ACC: {ST.SAMPLE_RATE: args.acc_rate, ST.RESOLUTION: 16, ST.RANGE: 8}}
        started = []
        for mt, w in wanted.items():
            if mt not in features:
                continue
            avail = await ctl.get_settings(mt)
            chosen = pmd.choose_settings(avail, w)
            print(f"  {mt.name}: disponibili {describe_settings(avail)}  →  scelti {describe_settings({k: [v] for k, v in chosen.items()})}")
            await ctl.start(mt, chosen)
            started.append(mt)

        h(f"Streaming per {args.seconds:.0f} s")
        t0 = time.time()
        while time.time() - t0 < args.seconds:
            await asyncio.sleep(1.0)
            print(f"  {time.time() - t0:4.0f} s  pacchetti {len(packets)}", end="\r")
        print()
        for mt in started:
            await ctl.stop(mt)

    h("Analisi")
    dec, eng = StreamDecoder(), Engine()
    ecg_n, acc_n, rr_strap, frame_sizes = 0, 0, [], {"ECG": [], "ACC": []}
    for p in packets:
        for e in dec.feed(p):
            eng.handle(e)
            if isinstance(e, EcgChunk):
                ecg_n += len(e.uv)
                frame_sizes["ECG"].append(len(e.uv))
            elif isinstance(e, AccChunk):
                acc_n += len(e.mg)
                frame_sizes["ACC"].append(len(e.mg))
            elif isinstance(e, HrEvent):
                rr_strap += e.rr_ms
    eng.finish()
    st = dec.stats
    for name, mt in (("ECG", MT.ECG), ("ACC", MT.ACC)):
        if frame_sizes[name]:
            print(f"  {name}: {len(frame_sizes[name])} frame, {np.median(frame_sizes[name]):.0f} campioni/frame, "
                  f"frequenza effettiva {dec.sample_rate(mt):.3f} Hz")
    print(f"  buchi {st.gaps}  ·  campioni persi {st.missing_samples}  ·  errori {st.errors} {st.last_error}")
    print(f"  jitter BLE (mediana − minimo) {dec.sync.jitter_ms:.1f} ms")
    m = eng.metrics
    print(f"  battiti rilevati dall'ECG {m.beats}  ·  RR dalla fascia {len(rr_strap)}")
    print(f"  FC ECG {m.hr_ecg:.0f}  ·  FC fascia {m.hr_strap:.0f}  ·  accordo RR {m.strap_match_pct:.0f}% "
          f"(MAE {m.strap_mae_ms:.1f} ms)  ·  qualità {m.quality:.0f} ({m.quality_label})")
    if ecg_n:
        snap = eng.snapshot(ecg_seconds=10)
        print(f"  ampiezza R mediana {np.median(np.abs(snap.beat_amp)):.0f} µV" if len(snap.beat_amp) else "")

    if args.save:
        d = new_session_dir(SESSIONS_DIR)
        started = datetime.fromtimestamp(packets[0].host_t).isoformat(timespec="seconds") if packets else None
        w = SessionWriter(d, {"source": "probe_h10", "address": address, "device_info": info,
                              **({"started": started} if started else {})})
        for p in packets:
            w.write(p)
        w.close()
        export_session(d)
        print(f"\n  sessione salvata in {d}  →  replay: venv.nosync/bin/python -m polarlab --replay '{d}'")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--address", help="UUID CoreBluetooth della fascia (default: la prima H10 trovata)")
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--acc-rate", type=int, default=100, choices=[25, 50, 100, 200])
    ap.add_argument("--save", action="store_true", help="salva la sessione in sessions/ per il replay")
    raise SystemExit(asyncio.run(main(ap.parse_args())))
