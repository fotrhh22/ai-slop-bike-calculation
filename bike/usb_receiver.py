"""FNB58 USB HID transport (hidapi), separate from BLE TLV framing.

Protocol facts cross-checked against blakelton/FNIRSI-FNB-Web-Server and
yeckel/OpenFNB58. No charge triggers or device counter reset commands.
"""
import asyncio
import hashlib
import struct
import threading
import time

from .measurement import StationError
from .protocol import Reading

VID, PID = 0x2E3C, 0x5558
REPORT_SIZE = 64


def command(kind):
    # Verified USB command trailers differ from BLE TLV checksums.
    trailer = {0x81: 0x8E, 0x82: 0x96, 0x83: 0x9E}[kind]
    return b"\x00\xaa" + bytes([kind]) + bytes(61) + bytes([trailer])


def decode_report(data):
    """Use sample 0 once per HID report, as OpenFNB58 does.

    Four packed samples have no verified per-sample host timestamps. Do not
    invent spacing or feed a burst of four points into receipt-time integration.
    USB input reports arrive without the unnumbered report-ID prefix.
    The upstream readers do not validate a data-report checksum; neither do we.
    """
    if len(data) != REPORT_SIZE or data[:2] != b"\xaa\x04":
        return None
    voltage, current = struct.unpack_from("<II", data, 2)
    return Reading(voltage / 100000, current / 100000)


class USBReceiver:
    def __init__(self, station):
        self.station = station
        self.task = None
        self.devices = {}
        self.busy = False
        self.raw_packets = 0
        self.bad_frames = 0
        self._stop = threading.Event()

    @staticmethod
    def hid():
        try:
            import hid
        except ImportError as exc:
            raise StationError("USB 패키지가 없습니다. python -m pip install -r bike/requirements.txt 를 실행하세요.") from exc
        if not hasattr(hid, "device"):
            raise StationError("hid 패키지 대신 hidapi 패키지가 필요합니다. 설치 환경을 확인하세요.")
        return hid

    async def scan(self):
        if self.task or self.busy:
            raise StationError("USB 연결을 해제한 뒤 검색하세요.")
        hid = self.hid()
        self.busy = True
        self.station.message = "FNB58 USB HID 검색 중…"
        try:
            entries = await asyncio.to_thread(hid.enumerate, VID, PID)
            self.devices = {}
            rows = []
            for entry in entries:
                path = entry["path"]
                token = hashlib.sha256(path if isinstance(path, bytes) else path.encode()).hexdigest()[:24]
                self.devices[token] = entry
                name = "FNB58 USB · HID " + str(entry.get("interface_number", "?"))
                rows.append({"id": token, "name": name})
            self.station.message = ("USB 기기를 선택하고 연결하세요." if rows else
                                    "FNB58 USB를 찾지 못했습니다. 전용 PC 포트·데이터 케이블을 확인하세요.")
            return rows
        finally:
            self.busy = False

    async def connect(self, device_id):
        if self.task or self.busy:
            raise StationError("기존 USB 연결을 해제한 뒤 연결하세요.")
        entry = self.devices.get(device_id)
        if entry is None:
            raise StationError("먼저 USB 기기를 검색하고 선택하세요.")
        hid = self.hid()
        self.raw_packets = self.bad_frames = 0
        self._stop.clear()
        self.station.message = "USB 연결 중…"
        self.task = asyncio.create_task(self._stream(hid, entry, device_id))

    async def _stream(self, hid, entry, device_id):
        loop = asyncio.get_running_loop()
        try:
            # All HID open/read/write/close operations share one worker thread.
            # Stop cooperatively, never close a handle during a blocking read.
            await asyncio.to_thread(self._worker, hid, entry, device_id, loop)
        except Exception as exc:
            self.station.clear_connection("USB 연결 오류: " + str(exc)[:240])
        finally:
            self.task = None

    def _worker(self, hid, entry, device_id, loop):
        device = hid.device()
        def send(kind):
            if device.write(command(kind)) != 65:
                raise OSError("USB HID 명령 전송 실패")
        try:
            device.open_path(entry["path"])
            device.set_nonblocking(0)
            for kind in (0x81, 0x82, 0x82):
                if self._stop.is_set():
                    return
                send(kind)
                self._stop.wait(0.05)
            loop.call_soon_threadsafe(self._opened, device_id)
            next_poll = time.monotonic() + 0.9
            while not self._stop.is_set():
                data = device.read(REPORT_SIZE, 200)
                if self._stop.is_set():
                    break
                if data:
                    loop.call_soon_threadsafe(self._received, bytes(data))
                if time.monotonic() >= next_poll:
                    send(0x83)
                    next_poll = time.monotonic() + 0.9
        finally:
            device.close()

    def _opened(self, device_id):
        if self._stop.is_set():
            return
        self.station.connected = True
        self.station.device_id = device_id
        self.station.device_name = "FNB58 USB HID"
        self.station.message = "USB 연결됨 · 측정 패킷 대기 중"

    def _received(self, data):
        if self._stop.is_set():
            return
        self.raw_packets += 1
        reading = decode_report(data)
        if reading is not None:
            self.station.ingest(reading)
        elif len(data) != REPORT_SIZE or data[:1] != b"\xaa":
            self.bad_frames += 1

    async def stop(self):
        if self.busy:
            raise StationError("USB 검색이 끝난 뒤 다시 시도하세요.")
        self._stop.set()
        if self.task:
            await self.task
        self.station.clear_connection("USB 연결 해제됨")
