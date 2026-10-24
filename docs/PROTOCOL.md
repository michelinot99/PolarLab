# Protocollo BLE della Polar H10

Fonte di verità per la versione iOS. Ogni esempio esadecimale qui sotto è verificato da
`tests/test_pmd.py` / `tests/test_hr_service.py`.

## Servizi GATT

| Servizio | UUID | Caratteristica | UUID | Proprietà |
|---|---|---|---|---|
| Heart Rate | `180D` | Heart Rate Measurement | `2A37` | notify |
| Battery | `180F` | Battery Level | `2A19` | read, notify |
| Device Information | `180A` | Manufacturer / Model / Serial / HW / FW / SW | `2A29` `2A24` `2A25` `2A27` `2A26` `2A28` | read |
| **Polar PMD** | `FB005C80-02E7-F387-1CAD-8ACD2D8DF0C8` | PMD Control Point | `FB005C81-…` | read, write, indicate |
| | | PMD Data | `FB005C82-…` | notify |

Gli UUID a 16 bit vanno espansi in `0000xxxx-0000-1000-8000-00805f9b34fb` (CoreBluetooth
accetta anche `CBUUID(string: "180D")`).

## Heart Rate Measurement (0x2A37)

```
byte 0: flag
  bit0  formato HR (0 = uint8, 1 = uint16 LE)
  bit1  contatto rilevato         (valido solo se bit2 = 1)
  bit2  rilevamento contatto supportato
  bit3  Energy Expended presente (uint16 LE, kJ)
  bit4  intervalli RR presenti   (uint16 LE ciascuno, unità 1/1024 s)
```

Esempio tipico della H10: `16 3C 00 04` → flag 0x16 (RR + contatto ok), 60 bpm, RR = 0x0400/1024 s = 1000 ms.
La notifica arriva circa ogni secondo, contiene 0–3 RR e **non ha timestamp**.

## PMD Control Point

### Lettura → funzioni supportate
`0F 05 00` → byte 1 è la bitmask dei tipi di misura: bit0 ECG, bit1 PPG, bit2 ACC, bit3 PPI,
bit5 GYRO, bit6 MAG. La H10 risponde `0x05` = ECG + ACC.

### Comandi (write with response)
| Op | Formato | Esempio |
|---|---|---|
| GET_SETTINGS | `01 <tipo>` | `01 00` (ECG) |
| START | `02 <tipo> <TLV…>` | ECG: `02 00 00 01 82 00 01 01 0E 00` |
| | | ACC 200 Hz ±8 G: `02 02 00 01 C8 00 01 01 10 00 02 01 08 00` |
| STOP | `03 <tipo>` | `03 00` |

TLV impostazioni: `<tipo> <conteggio> <valori LE>`
| Tipo | Significato | Byte per valore |
|---|---|---|
| 0x00 | frequenza di campionamento (Hz) | 2 |
| 0x01 | risoluzione (bit) | 2 |
| 0x02 | range (G) | 2 |
| 0x03 | range (milli-unità) | 4 |
| 0x04 | canali | 1 |
| 0x05 | fattore di conversione (float32) | 4 |

### Risposte (indication)
`F0 <op> <tipo> <errore> <more_frames> [parametri]`

Risposta reale della H10 a GET_SETTINGS ECG: `F0 01 00 00 00 00 01 82 00 01 01 0E 00`
→ 130 Hz, 14 bit.
ACC: frequenze 25/50/100/200 Hz, risoluzione 16 bit, range 2/4/8 G.

Codici di errore: 0 SUCCESS, 1 INVALID_OP_CODE, 2 INVALID_MEASUREMENT_TYPE, 3 NOT_SUPPORTED,
4 INVALID_LENGTH, 5 INVALID_PARAMETER, **6 ALREADY_IN_STATE** (stream rimasto acceso: si
manda STOP e poi di nuovo START), 7 INVALID_RESOLUTION, 8 INVALID_SAMPLE_RATE,
9 INVALID_RANGE, 10 INVALID_MTU, 11 INVALID_NUMBER_OF_CHANNELS, 12 INVALID_STATE,
13 DEVICE_IN_CHARGER.

## PMD Data (notify)

```
[0]     tipo di misura (00 ECG, 02 ACC)
[1..8]  timestamp uint64 LE in ns — istante dell'ULTIMO campione del frame
[9]     tipo di frame: bit7 = compresso a delta, bit0..6 = formato
[10..]  campioni
```

* **ECG, frame 0x00**: campioni int24 LE con segno, in µV. Con MTU 232 la H10 manda 73
  campioni per frame (~0.56 s); con MTU più piccoli ne manda meno.
  Esempio payload: `FF FF FF` = −1 µV, `E8 03 00` = 1000 µV, `30 F8 FF` = −2000 µV.
* **ACC, frame 0x00/0x01/0x02**: x, y, z come int8/int16/int24 LE in mG. La H10 usa 0x01 (int16).
* **Frame compressi (bit7 = 1)**: un campione di riferimento (canali × ⌈risoluzione/8⌉ byte),
  poi blocchi `[bit_per_delta][numero_campioni][delta impacchettati a bit, LSB first]`,
  deltas con segno in complemento a 2, cumulativi rispetto al campione precedente. La H10
  normalmente non li usa, ma il decoder li gestisce (Verity Sense e altri sì).

### Timestamp
Il timestamp è nell'orologio della fascia (epoch Polar 2000-01-01, ma sulla H10 non
impostato: va trattato come un orologio monotono qualsiasi). Gli istanti dei campioni
precedenti si ricostruiscono all'indietro con il periodo effettivo stimato dai timestamp
stessi (`timebase.StreamClock`): se l'intervallo tra due frame vale più di `n` campioni,
mancano dei pacchetti e la differenza dice quanti campioni.

La frequenza reale non coincide con quella dichiarata: su una H10 (firmware 5.0.0)
l'ECG a "130 Hz" ne fa ~129.7 e l'ACC a "100 Hz" ~101.9. Per l'ECG conta poco, perché gli
istanti dei battiti vengono dai timestamp; l'ACC invece si ricampiona sulla griglia nominale
(`dsp.filters.UniformResampler`) prima di stimare cadenza, oscillazione e respiro,
altrimenti risulterebbero ~2% troppo bassi.

Per allineare i dati della fascia all'orologio del telefono si usa l'offset minimo
`arrivo − timestamp` su una finestra di 60 s (`timebase.ClockSync`): è il pacchetto con
la latenza più bassa. Serve per collocare gli RR della 0x2A37, che non hanno timestamp.

## Sequenza di avvio

1. connessione (macOS e iOS negoziano l'MTU da soli: il numero di campioni per frame dipende da quello)
2. notifiche: 2A37, 2A19, PMD Control Point (indication), PMD Data
3. lettura del Control Point → funzioni
4. per ogni stream: `01 <tipo>` → risposta impostazioni → `02 <tipo> <TLV>` → `F0 02 <tipo> 00`
5. dati in arrivo su PMD Data
6. alla chiusura: `03 00`, `03 02`, poi disconnessione

Note pratiche
* La H10 si accende solo con il contatto sulla pelle (elettrodi umidi).
* Lo stream PMD accetta **un solo client**: chiudi Polar Beat/Flow sul telefono, o
  scollega l'orologio che usa la fascia, prima di collegarti dal Mac.
* Se uno stream risulta ancora attivo (per esempio dopo una disconnessione brusca), START
  risponde `ALREADY_IN_STATE`: il codice manda STOP e poi di nuovo START.
