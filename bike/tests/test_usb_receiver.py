"""HID contract and concurrent transports, without requiring a real meter."""
import asyncio
import struct
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from bike.app import create_app
from bike.measurement import Station, StationError, Store
from bike.protocol import Reading
from bike.receiver import ReceiverHub
from bike.usb_receiver import USBReceiver, command, decode_report


def report(voltage=5, current=0.2):
    data = bytearray(64)
    data[:2] = b"\xaa\x04"
    for i in range(4):
        struct.pack_into("<II", data, 2 + 15 * i, round(voltage * 100000), round(current * 100000))
    return bytes(data)


class USBProtocolTests(unittest.TestCase):
    def test_hid_commands_match_reference_including_report_id(self):
        for kind, crc in [(0x81, 0x8E), (0x82, 0x96), (0x83, 0x9E)]:
            self.assertEqual(command(kind), b"\x00\xaa" + bytes([kind]) + bytes(61) + bytes([crc]))

    def test_usb_scale_and_first_sample_only(self):
        data = bytearray(report(5.12345, 0.23456))
        struct.pack_into("<II", data, 17, 600000, 100000)
        reading = decode_report(data)
        self.assertEqual(reading, Reading(5.12345, 0.23456))
        self.assertAlmostEqual(reading.power_w, 5.12345 * 0.23456)

    def test_short_wrong_header_and_ble_frames_are_not_usb_samples(self):
        for data in [report()[:-1], b"\x00" + report(), b"\x00\x04" + report()[2:],
                     b"\xaa\x81" + bytes(62), bytes.fromhex("aa07048813c80000")]:
            self.assertIsNone(decode_report(data))


class HubTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.store = Store(":memory:")
        self.station = Station(self.store, clock=lambda: self.now)
        self.hub = ReceiverHub(self.station)
        for mode, sink in self.hub.sinks.items():
            sink.connected = True
            sink.device_id = mode + "-meter"
            sink.device_name = mode

    def tearDown(self):
        self.store.close()

    def test_both_streams_visible_but_only_selected_source_is_integrated(self):
        self.hub.set_mode("usb")
        usb, ble = self.hub.sinks["usb"], self.hub.sinks["ble"]
        usb.ingest(Reading(5, 0.2))  # 1W selected
        ble.ingest(Reading(5, 0.8))  # 4W diagnostic
        self.station.start()
        usb.ingest(Reading(5, 0.2))
        for _ in range(3):
            self.now += 1
            ble.ingest(Reading(5, 0.8))
            usb.ingest(Reading(5, 0.2))
        self.assertAlmostEqual(self.station.session["energy_wh"], 3 / 3600)
        self.assertEqual(self.station.session["mode"], "usb")
        self.assertEqual(self.hub.snapshot()["ble"]["reading"]["power_w"], 4)
        with self.assertRaises(StationError):
            self.hub.set_mode("ble")

    def test_shadow_disconnect_does_not_interrupt_primary_or_fill_its_gap(self):
        usb, ble = self.hub.sinks["usb"], self.hub.sinks["ble"]
        ble.ingest(Reading(5, 0.2))
        self.station.start()
        ble.ingest(Reading(5, 0.2))
        usb.clear_connection("USB unplugged")
        self.assertTrue(self.station.connected)
        self.now += 1
        ble.ingest(Reading(5, 0.2))
        before = self.station.session["energy_wh"]
        ble.clear_connection()
        self.now += 1
        usb.connected = True
        usb.ingest(Reading(5, 0.8))
        self.assertFalse(self.station.fresh())
        self.assertEqual(self.station.session["energy_wh"], before)
        ble.connected = True
        ble.ingest(Reading(5, 0.2))
        self.assertEqual(self.station.session["energy_wh"], before)

    def test_switch_keeps_connections_but_requires_new_sample(self):
        usb, ble = self.hub.sinks["usb"], self.hub.sinks["ble"]
        usb.ingest(Reading(5, 0.2))
        self.hub.set_mode("usb")
        self.assertTrue(self.station.connected)
        self.assertTrue(ble.connected)
        with self.assertRaises(StationError):
            self.station.start()
        usb.ingest(Reading(5, 0.2))
        self.station.start()
        self.assertEqual(self.station.session["device_id"], "usb-meter")

    def test_invalid_primary_clears_bridge_and_diagnostic_freshness(self):
        ble = self.hub.sinks["ble"]
        ble.ingest(Reading(5, 0.2))
        self.station.start()
        ble.ingest(Reading(5, 0.2))
        self.now += 1
        ble.ingest(Reading(50, 0.2))
        self.assertFalse(self.hub.snapshot()["ble"]["fresh"])
        self.now += 1
        ble.ingest(Reading(5, 0.2))
        self.assertEqual(self.station.session["energy_wh"], 0)


