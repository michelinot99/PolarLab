import pytest

from polarlab.protocol.hr_service import encode_hr_measurement, parse_hr_measurement


def test_h10_typical_packet():
    # flag 0x16: HR uint8, contatto supportato+rilevato, RR presenti; 60 bpm, RR 1024/1024 s
    m = parse_hr_measurement(bytes.fromhex("163c0004"))
    assert m.bpm == 60 and m.contact is True and m.rr_ms == [1000.0] and m.energy_kj is None


def test_uint16_hr_energy_and_multiple_rr():
    m = parse_hr_measurement(bytes([0x19]) + (300).to_bytes(2, "little") + (12).to_bytes(2, "little")
                             + (512).to_bytes(2, "little") + (256).to_bytes(2, "little"))
    assert m.bpm == 300 and m.energy_kj == 12 and m.rr_ms == [500.0, 250.0] and m.contact is None


def test_contact_not_detected():
    assert parse_hr_measurement(bytes([0x04, 70])).contact is False


@pytest.mark.parametrize("bpm,rr,contact", [(55, [1090.8], True), (180, [333.0, 334.0], None), (260, [], False)])
def test_roundtrip(bpm, rr, contact):
    m = parse_hr_measurement(encode_hr_measurement(bpm, rr, contact))
    assert m.bpm == bpm and m.contact == contact
    assert m.rr_ms == pytest.approx(rr, abs=0.5)    # risoluzione 1/1024 s
