"""Paper Bot — virtual trading per engine.

EXIT STRATEGIES:
1. v4.1 Stable:    Asymmetric Micro-Trail (BUY tight, SELL tighter)
2. Entry-Only:     Step Ladder Protection (linear interpolation)
                   - Configurable per side (BUY/SELL different ladders)
                   - Time-based stop (early exit if no progress)

TRACKING (for reports):
- bars_to_first_profit: when did pnl first reach +0.05%
- bars_to_peak:         when did peak occur
- peak_pnl_pct:         actual peak achieved
- giveback_at_exit_pct: how much we gave back from peak
- entry_position:       'early' | 'middle' | 'late' (in move)
"""
from __future__ import annotations
import time
import threading
from typing import Optional, Dict, Any, List, Tuple


# Default ladders (BUY allows larger giveback for slower moves; SELL tighter)
DEFAULT_BUY_LADDER: List[Tuple[float, float]] = [
    (0.05, 0.040),   # 20% giveback (early activation)
    (0.10, 0.085),   # 15%
    (0.15, 0.130),   # 13%
    (0.22, 0.195),   # 11%
    (0.32, 0.290),   # 9%
    (0.50, 0.460),   # 8%
    (0.75, 0.700),   # 7%
    (1.00, 0.930),   # 7%
]
BUY_OVERFLOW_RATIO = 0.93  # above 1.00%

DEFAULT_SELL_LADDER: List[Tuple[float, float]] = [
    (0.05, 0.038),   # 25% giveback (tighter)
    (0.10, 0.080),   # 20%
    (0.15, 0.123),   # 18%
    (0.22, 0.181),   # 18%
    (0.32, 0.265),   # 17%
    (0.50, 0.415),   # 17%
    (0.75, 0.625),   # 17%
    (1.00, 0.850),   # 15%
]
SELL_OVERFLOW_RATIO = 0.85

# First profit threshold (activates "runner mode" + bars tracking)
FIRST_PROFIT_THRESHOLD_PCT = 0.05

# Default time-based stop config (per-side)
DEFAULT_TIME_STOPS = {
    "exit_after_no_profit_bars": 8,   # if peak < first_profit_threshold after N bars, exit
    "exit_after_loss_bars": 15,       # if pnl < 0 after N bars, exit
}


def compute_lock_from_ladder(peak_pnl_pct: float, ladder: List[Tuple[float, float]],
                              overflow_ratio: float = 0.93) -> Optional[float]:
    """Linear interpolation lock from peak.
    
    Behavior:
        peak < ladder[0].trigger: no lock (SL protects)
        peak between two triggers: linear interpolation between locks
        peak >= max trigger: lock = peak * overflow_ratio
    """
    if not ladder:
        return None
    sorted_ladder = sorted(ladder, key=lambda x: x[0])
    
    if peak_pnl_pct < sorted_ladder[0][0]:
        return None
    
    max_trigger, _ = sorted_ladder[-1]
    if peak_pnl_pct >= max_trigger:
        return peak_pnl_pct * overflow_ratio
    
    for i in range(len(sorted_ladder) - 1):
        lo_trig, lo_lock = sorted_ladder[i]
        hi_trig, hi_lock = sorted_ladder[i + 1]
        if lo_trig <= peak_pnl_pct < hi_trig:
            if hi_trig == lo_trig:
                return lo_lock
            ratio = (peak_pnl_pct - lo_trig) / (hi_trig - lo_trig)
            return lo_lock + ratio * (hi_lock - lo_lock)
    
    return sorted_ladder[-1][1]


