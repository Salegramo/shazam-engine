"""Engine Manager — coordinates both engines + Binance + Paper Bot.

Architecture:
  Binance WS → candles buffer → DNA (rolling) → Mining (once at warmup)
                                           ↓
                              Active engine scan per candle
                                           ↓
                                    Signal → Paper Bot
"""
from __future__ import annotations
import threading
import time
from typing import Optional, Dict, Any, List, Callable
from collections import deque

import pandas as pd
import numpy as np

from live.binance_provider import fetch_klines_rest, BinanceWSClient
from live.dna_builder_live import build_dna_from_candles
from live.paper_bot import PaperBot
from core import (
    HybridInternalEquationMiner,
    HybridSignalPresetConfig,
    ShazamEntryOnlyEngine,
    EntryOnlyConfig,
)
from core.supertrend import compute_supertrend


# Engine selection
ENGINE_V41_STABLE = "v41_stable"
ENGINE_ENTRY_ONLY = "entry_only"


class LiveEngineManager:
    """Orchestrates live data + engines + paper trading."""
    
    def __init__(
        self,
        symbol: str = "BTCUSDT",
        timeframe: str = "5m",
        warmup_bars: int = 500,  # smaller than lab — for faster live start
        preset: str = "balanced",
    ):
        self.symbol = symbol.upper()
        self.timeframe = timeframe
        self.warmup_bars = warmup_bars
        self.preset = preset
        
        # State
        self.candles: List[Dict[str, Any]] = []  # buffer of closed candles
        self.current_tick: Optional[Dict[str, Any]] = None  # latest live candle (not closed)
        self.dna: Optional[pd.DataFrame] = None
        self.mining_ready = False
        self.mining_in_progress = False
        
        # Active engine (only one runs at a time)
        self.active_engine: str = ENGINE_V41_STABLE  # default
        
        # Engines (we keep instances; only the active one runs)
        self.preset_cfg = HybridSignalPresetConfig.from_name(preset)
        self.miner = HybridInternalEquationMiner(top_k=self.preset_cfg.top_k)
        
        # Signals & trades
        self.signals: List[Dict[str, Any]] = []  # recent signals (any engine)
        self.last_signal_per_side = {"BUY": None, "SELL": None}  # for dedup in entry_only
        
        # Paper bots — one per engine
        self.paper_v41 = PaperBot(initial_balance=10000.0, engine_name="v4.1 Stable")
        self.paper_entry_only = PaperBot(initial_balance=10000.0, engine_name="Entry-Only")
        
        # Entry-Only TP/SL settings (user-adjustable from UI)
        self.entry_only_settings = {
            "buy_tp_pct": 0.10,
            "buy_sl_pct": 1.50,
            "sell_tp_pct": 0.05,
            "sell_sl_pct": 0.75,
            "max_hold_bars": 144,
            "enabled": True,  # user can pause exits
        }
        
        # SuperTrend settings
        self.supertrend_settings = {
            "period": 10,
            "multiplier": 3.0,
            "offset_pct": 0.0,
            "thickness": 2,
        }
        
        # WebSocket client
        self.ws_client: Optional[BinanceWSClient] = None
        self._running = False
        self._lock = threading.Lock()
    
    # ---------- Lifecycle ----------
    
    def start(self):
        """Start: load historical, then connect WS."""
        if self._running:
            return {"status": "already_running"}
        self._running = True
        
        # 1. Fetch historical (warmup + extra for indicator)
        n_to_fetch = max(self.warmup_bars + 100, 600)
        print(f"📊 Fetching {n_to_fetch} historical candles for {self.symbol} {self.timeframe}...")
        try:
            hist = fetch_klines_rest(self.symbol, self.timeframe, limit=min(n_to_fetch, 1000))
        except Exception as e:
            print(f"⚠ Failed to fetch historical: {e}")
            self._running = False
            return {"status": "error", "error": str(e)}
        
        with self._lock:
            self.candles = hist[:-1]  # exclude the unclosed current candle
            if hist:
                self.current_tick = hist[-1]
        
        print(f"✓ Loaded {len(self.candles)} closed candles")
        
        # 2. Build initial DNA + start mining in background
        threading.Thread(target=self._initial_setup, daemon=True).start()
        
        # 3. Start WebSocket
        self.ws_client = BinanceWSClient(
            symbol=self.symbol,
            interval=self.timeframe,
            on_tick=self._on_tick,
            on_closed=self._on_closed,
        )
        self.ws_client.start()
        print(f"📡 WebSocket started for {self.symbol}@{self.timeframe}")
        
        return {"status": "started", "candles_loaded": len(self.candles)}
    
    def stop(self):
        self._running = False
        if self.ws_client:
            self.ws_client.stop()
        return {"status": "stopped"}
    
    def _initial_setup(self):
        """Build DNA + run mining (one-time, on warmup)."""
        self.mining_in_progress = True
        try:
            with self._lock:
                candles_copy = list(self.candles)
            
            if len(candles_copy) < self.warmup_bars:
                print(f"⚠ Not enough candles ({len(candles_copy)} < {self.warmup_bars})")
                self.mining_in_progress = False
                return
            
            print(f"🔨 Building DNA from {len(candles_copy)} candles...")
            t0 = time.time()
            dna = build_dna_from_candles(candles_copy)
            print(f"  DNA built in {time.time()-t0:.1f}s (cols={len(dna.columns)})")
            
            print(f"⛏ Mining (Multi-Window) — this takes ~30-60s one-time...")
            t0 = time.time()
            self.miner.prepare(dna, horizon=24, win_threshold_pct=0.10)
            print(f"  Mining done in {time.time()-t0:.1f}s ({len(self.miner.mined_rules)} rules)")
            
            with self._lock:
                self.dna = dna
                self.mining_ready = True
            
            print(f"✅ Engine ready. Scanning new candles for signals...")
        except Exception as e:
            print(f"❌ Mining error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self.mining_in_progress = False
    
    # ---------- Event handlers ----------
    
    def _on_tick(self, candle: Dict[str, Any]):
        """Live tick (not closed) — just update current_tick + paper bots' current price."""
        self.current_tick = candle
        # Update paper bots' floating PnL
        with self._lock:
            self.paper_v41.update_current_price(float(candle["close"]), int(candle["open_time"]))
            self.paper_entry_only.update_current_price(float(candle["close"]), int(candle["open_time"]))
    
    def _on_closed(self, candle: Dict[str, Any]):
        """A candle closed → append to buffer + rebuild DNA + scan signals."""
        with self._lock:
            # Avoid duplicates
            if self.candles and self.candles[-1]["open_time"] == candle["open_time"]:
                self.candles[-1] = candle
            else:
                self.candles.append(candle)
                # Keep buffer reasonable
                if len(self.candles) > 5000:
                    self.candles = self.candles[-5000:]
        
        if not self.mining_ready:
            return  # still warming up
        
        # Rebuild DNA + scan
        try:
            with self._lock:
                candles_copy = list(self.candles)
            dna = build_dna_from_candles(candles_copy)
            with self._lock:
                self.dna = dna
            self._scan_signal_at_latest_bar(candle)
        except Exception as e:
            print(f"⚠ scan error: {e}")
    
    def _scan_signal_at_latest_bar(self, closed_candle: Dict[str, Any]):
        """Run active engine on the latest closed bar — emit signal if any."""
        if self.dna is None or not self.mining_ready:
            return
        
        i = len(self.dna) - 1
        if i < 1:
            return
        
        dna_window = self.dna.iloc[max(0, i - self.warmup_bars + 1): i + 1]
        empty_expert_row = pd.Series(dtype=object)
        active = self.miner.active_rules(i, dna_window, empty_expert_row, top_k=99)
        
        if not active:
            return
        
        # Group by side
        by_side = {"BUY": [], "SELL": []}
        for r in active:
            if r.side in by_side:
                by_side[r.side].append(r)
        
        price = float(closed_candle["close"])
        ts = int(closed_candle["open_time"])
        
        for side, rules in by_side.items():
            if not rules:
                continue
            
            # Selection: differ by active engine
            if self.active_engine == ENGINE_V41_STABLE:
                # Quality-based (top by WR + score)
                top = max(rules, key=lambda r: (
                    r.rule_wr * 0.55 - r.rule_loss_rate * 3.0 + (2 if r.rule_clause_count >= 2 else 0)
                ))
            else:  # entry_only
                # Progressive First-Hit
                rules_with_w = [(self._extract_window(r), r) for r in rules]
                rules_with_w.sort(key=lambda x: (x[0], -float(getattr(x[1], "rule_wr", 0.0))))
                top = None
                fallback = None
                for w, r in rules_with_w:
                    wr = float(getattr(r, "rule_wr", 0.0))
                    if wr >= 98.0:
                        top = r
                        break
                    if fallback is None or wr > float(getattr(fallback, "rule_wr", 0.0)):
                        fallback = r
                if top is None:
                    top = fallback
            
            if top is None:
                continue
            
            # Build signal
            wr = float(getattr(top, "rule_wr", 0.0))
            window = self._extract_window(top)
            confidence = self._confidence_tier(wr, int(getattr(top, "rule_signals", 0)))
            
            signal = {
                "timestamp_ms": ts,
                "time": ts // 1000,  # seconds for chart
                "side": side,
                "price": price,
                "rule_window": window,
                "rule_wr": wr,
                "rule_formula": str(getattr(top, "formula", "")),
                "confidence": confidence,
                "engine": self.active_engine,
            }
            
            # Entry-Only dedup: same rule = bridge, skip
            if self.active_engine == ENGINE_ENTRY_ONLY:
                last = self.last_signal_per_side.get(side)
                if last and last["rule_formula"] == signal["rule_formula"]:
                    continue  # bridge, skip
                self.last_signal_per_side[side] = signal
            
            # Reset opposite side last
            opposite = "SELL" if side == "BUY" else "BUY"
            if self.last_signal_per_side.get(opposite):
                self.last_signal_per_side[opposite] = None
            
            with self._lock:
                self.signals.append(signal)
                if len(self.signals) > 500:
                    self.signals = self.signals[-500:]
            
            # Feed to active paper bot
            self._feed_signal_to_bot(signal)
    
    def _feed_signal_to_bot(self, signal: Dict[str, Any]):
        """Feed signal to the active engine's paper bot."""
        if self.active_engine == ENGINE_V41_STABLE:
            # v4.1 stable: open trade with locked exit (asymmetric)
            tp = 0.10 if signal["side"] == "BUY" else 0.05
            sl = 1.50 if signal["side"] == "BUY" else 0.75
            tp_hard = 3.00 if signal["side"] == "BUY" else 1.50
            self.paper_v41.handle_signal(
                signal, tp_pct=tp, sl_pct=sl, tp_hard_pct=tp_hard,
                trail_giveback=0.05 if signal["side"] == "BUY" else 0.03,
                max_hold_bars=144,
                use_trail=True,
            )
        else:
            # Entry-Only: use user-adjustable settings
            s = self.entry_only_settings
            if not s.get("enabled", True):
                return
            tp = s["buy_tp_pct"] if signal["side"] == "BUY" else s["sell_tp_pct"]
            sl = s["buy_sl_pct"] if signal["side"] == "BUY" else s["sell_sl_pct"]
            self.paper_entry_only.handle_signal(
                signal, tp_pct=tp, sl_pct=sl,
                max_hold_bars=int(s["max_hold_bars"]),
                use_trail=False,  # entry-only: fixed TP/SL
            )
    
    def _extract_window(self, rule) -> int:
        src = str(getattr(rule, "rule_source", ""))
        try:
            if "_W" in src:
                return int(src.rsplit("_W", 1)[-1])
        except Exception:
            pass
        return 0
    
    def _confidence_tier(self, wr: float, sigs: int) -> str:
        if wr >= 95 and sigs >= 10:
            return "HIGH"
        if wr >= 92 and sigs >= 7:
            return "MEDIUM"
        return "LOW"
    
    # ---------- Public API ----------
    
    def set_active_engine(self, engine_name: str) -> Dict[str, Any]:
        if engine_name not in (ENGINE_V41_STABLE, ENGINE_ENTRY_ONLY):
            return {"ok": False, "error": "unknown engine"}
        with self._lock:
            self.active_engine = engine_name
            # Reset dedup state on switch
            self.last_signal_per_side = {"BUY": None, "SELL": None}
        return {"ok": True, "active_engine": engine_name}
    
    def update_entry_only_settings(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            for k in ("buy_tp_pct", "buy_sl_pct", "sell_tp_pct", "sell_sl_pct", "max_hold_bars", "enabled"):
                if k in settings:
                    self.entry_only_settings[k] = settings[k]
        return {"ok": True, "settings": self.entry_only_settings}
    
    def update_supertrend_settings(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            for k in ("period", "multiplier", "offset_pct", "thickness"):
                if k in settings:
                    self.supertrend_settings[k] = settings[k]
        return {"ok": True, "settings": self.supertrend_settings}
    
    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            ws_alive = self.ws_client.is_alive() if self.ws_client else False
            last_event_age = self.ws_client.seconds_since_last_event() if self.ws_client else None
            mining_audit = {}
            if self.miner.audit:
                a = self.miner.audit[0]
                mining_audit = {
                    "mined_equations": a.get("mined_equations", 0),
                    "atom_equations": a.get("atom_equations", 0),
                    "pair_equations": a.get("pair_equations", 0),
                }
            return {
                "symbol": self.symbol,
                "timeframe": self.timeframe,
                "active_engine": self.active_engine,
                "mining_ready": self.mining_ready,
                "mining_in_progress": self.mining_in_progress,
                "candles_loaded": len(self.candles),
                "warmup_bars": self.warmup_bars,
                "current_price": float(self.current_tick["close"]) if self.current_tick else None,
                "ws_alive": ws_alive,
                "ws_last_event_age_sec": last_event_age,
                "signals_total": len(self.signals),
                "mining_audit": mining_audit,
                "entry_only_settings": self.entry_only_settings,
                "supertrend_settings": self.supertrend_settings,
            }
    
    def get_chart_data(self, n: int = 200) -> Dict[str, Any]:
        """Return candles + supertrend + signals for chart rendering."""
        with self._lock:
            candles = list(self.candles[-n:])
            current = self.current_tick
        
        candles_out = []
        for c in candles:
            candles_out.append({
                "time": c["open_time"] // 1000,
                "open": c["open"],
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
                "volume": c["volume"],
            })
        if current and (not candles_out or current["open_time"] // 1000 > candles_out[-1]["time"]):
            candles_out.append({
                "time": current["open_time"] // 1000,
                "open": current["open"],
                "high": current["high"],
                "low": current["low"],
                "close": current["close"],
                "volume": current["volume"],
            })
        
        # SuperTrend
        st = self.supertrend_settings
        all_candles_for_st = candles + ([current] if current else [])
        supertrend = compute_supertrend(
            all_candles_for_st,
            period=int(st["period"]),
            multiplier=float(st["multiplier"]),
            offset_pct=float(st["offset_pct"]),
        )
        # only the last N for the chart
        supertrend = supertrend[-n:]
        
        # Signals (only from active engine, last N candles range)
        if candles_out:
            min_time = candles_out[0]["time"]
        else:
            min_time = 0
        with self._lock:
            recent_signals = [
                {
                    "time": s["time"],
                    "side": s["side"],
                    "price": s["price"],
                    "confidence": s["confidence"],
                    "rule_window": s["rule_window"],
                    "rule_wr": s["rule_wr"],
                    "engine": s["engine"],
                }
                for s in self.signals
                if s["time"] >= min_time and s["engine"] == self.active_engine
            ]
        
        # Trade markers from active engine's paper bot
        active_bot = self.paper_v41 if self.active_engine == ENGINE_V41_STABLE else self.paper_entry_only
        trade_markers = active_bot.get_chart_markers(min_time=min_time)
        
        # Active TP line (for entry-only with open position)
        tp_line = None
        if self.active_engine == ENGINE_ENTRY_ONLY:
            open_pos = self.paper_entry_only.get_open_position()
            if open_pos:
                tp_line = {
                    "entry_time": open_pos["entry_time"],
                    "tp_price": open_pos["tp_price"],
                    "sl_price": open_pos["sl_price"],
                    "side": open_pos["side"],
                }
        
        return {
            "candles": candles_out,
            "supertrend": supertrend,
            "signals": recent_signals,
            "trade_markers": trade_markers,
            "tp_line": tp_line,
        }
    
    def get_paper_stats(self) -> Dict[str, Any]:
        """Get stats for both paper bots."""
        return {
            "v41_stable": self.paper_v41.get_stats(),
            "entry_only": self.paper_entry_only.get_stats(),
        }
    
    def reset_paper(self, engine_name: str, initial_balance: float = 10000.0) -> Dict[str, Any]:
        if engine_name == ENGINE_V41_STABLE:
            self.paper_v41.reset(initial_balance)
        elif engine_name == ENGINE_ENTRY_ONLY:
            self.paper_entry_only.reset(initial_balance)
        else:
            return {"ok": False, "error": "unknown engine"}
        return {"ok": True}
    
    def close_position_manual(self, engine_name: str) -> Dict[str, Any]:
        """User can manually close the open position (Entry-Only mainly)."""
        bot = self.paper_v41 if engine_name == ENGINE_V41_STABLE else self.paper_entry_only
        price = float(self.current_tick["close"]) if self.current_tick else None
        ts = int(self.current_tick["open_time"]) if self.current_tick else int(time.time()*1000)
        if price is None:
            return {"ok": False, "error": "no current price"}
        return bot.close_open_position(price, ts, reason="MANUAL")
