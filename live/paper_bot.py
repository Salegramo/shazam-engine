"""Paper Bot — virtual trading per engine.

v4.1 Stable: micro-trail (asymmetric, BUY vs SELL)
Entry-Only:  Step Ladder protection (configurable from UI)

Step Ladder default:
    0.10% → 0.06%
    0.18% → 0.11%
    0.28% → 0.18%
    0.40% → 0.27%
    0.55% → 0.38%
    0.75% → 0.52%
    1.00% → 0.72%
    > 1.00%: ratio-based (peak × 0.72)
"""
from __future__ import annotations
import time
import threading
from typing import Optional, Dict, Any, List, Tuple


# Default step ladder
DEFAULT_LADDER: List[Tuple[float, float]] = [
    (0.10, 0.06),
    (0.18, 0.11),
    (0.28, 0.18),
    (0.40, 0.27),
    (0.55, 0.38),
    (0.75, 0.52),
    (1.00, 0.72),
]
# Ratio for above ladder max
LADDER_OVERFLOW_RATIO = 0.72


def compute_lock_from_ladder(peak_pnl_pct: float, ladder: List[Tuple[float, float]]) -> Optional[float]:
    """Linear interpolation lock from peak.
    
    Behavior:
        peak < ladder[0].trigger: no lock (SL protects)
        peak between two triggers: linear interpolation between locks
        peak >= max trigger: lock = peak * overflow_ratio (~72%)
    
    Example with default ladder:
        Peak=0.10 → Lock=0.060 (exactly at trigger 1)
        Peak=0.14 → Lock=0.085 (interpolated: 50% between 0.10→0.18)
        Peak=0.18 → Lock=0.110 (exactly at trigger 2)
        Peak=0.50 → Lock=~0.34 (interpolated)
        Peak=1.50 → Lock=1.08 (overflow: 1.50*0.72)
    """
    if not ladder:
        return None
    sorted_ladder = sorted(ladder, key=lambda x: x[0])
    
    # Below first trigger → no lock
    if peak_pnl_pct < sorted_ladder[0][0]:
        return None
    
    # Above max trigger → overflow ratio
    max_trigger, _ = sorted_ladder[-1]
    if peak_pnl_pct >= max_trigger:
        return peak_pnl_pct * LADDER_OVERFLOW_RATIO
    
    # Find the two surrounding triggers and interpolate
    for i in range(len(sorted_ladder) - 1):
        lo_trig, lo_lock = sorted_ladder[i]
        hi_trig, hi_lock = sorted_ladder[i + 1]
        if lo_trig <= peak_pnl_pct < hi_trig:
            # Linear interpolation
            if hi_trig == lo_trig:
                return lo_lock
            ratio = (peak_pnl_pct - lo_trig) / (hi_trig - lo_trig)
            return lo_lock + ratio * (hi_lock - lo_lock)
    
    # Shouldn't reach here, but fallback to highest lock
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
        
        # Signals tracked (for reports - even if no trade was opened)
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
        """Record a signal received (for reporting), regardless of trade execution."""
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
        sl_pct: float = 1.50,
        tp_hard_pct: Optional[float] = None,
        trail_giveback: Optional[float] = None,
        max_hold_bars: int = 144,
        use_trail: bool = False,
        ladder: Optional[List[Tuple[float, float]]] = None,
        use_ladder: bool = False,
        cooldown_bars: int = 0,
        smart_reverse: bool = True,
    ):
        """Process a new entry signal.
        
        Behavior:
          - same side + open position: BRIDGE (ignore)
          - opposite side + open position:
              * if position is currently profitable AND smart_reverse: close+reverse
              * if position is currently in loss: IGNORE (let SL/ladder/trail handle exit)
          - cooldown_bars: after a position closes, ignore new entries for N bars
        
        Args:
            use_trail: v4.1 micro-trail exit
            use_ladder: Step Ladder protection (Entry-Only)
            cooldown_bars: bars to wait after close before new entry
            smart_reverse: only reverse on opposite signal if currently profitable
        """
        with self._lock:
            side = signal["side"]
            entry_price = float(signal["price"])
            entry_time = int(signal["timestamp_ms"])
            
            # ── Cooldown check (skip if too soon after last close) ──
            if cooldown_bars > 0 and self.trades:
                last_exit_time_ms = self.trades[-1]["exit_time"] * 1000
                bars_since_close = (entry_time - last_exit_time_ms) // (5 * 60 * 1000)
                if bars_since_close < cooldown_bars:
                    return  # still cooling down
            
            if self.open_position is not None:
                if self.open_position["side"] == side:
                    return  # bridge - same side, ignore
                
                # ── Smart REVERSE handling ──
                if smart_reverse:
                    # Check if current position is profitable
                    cur_price = entry_price  # the new signal price = current market
                    p = self.open_position
                    if p["side"] == "BUY":
                        cur_pnl = (cur_price - p["entry_price"]) / p["entry_price"] * 100
                    else:
                        cur_pnl = (p["entry_price"] - cur_price) / p["entry_price"] * 100
                    
                    # Only close+reverse if currently in profit
                    if cur_pnl > 0:
                        self._close_position(entry_price, entry_time, reason="REVERSE_PROFIT")
                    else:
                        # Position in loss → don't reverse, let SL/ladder/trail handle it
                        return
                else:
                    # Old behavior: always reverse on opposite signal
                    self._close_position(entry_price, entry_time, reason="REVERSE")
            
            if side == "BUY":
                tp_price = entry_price * (1 + tp_pct / 100.0)
                sl_price = entry_price * (1 - sl_pct / 100.0)
                tp_hard_price = entry_price * (1 + tp_hard_pct / 100.0) if tp_hard_pct else None
            else:
                tp_price = entry_price * (1 - tp_pct / 100.0)
                sl_price = entry_price * (1 + sl_pct / 100.0)
                tp_hard_price = entry_price * (1 - tp_hard_pct / 100.0) if tp_hard_pct else None
            
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
                "ladder": ladder or DEFAULT_LADDER,
                "peak_pnl_pct": 0.0,
                "current_lock_pct": None,  # current locked profit% (None until first trigger)
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
        """All data for the export report."""
        with self._lock:
            return {
                "engine_name": self.engine_name,
                "signals": list(self.signals_received),
                "trades": list(self.trades),
                "stats": self.get_stats_unlocked(),
            }
    
    def get_stats_unlocked(self) -> Dict[str, Any]:
        """Same as get_stats but without acquiring lock (caller must hold)."""
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
        """Check if open position should exit (assumes lock held)."""
        p = self.open_position
        if p is None:
            return
        
        side = p["side"]
        ep = p["entry_price"]
        pnl_pct = (price - ep) / ep * 100 if side == "BUY" else (ep - price) / ep * 100
        
        # Track peak
        if pnl_pct > p["peak_pnl_pct"]:
            p["peak_pnl_pct"] = pnl_pct
        
        # SL (always active as safety net)
        if pnl_pct <= -p["sl_pct"]:
            self._close_position(p["sl_price"], time_ms, reason="STOP_LOSS")
            return
        
        # TP_HARD (v4.1 only)
        if p.get("tp_hard_pct") and pnl_pct >= p["tp_hard_pct"]:
            self._close_position(p["tp_hard_price"], time_ms, reason="TP_HARD")
            return
        
        # ── EXIT STRATEGY 1: Step Ladder (Entry-Only with use_ladder=True) ──
        if p.get("use_ladder") and p.get("ladder"):
            ladder = p["ladder"]
            # Update the lock based on current peak
            new_lock = compute_lock_from_ladder(p["peak_pnl_pct"], ladder)
            if new_lock is not None:
                # Only move lock UP (never down)
                if p["current_lock_pct"] is None or new_lock > p["current_lock_pct"]:
                    p["current_lock_pct"] = new_lock
            
            # Check if price hit the current lock (came back down)
            if p["current_lock_pct"] is not None and pnl_pct <= p["current_lock_pct"]:
                lock_price = ep * (1 + p["current_lock_pct"] / 100.0) if side == "BUY" else ep * (1 - p["current_lock_pct"] / 100.0)
                self._close_position(lock_price, time_ms, reason="LADDER_LOCK")
                return
        
        # ── EXIT STRATEGY 2: Micro-trail (v4.1 stable) ──
        elif p.get("use_trail") and p.get("trail_giveback"):
            if p["peak_pnl_pct"] >= p["tp_pct"]:
                if pnl_pct <= p["peak_pnl_pct"] - p["trail_giveback"]:
                    exit_pnl = max(p["peak_pnl_pct"] - p["trail_giveback"], p["tp_pct"])
                    exit_price = ep * (1 + exit_pnl / 100.0) if side == "BUY" else ep * (1 - exit_pnl / 100.0)
                    self._close_position(exit_price, time_ms, reason="TRAIL_LOCK")
                    return
        
        # ── EXIT STRATEGY 3: Fixed TP (manual mode) ──
        else:
            if pnl_pct >= p["tp_pct"]:
                self._close_position(p["tp_price"], time_ms, reason="TAKE_PROFIT")
                return
        
        # Max hold (5m candle assumption)
        bars_held = (time_ms - p["entry_time_ms"]) // (5 * 60 * 1000)
        if bars_held >= p["max_hold_bars"]:
            self._close_position(price, time_ms, reason="MAX_HOLD")
            return
    
    def _close_position(self, exit_price: float, exit_time_ms: int, reason: str = "MANUAL"):
        """Close (assumes lock held)."""
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
            "peak_pnl_pct": p.get("peak_pnl_pct", 0.0),
        }
        self.trades.append(trade)
        self.open_position = None
