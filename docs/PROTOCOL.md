# Polar H10 Bluetooth protocol

The reference for both the Python and the Swift implementation. Every hexadecimal example
below is checked by `tests/test_pmd.py` / `tests/test_hr_service.py`.

## GATT services

| Service | UUID | Characteristic | UUID | Properties |
|---|---|---|---|---|
| Heart Rate | `180D` | Heart Rate Measurement | `2A37` | notify |
| Battery | `180F` | Battery Level | `2A19` | read, notify |
| Device Information | `180A` | Manufacturer / Model / Serial / HW / FW / SW | `2A29` `2A24` `2A25` `2A27` `2A26` `2A28` | read |
| **Polar PMD** | `FB005C80-02E7-F387-1CAD-8ACD2D8DF0C8` | PMD Control Point | `FB005C81-…` | read, write, indicate |
| | | PMD Data | `FB005C82-…` | notify |

16-bit UUIDs expand to `0000xxxx-0000-1000-8000-00805f9b34fb` (CoreBluetooth also accepts
`CBUUID(string: "180D")`).

## Heart Rate Measurement (0x2A37)

```
byte 0: flags
  bit0  HR format (0 = uint8, 1 = uint16 LE)
  bit1  contact detected           (valid only if bit2 = 1)
  bit2  contact detection supported
  bit3  Energy Expended present    (uint16 LE, kJ)
  bit4  RR intervals present       (uint16 LE each, unit 1/1024 s)
```

Typical H10 example: `16 3C 00 04` → flags 0x16 (RR + contact ok), 60 bpm, RR = 0x0400/1024 s = 1000 ms.
The notification arrives about once per second, carries 0–3 RR intervals and **has no timestamp**.

## PMD Control Point

### Read → supported features
`0F 05 00` → byte 1 is the bitmask of measurement types: bit0 ECG, bit1 PPG, bit2 ACC, bit3 PPI,
bit5 GYRO, bit6 MAG. The H10 answers `0x05` = ECG + ACC.

### Commands (write with response)
| Op | Format | Example |
|---|---|---|
| GET_SETTINGS | `01 <type>` | `01 00` (ECG) |
| START | `02 <type> <TLV…>` | ECG: `02 00 00 01 82 00 01 01 0E 00` |
| | | ACC 200 Hz ±8 G: `02 02 00 01 C8 00 01 01 10 00 02 01 08 00` |
| STOP | `03 <type>` | `03 00` |

Settings TLV: `<type> <count> <LE values>`
| Type | Meaning | Bytes per value |
|---|---|---|
| 0x00 | sample rate (Hz) | 2 |
| 0x01 | resolution (bits) | 2 |
| 0x02 | range (G) | 2 |
| 0x03 | range (milli-units) | 4 |
| 0x04 | channels | 1 |
| 0x05 | conversion factor (float32) | 4 |

### Responses (indication)
`F0 <op> <type> <error> <more_frames> [parameters]`

Real H10 answer to GET_SETTINGS ECG: `F0 01 00 00 00 00 01 82 00 01 01 0E 00`
→ 130 Hz, 14 bits.
ACC: sample rates 25/50/100/200 Hz, resolution 16 bits, range 2/4/8 G.

Error codes: 0 SUCCESS, 1 INVALID_OP_CODE, 2 INVALID_MEASUREMENT_TYPE, 3 NOT_SUPPORTED,
4 INVALID_LENGTH, 5 INVALID_PARAMETER, **6 ALREADY_IN_STATE** (a stream was left running: send
STOP, then START again), 7 INVALID_RESOLUTION, 8 INVALID_SAMPLE_RATE, 9 INVALID_RANGE,
10 INVALID_MTU, 11 INVALID_NUMBER_OF_CHANNELS, 12 INVALID_STATE, 13 DEVICE_IN_CHARGER.

## PMD Data (notify)

```
[0]     measurement type (00 ECG, 02 ACC)
[1..8]  uint64 LE timestamp in ns — time of the LAST sample in the frame
[9]     frame type: bit7 = delta-compressed, bit0..6 = format
[10..]  samples
```

* **ECG, frame 0x00**: signed int24 LE samples, in µV. With MTU 232 the H10 sends 73 samples
  per frame (~0.56 s); with smaller MTUs it sends fewer.
  Payload example: `FF FF FF` = −1 µV, `E8 03 00` = 1000 µV, `30 F8 FF` = −2000 µV.
* **ACC, frames 0x00/0x01/0x02**: x, y, z as int8/int16/int24 LE in mG. The H10 uses 0x01 (int16).
* **Compressed frames (bit7 = 1)**: one reference sample (channels × ⌈resolution/8⌉ bytes),
  then blocks `[bits_per_delta][sample_count][bit-packed deltas, LSB first]`; deltas are
  signed two's complement, cumulative with respect to the previous sample. The H10 normally
  does not use them, but the decoder handles them (Verity Sense and others do).

### Timestamps
The timestamp is in the strap's clock (Polar epoch 2000-01-01, but not set on the H10: treat
it as an arbitrary monotonic clock). The times of the earlier samples in a frame are
reconstructed backwards using the effective period estimated from the timestamps themselves
(`timebase.StreamClock`). If the interval between two frames is worth more than `n` samples,
packets are missing and the difference tells how many samples. The strap's clock keeps
running across a Bluetooth reconnection or a STOP + START, so a new START with the same
settings keeps the stream's clock: the first frame after it reveals the gap like any other
loss (gaps up to 1 s are filled and the RR across them is `invalid`, longer ones restart
beat detection). A timestamp that goes backwards means the strap restarted: detection
restarts too.

The real sample rate differs from the declared one: on one H10 (firmware 5.0.0) the
"130 Hz" ECG runs at ~129.7 Hz and the "100 Hz" ACC at ~101.9 Hz. For the ECG this matters
little, because beat times come from the timestamps. The ACC, however, is resampled onto the
nominal grid (`dsp.filters.UniformResampler`) before estimating cadence, oscillation and
breathing, otherwise they would come out ~2% too low.

To align the strap's data with the phone/Mac clock, the minimum offset `arrival − timestamp`
over a 60 s window is used (`timebase.ClockSync`): it belongs to the packet with the lowest
latency. It is needed to place the 0x2A37 RR intervals, which have no timestamp.

## Start-up sequence

1. connect (macOS and iOS negotiate the MTU themselves; the samples per frame depend on it)
2. notifications: 2A37, 2A19, PMD Control Point (indication), PMD Data
3. read the Control Point → features
4. for each stream: `01 <type>` → settings response → `02 <type> <TLV>` → `F0 02 <type> 00`
5. data arrives on PMD Data
6. on shutdown: `03 00`, `03 02`, then disconnect

Practical notes
* The H10 only turns on with skin contact (wet electrodes).
* The PMD stream accepts **a single client**: close Polar Beat/Flow on the phone, or unpair
  the watch using the strap, before connecting from the Mac.
* If a stream is still active (for example after an abrupt disconnection), START answers
  `ALREADY_IN_STATE`: the code sends STOP and then START again.
