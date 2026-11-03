"""Server-owned, monotonic-time session accounting and durable local storage."""
import math
import json
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
        existing = {row[1] for row in self.db.execute('pragma table_info(sessions)')}
        for name, definition in [('campaign_entry','TEXT'),('end_reason',"TEXT NOT NULL DEFAULT ''"),('multiplier','REAL NOT NULL DEFAULT 1')]:
            if name not in existing:
                self.db.execute('ALTER TABLE sessions ADD COLUMN ' + name + ' ' + definition)
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS sync_outbox(session_id TEXT PRIMARY KEY,payload TEXT NOT NULL,acked INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS campaign_cache(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS start_intents(entry_id TEXT PRIMARY KEY,session_id TEXT NOT NULL);
        ''')
        self.db.execute(
            "UPDATE sessions SET status='interrupted', end_reason='interrupted', ended_at=?, "
            "note='Mac 수신 프로그램 종료로 중단됨. 저장된 구간만 집계.' "
            "WHERE status='running'", (utc_now(),)
        )
        for row in self.db.execute("select * from sessions where status='interrupted' and campaign_entry is not null").fetchall():
            self._enqueue(dict(row))
        self.db.commit()

    def save(self, session, sample=None):
        columns = ("id", "participant", "mode", "status", "started_at", "ended_at",
                   "duration_s", "elapsed_s", "energy_wh", "measured_s", "peak_w",
                   "target_wh", "device_id", "gap_count", "note", "campaign_entry", "end_reason", "multiplier")
        data = {**session, 'campaign_entry': json.dumps(session.get('campaign_entry')) if session.get('campaign_entry') else None}
        self.db.execute(
            "INSERT OR REPLACE INTO sessions (" + ','.join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")",
            tuple(data[key] for key in columns),
        )
        if sample:
            self.db.execute("INSERT INTO samples VALUES (?,?,?,?,?,?)", sample)
        if session['status'] != 'running':
            self._enqueue(data)
        self.db.commit()

    def _enqueue(self, session):
        entry = session.get('campaign_entry')
        if isinstance(entry,str): entry=json.loads(entry)
        if not entry or session['mode'] not in ('usb','ble'): return
        payload = {'session_id':session['id'],'entry_id':entry['id'],'raw_wh':session['energy_wh'],
          'elapsed_s':session['elapsed_s'],'measured_s':session['measured_s'],'gap_count':session['gap_count'],
          'mode':session['mode'],'end_reason':session['end_reason'] or 'interrupted',
          'started_at':session['started_at'],'ended_at':session['ended_at']}
        self.db.execute('INSERT OR IGNORE INTO sync_outbox(session_id,payload) VALUES(?,?)',
                        (session['id'],json.dumps(payload)))

    def cache(self,key,value):
        self.db.execute('INSERT OR REPLACE INTO campaign_cache VALUES(?,?)',(key,json.dumps(value)))
        self.db.commit()

    def cached(self,key,default=None):
        row=self.db.execute('select value from campaign_cache where key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default

    def intent(self,entry_id):
        self.db.execute('INSERT OR IGNORE INTO start_intents VALUES(?,?)',(entry_id,str(uuid.uuid4())))
        self.db.commit()
        return self.db.execute('select session_id from start_intents where entry_id=?',(entry_id,)).fetchone()[0]

    def forget_waiting_entry(self,entry_id):
        entries=[e for e in self.cached('entries',[]) if e['id']!=entry_id]
        self.db.execute('INSERT OR REPLACE INTO campaign_cache VALUES(?,?)',('entries',json.dumps(entries)))
        self.db.execute('DELETE FROM start_intents WHERE entry_id=?',(entry_id,))
        self.db.commit()

    def pending(self):
        return [(r[0],json.loads(r[1])) for r in self.db.execute('select session_id,payload from sync_outbox where acked=0 order by rowid')]

    def acknowledge(self,session_id):
        self.db.execute('update sync_outbox set acked=1 where session_id=?',(session_id,));self.db.commit()

    def entry_ended(self,entry_id):
        return any((json.loads(row[0]) or {}).get('id') == entry_id for row in self.db.execute(
            "select campaign_entry from sessions where status<>'running' and campaign_entry is not null"))

    def history(self, limit=30):
        rows = [dict(row) for row in self.db.execute(
            "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,)
        )]
        for row in rows:
            row['campaign_entry']=json.loads(row['campaign_entry']) if row['campaign_entry'] else None
            row['campaign_energy_wh']=row['energy_wh']*row['multiplier']
            row['progress_pct']=row['campaign_energy_wh']/row['target_wh']*100 if row['target_wh'] else None
        return rows

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

    def start(self, participant="", target_wh=None, session_id=None, campaign_entry=None):
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
            "id": session_id or str(uuid.uuid4()), "participant": participant.strip()[:80] or "이름 미입력",
            "mode": self.mode, "status": "running", "started_at": utc_now(),
            # 0 means no automatic duration limit; retain the existing DB/CSV
            # column so historical timed sessions remain readable unchanged.
            "ended_at": None, "duration_s": 0.0, "elapsed_s": 0.0,
            "energy_wh": 0.0, "measured_s": 0.0, "peak_w": 0.0,
            "target_wh": target_wh, "device_id": self.device_id, "gap_count": 0,
            "note": "연속 수신 구간의 V×A를 적분. 미수신 구간은 추정하지 않음.",
            "campaign_entry":campaign_entry,"multiplier":100,"end_reason":"",
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
        elapsed = max(0, now - self.started)
        if self.previous:
            before, old_power = self.previous
            dt = now - before
            if 0 < dt <= self.MAX_INTERVAL_S:
                session["energy_wh"] += (old_power + reading.power_w) * 0.5 * dt / 3600
                session["measured_s"] += dt
            elif dt > self.MAX_INTERVAL_S:
                session["gap_count"] += 1
        self.previous = (now, reading.power_w)
        session["elapsed_s"] = elapsed
        session["peak_w"] = max(session["peak_w"], reading.power_w)
        self.trace.append({"t": elapsed, "w": reading.power_w})
        self.trace = self.trace[-2000:]
        self.store.save(session, (session["id"], elapsed, reading.voltage_v,
                                 reading.current_a, reading.power_w, session["energy_wh"]))
        if session['target_wh'] is not None and session['energy_wh']*session['multiplier'] >= session['target_wh']:
            self.finish('completed','target_reached')
        self.tick()
        return True

    def tick(self):
        if self.session and self.session["status"] == "running":
            self.session["elapsed_s"] = max(0, self.clock() - self.started)

    def finish(self, status="completed", reason="manual_stop"):
        if not self.session or self.session["status"] != "running":
            raise StationError("진행 중인 측정이 없습니다.")
        self.session["elapsed_s"] = max(0, self.clock() - self.started)
        self.session["status"] = status
        self.session['end_reason'] = 'interrupted' if status=='interrupted' else reason
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
            session['campaign_energy_wh']=session['energy_wh']*session['multiplier']
            session['progress_pct']=session['campaign_energy_wh']/session['target_wh']*100 if session['target_wh'] else None
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
