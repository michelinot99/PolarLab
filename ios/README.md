# PolarLab for iPhone

An iPhone app that records ECG, beats and accelerometer from the Polar H10 while you run.
Protocol, algorithms and file format are the same as PolarLab for Mac, so every run recorded
on the phone can be reviewed and re-analysed on the Mac.

```
ios/
  PolarKit/        Swift package: H10 protocol, DSP, engine, recording (1:1 port of polarlab/)
  App/             SwiftUI app: Bluetooth, run screen, session archive
  AppCheck/        type-checks the app without Xcode (./check.sh)
  project.yml      XcodeGen project description → PolarLab.xcodeproj
```

The user interface is in Italian.

## Installing on the iPhone

Apple only lets you install your own apps outside the App Store if they carry a developer
signature. A **free Apple ID** is enough, with two limits: the install **expires after 7 days**
and must be refreshed (recordings stay on the phone), and a free account can have at most 3
sideloaded apps at a time.

Requirements: iOS 17 or later.

### Option A: Xcode (build from source)
1. Install **Xcode** from the App Store and, on first launch, accept the iOS platform download.
2. Xcode → **Settings → Accounts** → **+** → add your Apple ID.
3. Open `ios/PolarLab.xcodeproj`.
4. Select the **PolarLab** target → **Signing & Capabilities** → **Team**: your "(Personal Team)".
   If Xcode says the identifier is taken, change the *Bundle Identifier* (for example append a number).
5. Connect the iPhone with a cable, unlock it and confirm **Trust This Computer**.
6. On the iPhone: **Settings → Privacy & Security → Developer Mode** → on, then restart.
   The option appears after the first install attempt from Xcode.
7. In Xcode choose your iPhone at the top (next to "PolarLab") and press **▶︎** (⌘R).
8. On first launch iOS blocks the app: **Settings → General → VPN & Device Management** →
   your Apple ID → **Trust**.

After 7 days, reconnect the iPhone and press ▶︎ again.

Without a strap you can try everything in the Xcode **simulator** with «Prova senza fascia
(demo)», which loops a real H10 recording.

### Option B: AltStore or Sideloadly (no Xcode)
Both tools sign an `.ipa` with your Apple ID and install it.

1. **Get `PolarLab.ipa`.** The [*iPhone* workflow](../.github/workflows/ios.yml) builds an
   unsigned `.ipa` on GitHub Actions for every push that touches `ios/` (or on demand:
   *Actions → iPhone → Run workflow*). Download the `PolarLab-ipa` artifact of the latest
   run (sign-in required; artifacts are kept for 30 days), or:
   ```bash
   gh run download -R michelinot99/PolarLab -n PolarLab-ipa
   ```
2. **AltStore**: install [AltServer](https://altstore.io) on the Mac and AltStore on the iPhone
   (follow their guide), connect the iPhone, then ⌥-click the AltServer menu-bar icon →
   **Sideload .ipa…** → choose your iPhone and `PolarLab.ipa`.
   Alternatively, AirDrop the `.ipa` to the iPhone and add it from AltStore → **My Apps** → **+**.
   AltStore refreshes the app automatically when the iPhone and a Mac running AltServer are on
   the same Wi-Fi. To make that reliable, enable Finder → your iPhone → *Show this iPhone when
   on Wi-Fi* once.

   **Sideloadly**: open [Sideloadly](https://sideloadly.io), drop in `PolarLab.ipa`, enter your
   Apple ID and press *Start*.
3. On the iPhone, turn on **Settings → Privacy & Security → Developer Mode** (iOS 16+) and,
   if the app says "Untrusted Developer", trust your Apple ID under
   **Settings → General → VPN & Device Management**.

To update, download the new `.ipa` and sideload it again: the app is replaced and its data kept.

## Usage

- **Corsa** (Run): on first launch tap «Collega la fascia» (Connect the strap) and allow
  Bluetooth. The app remembers your H10 and connects to it by itself from then on.
- **Auto-recording** (on by default): as soon as the strap streams, the run is recorded.
  «Termina» (End) closes it. After 10 minutes without data it closes by itself.
- **Sessioni** (Sessions): summary (duration, average and max HR, cadence, minutes above
  VT1/VT2 from DFA α1), HR and α1 charts, and a **Condividi** (Share) button. Share creates a
  zip with `raw.plab`, `meta.json` and the CSV files.
- The files are also in the **Files** app → **On My iPhone → PolarLab → Sessioni**.

On the Mac, drag the zip onto PolarLab (the window or the Dock icon): it is imported into the
archive and analysed. From the terminal:

```bash
venv.nosync/bin/python -m polarlab --replay ~/Downloads/PolarLab_2026-10-25_081500   # extracted folder
```

## Battery

| Choice | Why |
|---|---|
| No GPS | It is the biggest drain after the screen; pace and distance already come from your watch |
| Screen free to turn off | Recording continues in the background (`bluetooth-central` mode) |
| No drawing with the screen off | The UI timer only exists while the app is in the foreground |
| ECG drawn only when visible | 20 frames/s, can be turned off in the settings |
| Analysis on a single queue | One hour of running costs ~2.5 s of CPU on a Mac (≈0.1% of one core) |
| Disk writes every 10 s | Fewer flash wake-ups |
| Strap disconnected when not recording | In the background without a run in progress the H10 stops streaming |
| Reconnection without scanning | A pending iOS `connect` costs nothing until the strap comes back |
| CSV files generated at the end | Only once, in a few seconds |

The accelerometer can be turned off in the settings. The strap then sends about half the
data, but cadence, vertical oscillation and accelerometer-based breathing are lost.

## Verification (without Xcode)

```bash
cd ios/PolarKit && swift build -c release --scratch-path build.nosync --product plab
build.nosync/release/plab selftest                       # protocol byte vectors
cd ../.. && venv.nosync/bin/python scripts/compare_swift.py dsp       # primitives vs scipy
venv.nosync/bin/python scripts/compare_swift.py session sessions/2026-10-24_161029
venv.nosync/bin/python scripts/compare_swift.py sim --seconds 900     # simulated run
ios/AppCheck/check.sh                                    # the app type-checks on macOS
```

`compare_swift.py` processes the same `raw.plab` with Python and with Swift. Beats, RR
intervals, artefact classification and all metrics match.

After editing `project.yml`: `cd ios && xcodegen` (install XcodeGen with Homebrew).
