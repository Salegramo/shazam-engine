"""Live DNA Builder — wraps HybridDNABuilder for Binance candles.

CRITICAL: This uses the EXACT same HybridDNABuilder as the lab/training,
so the mined rules find the columns they expect (e.g. em_roll_34_demand_mean,
roll_50_demand_pressure_score_std, etc).

Binance candle format → renamed → fed to HybridDNABuilder → ~3000 columns DNA
"""
from __future__ import annotations
from typing import List, Dict, Any
import pandas as pd
import numpy as np

from core.v41_stable_engine import HybridDNABuilder


# Single shared builder instance
_builder = HybridDNABuilder.from_default_registry()


def candles_to_source_df(candles: List[Dict[str, Any]]) -> pd.DataFrame:
    """Convert Binance candles to source DataFrame ready for HybridDNABuilder.
    
    HybridDNABuilder's _normalize_sources() handles most aliases automatically:
      open → source_open, high → source_high, etc.
      quote_volume → source_quote_volume
      trades → source_trades
    
    But these need explicit rename (different name from Binance):
      taker_buy_base_volume → taker_buy_base
      taker_buy_quote_volume → taker_buy_quote
    """
    if not candles:
        return pd.DataFrame()
    
    df = pd.DataFrame(candles)
    
    # Rename Binance-specific names to what HybridDNABuilder expects
    rename_map = {
        'taker_buy_base_volume': 'taker_buy_base',
        'taker_buy_quote_volume': 'taker_buy_quote',
    }
    for old, new in rename_map.items():
        if old in df.columns and new not in df.columns:
            df = df.rename(columns={old: new})
    
    # Add timestamp column for reference
    if 'open_time' in df.columns:
        df['timestamp'] = pd.to_datetime(df['open_time'], unit='ms', utc=True)
    
    return df


def build_dna_from_candles(candles: List[Dict[str, Any]]) -> pd.DataFrame:
    """Build full DNA from list of Binance candles.
    
    Returns DataFrame with ~3000 columns matching the lab's DNA structure exactly.
    The mined rules will find their expected columns here.
    """
    if not candles or len(candles) < 30:
        return pd.DataFrame()
    
    source_df = candles_to_source_df(candles)
    dna = _builder.build(source_df)
    return dna
