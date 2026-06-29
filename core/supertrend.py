"""SuperTrend Indicator.

طريقة الحساب:
  1. ATR(period) on (high, low, close)
  2. basic_upper = (high+low)/2 + multiplier × ATR
  3. basic_lower = (high+low)/2 − multiplier × ATR
  4. final bands with carry-over logic
  5. trend = bullish if close > prev_supertrend else bearish

Returns a single supertrend line:
  - بـbullish: line يحاذي الـcandles من تحت (أخضر)
  - بـbearish: line يحاذي الـcandles من فوق (أحمر)
"""
from __future__ import annotations
from typing import List, Dict, Any
import pandas as pd
import numpy as np


def compute_supertrend(
    candles: List[Dict[str, Any]],
    period: int = 10,
    multiplier: float = 3.0,
    offset_pct: float = 0.0,
) -> List[Dict[str, Any]]:
    """Compute supertrend on a list of candles.
    
    Args:
        candles: list of {open_time, open, high, low, close, ...}
        period: ATR period (default 10)
        multiplier: ATR multiplier (default 3.0)
        offset_pct: extra distance from candles as % (e.g. 0.1 = 0.1% extra)
    
    Returns:
        list of {time, value, direction} where direction is 'bull' or 'bear'.
    """
    if len(candles) < period + 2:
        return []
    
    high = np.array([c['high'] for c in candles], dtype=float)
    low = np.array([c['low'] for c in candles], dtype=float)
    close = np.array([c['close'] for c in candles], dtype=float)
    n = len(close)
    
    # TR (True Range)
    tr = np.zeros(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i-1]),
            abs(low[i] - close[i-1])
        )
    
    # ATR (smoothed)
    atr = np.zeros(n)
    atr[period-1] = tr[:period].mean()
    for i in range(period, n):
        atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period
    
    # Basic bands
    hl_mid = (high + low) / 2.0
    basic_upper = hl_mid + multiplier * atr
    basic_lower = hl_mid - multiplier * atr
    
    # Final bands (with carry-over)
    final_upper = np.zeros(n)
    final_lower = np.zeros(n)
    final_upper[period-1] = basic_upper[period-1]
    final_lower[period-1] = basic_lower[period-1]
    for i in range(period, n):
        # Upper band: take min of basic and previous if previous was higher than current close
        if basic_upper[i] < final_upper[i-1] or close[i-1] > final_upper[i-1]:
            final_upper[i] = basic_upper[i]
        else:
            final_upper[i] = final_upper[i-1]
        # Lower band: take max
        if basic_lower[i] > final_lower[i-1] or close[i-1] < final_lower[i-1]:
            final_lower[i] = basic_lower[i]
        else:
            final_lower[i] = final_lower[i-1]
    
    # SuperTrend determination
    supertrend = np.zeros(n)
    direction = ['' for _ in range(n)]  # 'bull' or 'bear'
    
    # Start: assume bullish
    supertrend[period-1] = final_lower[period-1]
    direction[period-1] = 'bull'
    
    for i in range(period, n):
        prev_st = supertrend[i-1]
        prev_dir = direction[i-1]
        
        if prev_dir == 'bull':
            # Was bullish; check if close broke below final_lower
            if close[i] < final_lower[i]:
                supertrend[i] = final_upper[i]
                direction[i] = 'bear'
            else:
                supertrend[i] = final_lower[i]
                direction[i] = 'bull'
        else:  # bear
            if close[i] > final_upper[i]:
                supertrend[i] = final_lower[i]
                direction[i] = 'bull'
            else:
                supertrend[i] = final_upper[i]
                direction[i] = 'bear'
    
    # Apply offset (extra distance from candles)
    if offset_pct > 0:
        for i in range(n):
            if direction[i] == 'bull':
                # green line below candles → push DOWN further
                supertrend[i] *= (1 - offset_pct / 100.0)
            elif direction[i] == 'bear':
                # red line above candles → push UP further
                supertrend[i] *= (1 + offset_pct / 100.0)
    
    # Build result (skip warmup)
    out = []
    for i in range(period, n):
        out.append({
            'time': int(candles[i]['open_time'] // 1000),
            'value': float(supertrend[i]),
            'direction': direction[i],
        })
    
    return out
