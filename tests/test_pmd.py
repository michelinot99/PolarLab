import numpy as np
import pytest

from polarlab.protocol import pmd
from polarlab.protocol.pmd import MeasurementType as MT, SettingType as ST


def test_start_commands_match_polar_sdk_bytes():
    ecg = pmd.build_start(MT.ECG, {ST.SAMPLE_RATE: 130, ST.RESOLUTION: 14})
    assert ecg.hex(" ") == "02 00 00 01 82 00 01 01 0e 00"
    acc = pmd.build_start(MT.ACC, {ST.RANGE: 8, ST.SAMPLE_RATE: 200, ST.RESOLUTION: 16})
    assert acc.hex(" ") == "02 02 00 01 c8 00 01 01 10 00 02 01 08 00"
    assert pmd.build_get_settings(MT.ACC) == bytes([0x01, 0x02])
    assert pmd.build_stop(MT.ECG) == bytes([0x03, 0x00])


def test_parse_start_command_roundtrip():
    cmd = pmd.build_start(MT.ACC, {ST.SAMPLE_RATE: 100, ST.RESOLUTION: 16, ST.RANGE: 4})
    mt, s = pmd.parse_start_command(cmd)
    assert mt == MT.ACC and s == {ST.SAMPLE_RATE: 100, ST.RESOLUTION: 16, ST.RANGE: 4}
    assert pmd.parse_start_command(bytes([0x01, 0x00])) is None


def test_features():
    assert pmd.parse_features(bytes.fromhex("0f0500")) == {MT.ECG, MT.ACC}
    with pytest.raises(pmd.PmdProtocolError):
        pmd.parse_features(bytes.fromhex("f00100"))


def test_settings_response_from_h10():
    raw = bytes.fromhex("f0010000000001820001010e00")
    r = pmd.parse_control_response(raw)
    assert r.ok and r.op == 1 and r.meas_type == 0
    assert r.settings() == {ST.SAMPLE_RATE: [130], ST.RESOLUTION: [14]}
    acc = pmd.encode_settings_response(MT.ACC, pmd.H10_AVAILABLE[MT.ACC])
    assert pmd.parse_control_response(acc).settings()[ST.SAMPLE_RATE] == [25, 50, 100, 200]


def test_control_errors():
    r = pmd.parse_control_response(bytes.fromhex("f002000600"))
    assert r.error == pmd.PmdError.ALREADY_IN_STATE and r.ok
    r = pmd.parse_control_response(bytes.fromhex("f002020800"))
    assert r.error == pmd.PmdError.INVALID_SAMPLE_RATE and not r.ok


def test_choose_settings_picks_closest():
    avail = pmd.H10_AVAILABLE[MT.ACC]
    chosen = pmd.choose_settings(avail, {ST.SAMPLE_RATE: 120, ST.RANGE: 16})
    assert chosen == {ST.SAMPLE_RATE: 100, ST.RESOLUTION: 16, ST.RANGE: 8}


def test_ecg_frame_golden_vector():
    ts = 599_000_000_123_456_789
    raw = bytes([0x00]) + ts.to_bytes(8, "little") + bytes([0x00]) + bytes.fromhex("ffffff e80300 30f8ff ffff7f 000080")
    f = pmd.parse_data_frame(raw)
    assert f.meas_type == MT.ECG and f.timestamp_ns == ts and not f.compressed
    assert f.samples[:, 0].tolist() == [-1, 1000, -2000, 8_388_607, -8_388_608]


def test_acc_frame_int16():
    xyz = np.array([[-1000, 2, 3], [4, -5, 32767], [-32768, 0, 15]])
    raw = pmd.encode_data_frame(MT.ACC, 42, xyz)
    assert raw[9] == 0x01 and len(raw) == 10 + 6 * 3
    f = pmd.parse_data_frame(raw)
    assert np.array_equal(f.samples, xyz)


@pytest.mark.parametrize("channels,resolution", [(3, 16), (1, 22)])
def test_delta_frames_roundtrip(channels, resolution):
    rng = np.random.default_rng(0)
    x = np.cumsum(rng.integers(-300, 300, size=(150, channels)), axis=0) + 1000
    mt = MT.ACC if channels == 3 else MT.ECG
    raw = pmd.encode_delta_frame(mt, 7, x, resolution=resolution, frame_type=1 if channels == 3 else 0)
    assert raw[9] & pmd.COMPRESSED_FLAG
    st = pmd.StreamSettings(sample_rate=50, resolution=resolution, channels=channels)
    f = pmd.parse_data_frame(raw, st)
    assert f.compressed and np.array_equal(f.samples, x)


def test_malformed_frames_raise():
    with pytest.raises(pmd.PmdProtocolError):
        pmd.parse_data_frame(b"\x00\x01")
    with pytest.raises(pmd.PmdProtocolError):
        pmd.parse_data_frame(bytes(9) + b"\x00" + b"\x01\x02")   # ECG non multiplo di 3 byte
