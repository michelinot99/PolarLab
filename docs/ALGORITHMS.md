# Algoritmi

Tutti i processori lavorano **in streaming**: ricevono il blocco di campioni appena
arrivato (`push`) e mantengono lo stato interno. Il costo per campione è costante, e le
metriche su finestra (HRV, DFA, respirazione, cadenza) si ricalcolano a 0.2–1 Hz. È la
struttura che serve su iPhone per non consumare batteria durante la corsa.

```
ECG µV ─► passa-banda 0.5–40 Hz ─┬─► (notch 50 Hz) ─► monitor
                                 ├─► QRS Pan-Tompkins ─► picco R sub-campione ─► RR
                                 └─► battito medio / correlazione ─► qualità
RR ─► correzione artefatti (Lipponen-Tarvainen) ─► NN ─► HRV (60 s), LF/HF (5 min), DFA α1 (2 min)
RR + ampiezza R + ACC ─► respirazione
ACC ─► verticale dalla gravità ─► cadenza, oscillazione verticale, intensità
RR fascia (0x2A37) ─► confronto con i nostri RR ─► qualità
```

## Filtri — `dsp/filters.py`
* Passa-banda Butterworth 0.5–40 Hz (ordine 2 per lato), causale in SOS con stato.
  0.5 Hz toglie la deriva da respiro e movimento, 40 Hz il rumore muscolare.
* Notch IIR a 50 Hz (Q = 30) opzionale. A 130 Hz di campionamento i 50 Hz della rete sono
  dentro la banda e quindi visibili.
* Lo stato iniziale `sosfilt_zi · x[0]` evita il transitorio all'avvio.
* `zero_phase_ecg` (filtfilt) è per l'analisi offline della morfologia.

## Rilevamento QRS — `dsp/qrs.py`
Pan & Tompkins (IEEE TBME 1985), adattato a 130 Hz:
1. passa-banda 5–15 Hz → derivata a 5 punti → quadrato → integrazione mobile su 150 ms;
2. massimi locali della MWI confermati dopo 200 ms senza un valore più alto;
3. soglie adattive `TH1 = NPKI + 0.25 (SPKI − NPKI)` e `TH2 = TH1/2`, apprese nei primi 2 s;
4. refrattario di 200 ms; un candidato entro 360 ms con pendenza < 50% del QRS precedente
   è un'onda T;
5. search-back: se non arriva un battito entro 1.66 × RR medio, si accetta il picco di
   rumore più alto sopra TH2;
6. riapprendimento automatico dopo 3 s senza battiti (fascia staccata e rimessa);
7. **localizzazione del picco R** sul segnale 0.5–40 Hz in una finestra di 300 ms prima
   del picco MWI, con polarità appresa (e corretta a maggioranza se il QRS è invertito),
   più **interpolazione gaussiana su 3 punti** (parabola sui logaritmi).

A 130 Hz un campione vale 7.7 ms: senza interpolazione l'errore di quantizzazione
(±3.8 ms) sarebbe paragonabile all'RMSSD durante la corsa (3–10 ms).
Sul simulatore il jitter robusto (MAD) del picco R è ~0.3 ms (`tests/test_dsp.py`).
`scripts/validate_qrs.py` misura Se e +P sul MIT-BIH ricampionato a 130 Hz (22 ottobre 2026):

| Record | Battiti | Se | +P | Note |
|---|---|---|---|---|
| 100 | 931* | 100.00% | 100.00% | ritmo sinusale pulito |
| 105 | 1525* | 99.87% | 99.80% | rumore e artefatti pesanti |
| 203 | 2960 | 94.97% | 96.56% | fibrillazione atriale + tachicardia ventricolare, extrasistoli multiformi |

\* download parziale (rete lenta), valutato sulla parte scaricata. Errore di posizione: mediana
+2.4 ms (ritardo costante, si annulla negli RR), IQR 2.5 ms.
Nel 203 si perde ~20% dei battiti ventricolari larghi durante gli episodi di TV. Dimezzare le
soglie nel ritmo irregolare o ammorbidire la regola dell'onda T sposta solo il compromesso
Se/+P (provato: Se 96.1% ma +P 93.9%), quindi resta il Pan-Tompkins originale. Per una fascia
toracica in ritmo sinusale, i casi rilevanti sono 100 e 105.