class FakeDevice:
    def __init__(self):
        self.closed = False
        self.writes = []
        self.reads = 0
        self.fail = False
        self.thread_ids = set()

    def touch(self):
        self.thread_ids.add(threading.get_ident())

    def open_path(self, path):
        self.touch()
        self.path = path

    def set_nonblocking(self, mode):
        self.touch()

    def write(self, data):
        self.touch()
        self.writes.append(bytes(data))
        return len(data)

    def read(self, size, timeout):
        self.touch()
        if self.fail:
            raise OSError("unplugged")
        time.sleep(0.01)
        self.reads += 1
        return list(report())

    def close(self):
        self.touch()
        self.closed = True


class FakeHid:
    def __init__(self):
        self.handle = FakeDevice()

    def enumerate(self, vid, pid):
        self.ids = (vid, pid)
        return [{"path": b"private-hid-path", "interface_number": 3}]

    def device(self):
        return self.handle


class USBLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_delivers_on_event_loop_and_closes_before_stop_returns(self):
        store = Store(":memory:")
        station = Station(store)
        hub = ReceiverHub(station)
        hub.set_mode("usb")
        usb = hub.receivers["usb"]
        hid = FakeHid()
        owner_thread = threading.get_ident()
        seen_threads = []
        original = station.ingest
        def ingest(reading):
            seen_threads.append(threading.get_ident())
            return original(reading)
        station.ingest = ingest
        try:
            with patch.object(usb, "hid", return_value=hid):
                devices = await usb.scan()
                self.assertNotIn("private-hid-path", str(devices))
                await usb.connect(devices[0]["id"])
                for _ in range(100):
                    await asyncio.sleep(0.02)
                    if station.fresh():
                        break
                self.assertTrue(station.fresh())
                station.start()
                # Wait for the periodic request even with frequent read data.
                await asyncio.sleep(1.0)
                self.assertIn(command(0x83), hid.handle.writes)
                await usb.stop()
                self.assertTrue(hid.handle.closed)
                self.assertIsNone(usb.task)
                self.assertFalse(station.connected)
                self.assertEqual(set(seen_threads), {owner_thread})
                self.assertEqual(len(hid.handle.thread_ids), 1)
                self.assertNotIn(owner_thread, hid.handle.thread_ids)
                count = len(seen_threads)
                await asyncio.sleep(0.05)
                self.assertEqual(len(seen_threads), count)
        finally:
            await usb.stop()
            store.close()

    async def test_failed_handshake_closes_handle_and_never_reports_connected(self):
        store = Store(":memory:")
        station = Station(store)
        hub = ReceiverHub(station)
        hub.set_mode("usb")
        usb = hub.receivers["usb"]
        hid = FakeHid()
        hid.handle.write = lambda data: 0
        try:
            with patch.object(usb, "hid", return_value=hid):
                rows = await usb.scan()
                await usb.connect(rows[0]["id"])
                await usb.task
                self.assertTrue(hid.handle.closed)
                self.assertFalse(station.connected)
                self.assertIsNone(usb.task)
                self.assertIn("명령 전송 실패", station.message)
        finally:
            await usb.stop()
            store.close()

    async def test_unplug_clears_connection_and_allows_reconnect(self):
        store = Store(":memory:")
        station = Station(store)
        hub = ReceiverHub(station)
        hub.set_mode("usb")
        usb = hub.receivers["usb"]
        hid = FakeHid()
        try:
            with patch.object(usb, "hid", return_value=hid):
                rows = await usb.scan()
                await usb.connect(rows[0]["id"])
                for _ in range(100):
                    await asyncio.sleep(0.02)
                    if station.fresh():
                        break
                self.assertTrue(station.fresh())
                hid.handle.fail = True
                await usb.task
                self.assertFalse(station.connected)
                self.assertIsNone(usb.task)
                self.assertTrue(hid.handle.closed)
                self.assertIn("unplugged", station.message)
                hid.handle = FakeDevice()
                await usb.connect(rows[0]["id"])
                for _ in range(100):
                    await asyncio.sleep(0.02)
                    if station.fresh():
                        break
                self.assertTrue(station.fresh())
        finally:
            await usb.stop()
            store.close()


class USBApiTests(unittest.TestCase):
    def test_usb_mode_and_independent_search_routing(self):
        with TestClient(create_app(":memory:")) as client:
            self.assertEqual(client.post("/api/mode", json={"mode": "usb"}).status_code, 200)
            self.assertEqual(client.get("/api/state").json()["mode"], "usb")
            self.assertEqual(set(client.get("/api/state").json()["transports"]), {"ble", "usb"})
            hid = FakeHid()
            with patch.object(USBReceiver, "hid", return_value=hid):
                result = client.post("/api/device/scan", json={"transport": "usb"})
                self.assertEqual(len(result.json()), 1)
                self.assertEqual(hid.ids, (0x2E3C, 0x5558))
            self.assertEqual(client.post("/api/device/scan", json={"transport": "bogus"}).status_code, 409)
            self.assertEqual(client.post("/api/session/start", json={}).status_code, 409)
