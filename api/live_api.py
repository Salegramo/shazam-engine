"""FastAPI endpoints for Shazam Live."""
from __future__ import annotations
from typing import Dict, Any
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel


def register_live_api(app, manager) -> APIRouter:
    """Register all /api/* endpoints. Returns the router."""
    router = APIRouter(prefix="/api")
    
    @router.get("/status")
    def get_status():
        return manager.get_status()
    
    @router.get("/chart")
    def get_chart(n: int = 200):
        return manager.get_chart_data(n=int(n))
    
    @router.get("/paper-stats")
    def get_paper_stats():
        return manager.get_paper_stats()
    
    class EngineSelection(BaseModel):
        engine: str  # "v41_stable" or "entry_only"
    
    @router.post("/active-engine")
    def set_active_engine(body: EngineSelection):
        result = manager.set_active_engine(body.engine)
        if not result.get("ok"):
            raise HTTPException(400, result.get("error", "failed"))
        return result
    
    class EntryOnlySettings(BaseModel):
        buy_tp_pct: float = None
        buy_sl_pct: float = None
        sell_tp_pct: float = None
        sell_sl_pct: float = None
        max_hold_bars: int = None
        enabled: bool = None
    
    @router.post("/entry-only-settings")
    def update_entry_only_settings(body: EntryOnlySettings):
        # Only forward non-None values
        settings = {k: v for k, v in body.dict().items() if v is not None}
        return manager.update_entry_only_settings(settings)
    
    class SuperTrendSettings(BaseModel):
        period: int = None
        multiplier: float = None
        offset_pct: float = None
        thickness: int = None
    
    @router.post("/supertrend-settings")
    def update_supertrend_settings(body: SuperTrendSettings):
        settings = {k: v for k, v in body.dict().items() if v is not None}
        return manager.update_supertrend_settings(settings)
    
    class PaperReset(BaseModel):
        engine: str
        initial_balance: float = 10000.0
    
    @router.post("/paper-reset")
    def paper_reset(body: PaperReset):
        return manager.reset_paper(body.engine, initial_balance=body.initial_balance)
    
    class ClosePos(BaseModel):
        engine: str
    
    @router.post("/close-position")
    def close_position(body: ClosePos):
        return manager.close_position_manual(body.engine)
    
    app.include_router(router)
    return router