class PaperBot:
    def __init__(self, initial_balance: float = 10000.0, engine_name: str = "engine"):
        self.engine_name = engine_name
        self.initial_balance = float(initial_balance)
        self.balance = float(initial_balance)
        self.position_size_usd = self.balance
        
        self.trades: List[Dict[str, Any]] = []
        self.open_position: Optional[Dict[str, Any]] = None
        self.current_price: Optional[float] = None
        self.current_time_ms: Optional[int] = None
        
        self.wins = 0
        self.losses = 0
        self.neutrals = 0
        
        self.signals_received: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
    
    # ---------- Public API ----------
    
    def reset(self, initial_balance: float = 10000.0):
        with self._lock:
            self.initial_balance = float(initial_balance)
            self.balance = float(initial_balance)
            self.position_size_usd = self.balance
            self.trades = []
            self.open_position = None
            self.wins = 0
            self.losses = 0
            self.neutrals = 0
            self.signals_received = []
    
    def record_signal(self, signal: Dict[str, Any]):
        with self._lock:
            self.signals_received.append(dict(signal))
            if len(self.signals_received) > 10000:
                self.signals_received = self.signals_received[-10000:]
    
    def update_current_price(self, price: float, time_ms: int):
        with self._lock:
            self.current_price = float(price)
            self.current_time_ms = int(time_ms)
            if self.open_position:
                self._check_exit(price, time_ms)
    
    def handle_signal(
        self,
        signal: Dict[str, Any],
        tp_pct: float = 0.10,
        sl_pct: float = 0.40,
        tp_hard_pct: Optional[float] = None,
        trail_giveback: Optional[float] = None,
        max_hold_bars: int = 144,
        use_trail: bool = False,
        buy_ladder: Optional[List[Tuple[float, float]]] = None,
        sell_ladder: Optional[List[Tuple[float, float]]] = None,
        buy_overflow: float = BUY_OVERFLOW_RATIO,
        sell_overflow: float = SELL_OVERFLOW_RATIO,
        use_ladder: bool = False,
        cooldown_bars: int = 0,
        smart_reverse: bool = True,
        exit_after_no_profit_bars: int = 0,   # 0 = disabled
        exit_after_loss_bars: int = 0,        # 0 = disabled
    ):
        with self._lock:
            side = signal["side"]
            entry_price = float(signal["price"])
            entry_time = int(signal["timestamp_ms"])
            
            # Cooldown
            if cooldown_bars > 0 and self.trades:
                last_exit_time_ms = self.trades[-1]["exit_time"] * 1000
                bars_since_close = (entry_time - last_exit_time_ms) // (5 * 60 * 1000)
                if bars_since_close < cooldown_bars:
                    return
            
            if self.open_position is not None:
                if self.open_position["side"] == side:
                    return  # bridge same side
                
                if smart_reverse:
                    cur_price = entry_price
                    p = self.open_position
                    if p["side"] == "BUY":
                        cur_pnl = (cur_price - p["entry_price"]) / p["entry_price"] * 100
                    else:
                        cur_pnl = (p["entry_price"] - cur_price) / p["entry_price"] * 100
                    
                    if cur_pnl > 0:
                        self._close_position(entry_price, entry_time, reason="REVERSE_PROFIT")
                    else:
                        return
                else:
                    self._close_position(entry_price, entry_time, reason="REVERSE")
            
            if side == "BUY":
                tp_price = entry_price * (1 + tp_pct / 100.0)
                sl_price = entry_price * (1 - sl_pct / 100.0)
                tp_hard_price = entry_price * (1 + tp_hard_pct / 100.0) if tp_hard_pct else None
            else:
                tp_price = entry_price * (1 - tp_pct / 100.0)
                sl_price = entry_price * (1 + sl_pct / 100.0)
                tp_hard_price = entry_price * (1 - tp_hard_pct / 100.0) if tp_hard_pct else None
            
            # Choose ladder based on side
            active_ladder = buy_ladder if side == "BUY" else sell_ladder
            active_overflow = buy_overflow if side == "BUY" else sell_overflow
            
            self.open_position = {
                "side": side,
                "entry_price": entry_price,
                "entry_time": entry_time // 1000,
                "entry_time_ms": entry_time,
                "tp_price": tp_price,
                "sl_price": sl_price,
                "tp_pct": tp_pct,
                "sl_pct": sl_pct,
                "tp_hard_price": tp_hard_price,
                "tp_hard_pct": tp_hard_pct,
                "trail_giveback": trail_giveback,
                "max_hold_bars": max_hold_bars,
                "use_trail": use_trail,
                "use_ladder": use_ladder,
                "ladder": active_ladder or (DEFAULT_BUY_LADDER if side == "BUY" else DEFAULT_SELL_LADDER),
                "overflow_ratio": active_overflow,
                "peak_pnl_pct": 0.0,
                "current_lock_pct": None,
                "first_profit_bar": None,        # bar when pnl first reached threshold
                "peak_bar": None,                 # bar of current peak
                "exit_after_no_profit_bars": exit_after_no_profit_bars,
                "exit_after_loss_bars": exit_after_loss_bars,
                "confidence": signal.get("confidence", "MEDIUM"),
                "rule_window": signal.get("rule_window", 0),
                "rule_formula": signal.get("rule_formula", ""),
                "rule_wr": signal.get("rule_wr", 0.0),
            }
    
    def get_open_position(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return dict(self.open_position) if self.open_position else None
    
    def close_open_position(self, price: float, time_ms: int, reason: str = "MANUAL") -> Dict[str, Any]:
        with self._lock:
            if self.open_position is None:
                return {"ok": False, "error": "no open position"}
            self._close_position(price, time_ms, reason=reason)
            return {"ok": True}
    
    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            total_trades = len(self.trades)
            wr = (self.wins / max(1, self.wins + self.losses)) * 100 if (self.wins + self.losses) > 0 else 0.0
            
            floating_pnl_pct = 0.0
            floating_pnl_usd = 0.0
            if self.open_position and self.current_price is not None:
                ep = self.open_position["entry_price"]
                cp = self.current_price
                if self.open_position["side"] == "BUY":
                    floating_pnl_pct = (cp - ep) / ep * 100
                else:
                    floating_pnl_pct = (ep - cp) / ep * 100
                floating_pnl_usd = self.position_size_usd * floating_pnl_pct / 100.0
            
            total_pnl_usd = self.balance - self.initial_balance
            total_pnl_pct = (total_pnl_usd / self.initial_balance) * 100 if self.initial_balance > 0 else 0
            
            return {
                "engine_name": self.engine_name,
                "initial_balance": self.initial_balance,
                "balance": self.balance,
                "total_pnl_usd": total_pnl_usd,
                "total_pnl_pct": total_pnl_pct,
                "trades_total": total_trades,
                "wins": self.wins,
                "losses": self.losses,
                "neutrals": self.neutrals,
                "win_rate_pct": wr,
                "signals_received_count": len(self.signals_received),
                "has_open_position": self.open_position is not None,
                "open_position": dict(self.open_position) if self.open_position else None,
                "floating_pnl_pct": floating_pnl_pct,
                "floating_pnl_usd": floating_pnl_usd,
                "recent_trades": self.trades[-20:],
            }
    
    def get_chart_markers(self, min_time: int = 0) -> List[Dict[str, Any]]:
        with self._lock:
            markers = []
            for t in self.trades:
                if t["entry_time"] >= min_time:
                    markers.append({
                        "time": t["entry_time"], "type": "entry",
                        "side": t["side"], "price": t["entry_price"],
                    })
                    markers.append({
                        "time": t["exit_time"], "type": "exit",
                        "side": t["side"], "price": t["exit_price"],
                        "pnl_pct": t["pnl_pct"], "reason": t["reason"],
                    })
            if self.open_position and self.open_position["entry_time"] >= min_time:
                markers.append({
                    "time": self.open_position["entry_time"],
                    "type": "entry", "side": self.open_position["side"],
                    "price": self.open_position["entry_price"], "open": True,
                })
            return markers
    
    def get_report_data(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "engine_name": self.engine_name,
                "signals": list(self.signals_received),
                "trades": list(self.trades),
                "stats": self.get_stats_unlocked(),
            }
    
    def get_stats_unlocked(self) -> Dict[str, Any]:
        total_trades = len(self.trades)
        wr = (self.wins / max(1, self.wins + self.losses)) * 100 if (self.wins + self.losses) > 0 else 0.0
        total_pnl_usd = self.balance - self.initial_balance
        total_pnl_pct = (total_pnl_usd / self.initial_balance) * 100 if self.initial_balance > 0 else 0
        return {
            "engine_name": self.engine_name,
            "initial_balance": self.initial_balance,
            "balance": self.balance,
            "total_pnl_usd": total_pnl_usd,
            "total_pnl_pct": total_pnl_pct,
            "trades_total": total_trades,
            "wins": self.wins, "losses": self.losses, "neutrals": self.neutrals,
            "win_rate_pct": wr,
            "signals_received_count": len(self.signals_received),
        }
    
    # ---------- Internal ----------
    
    def _check_exit(self, price: float, time_ms: int):
        p = self.open_position
        if p is None:
            return
        
        side = p["side"]
        ep = p["entry_price"]
        pnl_pct = (price - ep) / ep * 100 if side == "BUY" else (ep - price) / ep * 100
        
        bars_held = (time_ms - p["entry_time_ms"]) // (5 * 60 * 1000)
        
        # Track first profit
        if p["first_profit_bar"] is None and pnl_pct >= FIRST_PROFIT_THRESHOLD_PCT:
            p["first_profit_bar"] = bars_held
        
        # Track peak
        if pnl_pct > p["peak_pnl_pct"]:
            p["peak_pnl_pct"] = pnl_pct
            p["peak_bar"] = bars_held
        
        # SL (always active)
        if pnl_pct <= -p["sl_pct"]:
            self._close_position(p["sl_price"], time_ms, reason="STOP_LOSS")
            return
        
        # TP_HARD (v4.1)
        if p.get("tp_hard_pct") and pnl_pct >= p["tp_hard_pct"]:
            self._close_position(p["tp_hard_price"], time_ms, reason="TP_HARD")
            return
        
        # Time-based stops
        if p.get("exit_after_no_profit_bars", 0) > 0:
            if bars_held >= p["exit_after_no_profit_bars"] and p["peak_pnl_pct"] < FIRST_PROFIT_THRESHOLD_PCT:
                self._close_position(price, time_ms, reason="TIME_STOP_NO_PROFIT")
                return
        if p.get("exit_after_loss_bars", 0) > 0:
            if bars_held >= p["exit_after_loss_bars"] and pnl_pct < 0:
                self._close_position(price, time_ms, reason="TIME_STOP_LOSS")
                return
        
        # ── Step Ladder ──
        if p.get("use_ladder") and p.get("ladder"):
            ladder = p["ladder"]
            overflow = p.get("overflow_ratio", 0.93)
            new_lock = compute_lock_from_ladder(p["peak_pnl_pct"], ladder, overflow_ratio=overflow)
            if new_lock is not None:
                if p["current_lock_pct"] is None or new_lock > p["current_lock_pct"]:
                    p["current_lock_pct"] = new_lock
            
            if p["current_lock_pct"] is not None and pnl_pct <= p["current_lock_pct"]:
                lock_price = ep * (1 + p["current_lock_pct"] / 100.0) if side == "BUY" else ep * (1 - p["current_lock_pct"] / 100.0)
                self._close_position(lock_price, time_ms, reason="LADDER_LOCK")
                return
        
        # ── Micro-trail (v4.1) ──
        elif p.get("use_trail") and p.get("trail_giveback"):
            if p["peak_pnl_pct"] >= p["tp_pct"]:
                if pnl_pct <= p["peak_pnl_pct"] - p["trail_giveback"]:
                    exit_pnl = max(p["peak_pnl_pct"] - p["trail_giveback"], p["tp_pct"])
                    exit_price = ep * (1 + exit_pnl / 100.0) if side == "BUY" else ep * (1 - exit_pnl / 100.0)
                    self._close_position(exit_price, time_ms, reason="TRAIL_LOCK")
                    return
        
        # ── Fixed TP ──
        else:
            if pnl_pct >= p["tp_pct"]:
                self._close_position(p["tp_price"], time_ms, reason="TAKE_PROFIT")
                return
        
        # Max hold (always last resort)
        if bars_held >= p["max_hold_bars"]:
            self._close_position(price, time_ms, reason="MAX_HOLD")
            return
    
    def _close_position(self, exit_price: float, exit_time_ms: int, reason: str = "MANUAL"):
        p = self.open_position
        if p is None:
            return
        
        side = p["side"]
        ep = p["entry_price"]
        pnl_pct = (exit_price - ep) / ep * 100 if side == "BUY" else (ep - exit_price) / ep * 100
        pnl_usd = self.position_size_usd * pnl_pct / 100.0
        self.balance += pnl_usd
        
        result = "WIN" if pnl_pct > 0.01 else ("LOSS" if pnl_pct < -0.01 else "NEUTRAL")
        if result == "WIN":
            self.wins += 1
        elif result == "LOSS":
            self.losses += 1
        else:
            self.neutrals += 1
        
        # Calculate giveback at exit
        peak = p.get("peak_pnl_pct", 0.0)
        giveback_at_exit = max(0.0, peak - pnl_pct)
        giveback_pct_of_peak = (giveback_at_exit / peak * 100) if peak > 0.001 else 0.0
        
        # Bars metrics
        bars_held = (exit_time_ms - p["entry_time_ms"]) // (5 * 60 * 1000)
        first_profit_bar = p.get("first_profit_bar")
        peak_bar = p.get("peak_bar", 0)
        
        # Entry timing classification (early/middle/late)
        # If peak happened in first 25% of hold → entry was LATE (peak was near entry)
        # If peak happened in middle 25-75% → entry was MIDDLE
        # If peak happened in last 25%+ → entry was EARLY (peak after long ride)
        if bars_held > 0 and peak_bar is not None:
            peak_position = peak_bar / max(1, bars_held)
            if peak_position < 0.25:
                entry_timing = "LATE"   # peak came fast → we entered near top of move
            elif peak_position < 0.75:
                entry_timing = "MIDDLE"
            else:
                entry_timing = "EARLY"  # peak came late → we caught most of move
        else:
            entry_timing = "UNKNOWN"
        
        trade = {
            "side": side,
            "entry_time": p["entry_time"],
            "exit_time": exit_time_ms // 1000,
            "entry_price": ep,
            "exit_price": exit_price,
            "pnl_pct": pnl_pct,
            "pnl_usd": pnl_usd,
            "reason": reason,
            "result": result,
            "confidence": p.get("confidence"),
            "rule_window": p.get("rule_window"),
            "rule_formula": p.get("rule_formula"),
            "rule_wr": p.get("rule_wr"),
            "peak_pnl_pct": peak,
            "bars_held": int(bars_held),
            "bars_to_first_profit": first_profit_bar,
            "bars_to_peak": peak_bar,
            "giveback_at_exit_pct": giveback_at_exit,
            "giveback_pct_of_peak": giveback_pct_of_peak,
            "entry_timing": entry_timing,
        }
        self.trades.append(trade)
        self.open_position = None
