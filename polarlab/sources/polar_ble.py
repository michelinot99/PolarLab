"""Sorgente Polar H10 via Bluetooth LE (bleak → CoreBluetooth su macOS).

Sequenza (identica a quella da implementare in iOS con CBCentralManager/CBPeripheral):
    1. scansione (per indirizzo memorizzato o per nome "Polar H10")
    2. connessione → lettura Device Information e batteria
    3. notifiche: Heart Rate Measurement, batteria, PMD Control Point, PMD Data
    4. lettura del PMD Control Point → funzioni supportate
    5. per ECG e ACC: GET_SETTINGS → scelta dei valori → START (attende la risposta F0 02 …)
    6. streaming finché non arriva lo stop o la disconnessione
    7. disconnessione → riconnessione automatica con backoff esponenziale (1 → 10 s)

Nota macOS: l'"indirizzo" è un UUID assegnato da CoreBluetooth, non il MAC della fascia.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from polarlab.protocol import pmd, uuids
from polarlab.protocol.channels import Channel, Packet
from polarlab.protocol.decoder import describe_settings
from polarlab.protocol.pmd import MeasurementType, SettingType
from polarlab.sources.base import SensorSource, SourceState, Status, sleep_or_stop

CONTROL_TIMEOUT_S = 5.0


@dataclass
class FoundDevice:
    name: str
    address: str
    rssi: int


async def scan(timeout: float = 6.0, name_prefix: str = "Polar") -> list[FoundDevice]:
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    out = [FoundDevice(d.name or adv.local_name or "?", d.address, adv.rssi)
           for d, adv in found.values() if (d.name or adv.local_name or "").startswith(name_prefix)]
    return sorted(out, key=lambda d: -d.rssi)


class PmdController:
    """Dialogo richiesta/risposta sul PMD Control Point."""

    def __init__(self, client: BleakClient, on_packet):
        self.client = client
        self.on_packet = on_packet
        self._responses: asyncio.Queue[pmd.ControlResponse] = asyncio.Queue()

    def on_control(self, _char, data: bytearray) -> None:
        data = bytes(data)
        self.on_packet(Packet(time.time(), Channel.PMD_CP, data))
        if data and data[0] == pmd.CONTROL_RESPONSE_ID:
            try:
                self._responses.put_nowait(pmd.parse_control_response(data))
            except pmd.PmdProtocolError:
                pass

    async def request(self, cmd: bytes) -> pmd.ControlResponse:
        while not self._responses.empty():
            self._responses.get_nowait()
        self.on_packet(Packet(time.time(), Channel.PMD_CP_WRITE, cmd))
        await self.client.write_gatt_char(uuids.PMD_CONTROL, cmd, response=True)
        deadline = time.monotonic() + CONTROL_TIMEOUT_S
        while True:
            r = await asyncio.wait_for(self._responses.get(), timeout=max(0.1, deadline - time.monotonic()))
            if r.op == cmd[0] and r.meas_type == cmd[1]:
                return r

    async def read_features(self) -> set[MeasurementType]:
        data = bytes(await self.client.read_gatt_char(uuids.PMD_CONTROL))
        self.on_packet(Packet(time.time(), Channel.PMD_CP, data))
        return pmd.parse_features(data)

    async def get_settings(self, mt: MeasurementType) -> dict[SettingType, list[float]]:
        r = await self.request(pmd.build_get_settings(mt))
        if not r.ok:
            raise BleakError(f"GET_SETTINGS {mt.name} rifiutato: {r.error.name}")
        return r.settings()

    async def start(self, mt: MeasurementType, settings: dict[SettingType, int]) -> None:
        r = await self.request(pmd.build_start(mt, settings))
        if r.error == pmd.PmdError.ALREADY_IN_STATE:
            # Stream rimasto attivo da una sessione precedente: fermalo e riparti pulito
            await self.request(pmd.build_stop(mt))
            r = await self.request(pmd.build_start(mt, settings))
        if not r.ok:
            raise BleakError(f"START {mt.name} rifiutato: {r.error.name}")

    async def stop(self, mt: MeasurementType) -> None:
        try:
            await self.request(pmd.build_stop(mt))
        except (BleakError, asyncio.TimeoutError):
            pass


class PolarH10Source(SensorSource):
    kind = "Polar H10"

    def __init__(self, address: str | None = None, name_prefix: str = "Polar H10",
                 ecg: bool = True, acc: bool = True, acc_rate: int = 100, acc_range: int = 8):
        self.address = address
        self.name_prefix = name_prefix
        self.wanted: dict[MeasurementType, dict[SettingType, int]] = {}
        if ecg:
            self.wanted[MeasurementType.ECG] = {SettingType.SAMPLE_RATE: 130, SettingType.RESOLUTION: 14}
        if acc:
            self.wanted[MeasurementType.ACC] = {SettingType.SAMPLE_RATE: acc_rate, SettingType.RESOLUTION: 16,
                                                SettingType.RANGE: acc_range}

    async def run(self, on_packet, on_status, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                device = await self._find(on_status, stop)
                if device is not None:
                    await self._session(device, on_packet, on_status, stop)
                    backoff = 1.0
            except (BleakError, asyncio.TimeoutError, OSError) as exc:
                on_status(Status(SourceState.RECONNECTING, f"{type(exc).__name__}: {exc}", address=self.address or ""))
            if stop.is_set() or await sleep_or_stop(stop, backoff):
                break
            backoff = min(backoff * 2.0, 10.0)
        on_status(Status(SourceState.STOPPED, "disconnesso"))

    async def _find(self, on_status, stop):
        on_status(Status(SourceState.SCANNING, "indossa la fascia e inumidisci gli elettrodi",
                         address=self.address or ""))
        if self.address:
            return await BleakScanner.find_device_by_address(self.address, timeout=8.0)
        return await BleakScanner.find_device_by_filter(
            lambda d, adv: (d.name or adv.local_name or "").startswith(self.name_prefix), timeout=8.0)

    async def _session(self, device, on_packet, on_status, stop: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        disconnected = asyncio.Event()
        name = device.name or self.name_prefix
        on_status(Status(SourceState.CONNECTING, name, device_name=name, address=device.address))

        def notifier(channel: Channel):
            return lambda _c, data: on_packet(Packet(time.time(), channel, bytes(data)))

        async with BleakClient(device, disconnected_callback=lambda _c: loop.call_soon_threadsafe(disconnected.set),
                               timeout=20.0) as client:
            self.address = device.address
            info = await self._device_info(client)
            info["mtu"] = getattr(client, "mtu_size", None)
            on_status(Status(SourceState.CONNECTED, "lettura capacità…", device_name=name,
                             address=device.address, info=info))
            try:
                on_packet(Packet(time.time(), Channel.BATTERY, bytes(await client.read_gatt_char(uuids.BATTERY_LEVEL))))
                await client.start_notify(uuids.BATTERY_LEVEL, notifier(Channel.BATTERY))
            except BleakError:
                pass
            await client.start_notify(uuids.HR_MEASUREMENT, notifier(Channel.HR))

            streams: dict[str, str] = {}
            ctl: PmdController | None = None
            started: list[MeasurementType] = []
            if client.services.get_service(uuids.PMD_SERVICE) is not None and self.wanted:
                ctl = PmdController(client, on_packet)
                await client.start_notify(uuids.PMD_CONTROL, ctl.on_control)
                await client.start_notify(uuids.PMD_DATA, notifier(Channel.PMD_DATA))
                features = await ctl.read_features()
                for mt, wanted in self.wanted.items():
                    if mt not in features:
                        streams[mt.name] = "non supportato"
                        continue
                    available = await ctl.get_settings(mt)
                    chosen = pmd.choose_settings(available, wanted)
                    await ctl.start(mt, chosen)
                    started.append(mt)
                    streams[mt.name] = describe_settings({k: [v] for k, v in chosen.items()})
            else:
                streams["ECG"] = "servizio PMD assente: solo HR/RR"

            on_status(Status(SourceState.STREAMING, "", device_name=name, address=device.address,
                             info=info, streams=streams))
            stop_task = asyncio.create_task(stop.wait())
            disc_task = asyncio.create_task(disconnected.wait())
            await asyncio.wait({stop_task, disc_task}, return_when=asyncio.FIRST_COMPLETED)
            for t in (stop_task, disc_task):
                t.cancel()

            if stop.is_set() and client.is_connected:
                if ctl is not None:
                    for mt in started:
                        await ctl.stop(mt)
                for ch in (uuids.PMD_DATA, uuids.PMD_CONTROL, uuids.HR_MEASUREMENT):
                    try:
                        await client.stop_notify(ch)
                    except (BleakError, KeyError, ValueError):
                        pass
        if not stop.is_set():
            raise BleakError("connessione persa")

    @staticmethod
    async def _device_info(client: BleakClient) -> dict:
        info = {}
        for key, uuid in uuids.DEVICE_INFO_FIELDS.items():
            try:
                info[key] = bytes(await client.read_gatt_char(uuid)).decode("utf-8", "replace").strip("\x00 ")
            except (BleakError, KeyError, ValueError):
                pass
        return info