## Correzione degli artefatti RR — `dsp/rr_clean.py`
Lipponen & Tarvainen, J Med Eng Technol 2019 (è l'algoritmo di Kubios):
* `dRR` normalizzato con 5.2 × quartile deviation su 91 battiti;
* differenza dalla mediana su 11 battiti (`mRR`, valori negativi raddoppiati);
* classificazione con le regioni S11/S12 (ectopici) e S21/S22 (lungo/corto) e i test
  "battito mancato" (RR/2 ≈ mediana) ed "extra" (RR + RR successivo ≈ mediana).

Versione causale: ogni intervallo si classifica appena arrivano i 2 successivi, con
statistiche calcolate sulla finestra che termina lì. Correzioni: battito mancato → due
intervalli da RR/2; extra → fusione dei due; ectopico/lungo-corto → mediana locale.
Soglia minima di 15 ms su entrambe le soglie, così con la variabilità bassissima della corsa i
battiti normali non vengono segnalati per pochi millisecondi.

Gli intervalli che attraversano un buco di pacchetti (riempito per continuità) sono marcati
`invalid` ed esclusi dall'HRV.

## HRV — `dsp/hrv.py`
* Dominio del tempo su 60 s: media NN, SDNN, RMSSD, pNN50; Poincaré SD1 = √½·sd(ΔNN),
  SD2 = √(2·SDNN² − SD1²).
* Frequenza su 5 min: spline cubica a 4 Hz, detrend, Welch (Hann), LF 0.04–0.15 Hz,
  HF 0.15–0.40 Hz in ms². Nella corsa la respirazione supera spesso 0.4 Hz, quindi l'HF
  "classica" perde significato: per questo si riporta anche il picco respiratorio.
  (Ho scelto ricampionamento + Welch invece di Lomb-Scargle, previsto all'inizio, perché dà
  direttamente potenze in ms² con normalizzazione standard.)

## DFA α1 — `dsp/dfa.py`
Peng et al. 1995; in ambito sportivo Rogers et al. 2021 (Front. Physiol.).
Integrazione della serie NN, finestre non sovrapposte da 4 a 16 battiti prese sia
dall'inizio sia dalla fine, detrend lineare, pendenza log-log di F(n). Finestra mobile di
2 minuti, ricalcolata ogni 5 s.
* α1 ≥ 0.75 → sotto la soglia aerobica (VT1)
* 0.5 – 0.75 → tra VT1 e VT2
* < 0.5 → oltre la soglia anaerobica

È molto sensibile agli artefatti: oltre il 3–5% di battiti corretti il valore non è
affidabile (la percentuale è mostrata in "Artefatti").
Validazione: rumore bianco → 0.5, rumore 1/f → 1.0, rumore browniano → 1.5 (`tests/test_dsp.py`).

## Qualità del segnale — `dsp/quality.py`, `dsp/template.py`
* **Battito medio**: media dei battiti allineati sul picco R, con allineamento sub-campione
  (i primi 8 in media aritmetica, poi media esponenziale α = 0.08 solo per i battiti con
  r ≥ 0.7). Mostra la morfologia P-QRS-T anche quando il singolo battito è rumoroso.
* **Correlazione battito-template** (mediana degli ultimi 20 battiti).
* **Accordo con la fascia**: la H10 calcola i propri RR con un algoritmo interno. Siccome la
  0x2A37 non ha timestamp, le due sequenze si allineano cercando lo sfasamento (±6 battiti)
  con il minimo errore medio. Due stime indipendenti che coincidono sono il miglior
  indizio di un RR corretto.
* Segnale piatto/saturo e flag di contatto della 0x2A37.
* Punteggio 0–100 = 50% correlazione + 35% accordo + 15% contatto.

## Respirazione — `dsp/respiration.py`
Tre stime, ciascuna come picco spettrale (Welch) in 0.1–1 Hz su 60 s, con prominenza come
confidenza:
1. **EDR**: modulazione dell'ampiezza R;
2. **RSA**: modulazione degli RR (aritmia sinusale respiratoria);
3. **ACC**: componente principale del movimento lento del torace (decimato a 10 Hz).

Le serie per battito sono campionate alla frequenza cardiaca, quindi vedono respiri solo
fino a FC/2: a riposo, con FC 60, non oltre 30 atti/min. L'ACC non ha questo limite.
Fusione: si sceglie la stima più prominente e si mediano quelle che concordano entro 3 atti/min.

## Movimento — `dsp/motion.py`
* L'ACC entra già ricampionato alla frequenza nominale con i timestamp della fascia
  (`UniformResampler`, interpolazione lineare): il quarzo della H10 va ~2% più veloce.
* Verticale = direzione della componente lenta (< 0.3 Hz) del vettore accelerazione: un
  accelerometro fermo misura +1 g verso l'alto, qualunque sia l'orientamento della fascia.
* Cadenza: autocorrelazione dell'accelerazione verticale (filtrata 1–6 Hz) su 8 s, picco
  tra 0.25 e 0.75 s (80–240 passi/min) con interpolazione parabolica. Se c'è un picco forte
  a metà ritardo, il primo era la falcata (2 passi) e si usa il passo.
* Oscillazione verticale: doppia integrazione con passa-alto prima e dopo ogni
  integrazione (per evitare la deriva), mediana dei picco-picco per passo. È una **stima**:
  va confrontata con una sorgente di riferimento (Garmin HRM-Pro, Stryd) prima di fidarsi.

## Riferimenti
* Pan J, Tompkins WJ. A real-time QRS detection algorithm. IEEE TBME 1985.
* Lipponen JA, Tarvainen MP. A robust algorithm for HRV time series artefact correction
  using novel beat classification. J Med Eng Technol 2019.
* Peng CK et al. Quantification of scaling exponents and crossover phenomena in
  nonstationary heartbeat time series. Chaos 1995.
* Rogers B et al. A new detection method defining the aerobic threshold for endurance
  exercise and training prescription based on fractal correlation properties of HRV.
  Front Physiol 2021.
* Task Force ESC/NASPE. Heart rate variability: standards of measurement. Circulation 1996.
* McSharry PE et al. A dynamical model for generating synthetic ECG signals. IEEE TBME 2003.
* Charlton PH et al. Breathing rate estimation from the ECG and PPG: a review. IEEE Rev
  Biomed Eng 2018.
