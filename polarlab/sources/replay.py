"""Replay di una sessione registrata, a velocità reale o accelerata, anche da un istante preciso."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from polarlab.core.session import read_packets
from polarlab.sources.base import SensorSource, SourceState, Status


class ReplaySource(SensorSource):
    kind = "Replay"

    def __init__(self, session_dir: Path, speed: float = 1.0, loop: bool = False, start_s: float = 0.0):
        self.dir = Path(session_dir)
        self.speed = speed
        self.loop = loop
        self.start_s = start_s

    async def run(self, on_packet, on_status, stop: asyncio.Event) -> None:
        meta_path = self.dir / "meta.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        name = f"Replay · {meta.get('device') or self.dir.name}"
        while True:
            on_status(Status(SourceState.STREAMING, f"{self.dir.name} ×{self.speed:g}", device_name=name,
                             address=str(self.dir), info=meta.get("device_info", {}), streams=meta.get("streams", {})))
            t_wall0 = time.monotonic()
            t_rec0 = t_play0 = None
            speed = self.speed
            skipped = 0
            for p in read_packets(self.dir):
                if stop.is_set():
                    break
                if t_rec0 is None:
                    t_rec0 = p.host_t
                rel = p.host_t - t_rec0
                if rel < self.start_s:
                    # Avanti veloce senza attese: il motore arriva al punto di partenza già "caldo"
                    on_packet(p)
                    skipped += 1
                    if skipped % 2000 == 0:
                        await asyncio.sleep(0)
                    continue
                if t_play0 is None or speed != self.speed:
                    # Partenza, o velocità cambiata dalla UI: si riparte a contare da qui
                    t_play0, t_wall0, speed = rel, time.monotonic(), self.speed
                due = (rel - t_play0) / speed - (time.monotonic() - t_wall0)
                if due > 0.005:
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=due)
                        break
                    except asyncio.TimeoutError:
                        pass
                on_packet(p)
            if stop.is_set() or not self.loop:
                break
        on_status(Status(SourceState.STOPPED, "fine del replay", device_name=name))
