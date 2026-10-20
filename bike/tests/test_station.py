import binascii
import math
import struct
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from bike.app import create_app
from bike.measurement import Station, StationError, Store
from bike.protocol import Decoder, Reading, REQUEST_INFO, START_STREAM, checksum


def packet(kind, payload):
    body = bytes([0xAA, kind, len(payload)]) + payload
    return body + bytes([binascii.crc_hqx(body, 0) & 0xFF])


class ProtocolTests(unittest.TestCase):
    def test_command_checksums(self):
        for command in (REQUEST_INFO, START_STREAM):
            self.assertEqual(checksum(command[:-1]), command[-1])

    def test_fragmented_and_concatenated_frames(self):
        frame = packet(7, struct.pack("<HH", 5000, 200))
        decoder = Decoder()
        self.assertEqual(decoder.feed(b"garbage" + frame[:4]), [])
        rows = decoder.feed(frame[4:] + frame)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].voltage_v, 5)
        self.assertAlmostEqual(rows[0].power_w, 1)

    def test_corrupted_packet_cannot_become_energy(self):
        frame = packet(7, struct.pack("<HH", 5000, 200))
        bad = bytearray(frame)
        bad[-1] ^= 1
        decoder = Decoder()
        self.assertEqual(decoder.feed(bad), [])
        self.assertEqual(decoder.feed(frame), [Reading(5, 0.2)])
        self.assertGreater(decoder.bad_frames, 0)

    def test_precise_measurement_uses_vi_not_untrusted_power_field(self):
        decoder = Decoder()
        rows = decoder.feed(packet(4, struct.pack("<iii", 50000, 2000, 999999)))
        self.assertAlmostEqual(rows[0].power_w, 1)


class StationTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.store = Store(":memory:")
        self.station = Station(self.store, clock=lambda: self.now)
        self.station.connected = True
        self.station.device_id = "meter-a"

    def tearDown(self):
        self.store.close()

    def sample(self, voltage=5, current=0.2):
        return self.station.ingest(Reading(voltage, current))

    def begin(self):
        self.sample()
        self.station.start("참가자 001")
        self.sample()

    def test_one_watt_for_three_minutes(self):
        self.begin()
        for _ in range(180):
            self.now += 1
            self.sample()
        session = self.station.snapshot()["session"]
        self.assertEqual(session["status"], "completed")
        self.assertAlmostEqual(session["energy_wh"], 0.05)
        self.assertAlmostEqual(session["energy_mwh"], 50)
        self.assertAlmostEqual(session["measured_s"], 180)
        self.assertEqual(len(self.store.history()), 1)

    def test_real_time_power_changes_are_integrated(self):
        self.begin()
        self.now += 1
        self.sample(current=0.4)
        self.assertAlmostEqual(self.station.session["energy_wh"], 1.5 / 3600)
        self.now += 1
        self.sample(current=0)
        self.assertAlmostEqual(self.station.session["energy_wh"], 2.5 / 3600)

    def test_no_prestart_or_long_gap_energy(self):
        self.sample()
        self.now += 1
        self.station.start()
        self.now += 0.5
        self.sample()
        self.assertEqual(self.station.session["energy_wh"], 0)
        self.now += 10
        self.sample()
        self.assertEqual(self.station.session["energy_wh"], 0)
        self.assertEqual(self.station.session["gap_count"], 1)
        self.now += 1
        self.sample()
        self.assertAlmostEqual(self.station.session["energy_wh"], 1 / 3600)

    def test_no_extrapolation_on_stale_or_disconnected_feed(self):
        self.begin()
        self.now += 1
        self.sample()
        expected = self.station.session["energy_wh"]
        self.now += 5
        self.assertFalse(self.station.snapshot()["fresh"])
        self.assertEqual(self.station.session["energy_wh"], expected)
        self.station.clear_connection()
        self.now += 1
        self.station.connected = True
        self.sample()
        self.assertEqual(self.station.session["energy_wh"], expected)

    def test_start_requires_fresh_sample_and_active_session_cannot_reset(self):
        with self.assertRaises(StationError):
            self.station.start()
        self.begin()
        for call in (self.station.start, self.station.reset, lambda: self.station.set_mode("demo")):
            with self.assertRaises(StationError):
                call()

    def test_deadline_clips_incoming_interval(self):
        self.begin()
        self.now += 179
        self.sample()  # gap is deliberately not estimated
        self.now += 2
        self.sample()
        self.assertAlmostEqual(self.station.session["energy_wh"], 1 / 3600)
        self.assertEqual(self.station.session["elapsed_s"], 180)
        self.now += 1
        self.sample()
        self.assertAlmostEqual(self.station.session["energy_wh"], 1 / 3600)

    def test_restart_recovers_unfinished_session_as_interrupted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "station.sqlite3"
            store = Store(path)
            station = Station(store, clock=lambda: self.now)
            station.connected = True
            station.ingest(Reading(5, 0.2))
            station.start()
            station.ingest(Reading(5, 0.2))
            self.now += 1
            station.ingest(Reading(5, 0.2))
            energy = station.session["energy_wh"]
            store.close()
            reopened = Store(path)
            row = reopened.history()[0]
            self.assertEqual(row["status"], "interrupted")
            self.assertEqual(row["energy_wh"], energy)
            reopened.close()

    def test_new_participant_does_not_erase_prior_results(self):
        self.begin()
        self.now += 1
        self.sample()
        self.station.finish()
        first_id = self.station.session["id"]
        self.station.reset()
        self.station.start("002")
        self.assertEqual(self.station.session["energy_wh"], 0)
        self.assertEqual(len(self.store.history()), 2)
        self.assertNotEqual(self.station.session["id"], first_id)

    def test_invalid_values_never_add_energy(self):
        self.begin()
        for voltage, current in [(float("nan"), 0.2), (5, float("inf")), (20, 0.2), (5, -1)]:
            self.now += 0.5
            self.assertFalse(self.sample(voltage, current))
        self.assertFalse(self.station.fresh())
        self.assertEqual(self.station.session["energy_wh"], 0)


