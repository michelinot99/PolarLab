# PolarLab per iPhone

App iPhone che registra ECG, battiti e accelerometro dalla Polar H10 durante la corsa.
Il protocollo, gli algoritmi e il formato dei file sono gli stessi di PolarLab per Mac:
ogni corsa registrata col telefono si può rivedere e rianalizzare sul Mac.

```
ios/
  PolarKit/        pacchetto Swift: protocollo H10, DSP, motore, registrazione (porting 1:1 di polarlab/)
  App/             app SwiftUI: Bluetooth, schermata di corsa, archivio delle sessioni
  AppCheck/        controllo dei tipi dell'app senza Xcode (./check.sh)
  project.yml      descrizione del progetto per XcodeGen → PolarLab.xcodeproj
```

## Installazione sull'iPhone (account Apple gratuito)

1. Installa **Xcode** dall'App Store. Al primo avvio accetta l'installazione del supporto iOS.
2. Xcode → **Settings → Accounts** → **+** → aggiungi il tuo Apple ID.
3. Apri `ios/PolarLab.xcodeproj`.
4. Seleziona il target **PolarLab** → scheda **Signing & Capabilities** → **Team**: il tuo
   "(Personal Team)". Se Xcode dice che l'identificativo è già in uso, cambia
   *Bundle Identifier* (per esempio aggiungendo un numero alla fine).
5. Collega l'iPhone con il cavo, sblocca il telefono e conferma **Autorizza questo computer**.
6. Sull'iPhone: **Impostazioni → Privacy e sicurezza → Modalità sviluppatore** → attiva e
   riavvia. La voce compare dopo il primo tentativo di installazione da Xcode.
7. In Xcode scegli il tuo iPhone in alto (accanto a "PolarLab") e premi **▶︎** (⌘R).
8. Al primo avvio l'iPhone blocca l'app: **Impostazioni → Generali → VPN e gestione
   dispositivi** → il tuo Apple ID → **Autorizza**.

Con l'account gratuito l'installazione **scade dopo 7 giorni**: basta ricollegare l'iPhone e
premere di nuovo ▶︎. Le sessioni registrate restano sul telefono.

Senza fascia si può provare tutto nel **simulatore** di Xcode con «Prova senza fascia (demo)»,
che riproduce in ciclo una registrazione vera della H10.

## Uso

- **Corsa**: alla prima apertura premi «Collega la fascia» e dai il permesso Bluetooth. L'app
  memorizza la tua H10 e dalle volte successive si collega da sola.
- **Registrazione automatica** (attiva di default): appena la fascia trasmette, la corsa viene
  registrata. «Termina» la chiude. Dopo 10 minuti senza dati si chiude da sola.
- **Sessioni**: riepilogo (durata, FC media e massima, cadenza, minuti oltre VT1/VT2 secondo DFA α1),
  grafici di FC e α1, pulsante **Condividi** che crea uno .zip con `raw.plab`, `meta.json` e le CSV.
- I file sono anche nell'app **File → Sul mio iPhone → PolarLab → Sessioni**.

Sul Mac: estrai lo zip e apri la cartella in PolarLab.

```bash
venv.nosync/bin/python -m polarlab --replay ~/Downloads/PolarLab_2026-10-25_081500
```

## Batteria

| Scelta | Perché |
|---|---|
| Niente GPS | È il consumo più alto dopo lo schermo; passo e distanza li dà già l'orologio |
| Schermo libero di spegnersi | La registrazione continua in background (modalità `bluetooth-central`) |
| Nessun disegno a schermo spento | Il timer dell'interfaccia esiste solo con l'app in primo piano |
| ECG disegnato solo se visibile | 20 fotogrammi/s, disattivabile nelle impostazioni |
| Analisi su una sola coda | Un'ora di corsa costa ~2.5 s di CPU sul Mac (≈0.1% di un core) |
| Scrittura su disco ogni 10 s | Meno risvegli della memoria flash |
| Fascia scollegata se non registri | In background senza corsa in corso la H10 smette di trasmettere |
| Riconnessione senza scansioni | La `connect` pendente di iOS non costa nulla finché la fascia non torna |
| CSV generate a fine corsa | Una sola volta, in pochi secondi |

L'accelerometro si può spegnere nelle impostazioni: la fascia trasmette circa metà dei dati,
ma si perdono cadenza, oscillazione verticale e respiro dall'accelerometro.

## Verifica (senza Xcode)

```bash
cd ios/PolarKit && swift build -c release --scratch-path build.nosync --product plab
build.nosync/release/plab selftest                       # vettori di byte del protocollo
cd ../.. && venv.nosync/bin/python scripts/compare_swift.py dsp       # primitive vs scipy
venv.nosync/bin/python scripts/compare_swift.py session sessions/2026-10-24_161029
venv.nosync/bin/python scripts/compare_swift.py sim --seconds 900     # corsa simulata
ios/AppCheck/check.sh                                    # l'app compila (controllo dei tipi su macOS)
```

`compare_swift.py` rielabora lo stesso `raw.plab` con Python e con Swift: battiti, RR,
classificazione degli artefatti e tutte le metriche coincidono.

Dopo aver modificato `project.yml`: `cd ios && xcodegen` (XcodeGen si installa con Homebrew).
