# Porting to iPhone (Xcode, Swift)

> **Status (2026-10-24):** the port is done, in `ios/` (see [ios/README.md](../ios/README.md)).
> `PolarKit` reproduces `polarlab/` 1:1: on real and simulated sessions, beats, RR intervals
> and metrics match Python (`scripts/compare_swift.py`). The scipy primitives (Butterworth,
> filtfilt, Welch, CubicSpline, PCHIP, find_peaks) are rewritten in `PolarKit/DSP/Signal.swift`.
> The whole-session analysis (`polarlab/analysis/`) exists only on the Mac.

PolarLab was structured for this step: every Python module has a direct Swift equivalent,
and the byte-vector tests and recorded sessions serve as the reference for checking the iOS
version.

## Module map

| Python | Swift | Notes |
|---|---|---|
| `protocol/uuids.py` | `enum GATT { static let pmdService = CBUUID(string: "FB005C80-…") }` | |
| `protocol/pmd.py` | `struct PmdFrame`, `func parseDataFrame(_ data: Data) throws -> PmdFrame` | signed int24: `Int32(bitPattern: (UInt32(b0) \| UInt32(b1) << 8 \| UInt32(b2) << 16) << 8) >> 8` |
| `protocol/hr_service.py` | `struct HrMeasurement`, `init(data: Data)` | |
| `protocol/timebase.py` | `final class StreamClock`, `final class ClockSync` | UInt64 ns timestamp → `Double` seconds |
| `protocol/decoder.py` | `final class StreamDecoder` | same state, same events |
| `sources/polar_ble.py` | `final class PolarH10: NSObject, CBCentralManagerDelegate, CBPeripheralDelegate` | see below |
| `sources/simulator.py` | `H10Simulator` (DEBUG builds only) | to develop the UI in the Xcode simulator, which has no Bluetooth |
| `sources/replay.py` | `ReplaySource` | reads the same `raw.plab` |
| `dsp/filters.py` | `vDSP.Biquad` (Accelerate) | scipy's SOS coefficients copy over 1:1 |
| `dsp/qrs.py` | `final class QRSDetector` | per-sample loop in plain Swift, already O(1) |
| `dsp/rr_clean.py`, `hrv.py`, `dfa.py` | pure functions on `[Double]` | |
| `dsp/respiration.py`, `motion.py` | Accelerate (`vDSP.FFT`, autocorrelation with `vDSP.convolve`) | |
| `core/engine.py` | `actor Engine` or a class on a serial queue | `snapshot()` → `struct Snapshot: Sendable` |
| `core/session.py` | `SessionWriter` with `FileHandle` | **same binary format** |
| `ui/` | SwiftUI | see below |

## Bluetooth on iOS

```swift
central = CBCentralManager(delegate: self, queue: bleQueue,
    options: [CBCentralManagerOptionRestoreIdentifierKey: "polarlab.central"])
```

* **Info.plist**: `NSBluetoothAlwaysUsageDescription`; `UIBackgroundModes` = `bluetooth-central`
  (streaming continues with the screen off, while running).
* **Remembered strap**: save `peripheral.identifier` (UUID), then
  `central.retrievePeripherals(withIdentifiers:)` + `central.connect(p)`. A pending `connect`
  on iOS never times out: it is the right way to handle automatic reconnection when the strap
  moves away or loses contact. No back-off with repeated scans is needed, unlike on macOS.
* In the background, scanning only works when filtering by service:
  `scanForPeripherals(withServices: [CBUUID(string: "180D")])`.
* State restoration: `centralManager(_:willRestoreState:)` re-subscribes to notifications if
  iOS terminated the app during the run.
* GATT sequence identical to `docs/PROTOCOL.md`: `setNotifyValue(true)` on 2A37, 2A19,
  PMD CP and PMD Data, `readValue` on the CP, then `writeValue(_, type: .withResponse)` for the
  commands and wait for the F0 response in `didUpdateValueFor`.
* `peripheral.maximumWriteValueLength(for: .withoutResponse) + 3` gives the negotiated MTU.

### Polar SDK or our own implementation?
Polar distributes [PolarBleSdk](https://github.com/polarofficial/polar-ble-sdk) for iOS
(Swift Package, based on RxSwift). It works, but it brings RxSwift and an abstraction designed
for every Polar device. The H10 protocol is small and already validated byte by byte here, so
**our own CoreBluetooth implementation was chosen** (~500 lines): same decoder as the Mac, no
dependencies, full control over reconnection and background behaviour. The SDK would only be
worth it for features such as offline recording into the strap's memory.

## Concurrency

```
bleQueue (CoreBluetooth delegate) ──Packet──► processingQueue (serial): decoder → engine → writer
                                                          │
                                         @MainActor  ◄────┘ snapshot at 30 Hz (screen on only)
```

## Interface
* ECG: `TimelineView(.animation)` + `Canvas`, with the same sweep algorithm as
  `ui/widgets/ecg_sweep.py`. If profiling requires it, move to Metal (`MTKView`).
* Tachogram, Poincaré: Swift Charts.
* While running: a minimal screen (HR, DFA α1 zone, cadence, quality) with large type;
  possible future additions: Live Activity / Dynamic Island for HR and zone, spoken cues
  (`AVSpeechSynthesizer`) on α1 zone changes.
* Nothing is drawn with the screen off: only the metrics are updated.

## Data and HealthKit
* Sessions are saved in `raw.plab` format in the Documents folder (visible in Files): with
  AirDrop they move to the Mac, where PolarLab imports and re-analyses them. Every real run
  thus becomes a test case.
* HealthKit (not implemented): HR (`HKQuantityType(.heartRate)`), HRV SDNN and the beat series
  (`HKHeartbeatSeriesSample`) could be written, attached to a running `HKWorkout`. Raw ECG
  **cannot** be written: `HKElectrocardiogram` is read-only and reserved to Apple Watch.

## Verifying the iOS version
1. Port the tests in `tests/test_pmd.py`, `test_hr_service.py`, `test_timebase.py` with the
   same hexadecimal vectors (`plab selftest`).
2. Non-regression: decode and analyse a `raw.plab` session recorded on the Mac and compare
   `beats.csv` and `metrics.csv` with those produced by PolarLab (tolerance: identical RR
   within 0.5 ms) — `scripts/compare_swift.py`.
3. Only then, field tests.
