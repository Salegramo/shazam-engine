"""Engine Manager — coordinates both engines + Binance + Paper Bots + Reports."""
from __future__ import annotations
import threading
import time
import json
import io
import zipfile
from typing import Optional, Dict, Any, List
from datetime import datetime

import pandas as pd
import numpy as np

from live.binance_provider import fetch_klines_rest, BinanceWSClient
from live.dna_builder_live import build_dna_from_candles
from live.paper_bot import PaperBot, DEFAULT_BUY_LADDER, DEFAULT_SELL_LADDER, BUY_OVERFLOW_RATIO, SELL_OVERFLOW_RATIO
from core import HybridInternalEquationMiner, HybridSignalPresetConfig
from core.supertrend import compute_supertrend


ENGINE_V41_STABLE = "v41_stable"
ENGINE_ENTRY_ONLY = "entry_only"
DISPLAY_MODE_SINGLE = "single"    # show only active engine's markers
DISPLAY_MODE_COMPARE = "compare"  # both engines emit signals; chart shows nothing




def compute_support_resistance(candles, lookback=300, n_levels=5, window=6):
    """Compute support and resistance levels from pivot points.
    
    Improved version:
        1. Smaller window=6 → captures more pivots in noisy markets
        2. ATR-based clustering tolerance (adaptive to volatility)
        3. Returns n_levels=5 per side
        4. Falls back to weaker pivots if no strong clusters
    """
    if not candles or len(candles) < window * 2 + 1:
        return {"resistance": [], "support": []}
    
    use = candles[-lookback:] if len(candles) > lookback else candles
    
    # Find pivots
    pivot_highs = []
    pivot_lows = []
    for i in range(window, len(use) - window):
        h = float(use[i]["high"])
        l = float(use[i]["low"])
        is_high = all(h >= float(use[i-j]["high"]) for j in range(1, window+1)) and \
                  all(h >= float(use[i+j]["high"]) for j in range(1, window+1))
        is_low = all(l <= float(use[i-j]["low"]) for j in range(1, window+1)) and \
                 all(l <= float(use[i+j]["low"]) for j in range(1, window+1))
        if is_high: pivot_highs.append(h)
        if is_low: pivot_lows.append(l)
    
    current_price = float(use[-1]["close"])
    
    # ATR-based clustering tolerance (adaptive to volatility)
    # Compute average true range over last 50 candles
    recent = use[-50:] if len(use) >= 50 else use
    ranges = []
    for c in recent:
        ranges.append(float(c["high"]) - float(c["low"]))
    avg_range = sum(ranges) / max(1, len(ranges))
    # Cluster tolerance: 1x ATR (smaller = more distinct levels)
    cluster_tol_abs = max(avg_range * 1.0, current_price * 0.0008)  # min 0.08%
    
    def cluster_pivots(pivots):
        """Cluster nearby pivots; return list of {price, count}."""
        if not pivots:
            return []
        sorted_p = sorted(pivots)
        clusters = []
        current_cluster = [sorted_p[0]]
        for p in sorted_p[1:]:
            if (p - current_cluster[-1]) <= cluster_tol_abs:
                current_cluster.append(p)
            else:
                clusters.append({
                    "price": sum(current_cluster) / len(current_cluster),
                    "strength": len(current_cluster),
                })
                current_cluster = [p]
        clusters.append({
            "price": sum(current_cluster) / len(current_cluster),
            "strength": len(current_cluster),
        })
        return clusters
    
    high_clusters = cluster_pivots(pivot_highs)
    low_clusters = cluster_pivots(pivot_lows)
    
    # Resistance: above current price; sort by strength desc, then proximity
    resistance = [c for c in high_clusters if c["price"] > current_price]
    resistance.sort(key=lambda c: (-c["strength"], abs(c["price"] - current_price)))
    resistance = resistance[:n_levels]
    
    # Support: below current price
    support = [c for c in low_clusters if c["price"] < current_price]
    support.sort(key=lambda c: (-c["strength"], abs(c["price"] - current_price)))
    support = support[:n_levels]
    
    return {"resistance": resistance, "support": support}


