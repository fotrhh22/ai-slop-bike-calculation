"""Server-owned, monotonic-time session accounting and durable local storage."""
import math
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .protocol import Reading


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class StationError(ValueError):
    pass


class Store:
    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, participant TEXT NOT NULL,
                mode TEXT NOT NULL, status TEXT NOT NULL,
                started_at TEXT NOT NULL, ended_at TEXT,
                duration_s REAL NOT NULL, elapsed_s REAL NOT NULL,
                energy_wh REAL NOT NULL, measured_s REAL NOT NULL,
                peak_w REAL NOT NULL, target_wh REAL,
                device_id TEXT, gap_count INTEGER NOT NULL,
                note TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS samples (
                session_id TEXT NOT NULL, elapsed_s REAL NOT NULL,
                voltage_v REAL NOT NULL, current_a REAL NOT NULL,
                power_w REAL NOT NULL, energy_wh REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS samples_session ON samples(session_id);
        """)
        self.db.execute(
            "UPDATE sessions SET status='interrupted', ended_at=?, "
            "note='Mac 수신 프로그램 종료로 중단됨. 저장된 구간만 집계.' "
            "WHERE status='running'", (utc_now(),)
        )
        self.db.commit()

    def save(self, session, sample=None):
        columns = ("id", "participant", "mode", "status", "started_at", "ended_at",
                   "duration_s", "elapsed_s", "energy_wh", "measured_s", "peak_w",
                   "target_wh", "device_id", "gap_count", "note")
        self.db.execute(
            "INSERT OR REPLACE INTO sessions VALUES (" + ",".join("?" for _ in columns) + ")",
            tuple(session[key] for key in columns),
        )
        if sample:
            self.db.execute("INSERT INTO samples VALUES (?,?,?,?,?,?)", sample)
        self.db.commit()

    def history(self, limit=30):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,)
        )]

    def samples(self, session_id):
        return [dict(row) for row in self.db.execute(
            "SELECT elapsed_s,voltage_v,current_a,power_w,energy_wh FROM samples "
            "WHERE session_id=? ORDER BY rowid", (session_id,)
        )]

    def close(self):
        self.db.close()


class Station:
    STALE_S = 2.0
    MAX_INTERVAL_S = 2.0

    def __init__(self, store, clock=time.monotonic):
        self.store = store
        self.clock = clock
        self.mode = "ble"
        self.connected = False
        self.device_id = None
        self.device_name = None
        self.message = "FNB58 Bluetooth를 켜고 기기를 검색하세요."
        self.last_reading = None
        self.last_at = None
        self.previous = None
        self.session = None
        self.started = None
        self.trace = []

    def fresh(self):
        return (self.connected and self.last_at is not None
                and self.clock() - self.last_at <= self.STALE_S)

    def set_mode(self, mode):
        if mode not in ("ble", "usb", "demo"):
            raise StationError("지원하지 않는 모드입니다.")
        if self.session and self.session["status"] == "running":
            raise StationError("측정을 종료한 뒤 모드를 변경하세요.")
        self.mode = mode
        self.clear_connection()

    def clear_connection(self, message="연결 해제됨"):
        self.connected = False
        self.device_id = None
        self.device_name = None
        self.last_reading = None
        self.last_at = None
        self.previous = None
        self.message = message

    def start(self, participant="", target_wh=None):
        self.tick()
        if self.session and self.session["status"] == "running":
            raise StationError("이미 측정 중입니다.")
        if not self.fresh():
            raise StationError("새 측정값이 수신되는 상태에서 시작하세요.")
        if target_wh is not None and (not math.isfinite(target_wh) or target_wh <= 0):
            raise StationError("목표 Wh는 0보다 큰 유한한 숫자여야 합니다.")
        self.started = self.clock()
        self.previous = None  # never bridge samples from before Start
        self.trace = []
        self.session = {
            "id": str(uuid.uuid4()), "participant": participant.strip()[:80] or "이름 미입력",
            "mode": self.mode, "status": "running", "started_at": utc_now(),
            "ended_at": None, "duration_s": 180.0, "elapsed_s": 0.0,
            "energy_wh": 0.0, "measured_s": 0.0, "peak_w": 0.0,
            "target_wh": target_wh, "device_id": self.device_id, "gap_count": 0,
            "note": "연속 수신 구간의 V×A를 적분. 미수신 구간은 추정하지 않음.",
        }
        self.store.save(self.session)
        return self.session

    def ingest(self, reading: Reading):
        # This campaign measures the 5 V USB output, not fast-charge sources.
        # A bad packet/scaling must not silently become an enormous Wh result.
        if not (math.isfinite(reading.voltage_v) and math.isfinite(reading.current_a)
                and 0 <= reading.voltage_v <= 6 and 0 <= reading.current_a <= 1):
            self.message = "5V USB 체험 범위를 벗어난 값입니다. 배선·부하·패킷 해석을 확인하세요."
            self.previous = None
            self.last_at = None
            return False
        now = self.clock()
        self.last_reading, self.last_at = reading, now
        self.message = "시뮬레이션 데이터 수신 중" if self.mode == "demo" else "실측 데이터 수신 중"
        session = self.session
        if not session or session["status"] != "running":
            return True
        elapsed = min(max(0, now - self.started), session["duration_s"])
        if self.previous:
            before, old_power = self.previous
            dt = now - before
            if 0 < dt <= self.MAX_INTERVAL_S:
                left = max(before, self.started)
                right = min(now, self.started + session["duration_s"])
                if right > left:
                    # Clip the trapezoid at the session deadline.
                    slope = (reading.power_w - old_power) / dt
                    p_left = old_power + slope * (left - before)
                    p_right = old_power + slope * (right - before)
                    session["energy_wh"] += (p_left + p_right) * 0.5 * (right - left) / 3600
                    session["measured_s"] += right - left
            elif dt > self.MAX_INTERVAL_S:
                session["gap_count"] += 1
        self.previous = (now, reading.power_w)
        session["elapsed_s"] = elapsed
        # Do not count readings strictly after the deadline as the session peak.
        if now <= self.started + session["duration_s"]:
            session["peak_w"] = max(session["peak_w"], reading.power_w)
            self.trace.append({"t": elapsed, "w": reading.power_w})
            self.trace = self.trace[-2000:]
            self.store.save(session, (session["id"], elapsed, reading.voltage_v,
                                     reading.current_a, reading.power_w, session["energy_wh"]))
        self.tick()
        return True

    def tick(self):
        if self.session and self.session["status"] == "running":
            self.session["elapsed_s"] = min(self.clock() - self.started, self.session["duration_s"])
            if self.session["elapsed_s"] >= self.session["duration_s"]:
                self.finish("completed")

    def finish(self, status="stopped"):
        if not self.session or self.session["status"] != "running":
            raise StationError("진행 중인 측정이 없습니다.")
        self.session["elapsed_s"] = min(self.clock() - self.started, self.session["duration_s"])
        self.session["status"] = status
        self.session["ended_at"] = utc_now()
        self.previous = None
        self.store.save(self.session)

    def reset(self):
        if self.session and self.session["status"] == "running":
            raise StationError("측정을 종료한 뒤 새 참가자를 준비하세요.")
        self.session = None
        self.started = None
        self.previous = None
        self.trace = []

    def snapshot(self):
        self.tick()
        session = dict(self.session) if self.session else None
        if session:
            session["energy_mwh"] = session["energy_wh"] * 1000
            session["average_w"] = (session["energy_wh"] * 3600 / session["elapsed_s"]
                                    if session["elapsed_s"] > 0 else 0)
            session["coverage_pct"] = (session["measured_s"] / session["elapsed_s"] * 100
                                       if session["elapsed_s"] > 0 else 0)
        reading = self.last_reading
        return {
            "mode": self.mode, "connected": self.connected, "fresh": self.fresh(),
            "device_name": self.device_name, "device_id": self.device_id,
            "message": self.message,
            "sample_age_s": None if self.last_at is None else max(0, self.clock() - self.last_at),
            "reading": None if reading is None else {
                "voltage_v": reading.voltage_v, "current_a": reading.current_a,
                "power_w": reading.power_w,
            },
            "session": session, "trace": self.trace,
        }