class ApiTests(unittest.TestCase):
    def test_demo_session_persists_across_clients_and_exports(self):
        with tempfile.TemporaryDirectory() as folder:
            with TestClient(create_app(Path(folder) / "db.sqlite3")) as client:
                self.assertEqual(client.get("/").status_code, 200)
                self.assertEqual(client.post("/api/session/start", json={}).status_code, 409)
                self.assertEqual(client.post("/api/mode", json={"mode": "demo"}).status_code, 200)
                import time
                time.sleep(0.15)
                first = client.post("/api/session/start", json={"participant": "=TEST", "target_wh": 0.1})
                self.assertEqual(first.status_code, 200, first.text)
                self.assertEqual(client.post("/api/session/start", json={}).status_code, 409)
                self.assertEqual(client.post("/api/mode", json={"mode": "ble"}).status_code, 409)
                time.sleep(0.3)
                state = client.get("/api/state").json()
                self.assertGreater(state["session"]["energy_mwh"], 0)
                self.assertEqual(state["session"]["id"], first.json()["id"])
                self.assertEqual(client.post("/api/session/stop", json={}).status_code, 200)
                self.assertEqual(client.post("/api/session/reset", json={}).status_code, 200)
                rows = client.get("/api/history").json()
                self.assertEqual(rows[0]["mode"], "demo")
                self.assertGreater(rows[0]["energy_wh"], 0)
                exported = client.get("/api/export/sessions.csv").text
                self.assertIn("'=TEST", exported)
                self.assertIn("demo", exported)

    def test_cross_site_commands_are_rejected(self):
        with TestClient(create_app(":memory:")) as client:
            response = client.post("/api/mode", json={"mode": "demo"}, headers={"Origin": "https://other.test"})
            self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
