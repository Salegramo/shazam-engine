"""Paper Bot — virtual trading per engine.

Supports:
  - v4.1 stable: locked exit (TP/SL/trail/max_hold) — fully automated
  - Entry-Only: fixed TP/SL (user-adjustable) — manual close option

Tracks:
  - balance, total_pnl_pct, total_pnl_usd
  - wins, losses, neutrals
  - chart markers (entry, exit)
  - open position (with floating PnL)
"""
from __future__ import annotations
import time
from typing import Optional, Dict, Any, List
import threading


class PaperBot:
    def __init__(self, initial_balance: float = 10000.0, engine_name: str = "engine"):
        self.engine_name = engine_name
        self.initial_balance = float(initial_balance)
        self.balance = float(initial_balance)
        self.position_size_usd = self.balance  # use full balance per trade (paper)
        
        # Trades history
        self.trades: List[Dict[str, Any]] = []
        
        # Current open position
        self.open_position: Optional[Dict[str, Any]] = None
        
        # Current market state (updated on every tick)
        self.current_price: Optional[float] = None
        self.current_time_ms: Optional[int] = None
        
        # Stats
        self.wins = 0
        self.losses = 0
        self.neutrals = 0
        
        # Lock
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
    
    def update_current_price(self, price: float, time_ms: int):
        """Called on every tick. Updates current price + checks exit conditions."""
        with self._lock:
            self.current_price = float(price)
            self.current_time_ms = int(time_ms)
            
            # Check if open position should exit (TP/SL/trail/max_hold)
            if self.open_position:
                self._check_exit(price, time_ms)
    
    def handle_signal(
        self,
        signal: Dict[str, Any],
        tp_pct: float,
        sl_pct: float,
        tp_hard_pct: Optional[float] = None,
        trail_giveback: Optional[float] = None,
        max_hold_bars: int = 144,
        use_trail: bool = False,
    ):
        """Process a new entry signal.
        
        If there's an open position with the OPPOSITE side, close it first then open new.
        If same side, ignore (bridge).
        """
        with self._lock:
            side = signal["side"]
            entry_price = float(signal["price"])
            entry_time = int(signal["timestamp_ms"])
            
            # If open position exists
            if self.open_position is not None:
                if self.open_position["side"] == side:
                    # Same side: ignore (this is a bridge in entry-only)
                    return
                else:
                    # Opposite side: close current first
                    self._close_position(entry_price, entry_time, reason="REVERSE")
            
            # Open new position
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
                "peak_pnl_pct": 0.0,
                "confidence": signal.get("confidence", "MEDIUM"),
                "rule_window": signal.get("rule_window", 0),
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
            
            # Floating PnL for open position
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
                "has_open_position": self.open_position is not None,
                "open_position": dict(self.open_position) if self.open_position else None,
                "floating_pnl_pct": floating_pnl_pct,
                "floating_pnl_usd": floating_pnl_usd,
                "recent_trades": self.trades[-20:],
            }
    
    def get_chart_markers(self, min_time: int = 0) -> List[Dict[str, Any]]:
        """Return entry/exit markers for chart."""
        with self._lock:
            markers = []
            for t in self.trades:
                if t["entry_time"] >= min_time:
                    markers.append({
                        "time": t["entry_time"],
                        "type": "entry",
                        "side": t["side"],
                        "price": t["entry_price"],
                    })
                    markers.append({
                        "time": t["exit_time"],
                        "type": "exit",
                        "side": t["side"],
                        "price": t["exit_price"],
                        "pnl_pct": t["pnl_pct"],
                        "reason": t["reason"],
                    })
            # Include current open position entry
            if self.open_position and self.open_position["entry_time"] >= min_time:
                markers.append({
                    "time": self.open_position["entry_time"],
                    "type": "entry",
                    "side": self.open_position["side"],
                    "price": self.open_position["entry_price"],
                    "open": True,
                })
            return markers
    
    # ---------- Internal ----------
    
    def _check_exit(self, price: float, time_ms: int):
        """Check if open position should exit (called on every tick)."""
        p = self.open_position
        if p is None:
            return
        
        side = p["side"]
        ep = p["entry_price"]
        
        if side == "BUY":
            pnl_pct = (price - ep) / ep * 100
        else:
            pnl_pct = (ep - price) / ep * 100
        
        # Track peak
        if pnl_pct > p["peak_pnl_pct"]:
            p["peak_pnl_pct"] = pnl_pct
        
        # SL check
        if pnl_pct <= -p["sl_pct"]:
            sl_price = p["sl_price"]
            self._close_position(sl_price, time_ms, reason="STOP_LOSS")
            return
        
        # TP HARD
        if p.get("tp_hard_pct") and pnl_pct >= p["tp_hard_pct"]:
            self._close_position(p["tp_hard_price"], time_ms, reason="TP_HARD")
            return
        
        # Trail (v4.1 stable only)
        if p.get("use_trail") and p.get("trail_giveback"):
            if p["peak_pnl_pct"] >= p["tp_pct"]:
                # trail armed
                if pnl_pct <= p["peak_pnl_pct"] - p["trail_giveback"]:
                    exit_pnl = max(p["peak_pnl_pct"] - p["trail_giveback"], p["tp_pct"])
                    if side == "BUY":
                        exit_price = ep * (1 + exit_pnl / 100.0)
                    else:
                        exit_price = ep * (1 - exit_pnl / 100.0)
                    self._close_position(exit_price, time_ms, reason="TRAIL_LOCK")
                    return
        else:
            # Entry-Only mode: fixed TP
            if pnl_pct >= p["tp_pct"]:
                self._close_position(p["tp_price"], time_ms, reason="TAKE_PROFIT")
                return
        
        # Max hold (approximate via time)
        bars_held = (time_ms - p["entry_time_ms"]) // (5 * 60 * 1000)  # assumes 5m candles
        if bars_held >= p["max_hold_bars"]:
            self._close_position(price, time_ms, reason="MAX_HOLD")
            return
    
    def _close_position(self, exit_price: float, exit_time_ms: int, reason: str = "MANUAL"):
        """Close open position (assumes lock is held)."""
        p = self.open_position
        if p is None:
            return
        
        side = p["side"]
        ep = p["entry_price"]
        if side == "BUY":
            pnl_pct = (exit_price - ep) / ep * 100
        else:
            pnl_pct = (ep - exit_price) / ep * 100
        
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
        }
        self.trades.append(trade)
        self.open_position = None
