"""Run locally: python -m bike --host 0.0.0.0 --port 8765"""
import asyncio
import csv
import io
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .measurement import Station, StationError, Store
from .receiver import ReceiverHub

STATIC = Path(__file__).parent / "static"


def create_app(db_path=None):
    @asynccontextmanager
    async def lifespan(app):
        store = Store(db_path or os.environ.get("BIKE_DB_PATH", str(Path(__file__).parent / "data/station.sqlite3")))
        station = Station(store)
        receiver = ReceiverHub(station)
        app.state.station, app.state.receiver = station, receiver

        async def heartbeat():
            while True:
                if station.mode == "demo":
                    receiver.demo_sample()
                station.tick()
                await asyncio.sleep(0.1)

        task = asyncio.create_task(heartbeat())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await receiver.stop()
            if station.session and station.session["status"] == "running":
                station.finish("interrupted")
            store.close()

    app = FastAPI(title="자전거 발전 측정소", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def local_controls(request: Request, call_next):
        # Same-origin browser controls on a trusted event LAN. No CORS.
        # Reject cross-site form posts instead of allowing arbitrary websites
        # to switch modes or start/reset a shared station.
        if request.method == "POST":
            origin = request.headers.get("origin")
            host = request.headers.get("host")
            if ((origin and urlsplit(origin).netloc != host)
                    or request.headers.get("sec-fetch-site") == "cross-site"
                    or request.headers.get("content-type", "").split(";")[0] != "application/json"):
                return JSONResponse({"detail": "같은 측정소 화면에서 조작하세요."}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(StationError)
    async def station_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    async def state():
        # All state mutations run on the asyncio event loop, not worker threads.
        return app.state.receiver.state()

    # async endpoints below intentionally do not await while changing session
    # state; simultaneous iPad Start requests are serialized by the event loop.
    @app.get("/api/history")
    async def history():
        return app.state.station.store.history()

    @app.post("/api/device/scan")
    async def scan(body: TransportBody):
        try:
            return await app.state.receiver.scan(body.transport)
        except StationError:
            raise
        except Exception as exc:
            raise StationError("기기 검색 실패: " + str(exc)[:240])

    @app.post("/api/device/connect")
    async def connect(body: ConnectionBody):
        await app.state.receiver.connect(body.device_id, body.transport)
        return {"ok": True}

    @app.post("/api/device/disconnect")
    async def disconnect(body: TransportBody):
        station = app.state.station
        if station.session and station.session["status"] == "running":
            raise StationError("먼저 현재 측정을 종료하세요.")
        await app.state.receiver.stop(body.transport)
        return {"ok": True}

    @app.post("/api/mode")
    async def mode(body: ModeBody):
        station = app.state.station
        if station.session and station.session["status"] == "running":
            raise StationError("먼저 현재 측정을 종료하세요.")
        if body.mode not in ("ble", "usb", "demo"):
            raise StationError("지원하지 않는 모드입니다.")
        app.state.receiver.set_mode(body.mode)
        return {"ok": True}

    @app.post("/api/session/start")
    async def start(body: StartBody):
        return app.state.station.start(body.participant, body.target_wh)

    @app.post("/api/session/stop")
    async def stop():
        station = app.state.station
        station.tick()
        if station.session and station.session["status"] == "running":
            station.finish()
        return {"ok": True}

    @app.post("/api/session/reset")
    async def reset():
        app.state.station.reset()
        return {"ok": True}

    @app.get("/api/export/sessions.csv")
    async def export_sessions():
        rows = app.state.station.store.history(limit=100000)
        return csv_response(rows, "bike-sessions.csv")

    @app.get("/api/export/{session_id}.csv")
    async def export_samples(session_id: str):
        rows = app.state.station.store.samples(session_id)
        return csv_response(rows, "bike-samples.csv")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


class StartBody(BaseModel):
    participant: str = Field(default="", max_length=80)
    target_wh: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False)


class TransportBody(BaseModel):
    transport: Optional[str] = None


class ConnectionBody(TransportBody):
    device_id: str = Field(min_length=1, max_length=100)


class ModeBody(BaseModel):
    mode: str


def csv_response(rows, filename):
    output = io.StringIO()
    output.write("\ufeff")
    if rows:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            # Avoid spreadsheet formula execution in user-entered names.
            writer.writerow({key: ("'" + value if isinstance(value, str)
                                   and value.startswith(("=", "+", "-", "@", "\t", "\r")) else value)
                             for key, value in row.items()})
    return Response(output.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="' + filename + '"'})


app = create_app()
