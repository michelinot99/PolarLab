# PolarLab

Streaming e analisi in tempo reale dell'**ECG della Polar H10** su Mac. È il banco di prova
della futura app iPhone da usare in corsa: il protocollo Bluetooth, gli algoritmi e il
formato delle registrazioni sono pensati per essere portati 1:1 in Swift
(vedi [docs/IOS_PORTING.md](docs/IOS_PORTING.md)).

Cosa mostra:
- **ECG grezzo a 130 Hz** su monitor a scorrimento, con griglia da carta ECG (25 mm/s, 10 mm/mV) e picchi R marcati
- **Frequenza cardiaca** dal nostro rilevatore QRS e quella calcolata dalla fascia, a confronto
- **HRV**: RMSSD, SDNN, pNN50, Poincaré SD1/SD2, LF/HF, tacogramma con gli artefatti corretti
- **DFA α1** (finestra di 2 min) con le zone soglia aerobica / anaerobica per l'allenamento
- **Battito medio** (morfologia P-QRS-T) e **qualità del segnale** 0–100
- **Respirazione** (da ECG e accelerometro), **cadenza**, **oscillazione verticale**, intensità del movimento
- Batteria, contatto degli elettrodi, pacchetti al secondo, buchi nello stream, jitter Bluetooth

## Avvio

Doppio clic su `Avvia_PolarLab.command`, oppure:

```bash
cd PolarLab
python3.13 -m venv venv.nosync && venv.nosync/bin/pip install -r requirements.txt   # solo la prima volta
venv.nosync/bin/python -m polarlab            # scegli la sorgente dalla barra in alto
venv.nosync/bin/python -m polarlab --sim      # simulatore (nessuna fascia necessaria)
venv.nosync/bin/python -m polarlab --h10      # si collega subito alla fascia
venv.nosync/bin/python -m polarlab --replay sessions/2026-10-22_183015 --speed 4
```

> **Perché `venv.nosync` e non `.venv`?** La Scrivania è sincronizzata con iCloud: iCloud
> marca come nascosto tutto il contenuto delle cartelle che iniziano con un punto, e Qt su macOS
> salta i plugin nascosti, quindi l'app non partirebbe. Il suffisso `.nosync` invece esclude la
> cartella da iCloud, così non vengono caricati ~1 GB di librerie.

### Primo collegamento con la fascia
1. Inumidisci gli elettrodi e indossa la fascia: la H10 si accende solo a contatto con la pelle.
2. Chiudi Polar Beat/Flow sul telefono e scollega l'orologio dalla fascia: lo stream ECG
   accetta **un solo client** alla volta.
3. Lancia la diagnostica:
   ```bash
   venv.nosync/bin/python scripts/probe_h10.py --seconds 30 --save
   ```
   La prima volta macOS chiede il permesso Bluetooth per il Terminale (Impostazioni di
   Sistema → Privacy e sicurezza → Bluetooth). Lo script mostra servizi GATT, firmware, MTU,
   impostazioni PMD, frequenza di campionamento effettiva, pacchetti persi e l'accordo tra
   i nostri RR e quelli della fascia.
4. Poi apri l'app, sorgente **Polar H10** → **Connetti**. Con **Fascia: …** puoi
   memorizzare la tua H10, così l'app non si collega a quella di qualcun altro.

## App iPhone
In `ios/` c'è l'app per iPhone con lo stesso motore di analisi, portato in Swift e verificato
contro questa versione. Registra le corse e le salva in un formato che si apre qui con
`--replay`. Installazione e uso: [ios/README.md](ios/README.md).

## Registrazioni
**● Registra** salva in `sessions/AAAA-MM-GG_hhmmss/`:
- `raw.plab`: ogni pacchetto Bluetooth così com'è arrivato. È la fonte di verità.
- `meta.json`: dispositivo, firmware, impostazioni degli stream, statistiche.
- `ecg.csv`, `acc.csv`, `hr.csv`, `beats.csv` (RR, NN corretti, tipo di artefatto), `metrics.csv` (1 Hz).

Le CSV si rigenerano dal log grezzo, quindi quando un algoritmo migliora lo si può
riapplicare a tutte le corse già registrate. Le sessioni si possono anche riprodurre nell'app
(**Replay sessione…**).

## Struttura

```
polarlab/
  protocol/   byte → strutture, senza I/O (PMD, Heart Rate Service, timestamp, decoder)
  sources/    fascia reale (bleak/CoreBluetooth), simulatore a livello di byte, replay
  dsp/        filtri, QRS, correzione RR, HRV, DFA, qualità, battito medio, movimento, respiro
  core/       motore di analisi, pipeline a thread, registrazione
  ui/         PySide6 + pyqtgraph
scripts/      probe_h10.py (diagnostica), validate_qrs.py (benchmark MIT-BIH)
tests/        protocollo con vettori di byte, DSP, pipeline end-to-end
docs/         PROTOCOL.md, ALGORITHMS.md, IOS_PORTING.md
```

Il simulatore genera **gli stessi byte** della H10, quindi decoder, DSP, registrazione e UI si
provano senza indossare la fascia. Lo scenario va da riposo a camminata, corsa facile,
ritmo soglia e defaticamento, e include battiti ectopici.

## Test

```bash
venv.nosync/bin/python -m pytest -q
venv.nosync/bin/python scripts/validate_qrs.py 100 105 203   # richiede wfdb e scarica da PhysioNet
```

Dettagli tecnici: [docs/PROTOCOL.md](docs/PROTOCOL.md) · [docs/ALGORITHMS.md](docs/ALGORITHMS.md)

> PolarLab è uno strumento sperimentale per l'allenamento, **non un dispositivo medico**.
> Il tracciato di una fascia toracica è una singola derivazione e non sostituisce un ECG clinico.
