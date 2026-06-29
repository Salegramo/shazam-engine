"""Shazam Live v1 — Main FastAPI app.

Serves:
- / → dashboard.html
- /api/* → live trading APIs
- /static/* → CSS/JS
"""
from __future__ import annotations
import os
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from live import LiveEngineManager
from api import register_live_api


ROOT_DIR = Path(__file__).resolve().parent

manager: LiveEngineManager = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global manager
    symbol = os.getenv("SHAZAM_SYMBOL", "BTCUSDT")
    timeframe = os.getenv("SHAZAM_TIMEFRAME", "5m")
    warmup = int(os.getenv("SHAZAM_WARMUP", "500"))
    
    print(f"🚀 Initializing Shazam Live v1")
    print(f"   symbol: {symbol}, timeframe: {timeframe}, warmup: {warmup}")
    
    try:
        manager = LiveEngineManager(
            symbol=symbol,
            timeframe=timeframe,
            warmup_bars=warmup,
        )
        register_live_api(app, manager)
        manager.start()
        print(f"✓ Manager started")
    except Exception as e:
        print(f"❌ Startup error: {e}")
        import traceback; traceback.print_exc()
    
    yield
    
    print(f"🛑 Shutting down")
    if manager:
        manager.stop()


app = FastAPI(title="Shazam Live", lifespan=lifespan)

# CORS (open for now)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files
app.mount("/static", StaticFiles(directory=str(ROOT_DIR / "static")), name="static")


@app.get("/")
def root():
    return FileResponse(str(ROOT_DIR / "templates" / "dashboard.html"))


@app.get("/health")
def health():
    if manager is None:
        return JSONResponse({"status": "starting"}, status_code=503)
    return {"status": "ok"}
