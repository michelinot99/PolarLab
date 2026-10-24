"""Pipeline in tempo reale: thread BLE (asyncio) → coda → thread DSP → Engine.

    [sorgente asyncio] --Packet--> queue.Queue --> [DSP] decoder → engine
                                                       └→ SessionWriter (se registra)

La UI non tocca mai i thread: legge `engine.snapshot()` con un timer a ~30 Hz.
Su iOS la stessa struttura diventa: delegate CoreBluetooth su una DispatchQueue dedicata,
elaborazione su un'altra queue seriale, UI sul main thread.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from pathlib import Path

from polarlab.core.engine import Engine
from polarlab.core.session import SessionWriter, export_session, new_session_dir
from polarlab.protocol.channels import Packet
from polarlab.protocol.decoder import StreamDecoder
from polarlab.sources.base import SensorSource, SourceState, Status

SESSIONS_DIR = Path(__file__).resolve().parents[2] / "sessions"


class Pipeline:
    def __init__(self, source: SensorSource, engine: Engine | None = None, sessions_dir: Path = SESSIONS_DIR):
        self.source = source
        self.engine = engine or Engine()
        self.decoder = StreamDecoder()
        self.sessions_dir = Path(sessions_dir)
        self.status = Status(SourceState.IDLE)
        self._q: queue.Queue[Packet] = queue.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_evt: asyncio.Event | None = None
        self._threads: list[threading.Thread] = []
        self._running = False
        self._writer: SessionWriter | None = None
        self._rec_request: str | None = None   # "start" | "stop", eseguita dal thread DSP
        self._rec_lock = threading.Lock()
        self.last_export: Path | None = None
        self.export_error: str | None = None
        self.packets_per_s = 0.0
        self._pps_count, self._pps_t = 0, time.monotonic()

    # ------------------------------------------------------------------ ciclo di vita

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._threads = [threading.Thread(target=self._ble_main, name="ble", daemon=True),
                         threading.Thread(target=self._dsp_main, name="dsp", daemon=True)]
        for t in self._threads:
            t.start()

    def stop(self, timeout: float = 6.0) -> None:
        if not self._running:
            return
        if self._loop is not None and self._stop_evt is not None:
            self._loop.call_soon_threadsafe(self._stop_evt.set)
        self._threads[0].join(timeout)
        self._running = False
        self._threads[1].join(timeout)
        if self._writer is not None:
            self._finish_recording()

    @property
    def running(self) -> bool:
        return self._running

    def _set_status(self, s: Status) -> None:
        self.status = s

    def _ble_main(self) -> None:
        async def main():
            self._loop = asyncio.get_running_loop()
            self._stop_evt = asyncio.Event()
            try:
                await self.source.run(self._q.put, self._set_status, self._stop_evt)
            except Exception as exc:   # la UI deve vedere l'errore, non un thread morto in silenzio
                self._set_status(Status(SourceState.ERROR, f"{type(exc).__name__}: {exc}"))
        asyncio.run(main())

    def _dsp_main(self) -> None:
        while self._running or not self._q.empty():
            self._handle_rec_request()
            try:
                p = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            if self._writer is not None:
                self._writer.write(p)
            for ev in self.decoder.feed(p):
                self.engine.handle(ev)
            self._pps_count += 1
            now = time.monotonic()
            if now - self._pps_t >= 1.0:
                self.packets_per_s = self._pps_count / (now - self._pps_t)
                self._pps_count, self._pps_t = 0, now

    # ------------------------------------------------------------------ registrazione

    @property
    def recording(self) -> bool:
        return self._writer is not None or self._rec_request == "start"

    @property
    def record_elapsed(self) -> float:
        return time.time() - self._writer.started if self._writer else 0.0

    @property
    def record_dir(self) -> Path | None:
        return self._writer.dir if self._writer else None

    def start_recording(self) -> None:
        self._rec_request = "start"

    def stop_recording(self) -> None:
        self._rec_request = "stop"

    def _handle_rec_request(self) -> None:
        req, self._rec_request = self._rec_request, None
        if req == "start" and self._writer is None:
            s = self.status
            meta = {"source": self.source.kind, "device": s.device_name, "address": s.address,
                    "device_info": s.info, "streams": s.streams}
            self._writer = SessionWriter(new_session_dir(self.sessions_dir), meta)
            # Apre il log con la configurazione corrente: il replay sa così come decodificare
            for p in self.decoder.config_packets(time.time()):
                self._writer.write(p)
        elif req == "stop" and self._writer is not None:
            self._finish_recording()

    def _finish_recording(self) -> None:
        directory = self._writer.close()
        self._writer = None

        def export():
            try:
                export_session(directory)
                self.last_export = directory
            except Exception as exc:
                self.export_error = f"{type(exc).__name__}: {exc}"
        threading.Thread(target=export, name="export", daemon=True).start()
