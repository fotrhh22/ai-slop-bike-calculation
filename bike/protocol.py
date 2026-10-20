"""FNB58 BLE TLV framing. See README for protocol references.

Only read-stream initialization is supported: no charging-protocol triggers,
device counter resets, or voltage negotiation commands.
"""
import binascii
import struct
from dataclasses import dataclass

NOTIFY_UUID = "0000ffe4-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ffe9-0000-1000-8000-00805f9b34fb"
REQUEST_INFO = bytes.fromhex("aa8100f4")
START_STREAM = bytes.fromhex("aa8200a7")
STOP_STREAM = bytes.fromhex("aa840001")


def checksum(data: bytes) -> int:
    return binascii.crc_hqx(data, 0) & 0xFF


@dataclass(frozen=True)
class Reading:
    voltage_v: float
    current_a: float

    @property
    def power_w(self):
        return self.voltage_v * self.current_a


class Decoder:
    def __init__(self):
        self.buffer = bytearray()
        self.bad_frames = 0

    def feed(self, chunk: bytes):
        self.buffer.extend(chunk)
        readings = []
        while self.buffer:
            if self.buffer[0] != 0xAA:
                del self.buffer[0]
                continue
            if len(self.buffer) < 3:
                break
            kind, length = self.buffer[1], self.buffer[2]
            # Known documented TLV payloads are at most 17 bytes. Bound false
            # headers so a corrupt length cannot indefinitely block new frames.
            if length > 64:
                self.bad_frames += 1
                del self.buffer[0]
                continue
            size = length + 4
            if len(self.buffer) < size:
                break
            frame = bytes(self.buffer[:size])
            if checksum(frame[:-1]) != frame[-1]:
                self.bad_frames += 1
                del self.buffer[0]
                continue
            del self.buffer[:size]
            payload = frame[3:-1]
            if kind == 0x07 and length == 4:
                voltage, current = struct.unpack("<HH", payload)
                readings.append(Reading(voltage / 1000, current / 1000))
            elif kind == 0x04 and length == 12:
                voltage, current, _device_power = struct.unpack("<iii", payload)
                readings.append(Reading(voltage / 10000, current / 10000))
        return readings
