"""BLE transport using bleak; imported only for real scan/connect operations."""
import asyncio
import math
import time

from .measurement import StationError
from .protocol import Decoder, NOTIFY_UUID, WRITE_UUID, REQUEST_INFO, START_STREAM, Reading


class Receiver:
    def __init__(self, station):
        self.station = station
        self.task = None
        self.devices = {}
        self.busy = False
        self.raw_packets = 0
        self.bad_frames = 0

    @staticmethod
    def bleak():
        try:
            from bleak import BleakClient, BleakScanner
        except ImportError:
            raise StationError("BLE 패키지가 없습니다. pip install -r bike/requirements.txt 를 실행하세요.")
        return BleakClient, BleakScanner

    async def scan(self):
        if self.station.mode != "ble":
            raise StationError("실측 모드에서 기기를 검색하세요.")
        if self.task or self.busy:
            raise StationError("기기 연결을 해제한 뒤 검색하세요.")
        _, scanner = self.bleak()
        self.busy = True
        self.station.message = "FNB58 검색 중… Mac의 Bluetooth 권한 요청을 확인하세요."
        try:
            found = await scanner.discover(timeout=6)
            self.devices = {dev.address: dev for dev in found
                            if "FNB58" in (dev.name or "").upper()
                            or "FNB-58" in (dev.name or "").upper()}
            self.station.message = ("기기를 선택하고 연결하세요." if self.devices else
                                    "FNB58을 찾지 못했습니다. 전력계의 Bluetooth·전원·Mac 권한을 확인하세요.")
            return [{"id": dev.address, "name": dev.name or "FNB58"}
                    for dev in self.devices.values()]
        finally:
            self.busy = False

    async def connect(self, device_id):
        if self.station.mode != "ble" or self.task or self.busy:
            raise StationError("실측 모드에서 기존 연결을 해제한 뒤 연결하세요.")
        dev = self.devices.get(device_id)
        if dev is None:
            raise StationError("먼저 기기를 검색하고 목록에서 선택하세요.")
        self.bleak()
        self.raw_packets = self.bad_frames = 0
        self.station.message = "FNB58 연결 중…"
        self.task = asyncio.create_task(self._stream(dev))

    async def _stream(self, dev):
        client_class, _ = self.bleak()
        decoder = Decoder()
        failure = "FNB58 연결이 끊겼습니다. 검색 후 다시 연결하세요."
        try:
            async with client_class(dev, timeout=15) as client:
                self.station.connected = True
                self.station.device_id = dev.address
                self.station.device_name = dev.name
                self.station.message = "연결됨 · 측정 패킷 대기 중"

                def on_data(_sender, data):
                    self.raw_packets += 1
                    for reading in decoder.feed(bytes(data)):
                        self.station.ingest(reading)
                    self.bad_frames = decoder.bad_frames

                await client.start_notify(NOTIFY_UUID, on_data)
                properties = client.services.get_characteristic(WRITE_UUID).properties
                response = "write" in properties
                await client.write_gatt_char(WRITE_UUID, REQUEST_INFO, response=response)
                await asyncio.sleep(2)
                await client.write_gatt_char(WRITE_UUID, START_STREAM, response=response)
                while client.is_connected:
                    await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            failure = "연결 해제됨"
            raise
        except Exception as exc:
            failure = "BLE 연결 오류: " + str(exc)[:240]
        finally:
            self.station.clear_connection(failure)
            self.task = None

    async def stop(self):
        if self.busy:
            raise StationError("기기 검색이 끝난 뒤 다시 시도하세요.")
        if self.task:
            task = self.task
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self.task = None
        self.station.clear_connection()

    def demo_sample(self):
        # Intentionally synthetic, never used as fallback in real mode.
        t = time.monotonic()
        current = 0.17 + 0.04 * math.sin(t * 0.7)
        self.station.connected = True
        self.station.device_name = "시뮬레이션 · 실제 발전기 아님"
        self.station.device_id = "SIMULATED"
        self.station.ingest(Reading(5.0, current))


