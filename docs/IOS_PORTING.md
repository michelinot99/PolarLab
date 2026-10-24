# Porting su iPhone (Xcode, Swift)

> **Stato (2026-10-24):** il porting è fatto, in `ios/` (vedi [ios/README.md](../ios/README.md)).
> `PolarKit` riproduce `polarlab/` 1:1: su sessioni reali e simulate battiti, RR e metriche
> coincidono con Python (`scripts/compare_swift.py`). Le primitive di scipy (Butterworth,
> filtfilt, Welch, CubicSpline, PCHIP, find_peaks) sono riscritte in `PolarKit/DSP/Signal.swift`.

PolarLab è stato strutturato per questo passaggio: ogni modulo Python ha un equivalente
Swift diretto, e i test con vettori di byte e le sessioni registrate servono da riferimento
per verificare la versione iOS.

## Mappa dei moduli

| Python | Swift | Note |
|---|---|---|
| `protocol/uuids.py` | `enum GATT { static let pmdService = CBUUID(string: "FB005C80-…") }` | |
| `protocol/pmd.py` | `struct PmdFrame`, `func parseDataFrame(_ data: Data) throws -> PmdFrame` | int24 con segno: `Int32(bitPattern: (UInt32(b0) \| UInt32(b1) << 8 \| UInt32(b2) << 16) << 8) >> 8` |
| `protocol/hr_service.py` | `struct HrMeasurement`, `init(data: Data)` | |
| `protocol/timebase.py` | `final class StreamClock`, `final class ClockSync` | timestamp UInt64 ns → `Double` secondi |
| `protocol/decoder.py` | `final class StreamDecoder` | stesso stato, stessi eventi |
| `sources/polar_ble.py` | `final class PolarH10: NSObject, CBCentralManagerDelegate, CBPeripheralDelegate` | vedi sotto |
| `sources/simulator.py` | `H10Simulator` (solo build DEBUG) | per sviluppare l'UI nel simulatore Xcode, che non ha Bluetooth |
| `sources/replay.py` | `ReplaySource` | legge lo stesso `raw.plab` |
| `dsp/filters.py` | `vDSP.Biquad` (Accelerate) | i coefficienti SOS di scipy si copiano 1:1 |
| `dsp/qrs.py` | `final class QRSDetector` | ciclo per campione in Swift puro, è già O(1) |
| `dsp/rr_clean.py`, `hrv.py`, `dfa.py` | funzioni pure su `[Double]` | |
| `dsp/respiration.py`, `motion.py` | Accelerate (`vDSP.FFT`, autocorrelazione con `vDSP.convolve`) | |
| `core/engine.py` | `actor Engine` o classe su una serial queue | `snapshot()` → `struct Snapshot: Sendable` |
| `core/session.py` | `SessionWriter` con `FileHandle` | **stesso formato binario** |
| `ui/` | SwiftUI | vedi sotto |

## Bluetooth su iOS

```swift
central = CBCentralManager(delegate: self, queue: bleQueue,
    options: [CBCentralManagerOptionRestoreIdentifierKey: "polarlab.central"])
```

* **Info.plist**: `NSBluetoothAlwaysUsageDescription`; `UIBackgroundModes` = `bluetooth-central`
  (lo streaming continua a schermo spento, mentre si corre).
* **Fascia memorizzata**: salva `peripheral.identifier` (UUID), poi
  `central.retrievePeripherals(withIdentifiers:)` + `central.connect(p)`. Una `connect`
  pendente su iOS non scade mai: è il modo corretto di gestire la riconnessione automatica
  quando la fascia si allontana o perde contatto. Non serve un backoff con scansioni ripetute
  come su macOS.
* In background la scansione funziona solo filtrando per servizio:
  `scanForPeripherals(withServices: [CBUUID(string: "180D")])`.
* State restoration: `centralManager(_:willRestoreState:)` ricollega le notifiche se iOS ha
  terminato l'app durante la corsa.
* Sequenza GATT identica a quella di `docs/PROTOCOL.md`: `setNotifyValue(true)` su 2A37, 2A19,
  PMD CP e PMD Data, `readValue` sul CP, poi `writeValue(_, type: .withResponse)` dei comandi
  e attesa della risposta F0 in `didUpdateValueFor`.
* `peripheral.maximumWriteValueLength(for: .withoutResponse) + 3` dà l'MTU negoziato.

### SDK Polar o implementazione propria?
Polar distribuisce [PolarBleSdk](https://github.com/polarofficial/polar-ble-sdk) per iOS
(Swift Package, basato su RxSwift). Funziona, ma porta con sé RxSwift e un'astrazione
pensata per tutti i dispositivi Polar. Il protocollo della H10 è piccolo e qui è già
validato byte per byte, quindi **consiglio l'implementazione propria con CoreBluetooth**
(~500 righe): stesso decoder del Mac, nessuna dipendenza, controllo completo su riconnessione
e background. L'SDK conviene solo se in futuro servono funzioni come la registrazione
offline nella memoria della fascia.

## Concorrenza

```
bleQueue (CoreBluetooth delegate) ──Packet──► processingQueue (serial): decoder → engine → writer
                                                          │
                                         @MainActor  ◄────┘ snapshot a 30 Hz (solo con schermo acceso)
```

## Interfaccia
* ECG: `TimelineView(.animation)` + `Canvas`, con lo stesso algoritmo sweep di
  `ui/widgets/ecg_sweep.py`. Se il profiling lo richiede, si passa a Metal (`MTKView`).
* Tacogramma, Poincaré: Swift Charts.
* In corsa: schermata essenziale (FC, zona DFA α1, cadenza, qualità) con caratteri grandi;
  Live Activity / Dynamic Island per FC e zona; avvisi vocali (`AVSpeechSynthesizer`) al
  cambio di zona α1.
* A schermo spento non si disegna nulla: si aggiornano solo le metriche.

## Dati e HealthKit
* Le sessioni si salvano in formato `raw.plab` nella cartella Documenti (visibile in File):
  con AirDrop si trasferiscono sul Mac e si rianalizzano con
  `python -m polarlab --replay <cartella>`. Così ogni corsa reale diventa un caso di test.
* HealthKit: si possono scrivere FC (`HKQuantityType(.heartRate)`), HRV SDNN e la serie dei
  battiti (`HKHeartbeatSeriesSample`) associati a un `HKWorkout` di corsa. L'ECG grezzo **non**
  si può scrivere: `HKElectrocardiogram` è in sola lettura e riservato ad Apple Watch.

## Verifica della versione iOS
1. Port dei test di `tests/test_pmd.py`, `test_hr_service.py`, `test_timebase.py` in XCTest
   con gli stessi vettori esadecimali.
2. Test di non regressione: si decodifica e si analizza una sessione `raw.plab` registrata
   col Mac e si confrontano `beats.csv` e `metrics.csv` con quelli prodotti da PolarLab
   (tolleranza: RR identici entro 0.5 ms).
3. Solo dopo, test sul campo.
