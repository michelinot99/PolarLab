"""UUID GATT usati dalla Polar H10."""


def _sig(short: int) -> str:
    """UUID a 16 bit del Bluetooth SIG espanso alla forma a 128 bit."""
    return f"0000{short:04x}-0000-1000-8000-00805f9b34fb"


# Heart Rate Service (standard Bluetooth SIG)
HR_SERVICE = _sig(0x180D)
HR_MEASUREMENT = _sig(0x2A37)

# Battery Service
BATTERY_SERVICE = _sig(0x180F)
BATTERY_LEVEL = _sig(0x2A19)

# Device Information Service
DEVICE_INFO_SERVICE = _sig(0x180A)
MANUFACTURER_NAME = _sig(0x2A29)
MODEL_NUMBER = _sig(0x2A24)
SERIAL_NUMBER = _sig(0x2A25)
HARDWARE_REVISION = _sig(0x2A27)
FIRMWARE_REVISION = _sig(0x2A26)
SOFTWARE_REVISION = _sig(0x2A28)

DEVICE_INFO_FIELDS = {
    "manufacturer": MANUFACTURER_NAME,
    "model": MODEL_NUMBER,
    "serial": SERIAL_NUMBER,
    "hardware": HARDWARE_REVISION,
    "firmware": FIRMWARE_REVISION,
    "software": SOFTWARE_REVISION,
}

# Polar Measurement Data (PMD) — servizio proprietario per ECG/ACC grezzi
PMD_SERVICE = "fb005c80-02e7-f387-1cad-8acd2d8df0c8"
PMD_CONTROL = "fb005c81-02e7-f387-1cad-8acd2d8df0c8"
PMD_DATA = "fb005c82-02e7-f387-1cad-8acd2d8df0c8"
