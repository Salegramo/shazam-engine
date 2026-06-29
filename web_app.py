"""Shazam Live v1 — Main FastAPI app (FIXED: router registered before app starts)."""
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

# Build manager early (no Binance connection yet)
SYMBOL = os.getenv("SHAZAM_SYMBOL", "BTCUSDT")
TIMEFRAME = os.getenv("SHAZAM_TIMEFRAME", "5m")
WARMUP = int(os.getenv("SHAZAM_WARMUP", "500"))

print(f"🚀 Shazam Live v1 — building manager (no connection yet)")
print(f"   symbol: {SYMBOL}, timeframe: {TIMEFRAME}, warmup: {WARMUP}")

manager = LiveEngineManager(symbol=SYMBOL, timeframe=TIMEFRAME, warmup_bars=WARMUP)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        manager.start()
        print(f"✓ Manager started (connecting to Binance)")
    except Exception as e:
        print(f"❌ Startup error: {e}")
        import traceback; traceback.print_exc()
    yield
    print(f"🛑 Shutting down")
    manager.stop()


app = FastAPI(title="Shazam Live", lifespan=lifespan)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# Register API routes BEFORE app starts (this is the fix for the 404s)
register_live_api(app, manager)

# Static files
app.mount("/static", StaticFiles(directory=str(ROOT_DIR / "static")), name="static")


@app.get("/")
def root():
    return FileResponse(str(ROOT_DIR / "templates" / "dashboard.html"))


@app.get("/health")
def health():
    return {"status": "ok"}