class TransportSink:
    """Per-transport diagnostics; only the selected source reaches accounting.

    Both sinks and SQLite are mutated on the asyncio event loop. USB workers
    schedule callbacks onto that loop rather than touching Station themselves.
    """
    def __init__(self, station, mode):
        self.owner = station
        self.mode = mode
        self.connected = False
        self.device_id = None
        self.device_name = None
        self.message = "기기를 검색하고 연결하세요."
        self.last_reading = None
        self.last_at = None
        self.samples = 0

    def ingest(self, reading):
        if not (math.isfinite(reading.voltage_v) and math.isfinite(reading.current_a)
                and 0 <= reading.voltage_v <= 6 and 0 <= reading.current_a <= 1):
            self.last_at = None
            self.message = "5V USB 체험 범위를 벗어난 패킷입니다."
            if self.owner.mode == self.mode:
                self.owner.ingest(reading)  # clears the integration bridge
            return False
        self.last_reading = reading
        self.last_at = self.owner.clock()
        self.samples += 1
        self.message = "실측 데이터 수신 중"
        if self.owner.mode == self.mode:
            self.sync()
            return self.owner.ingest(reading)
        return True

    def sync(self):
        self.owner.connected = self.connected
        self.owner.device_id = self.device_id
        self.owner.device_name = self.device_name
        self.owner.message = self.message

    def clear_connection(self, message="연결 해제됨"):
        self.connected = False
        self.device_id = self.device_name = None
        self.last_at = None
        self.last_reading = None
        self.message = message
        if self.owner.mode == self.mode:
            self.owner.clear_connection(message)

    def snapshot(self, receiver):
        age = None if self.last_at is None else max(0, self.owner.clock() - self.last_at)
        reading = self.last_reading
        return {"connected": self.connected, "connecting": bool(receiver.task) and not self.connected,
                "busy": receiver.busy, "fresh": self.connected and age is not None and age <= self.owner.STALE_S,
                "sample_age_s": age, "samples": self.samples,
                "raw_packets": receiver.raw_packets, "bad_frames": receiver.bad_frames,
                "device_name": self.device_name, "message": self.message,
                "reading": None if reading is None else {"voltage_v": reading.voltage_v,
                    "current_a": reading.current_a, "power_w": reading.power_w}}


class ReceiverHub:
    def __init__(self, station):
        from .usb_receiver import USBReceiver
        self.station = station
        self.sinks = {mode: TransportSink(station, mode) for mode in ("ble", "usb")}
        self.receivers = {"ble": Receiver(self.sinks["ble"]), "usb": USBReceiver(self.sinks["usb"])}
        self.demo = Receiver(station)

    def resolve(self, transport=None):
        mode = transport or self.station.mode
        if mode not in self.receivers:
            raise StationError("USB 또는 BLE 수신 방식을 선택하세요.")
        return self.receivers[mode]

    async def scan(self, transport=None):
        return await self.resolve(transport).scan()

    async def connect(self, device_id, transport=None):
        if self.station.session and self.station.session["status"] == "running":
            # Reconnect the selected source after an unexpected drop is allowed;
            # swapping device identity inside a participant's result is not.
            previous = self.station.session["device_id"]
            if (transport or self.station.mode) == self.station.mode and device_id != previous:
                raise StationError("측정 중에는 같은 기기만 재연결할 수 있습니다.")
        await self.resolve(transport).connect(device_id)

    async def stop(self, transport=None):
        receivers = [self.resolve(transport)] if transport else list(self.receivers.values())
        if any(receiver.busy for receiver in receivers):
            raise StationError("기기 검색이 끝난 뒤 다시 시도하세요.")
        for receiver in receivers:
            await receiver.stop()

    def set_mode(self, mode):
        self.station.set_mode(mode)
        self.station.reset()
        # Keep both physical connections for comparison. Require a NEW sample
        # after switching so cached measurements cannot start a session.
        if mode in self.sinks:
            self.sinks[mode].sync()

    def state(self):
        if self.station.mode in self.sinks:
            self.sinks[self.station.mode].sync()
        return {**self.station.snapshot(), "transports": self.snapshot()}

    def snapshot(self):
        return {mode: sink.snapshot(self.receivers[mode]) for mode, sink in self.sinks.items()}

    def demo_sample(self):
        self.demo.demo_sample()
