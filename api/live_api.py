"""FastAPI endpoints for Shazam Live.

IMPORTANT: Pydantic v2 requires BaseModel classes at module level (not nested inside functions).
Nested classes cause: NameError + PydanticUndefinedAnnotation on forward-ref resolution.
"""
from __future__ import annotations
from typing import List, Optional
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel


# ─── All request models at module level ───

class EngineSelection(BaseModel):
    engine: str


class DisplayMode(BaseModel):
    mode: str  # "single" or "compare"


class ShowMarkers(BaseModel):
    show: bool


class EntryOnlySettings(BaseModel):
    buy_tp_pct: Optional[float] = None
    buy_sl_pct: Optional[float] = None
    sell_tp_pct: Optional[float] = None
    sell_sl_pct: Optional[float] = None
    max_hold_bars: Optional[int] = None
    enabled: Optional[bool] = None
    exit_mode: Optional[str] = None
    use_ladder: Optional[bool] = None
    ladder: Optional[List[List[float]]] = None


class SuperTrendSettings(BaseModel):
    period: Optional[int] = None
    multiplier: Optional[float] = None
    offset_pct: Optional[float] = None
    thickness: Optional[int] = None


class PaperReset(BaseModel):
    engine: str
    initial_balance: float = 10000.0


class ClosePos(BaseModel):
    engine: str


def register_live_api(app, manager) -> APIRouter:
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
    
    @router.post("/active-engine")
    def set_active_engine(body: EngineSelection):
        result = manager.set_active_engine(body.engine)
        if not result.get("ok"):
            raise HTTPException(400, result.get("error", "failed"))
        return result
    
    @router.post("/display-mode")
    def set_display_mode(body: DisplayMode):
        result = manager.set_display_mode(body.mode)
        if not result.get("ok"):
            raise HTTPException(400, result.get("error", "failed"))
        return result
    
    @router.post("/show-markers")
    def set_show_markers(body: ShowMarkers):
        return manager.set_show_markers(body.show)
    
    @router.post("/entry-only-settings")
    def update_entry_only_settings(body: EntryOnlySettings):
        settings = {k: v for k, v in body.dict().items() if v is not None}
        return manager.update_entry_only_settings(settings)
    
    @router.post("/supertrend-settings")
    def update_supertrend_settings(body: SuperTrendSettings):
        settings = {k: v for k, v in body.dict().items() if v is not None}
        return manager.update_supertrend_settings(settings)
    
    @router.post("/paper-reset")
    def paper_reset(body: PaperReset):
        return manager.reset_paper(body.engine, initial_balance=body.initial_balance)
    
    @router.post("/close-position")
    def close_position(body: ClosePos):
        return manager.close_position_manual(body.engine)
    
    @router.get("/report/{engine_name}")
    def get_report(engine_name: str):
        if engine_name not in ("v41_stable", "entry_only"):
            raise HTTPException(404, "unknown engine")
        data = manager.generate_report(engine_name)
        from datetime import datetime
        filename = f"shazam_report_{engine_name}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.zip"
        return Response(
            content=data,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    
    app.include_router(router)
    return router