class LiveEngineManager:
    def __init__(
        self,
        symbol: str = "BTCUSDT",
        timeframe: str = "5m",
        warmup_bars: int = 500,
        preset: str = "balanced",
    ):
        self.symbol = symbol.upper()
        self.timeframe = timeframe
        self.warmup_bars = warmup_bars
        self.preset = preset
        
        self.candles: List[Dict[str, Any]] = []
        self.current_tick: Optional[Dict[str, Any]] = None
        self.dna: Optional[pd.DataFrame] = None
        self.dna_snapshot: Optional[pd.DataFrame] = None  # saved at warmup for reports
        self.mining_ready = False
        self.mining_in_progress = False
        
        # Active engine (Single mode) or both (Compare mode)
        self.active_engine: str = ENGINE_V41_STABLE
        self.display_mode: str = DISPLAY_MODE_SINGLE
        
        # Chart display toggles
        self.show_signals: bool = True  # BUY/SELL arrows on chart
        self.show_sr: bool = False     # Support/Resistance lines
        self.show_trades: bool = True   # entry/exit trade markers on chart
        self.show_tp_sl: bool = True    # TP/SL horizontal lines on chart
        
        self.preset_cfg = HybridSignalPresetConfig.from_name(preset)
        self.miner = HybridInternalEquationMiner(top_k=self.preset_cfg.top_k)
        
        # Signals (all signals from both engines, tagged)
        self.signals: List[Dict[str, Any]] = []
        # Per-engine dedup state
        self.last_signal_per_side = {
            ENGINE_V41_STABLE: {"BUY": None, "SELL": None},
            ENGINE_ENTRY_ONLY: {"BUY": None, "SELL": None},
        }
        
        # Paper bots (one per engine)
        self.paper_v41 = PaperBot(initial_balance=10000.0, engine_name="v4.1 Stable")
        self.paper_entry_only = PaperBot(initial_balance=10000.0, engine_name="Entry-Only")
        
        # Entry-Only settings
        self.entry_only_settings = {
            "exit_mode": "ladder",
            "use_ladder": True,
            "buy_ladder": [list(p) for p in DEFAULT_BUY_LADDER],
            "sell_ladder": [list(p) for p in DEFAULT_SELL_LADDER],
            "buy_overflow_ratio": BUY_OVERFLOW_RATIO,
            "sell_overflow_ratio": SELL_OVERFLOW_RATIO,
            "buy_tp_pct": 0.10,
            "buy_sl_pct": 0.40,      # tighter SL (was 1.50)
            "sell_tp_pct": 0.05,
            "sell_sl_pct": 0.30,     # tighter SL (was 0.75)
            "max_hold_bars": 144,
            "enabled": True,
            "cooldown_bars": 6,
            "smart_reverse": True,
            "exit_after_no_profit_bars": 8,   # time-stop: no progress
            "exit_after_loss_bars": 15,        # time-stop: in loss
        }
        
        # SuperTrend
        self.supertrend_settings = {
            "period": 10, "multiplier": 3.0,
            "offset_pct": 0.0, "thickness": 2,
        }
        
        self.ws_client: Optional[BinanceWSClient] = None
        self._running = False
        self._lock = threading.Lock()
        self.started_at_ms: Optional[int] = None
    
    # ---------- Lifecycle ----------
    
    def start(self):
        if self._running:
            return {"status": "already_running"}
        self._running = True
        self.started_at_ms = int(time.time() * 1000)
        
        n_to_fetch = max(self.warmup_bars + 100, 600)
        print(f"📊 Fetching {n_to_fetch} historical candles for {self.symbol} {self.timeframe}...")
        try:
            hist = fetch_klines_rest(self.symbol, self.timeframe, limit=min(n_to_fetch, 1000))
        except Exception as e:
            print(f"⚠ Failed to fetch historical: {e}")
            self._running = False
            return {"status": "error", "error": str(e)}
        
        with self._lock:
            self.candles = hist[:-1]
            if hist:
                self.current_tick = hist[-1]
        
        print(f"✓ Loaded {len(self.candles)} closed candles")
        
        threading.Thread(target=self._initial_setup, daemon=True).start()
        
        self.ws_client = BinanceWSClient(
            symbol=self.symbol, interval=self.timeframe,
            on_tick=self._on_tick, on_closed=self._on_closed,
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
        self.mining_in_progress = True
        try:
            with self._lock:
                candles_copy = list(self.candles)
            if len(candles_copy) < min(self.warmup_bars, 200):
                print(f"⚠ Not enough candles ({len(candles_copy)})")
                return
            
            print(f"🔨 Building DNA from {len(candles_copy)} candles...")
            t0 = time.time()
            dna = build_dna_from_candles(candles_copy)
            print(f"  DNA built in {time.time()-t0:.1f}s (cols={len(dna.columns)})")
            
            print(f"⛏ Mining (Multi-Window) — ~30-60s one-time...")
            t0 = time.time()
            self.miner.prepare(dna, horizon=24, win_threshold_pct=0.10)
            print(f"  Mining done in {time.time()-t0:.1f}s ({len(self.miner.mined_rules)} rules)")
            
            with self._lock:
                self.dna = dna
                self.dna_snapshot = dna.copy()  # snapshot for reports
                self.mining_ready = True
            print(f"✅ Engine ready. Scanning new candles for signals...")
        except Exception as e:
            print(f"❌ Mining error: {e}")
            import traceback; traceback.print_exc()
        finally:
            self.mining_in_progress = False
    
    # ---------- Event handlers ----------
    
    def _on_tick(self, candle: Dict[str, Any]):
        self.current_tick = candle
        with self._lock:
            self.paper_v41.update_current_price(float(candle["close"]), int(candle["open_time"]))
            self.paper_entry_only.update_current_price(float(candle["close"]), int(candle["open_time"]))
    
    def _on_closed(self, candle: Dict[str, Any]):
        with self._lock:
            if self.candles and self.candles[-1]["open_time"] == candle["open_time"]:
                self.candles[-1] = candle
            else:
                self.candles.append(candle)
                if len(self.candles) > 5000:
                    self.candles = self.candles[-5000:]
        
        if not self.mining_ready:
            return
        
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
        
        by_side = {"BUY": [], "SELL": []}
        for r in active:
            if r.side in by_side:
                by_side[r.side].append(r)
        
        price = float(closed_candle["close"])
        ts = int(closed_candle["open_time"])
        
        # In Compare mode: both engines emit signals. In Single: only active engine.
        engines_to_run = [ENGINE_V41_STABLE, ENGINE_ENTRY_ONLY] if self.display_mode == DISPLAY_MODE_COMPARE else [self.active_engine]
        
        for engine_name in engines_to_run:
            for side, rules in by_side.items():
                if not rules:
                    continue
                
                # Selection (different per engine)
                if engine_name == ENGINE_V41_STABLE:
                    top = max(rules, key=lambda r: (
                        r.rule_wr * 0.55 - r.rule_loss_rate * 3.0 + (2 if r.rule_clause_count >= 2 else 0)
                    ))
                else:  # entry_only — Progressive First-Hit
                    rules_with_w = [(self._extract_window(r), r) for r in rules]
                    rules_with_w.sort(key=lambda x: (x[0], -float(getattr(x[1], "rule_wr", 0.0))))
                    top = None
                    fallback = None
                    for w, r in rules_with_w:
                        wr = float(getattr(r, "rule_wr", 0.0))
                        if wr >= 98.0:
                            top = r; break
                        if fallback is None or wr > float(getattr(fallback, "rule_wr", 0.0)):
                            fallback = r
                    if top is None:
                        top = fallback
                if top is None:
                    continue
                
                wr = float(getattr(top, "rule_wr", 0.0))
                window = self._extract_window(top)
                confidence = self._confidence_tier(wr, int(getattr(top, "rule_signals", 0)))
                
                signal = {
                    "timestamp_ms": ts,
                    "time": ts // 1000,
                    "side": side,
                    "price": price,
                    "rule_window": window,
                    "rule_wr": wr,
                    "rule_loss_rate": float(getattr(top, "rule_loss_rate", 0.0)),
                    "rule_signals_in_window": int(getattr(top, "rule_signals", 0)),
                    "rule_formula": str(getattr(top, "formula", "")),
                    "rule_family": str(getattr(top, "rule_family", "?")),
                    "confidence": confidence,
                    "engine": engine_name,
                }
                
                # Entry-Only dedup (per engine)
                if engine_name == ENGINE_ENTRY_ONLY:
                    last = self.last_signal_per_side[engine_name].get(side)
                    if last and last["rule_formula"] == signal["rule_formula"]:
                        continue
                    self.last_signal_per_side[engine_name][side] = signal
                    opposite = "SELL" if side == "BUY" else "BUY"
                    if self.last_signal_per_side[engine_name].get(opposite):
                        self.last_signal_per_side[engine_name][opposite] = None
                
                with self._lock:
                    self.signals.append(signal)
                    if len(self.signals) > 2000:
                        self.signals = self.signals[-2000:]
                
                # Record signal in the engine's bot (for reporting)
                if engine_name == ENGINE_V41_STABLE:
                    self.paper_v41.record_signal(signal)
                else:
                    self.paper_entry_only.record_signal(signal)
                
                # Feed to bot for execution
                self._feed_signal_to_bot(signal, engine_name)
    
    def _feed_signal_to_bot(self, signal: Dict[str, Any], engine_name: str):
        if engine_name == ENGINE_V41_STABLE:
            tp = 0.10 if signal["side"] == "BUY" else 0.05
            sl = 0.40 if signal["side"] == "BUY" else 0.30   # tighter SL
            tp_hard = 1.50 if signal["side"] == "BUY" else 1.00
            self.paper_v41.handle_signal(
                signal, tp_pct=tp, sl_pct=sl, tp_hard_pct=tp_hard,
                trail_giveback=0.05 if signal["side"] == "BUY" else 0.03,
                max_hold_bars=144, use_trail=True,
                cooldown_bars=12,
                smart_reverse=True,
                exit_after_no_profit_bars=8,  # time-stop
                exit_after_loss_bars=15,
            )
        else:
            s = self.entry_only_settings
            if not s.get("enabled", True):
                return
            mode = s.get("exit_mode", "ladder")
            tp = s["buy_tp_pct"] if signal["side"] == "BUY" else s["sell_tp_pct"]
            sl = s["buy_sl_pct"] if signal["side"] == "BUY" else s["sell_sl_pct"]
            buy_ladder = [tuple(x) for x in s.get("buy_ladder", DEFAULT_BUY_LADDER)]
            sell_ladder = [tuple(x) for x in s.get("sell_ladder", DEFAULT_SELL_LADDER)]
            self.paper_entry_only.handle_signal(
                signal, tp_pct=tp, sl_pct=sl,
                max_hold_bars=int(s["max_hold_bars"]),
                use_trail=False,
                use_ladder=(mode == "ladder"),
                buy_ladder=buy_ladder,
                sell_ladder=sell_ladder,
                buy_overflow=float(s.get("buy_overflow_ratio", BUY_OVERFLOW_RATIO)),
                sell_overflow=float(s.get("sell_overflow_ratio", SELL_OVERFLOW_RATIO)),
                cooldown_bars=int(s.get("cooldown_bars", 6)),
                smart_reverse=bool(s.get("smart_reverse", True)),
                exit_after_no_profit_bars=int(s.get("exit_after_no_profit_bars", 8)),
                exit_after_loss_bars=int(s.get("exit_after_loss_bars", 15)),
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
        if wr >= 95 and sigs >= 10: return "HIGH"
        if wr >= 92 and sigs >= 7: return "MEDIUM"
        return "LOW"
    
    # ---------- Public API ----------
    
    def set_active_engine(self, engine_name: str) -> Dict[str, Any]:
        if engine_name not in (ENGINE_V41_STABLE, ENGINE_ENTRY_ONLY):
            return {"ok": False, "error": "unknown engine"}
        with self._lock:
            self.active_engine = engine_name
            for e in self.last_signal_per_side:
                self.last_signal_per_side[e] = {"BUY": None, "SELL": None}
        return {"ok": True, "active_engine": engine_name}
    
    def set_display_mode(self, mode: str) -> Dict[str, Any]:
        if mode not in (DISPLAY_MODE_SINGLE, DISPLAY_MODE_COMPARE):
            return {"ok": False, "error": "unknown mode"}
        with self._lock:
            self.display_mode = mode
        return {"ok": True, "display_mode": mode}
    
    def set_show_toggles(self, show_signals: Optional[bool] = None,
                          show_trades: Optional[bool] = None,
                          show_tp_sl: Optional[bool] = None,
                          show_sr: Optional[bool] = None) -> Dict[str, Any]:
        with self._lock:
            if show_signals is not None:
                self.show_signals = bool(show_signals)
            if show_trades is not None:
                self.show_trades = bool(show_trades)
            if show_tp_sl is not None:
                self.show_tp_sl = bool(show_tp_sl)
            if show_sr is not None:
                self.show_sr = bool(show_sr)
        return {
            "ok": True,
            "show_signals": self.show_signals,
            "show_trades": self.show_trades,
            "show_tp_sl": self.show_tp_sl,
                "show_sr": self.show_sr,
            "show_sr": self.show_sr,
        }
    
    def update_entry_only_settings(self, settings: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            allowed = ("buy_tp_pct", "buy_sl_pct", "sell_tp_pct", "sell_sl_pct",
                       "max_hold_bars", "enabled", "exit_mode", "use_ladder",
                       "buy_ladder", "sell_ladder", "buy_overflow_ratio", "sell_overflow_ratio",
                       "cooldown_bars", "smart_reverse",
                       "exit_after_no_profit_bars", "exit_after_loss_bars")
            for k in allowed:
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
                "display_mode": self.display_mode,
                "show_signals": self.show_signals,
                "show_trades": self.show_trades,
                "show_tp_sl": self.show_tp_sl,
                "show_sr": self.show_sr,
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
        with self._lock:
            candles = list(self.candles[-n:])
            current = self.current_tick
        
        candles_out = [{"time": c["open_time"]//1000, "open": c["open"], "high": c["high"],
                        "low": c["low"], "close": c["close"], "volume": c["volume"]} for c in candles]
        if current and (not candles_out or current["open_time"]//1000 > candles_out[-1]["time"]):
            candles_out.append({"time": current["open_time"]//1000, "open": current["open"],
                                "high": current["high"], "low": current["low"],
                                "close": current["close"], "volume": current["volume"]})
        
        st = self.supertrend_settings
        all_candles_for_st = candles + ([current] if current else [])
        supertrend = compute_supertrend(all_candles_for_st,
            period=int(st["period"]), multiplier=float(st["multiplier"]),
            offset_pct=float(st["offset_pct"]))[-n:]
        
        min_time = candles_out[0]["time"] if candles_out else 0
        
        # Signals + trade markers — only for ACTIVE engine (or none in compare mode)
        # Client-side toggles filter further
        with self._lock:
            if self.display_mode == DISPLAY_MODE_COMPARE:
                recent_signals = []
                trade_markers = []
            else:
                recent_signals = [s for s in self.signals
                                  if s["time"] >= min_time and s["engine"] == self.active_engine]
                active_bot = self.paper_v41 if self.active_engine == ENGINE_V41_STABLE else self.paper_entry_only
                trade_markers = active_bot.get_chart_markers(min_time=min_time)
        
        # TP/SL line: only for Entry-Only with open position in single mode
        tp_line = None
        if self.display_mode == DISPLAY_MODE_SINGLE and self.active_engine == ENGINE_ENTRY_ONLY:
            open_pos = self.paper_entry_only.get_open_position()
            if open_pos:
                tp_line = {
                    "entry_time": open_pos["entry_time"],
                    "tp_price": open_pos["tp_price"],
                    "sl_price": open_pos["sl_price"],
                    "side": open_pos["side"],
                }
        
        # Support/Resistance (only computed if toggle is on, to save CPU)
        sr_data = {"resistance": [], "support": []}
        if self.show_sr:
            sr_data = compute_support_resistance(candles, lookback=300, n_levels=5, window=6)
        
        return {
            "candles": candles_out,
            "supertrend": supertrend,
            "signals": recent_signals,
            "trade_markers": trade_markers,
            "tp_line": tp_line,
            "sr": sr_data,
        }
    
    def get_paper_stats(self) -> Dict[str, Any]:
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
        bot = self.paper_v41 if engine_name == ENGINE_V41_STABLE else self.paper_entry_only
        price = float(self.current_tick["close"]) if self.current_tick else None
        ts = int(self.current_tick["open_time"]) if self.current_tick else int(time.time()*1000)
        if price is None:
            return {"ok": False, "error": "no current price"}
        return bot.close_open_position(price, ts, reason="MANUAL")
    
    # ---------- Reports ----------
    
    def generate_report(self, engine_name: str) -> bytes:
        """Generate a full report ZIP for the engine.
        
        Contains:
          - signals.csv (all signals captured by this engine)
          - trades.csv (executed trades + outcomes)
          - equations_report.csv (per-equation aggregated stats)
          - summary.json (overall metrics)
          - live_dna_snapshot.csv (DNA at warmup, for auditing)
        """
        bot = self.paper_v41 if engine_name == ENGINE_V41_STABLE else self.paper_entry_only
        report = bot.get_report_data()
        
        signals_df = pd.DataFrame(report["signals"])
        trades_df = pd.DataFrame(report["trades"])
        
        # Equations report (aggregated by formula+side)
        equations_data = []
        if len(signals_df) > 0:
            for (formula, side), grp in signals_df.groupby(["rule_formula", "side"]):
                # Try to find trades that came from this formula
                if len(trades_df) > 0:
                    matching_trades = trades_df[(trades_df["rule_formula"] == formula) & (trades_df["side"] == side)]
                else:
                    matching_trades = pd.DataFrame()
                
                eq = {
                    "rule_formula": formula,
                    "side": side,
                    "rule_window": int(grp["rule_window"].iloc[0]) if "rule_window" in grp else 0,
                    "rule_family": grp["rule_family"].iloc[0] if "rule_family" in grp else "?",
                    "mining_signals": int(grp["rule_signals_in_window"].iloc[0]) if "rule_signals_in_window" in grp else 0,
                    "mining_wr": float(grp["rule_wr"].iloc[0]) if "rule_wr" in grp else 0.0,
                    "mining_loss_rate": float(grp["rule_loss_rate"].iloc[0]) if "rule_loss_rate" in grp else 0.0,
                    "signals_emitted_live": int(len(grp)),
                    "trades_executed": int(len(matching_trades)),
                    "trade_wins": int((matching_trades["result"] == "WIN").sum()) if len(matching_trades) > 0 else 0,
                    "trade_losses": int((matching_trades["result"] == "LOSS").sum()) if len(matching_trades) > 0 else 0,
                    "trade_avg_pnl_pct": float(matching_trades["pnl_pct"].mean()) if len(matching_trades) > 0 else 0.0,
                    "trade_total_pnl_pct": float(matching_trades["pnl_pct"].sum()) if len(matching_trades) > 0 else 0.0,
                    "confidence": grp["confidence"].iloc[0] if "confidence" in grp else "LOW",
                }
                # Estimate mining wins/losses
                eq["mining_wins"] = int(round(eq["mining_wr"]/100.0 * eq["mining_signals"]))
                eq["mining_losses"] = int(round(eq["mining_loss_rate"]/100.0 * eq["mining_signals"]))
                equations_data.append(eq)
        equations_df = pd.DataFrame(equations_data)
        if len(equations_df) > 0:
            equations_df = equations_df.sort_values(["side", "signals_emitted_live"], ascending=[True, False])
        
        # Summary
        stats = report["stats"]
        summary = {
            "engine_name": stats["engine_name"],
            "report_generated_at": datetime.utcnow().isoformat() + "Z",
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "warmup_bars": self.warmup_bars,
            "candles_loaded_at_start": len(self.candles),
            "mining_audit": self.miner.audit[0] if self.miner.audit else {},
            "started_at_ms": self.started_at_ms,
            "current_time_ms": int(time.time() * 1000),
            "uptime_minutes": ((int(time.time()*1000) - (self.started_at_ms or int(time.time()*1000))) / 60000),
            "settings": {
                "entry_only": self.entry_only_settings if engine_name == ENGINE_ENTRY_ONLY else None,
                "supertrend": self.supertrend_settings,
            },
            "performance": stats,
            "signals_breakdown": {
                "total": len(signals_df),
                "buy": int((signals_df["side"] == "BUY").sum()) if len(signals_df) > 0 else 0,
                "sell": int((signals_df["side"] == "SELL").sum()) if len(signals_df) > 0 else 0,
                "high_confidence": int((signals_df["confidence"] == "HIGH").sum()) if len(signals_df) > 0 else 0,
                "medium_confidence": int((signals_df["confidence"] == "MEDIUM").sum()) if len(signals_df) > 0 else 0,
                "low_confidence": int((signals_df["confidence"] == "LOW").sum()) if len(signals_df) > 0 else 0,
            },
            "trades_breakdown": {
                "total": len(trades_df),
                "wins": stats["wins"],
                "losses": stats["losses"],
                "neutrals": stats["neutrals"],
                "win_rate_pct": stats["win_rate_pct"],
                "exit_reasons": (
                    {k: int(v) for k, v in trades_df["reason"].value_counts().to_dict().items()}
                    if len(trades_df) > 0 else {}
                ),
                "best_trade_pct": float(trades_df["pnl_pct"].max()) if len(trades_df) > 0 else 0.0,
                "worst_trade_pct": float(trades_df["pnl_pct"].min()) if len(trades_df) > 0 else 0.0,
                "avg_win_pct": float(trades_df[trades_df["pnl_pct"] > 0]["pnl_pct"].mean()) if (trades_df["pnl_pct"] > 0).any() else 0.0,
                "avg_loss_pct": float(trades_df[trades_df["pnl_pct"] < 0]["pnl_pct"].mean()) if (trades_df["pnl_pct"] < 0).any() else 0.0,
            },
            "timing_analysis": _build_timing_analysis(trades_df),
            "entry_position_breakdown": _build_entry_position_breakdown(trades_df),
            "unique_equations": {
                "buy": int(((equations_df["side"] == "BUY")).sum()) if len(equations_df) > 0 else 0,
                "sell": int(((equations_df["side"] == "SELL")).sum()) if len(equations_df) > 0 else 0,
            },
        }
        
        # Build ZIP
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            if len(signals_df) > 0:
                zf.writestr("signals.csv", signals_df.to_csv(index=False))
            if len(trades_df) > 0:
                zf.writestr("trades.csv", trades_df.to_csv(index=False))
            if len(equations_df) > 0:
                zf.writestr("equations_report.csv", equations_df.to_csv(index=False))
            zf.writestr("summary.json", json.dumps(summary, indent=2, default=str))
            
            # DNA snapshot (for auditing) — full raw DNA matching the lab
            with self._lock:
                snap = self.dna_snapshot
            if snap is not None and len(snap) > 0:
                # All 32 raw DNA columns (matches expanded_safe_live_dna.csv from the lab)
                raw_cols = [
                    "timestamp", "source_open", "source_high", "source_low", "source_close",
                    "source_volume", "source_quote_volume", "source_trades",
                    "source_taker_buy_base", "source_taker_buy_quote",
                    "candle_return_pct", "candle_range_pct", "candle_body_pct", "candle_body_abs_pct",
                    "candle_upper_wick_ratio_pct", "candle_lower_wick_ratio_pct",
                    "candle_close_position_pct", "candle_direction_num",
                    "volume_taker_buy_ratio_pct", "volume_taker_sell_ratio_pct",
                    "demand_supply_delta", "demand_pressure_score", "supply_pressure_score",
                    "effort_volume_range_ratio", "effort_quote_volume_range_ratio",
                    "trades_per_volume", "abs_return_pct", "signed_body_to_range_pct",
                    "upper_minus_lower_wick_pct", "quote_per_trade", "base_volume_per_trade",
                    "taker_quote_ratio_pct",
                ]
                # Filter to only columns that actually exist
                present_cols = [c for c in raw_cols if c in snap.columns]
                snap_full = snap[present_cols] if present_cols else snap.iloc[:, :30]
                zf.writestr("live_dna_snapshot.csv", snap_full.to_csv(index=False))
            
            zf.writestr("README.md", _REPORT_README.format(engine=stats["engine_name"]))
        
        return buf.getvalue()



def _build_timing_analysis(trades_df):
    """How fast did signals lead to profit / peak? Were they early or late?"""
    if len(trades_df) == 0:
        return {}
    
    # Filter to trades that reached first profit
    with_first_profit = trades_df[trades_df["bars_to_first_profit"].notna()] if "bars_to_first_profit" in trades_df else trades_df.iloc[0:0]
    with_peak = trades_df[(trades_df["bars_to_peak"].notna()) & (trades_df["peak_pnl_pct"] > 0.01)] if "bars_to_peak" in trades_df else trades_df.iloc[0:0]
    
    result = {
        "trades_with_first_profit": int(len(with_first_profit)),
        "trades_with_profit_peak": int(len(with_peak)),
    }
    if len(with_first_profit) > 0:
        bars = with_first_profit["bars_to_first_profit"].astype(float)
        result["bars_to_first_profit_avg"] = float(bars.mean())
        result["bars_to_first_profit_median"] = float(bars.median())
        result["bars_to_first_profit_min"] = int(bars.min())
        result["bars_to_first_profit_max"] = int(bars.max())
    if len(with_peak) > 0:
        bars = with_peak["bars_to_peak"].astype(float)
        result["bars_to_peak_avg"] = float(bars.mean())
        result["bars_to_peak_median"] = float(bars.median())
        result["bars_to_peak_min"] = int(bars.min())
        result["bars_to_peak_max"] = int(bars.max())
        if "giveback_pct_of_peak" in with_peak:
            result["avg_giveback_pct_of_peak"] = float(with_peak["giveback_pct_of_peak"].mean())
    return result


def _build_entry_position_breakdown(trades_df):
    """Classification: EARLY / MIDDLE / LATE entries.
    
    EARLY  = peak came late in trade (we caught most of move)
    MIDDLE = peak in middle of trade
    LATE   = peak came fast (we entered near top)
    """
    if len(trades_df) == 0 or "entry_timing" not in trades_df:
        return {}
    
    counts = trades_df["entry_timing"].value_counts().to_dict()
    total = len(trades_df)
    result = {"total": total}
    for tier in ("EARLY", "MIDDLE", "LATE", "UNKNOWN"):
        n = int(counts.get(tier, 0))
        result[tier.lower()] = {
            "count": n,
            "pct": round(n / total * 100, 1) if total > 0 else 0.0,
        }
    
    # Performance per tier
    perf = {}
    for tier in ("EARLY", "MIDDLE", "LATE"):
        subset = trades_df[trades_df["entry_timing"] == tier]
        if len(subset) > 0:
            wins = (subset["pnl_pct"] > 0).sum()
            perf[tier.lower()] = {
                "trades": int(len(subset)),
                "wr": round(wins / len(subset) * 100, 1),
                "avg_pnl": round(float(subset["pnl_pct"].mean()), 4),
                "total_pnl": round(float(subset["pnl_pct"].sum()), 4),
            }
    result["performance_by_tier"] = perf
    return result


_REPORT_README = """# Shazam Live Report — {engine}

## Files

- **signals.csv**: every signal emitted by this engine (with rule details)
- **trades.csv**: actual paper trades + timing metrics
- **equations_report.csv**: aggregated per-equation stats (mining + live performance)
- **summary.json**: overall metrics, settings, timing analysis, entry position breakdown
- **live_dna_snapshot.csv**: raw DNA at warmup (32 cols, matches lab)

## Trades CSV — new columns

| Column                     | Meaning                                     |
|----------------------------|---------------------------------------------|
| bars_held                  | Total candles held                          |
| bars_to_first_profit       | When pnl first crossed +0.05%               |
| bars_to_peak               | When peak_pnl_pct was achieved              |
| peak_pnl_pct               | Best PnL% during the trade                  |
| giveback_at_exit_pct       | How much we gave back from peak (absolute)  |
| giveback_pct_of_peak       | Giveback as % of peak (e.g. 15%)            |
| entry_timing               | EARLY / MIDDLE / LATE (see below)           |

## Entry Timing Classification

Each trade is tagged based on WHERE the peak occurred within the trade:

- **EARLY** = peak came in last 25% of trade → we caught most of the move ✓
- **MIDDLE** = peak in middle 25-75% → average entry
- **LATE** = peak in first 25% (came fast) → we entered near top ✗

## Summary.json — key sections

- `timing_analysis`: avg/median bars to first profit + peak
- `entry_position_breakdown`: count + WR per timing tier
- `performance_by_tier`: which tier wins more

## Exit reasons

- `LADDER_LOCK`: Step Ladder protection triggered
- `TRAIL_LOCK`: micro-trail closed (v4.1)
- `TIME_STOP_NO_PROFIT`: 8 bars without reaching +0.05%
- `TIME_STOP_LOSS`: 15 bars while in loss
- `STOP_LOSS`: hit SL
- `TP_HARD`: hard ceiling
- `MAX_HOLD`: reached max bars
- `REVERSE_PROFIT`: closed (profitable) on opposite signal
- `MANUAL`: user closed
"""
