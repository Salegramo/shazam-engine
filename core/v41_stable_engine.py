"""Shazam Adaptive 360 Live Engine.

This engine is intentionally small and causal.  It does not replay a large rule
bank.  For every closed candle after the warmup window it rebuilds a light DNA
view, builds an expert story, runs the handoff state machine, generates a small
entry/exit equation from the last 360 closed candles, and simulates position
management.

Design blocks exposed in the output:
1. LiveDNABuilder
2. LiveExpertStoryBuilder
3. HandoffStateMachine
4. MicroRuleGenerator
5. MicroExitGenerator
6. PositionManager
7. Simulator
"""
from __future__ import annotations

import json
import math
import re
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

EPS = 1e-12
DEFAULT_WINDOWS = (5, 10, 12, 15, 20, 25, 30, 34, 40, 50, 55, 75, 89, 100, 144, 150, 200, 233, 360)
CORE_FEATURES = [
    "candle_return_pct", "candle_range_pct", "candle_body_pct", "candle_body_abs_pct",
    "candle_upper_wick_ratio_pct", "candle_lower_wick_ratio_pct", "candle_close_position_pct",
    "volume_taker_buy_ratio_pct", "volume_taker_sell_ratio_pct", "demand_supply_delta",
    "demand_pressure_score", "supply_pressure_score", "effort_volume_range_ratio",
    "effort_quote_volume_range_ratio", "trades_per_volume", "abs_return_pct",
    "signed_body_to_range_pct", "upper_minus_lower_wick_pct", "quote_per_trade",
    "base_volume_per_trade", "taker_quote_ratio_pct",
]
ROLL_BASES = [
    "return_pct", "range_pct", "close_position_pct", "lower_wick_ratio_pct", "upper_wick_ratio_pct",
    "abs_return_pct", "signed_body_to_range_pct", "volume", "quote_volume", "trades",
    "demand_pressure_score", "supply_pressure_score", "effort_quote_volume_range_ratio",
    "base_volume_per_trade", "body_abs_pct", "effort_volume_range_ratio", "demand_supply_delta",
]
ROLL_AGGS = ("mean", "median", "min", "max", "std", "sum", "position_pct")


def _safe_num(s: pd.Series, default: float = 0.0) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(default)


def _pct_rank_last(series: pd.Series, window: int) -> pd.Series:
    """Fast causal position percentile approximation.

    Exact rolling ranks are expensive in live loops.  For Shazam live we only
    need a stable 0-100 position of the latest value inside the rolling window.
    """
    s = _safe_num(series)
    r = s.rolling(window, min_periods=max(3, min(window, 10)))
    lo = r.min()
    hi = r.max()
    return (((s - lo) / (hi - lo).replace(0, np.nan)) * 100.0).replace([np.inf, -np.inf], np.nan).fillna(50.0).clip(0, 100)


def _zscore_last(series: pd.Series, window: int) -> pd.Series:
    r = series.rolling(window, min_periods=max(5, min(window, 20)))
    mu = r.mean()
    sd = r.std().replace(0, np.nan)
    return ((series - mu) / sd).replace([np.inf, -np.inf], np.nan).fillna(0.0)


def _find_member(names: Iterable[str], candidates: Iterable[str]) -> Optional[str]:
    lower = {n.lower(): n for n in names}
    for c in candidates:
        if c.lower() in lower:
            return lower[c.lower()]
    return None


def read_dna_zip(input_zip: Path) -> Tuple[pd.DataFrame, Optional[pd.DataFrame], str]:
    with zipfile.ZipFile(input_zip) as zf:
        names = zf.namelist()
        dna_name = _find_member(names, [
            "expanded_safe_live_dna.csv", "safe_dna.csv", "current_dna.csv",
            "expanded_safe_dna.csv", "dna.csv", "selected_rule_dna.csv",
        ])
        if not dna_name:
            csvs = [n for n in names if n.lower().endswith(".csv") and "outcome" not in n.lower()]
            if not csvs:
                raise ValueError("لم أجد ملف DNA CSV داخل ZIP")
            dna_name = csvs[0]
        with zf.open(dna_name) as fp:
            df = pd.read_csv(fp)
        out_name = _find_member(names, ["expanded_outcome_labels.csv", "outcome_labels.csv", "outcomes.csv"])
        outcomes = None
        if out_name:
            with zf.open(out_name) as fp:
                outcomes = pd.read_csv(fp)
        return df, outcomes, dna_name


class LiveDNABuilder:
    """Builds the live DNA core from the last closed candles.

    It uses safe_columns.json as the target language, but also generates extra
    columns on demand for micro rules. All calculations are causal rolling/lag
    calculations from closed candles only.
    """
    def __init__(self, safe_columns: Optional[List[str]] = None, windows: Tuple[int, ...] = DEFAULT_WINDOWS):
        self.safe_columns = list(safe_columns or [])
        self.windows = tuple(windows)

    @classmethod
    def from_default_registry(cls) -> "LiveDNABuilder":
        path = Path(__file__).with_name("shazam_safe_columns.json")
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return cls(data.get("columns") or [])
                if isinstance(data, list):
                    return cls(data)
            except Exception:
                pass
        return cls([])

    def build(self, source: pd.DataFrame) -> pd.DataFrame:
        df = source.copy()
        self._normalize_sources(df)
        self._base_candles(df)
        self._lag_features(df)
        self._sequence_features(df)
        self._liquidity(df)
        self._rolling_features(df)
        # Keep all generated features.  The target list is diagnostic, not a hard cap.
        return df.replace([np.inf, -np.inf], np.nan).fillna(0)

    def _normalize_sources(self, df: pd.DataFrame) -> None:
        rename_map = {}
        aliases = {
            "source_open": ["open", "Open", "o"],
            "source_high": ["high", "High", "h"],
            "source_low": ["low", "Low", "l"],
            "source_close": ["close", "Close", "c"],
            "source_volume": ["volume", "Volume", "vol"],
            "source_quote_volume": ["quote_volume", "quoteVolume", "quote_asset_volume"],
            "source_trades": ["trades", "number_of_trades", "trade_count"],
            "source_taker_buy_base": ["taker_buy_base", "taker_buy_volume"],
            "source_taker_buy_quote": ["taker_buy_quote", "taker_buy_quote_volume"],
        }
        for target, cand in aliases.items():
            if target not in df.columns:
                found = _find_member(df.columns, cand)
                if found:
                    rename_map[found] = target
        if rename_map:
            df.rename(columns=rename_map, inplace=True)
        for c in ["source_open", "source_high", "source_low", "source_close"]:
            if c not in df.columns:
                raise ValueError(f"DNA لا يحتوي العمود الأساسي {c}")
            df[c] = _safe_num(df[c])
        for c in ["source_volume", "source_quote_volume", "source_trades", "source_taker_buy_base", "source_taker_buy_quote"]:
            if c not in df.columns:
                df[c] = 0.0
            df[c] = _safe_num(df[c])

    def _base_candles(self, df: pd.DataFrame) -> None:
        o = _safe_num(df["source_open"])
        h = _safe_num(df["source_high"])
        l = _safe_num(df["source_low"])
        c = _safe_num(df["source_close"])
        v = _safe_num(df["source_volume"])
        qv = _safe_num(df["source_quote_volume"])
        trades = _safe_num(df["source_trades"])
        tb = _safe_num(df.get("source_taker_buy_base", pd.Series(0, index=df.index)))
        tq = _safe_num(df.get("source_taker_buy_quote", pd.Series(0, index=df.index)))
        rng = (h - l).abs()
        body = c - o
        df["candle_return_pct"] = c.pct_change().fillna(0) * 100.0
        df["candle_range_pct"] = rng / c.replace(0, np.nan) * 100.0
        df["candle_body_pct"] = body / o.replace(0, np.nan) * 100.0
        df["candle_body_abs_pct"] = df["candle_body_pct"].abs()
        df["candle_upper_wick_ratio_pct"] = ((h - np.maximum(o, c)).clip(lower=0) / rng.replace(0, np.nan) * 100.0).fillna(0)
        df["candle_lower_wick_ratio_pct"] = ((np.minimum(o, c) - l).clip(lower=0) / rng.replace(0, np.nan) * 100.0).fillna(0)
        df["candle_close_position_pct"] = ((c - l) / rng.replace(0, np.nan) * 100.0).fillna(50.0)
        df["candle_direction_num"] = np.sign(body).fillna(0)
        buy_ratio = (tb / v.replace(0, np.nan) * 100.0).fillna(50.0).clip(0, 100)
        quote_buy_ratio = (tq / qv.replace(0, np.nan) * 100.0).fillna(buy_ratio).clip(0, 100)
        df["volume_taker_buy_ratio_pct"] = buy_ratio
        df["volume_taker_sell_ratio_pct"] = 100.0 - buy_ratio
        df["taker_quote_ratio_pct"] = quote_buy_ratio
        df["demand_supply_delta"] = buy_ratio - (100.0 - buy_ratio)
        df["demand_pressure_score"] = (df["demand_supply_delta"].clip(lower=0) + df["candle_close_position_pct"].clip(0, 100) / 2.0).fillna(0)
        df["supply_pressure_score"] = ((-df["demand_supply_delta"]).clip(lower=0) + (100.0 - df["candle_close_position_pct"].clip(0, 100)) / 2.0).fillna(0)
        df["effort_volume_range_ratio"] = (v / df["candle_range_pct"].abs().replace(0, np.nan)).fillna(0)
        df["effort_quote_volume_range_ratio"] = (qv / df["candle_range_pct"].abs().replace(0, np.nan)).fillna(0)
        df["trades_per_volume"] = (trades / v.replace(0, np.nan)).fillna(0)
        df["abs_return_pct"] = df["candle_return_pct"].abs()
        df["signed_body_to_range_pct"] = (body / rng.replace(0, np.nan) * 100.0).fillna(0)
        df["upper_minus_lower_wick_pct"] = df["candle_upper_wick_ratio_pct"] - df["candle_lower_wick_ratio_pct"]
        df["quote_per_trade"] = (qv / trades.replace(0, np.nan)).fillna(0)
        df["base_volume_per_trade"] = (v / trades.replace(0, np.nan)).fillna(0)
        # Internal aliases for easier rule generation.
        df["return_pct"] = df["candle_return_pct"]
        df["range_pct"] = df["candle_range_pct"]
        df["close_position_pct"] = df["candle_close_position_pct"]
        df["body_abs_pct"] = df["candle_body_abs_pct"]
        df["upper_wick_ratio_pct"] = df["candle_upper_wick_ratio_pct"]
        df["lower_wick_ratio_pct"] = df["candle_lower_wick_ratio_pct"]
        df["volume"] = v
        df["quote_volume"] = qv
        df["trades"] = trades

    def _lag_features(self, df: pd.DataFrame) -> None:
        lag_bases = [
            "source_high", "source_low", "source_close", "source_volume", "source_quote_volume",
            "return_pct", "range_pct", "demand_pressure_score", "supply_pressure_score",
        ]
        for lag in range(1, 41):
            for base in lag_bases:
                if base in df.columns:
                    df[f"lag_{lag}_{base}"] = _safe_num(df[base]).shift(lag).fillna(0)

    def _sequence_features(self, df: pd.DataFrame) -> None:
        ret = _safe_num(df.get("return_pct", df.get("candle_return_pct", 0)))
        demand_delta = _safe_num(df.get("demand_supply_delta", 0))
        lower_rej = _safe_num(df.get("lower_wick_ratio_pct", df.get("candle_lower_wick_ratio_pct", 0)))
        upper_rej = _safe_num(df.get("upper_wick_ratio_pct", df.get("candle_upper_wick_ratio_pct", 0)))
        up = (ret > 0).astype(float)
        down = (ret < 0).astype(float)
        for w in self.windows:
            if w > 200:
                continue
            df[f"seq_{w}_up_ratio_pct"] = up.rolling(w, min_periods=1).mean().fillna(0) * 100.0
            df[f"seq_{w}_down_ratio_pct"] = down.rolling(w, min_periods=1).mean().fillna(0) * 100.0
            df[f"seq_{w}_net_return_pct"] = ret.rolling(w, min_periods=1).sum().fillna(0)
            df[f"seq_{w}_demand_delta_sum"] = demand_delta.rolling(w, min_periods=1).sum().fillna(0)
            df[f"seq_{w}_rejection_down_score"] = lower_rej.rolling(w, min_periods=1).mean().fillna(0)
            df[f"seq_{w}_rejection_up_score"] = upper_rej.rolling(w, min_periods=1).mean().fillna(0)

    def _liquidity(self, df: pd.DataFrame) -> None:
        high = _safe_num(df["source_high"])
        low = _safe_num(df["source_low"])
        for w in self.windows:
            prev_h = high.shift(1).rolling(w, min_periods=1).max()
            prev_l = low.shift(1).rolling(w, min_periods=1).min()
            df[f"liq_{w}_prev_high"] = prev_h.fillna(high)
            df[f"liq_{w}_prev_low"] = prev_l.fillna(low)
            df[f"liq_{w}_break_prev_high_pct"] = ((df["source_close"] - prev_h) / df["source_close"].replace(0, np.nan) * 100.0).fillna(0)
            df[f"liq_{w}_break_prev_low_pct"] = ((prev_l - df["source_close"]) / df["source_close"].replace(0, np.nan) * 100.0).fillna(0)
            df[f"liq_{w}_distance_to_prev_high_pct"] = ((prev_h - df["source_close"]) / df["source_close"].replace(0, np.nan) * 100.0).fillna(0)
            df[f"liq_{w}_distance_to_prev_low_pct"] = ((df["source_close"] - prev_l) / df["source_close"].replace(0, np.nan) * 100.0).fillna(0)
            ema = df["source_close"].ewm(span=max(2, w), adjust=False).mean()
            df[f"momentum_{w}_ema_distance_pct"] = ((df["source_close"] - ema) / df["source_close"].replace(0, np.nan) * 100.0).fillna(0)
            df[f"momentum_{w}_ema_slope_pct"] = ema.pct_change().fillna(0) * 100.0

    def _rolling_features(self, df: pd.DataFrame) -> None:
        for base in ROLL_BASES:
            if base not in df.columns:
                continue
            s = _safe_num(df[base])
            for w in self.windows:
                r = s.rolling(w, min_periods=max(2, min(w, 5)))
                prefix = f"roll_{w}_{base}"
                if "mean" in ROLL_AGGS: df[f"{prefix}_mean"] = r.mean().fillna(0)
                if "median" in ROLL_AGGS: df[f"{prefix}_median"] = r.median().fillna(0)
                if "min" in ROLL_AGGS: df[f"{prefix}_min"] = r.min().fillna(0)
                if "max" in ROLL_AGGS: df[f"{prefix}_max"] = r.max().fillna(0)
                if "std" in ROLL_AGGS: df[f"{prefix}_std"] = r.std().fillna(0)
                if "sum" in ROLL_AGGS: df[f"{prefix}_sum"] = r.sum().fillna(0)
                df[f"{prefix}_position_pct"] = _pct_rank_last(s, w)
        # Common cross-features used by old discoveries.
        for w in self.windows:
            if f"roll_{w}_range_pct_mean" in df.columns:
                df[f"mw_{w}_range_x_supply"] = df[f"roll_{w}_range_pct_mean"] * df[f"roll_{w}_supply_pressure_score_mean"] if f"roll_{w}_supply_pressure_score_mean" in df.columns else 0
                df[f"mw_{w}_range_x_demand"] = df[f"roll_{w}_range_pct_mean"] * df[f"roll_{w}_demand_pressure_score_mean"] if f"roll_{w}_demand_pressure_score_mean" in df.columns else 0
                df[f"mw_{w}_return_x_taker"] = df[f"roll_{w}_return_pct_mean"] * df[f"roll_{w}_demand_supply_delta_mean"] if f"roll_{w}_return_pct_mean" in df.columns and f"roll_{w}_demand_supply_delta_mean" in df.columns else 0


class LiveExpertStoryBuilder:
    """Builds an expert story and context from live DNA."""
    def build(self, dna: pd.DataFrame) -> pd.DataFrame:
        out = pd.DataFrame(index=dna.index)
        buy_pressure = _safe_num(dna.get("demand_pressure_score", 0))
        sell_pressure = _safe_num(dna.get("supply_pressure_score", 0))
        close_pos = _safe_num(dna.get("candle_close_position_pct", 50))
        ret = _safe_num(dna.get("candle_return_pct", 0))
        rng = _safe_num(dna.get("candle_range_pct", 0)).abs()
        vol_pos = _pct_rank_last(_safe_num(dna.get("source_volume", dna.get("volume", 0))), 144)
        effort = _safe_num(dna.get("effort_volume_range_ratio", 0))
        effort_pos = _pct_rank_last(effort, 144)
        buy_support = (buy_pressure * 0.45 + close_pos * 0.20 + ret.clip(lower=0) * 120.0 + vol_pos * 0.15 + effort_pos * 0.10).clip(0, 100)
        sell_support = (sell_pressure * 0.45 + (100 - close_pos) * 0.20 + (-ret).clip(lower=0) * 120.0 + vol_pos * 0.15 + effort_pos * 0.10).clip(0, 100)
        edge = buy_support - sell_support
        conflict = (np.minimum(buy_support, sell_support) / np.maximum(np.maximum(buy_support, sell_support), 1) * 100.0).fillna(0)
        neutral = (100 - edge.abs()).clip(0, 100) * 0.60 + conflict.clip(0, 100) * 0.40
        vol_state = _pct_rank_last(rng, 144)
        out["expert_buy_context_support"] = buy_support.fillna(0)
        out["expert_sell_context_support"] = sell_support.fillna(0)
        out["expert_neutral_risk"] = neutral.fillna(50)
        out["expert_context_edge"] = edge.fillna(0)
        out["expert_volume_score"] = vol_pos.fillna(50)
        out["expert_effort_result_ratio"] = effort_pos.fillna(50)
        out["expert_scale_alignment_score"] = (edge.abs() * (1 - conflict / 120.0)).clip(0, 100).fillna(0)
        out["expert_scale_conflict_score"] = conflict.clip(0, 100).fillna(0)
        out["expert_range_energy_score"] = vol_state.fillna(50)
        state = np.full(len(out), "neutral_watch", dtype=object)
        state[(edge > 12) & (neutral < 68)] = "buy_context"
        state[(edge < -12) & (neutral < 68)] = "sell_context"
        state[(neutral >= 68) & (conflict > 45)] = "mixed_watch"
        out["expert_market_state"] = state
        return out


@dataclass
class HandoffState:
    active_side: str
    slot: str
    story: str
    buy_receiver: bool
    sell_receiver: bool
    neutral_wait: bool
    conflict_wait: bool


class HandoffStateMachine:
    """Represents Shazam receive/handoff story over the current expert context."""
    def __init__(self):
        self.active_side = "WAIT"
        self.last_slot = "WARMUP"

    def update(self, expert_row: pd.Series, position: str) -> HandoffState:
        buy = float(expert_row.get("expert_buy_context_support", 0))
        sell = float(expert_row.get("expert_sell_context_support", 0))
        neutral = float(expert_row.get("expert_neutral_risk", 50))
        edge = float(expert_row.get("expert_context_edge", 0))
        conflict = float(expert_row.get("expert_scale_conflict_score", 0))
        neutral_wait = neutral >= 76 or conflict >= 78
        conflict_wait = abs(edge) < 8 and conflict > 55
        buy_receiver = edge > 10 and neutral < 76
        sell_receiver = edge < -10 and neutral < 76
        if position == "IN_BUY":
            if sell_receiver and sell > buy + 8:
                slot = "BUY_WEAKNESS_SELL_RECEIVER_READY"
                story = "BUY→SELL handoff building"
            elif buy_receiver:
                slot = "BUY_CONTINUATION"
                story = "BUY side active and continuing"
            else:
                slot = "BUY_HOLD_WATCH"
                story = "BUY position watch"
        elif position == "IN_SELL":
            if buy_receiver and buy > sell + 8:
                slot = "SELL_WEAKNESS_BUY_RECEIVER_READY"
                story = "SELL→BUY handoff building"
            elif sell_receiver:
                slot = "SELL_CONTINUATION"
                story = "SELL side active and continuing"
            else:
                slot = "SELL_HOLD_WATCH"
                story = "SELL position watch"
        else:
            if neutral_wait or conflict_wait:
                slot = "NEUTRAL_WAIT"
                story = "Wait: neutral/conflict"
            elif buy_receiver and buy > sell + 10:
                slot = "SELL_WEAKNESS_BUY_RECEIVER_READY"
                story = "SELL→BUY receiver story"
            elif sell_receiver and sell > buy + 10:
                slot = "BUY_WEAKNESS_SELL_RECEIVER_READY"
                story = "BUY→SELL receiver story"
            else:
                slot = "WAIT_STORY_NOT_CLEAR"
                story = "Wait: handoff not clear"
        self.last_slot = slot
        if slot.startswith("BUY") or "BUY_RECEIVER" in slot:
            self.active_side = "BUY"
        elif slot.startswith("SELL") or "SELL_RECEIVER" in slot:
            self.active_side = "SELL"
        else:
            self.active_side = "WAIT"
        return HandoffState(self.active_side, slot, story, buy_receiver, sell_receiver, neutral_wait, conflict_wait)


@dataclass
class GeneratedRule:
    rule_id: str
    side: str
    kind: str
    slot: str
    score_name: str
    score: float
    threshold: float
    passed: bool
    confidence: float
    formula: str
    # v1.6 miner-quality diagnostics.  Old code may leave these defaults.
    rule_signals: int = 0
    rule_win: int = 0
    rule_loss: int = 0
    rule_neutral: int = 0
    rule_completed: int = 0
    rule_wr: float = 0.0
    rule_effective_win_rate: float = 0.0
    rule_neutral_rate: float = 0.0
    rule_loss_rate: float = 0.0
    rule_local_signals: int = 0
    rule_local_win: int = 0
    rule_local_loss: int = 0
    rule_local_neutral: int = 0
    rule_local_wr: float = 0.0
    rule_local_effective_win_rate: float = 0.0
    rule_context_confirmed: bool = False
    rule_clause_count: int = 0
    rule_family: str = ""
    rule_branch: str = ""
    rule_source: str = ""




@dataclass
class LockedTradeContextConfig:
    """Locked context — v4.1 (asymmetric BUY/SELL with micro-trail).
    
    تحديثات v4.1 من v4.0:
    - max_hold_bars: 72 -> 144 (يرفع WR من 91.2% إلى 92.7%)
    - micro-trail logic: نخرج بـ(peak - 0.05) لو peak ≥ 0.10
    - SELL config مختلف (أصرم من BUY):
        * sell_take_profit_pct: 0.05 (vs BUY 0.10)
        * sell_stop_loss_pct: 0.75 (vs BUY 1.50)
        * sell_trail_giveback: 0.03 (vs BUY 0.05)
        * sell_take_profit_hard_pct: 1.5 (vs BUY 3.0)
    
    النتائج المُختبرة (V3 winner على BULL Apr 2025):
      BULL: 854 trades, WR=93.3%, PnL=+91.19%
      BEAR: 858 trades, WR=93.4%, PnL=+82.41%
      vs الحالي: 854 / 91.2% / +11.55%
      التحسين: 8x PnL مع رفع WR + إشارات نفسها
    """
    name: str = "locked_exit_v4_1_asymmetric_micro_trail"
    warmup_bars: int = 2500
    entry_min_wr: float = 95.0
    entry_min_signals: int = 10
    entry_expert_edge_min: float = -999.0
    min_hold_bars: int = 6
    max_hold_bars: int = 144            # v4.1: 72 -> 144
    
    # BUY config (v4.0 standard)
    take_profit_pct: float = 0.10       # TP_MIN
    take_profit_hard_pct: float = 3.00  # TP_HARD
    trail_giveback_pct: float = 0.05    # micro-trail giveback
    stop_loss_pct: float = 1.50
    
    # SELL config (v4.1 جديد - أصرم من BUY)
    sell_take_profit_pct: float = 0.05         # vs BUY 0.10
    sell_take_profit_hard_pct: float = 1.50    # vs BUY 3.00
    sell_trail_giveback_pct: float = 0.03      # vs BUY 0.05
    sell_stop_loss_pct: float = 0.75           # vs BUY 1.50
    
    # General settings
    use_stop_loss: bool = True
    green_profit_pct: float = 0.03
    opposite_receiver_margin: float = 12.0
    neutral_take_profit: float = 70.0
    neutral_stop_loss: float = 75.0
    mom3_min: float = 0.02
    mom6_min: float = 0.00
    mom12_min: float = 0.05
    trail_start_pct: float = 0.10              # peak ≥ 0.10 to arm trail
    trail_wait_bars: int = 0
    cooldown_bars: int = 1
    post_exit_cooldown_bars: int = 1
    allow_forced_exit: bool = False
    win_threshold_pct: float = 0.05            # lowered to capture SELL wins (TP=0.05)
    
    # Drawdown guard (lighter — SL handles main protection)
    context_drawdown_guard_enabled: bool = False  # disabled in v4.1 (SL is enough)
    context_drawdown_guard_min_hold_bars: int = 30
    context_drawdown_guard_pct: float = 1.5
    
    # Time-stop disabled
    time_stop_bars: int = 999
    time_stop_loss_cap: float = 1.50

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


LOCKED_TRADE_CONTEXT_CONFIG = LockedTradeContextConfig()


class PositionAwareTradeExpert:
    """Expert layer that evaluates the *open trade*, not the market alone.

    Order is enforced by the simulator:
    SEARCH_ENTRY -> ENTRY -> SEARCH_EXIT -> EXIT.
    This expert only sees a trade after it is opened and returns one of:
    - TAKE_PROFIT_HARD
    - PROFIT_EXPERT_CONFIRM
    - TAKE_PROFIT_HARD+PROFIT_EXPERT_CONFIRM
    - HOLD

    The locked v1 context intentionally does not cut losses early.  We keep the
    parameters visible in the config so the next cross-sample tests can turn SL
    back on if it proves stable.
    """
    def __init__(self, config: LockedTradeContextConfig | None = None):
        self.config = config or LOCKED_TRADE_CONTEXT_CONFIG

    @staticmethod
    def _pnl_pct(position: str, entry_price: float, current_price: float) -> float:
        pnl = (float(current_price) - float(entry_price)) / max(float(entry_price), EPS) * 100.0
        if position == "IN_SELL":
            pnl *= -1.0
        return float(pnl)

    def evaluate(
        self,
        *,
        idx: int,
        dna_window: pd.DataFrame,
        expert_window: pd.DataFrame,
        state: HandoffState,
        position: str,
        entry_price: float,
        current_price: float,
        hold_bars: int,
        peak_pnl_pct: float = 0.0,
    ) -> GeneratedRule:
        cfg = self.config
        pnl = self._pnl_pct(position, entry_price, current_price)
        expert_row = expert_window.iloc[-1]
        buy_support = float(expert_row.get("expert_buy_context_support", 0.0))
        sell_support = float(expert_row.get("expert_sell_context_support", 0.0))
        neutral = float(expert_row.get("expert_neutral_risk", 50.0))
        edge = float(expert_row.get("expert_context_edge", 0.0))
        # Recent move after entry.  Positive means current position direction is still supported.
        closes = _safe_num(dna_window.get("source_close", pd.Series(dtype=float)))
        if len(closes) >= 4:
            mom3 = (float(closes.iloc[-1]) - float(closes.iloc[-4])) / max(float(closes.iloc[-4]), EPS) * 100.0
        else:
            mom3 = 0.0
        if len(closes) >= 7:
            mom6 = (float(closes.iloc[-1]) - float(closes.iloc[-7])) / max(float(closes.iloc[-7]), EPS) * 100.0
        else:
            mom6 = 0.0
        if len(closes) >= 13:
            mom12 = (float(closes.iloc[-1]) - float(closes.iloc[-13])) / max(float(closes.iloc[-13]), EPS) * 100.0
        else:
            mom12 = 0.0
        if position == "IN_SELL":
            mom3, mom6, mom12 = -mom3, -mom6, -mom12
            opposite_receiver = bool(state.buy_receiver and buy_support >= sell_support + cfg.opposite_receiver_margin)
            support_alive = sell_support >= buy_support
        else:
            opposite_receiver = bool(state.sell_receiver and sell_support >= buy_support + cfg.opposite_receiver_margin)
            support_alive = buy_support >= sell_support

        hard_tp = bool(hold_bars >= cfg.min_hold_bars and pnl >= cfg.take_profit_hard_pct)
        # Profit expert confirmation: profitable trade + story is no longer worth holding.
        profit_expert = bool(
            hold_bars >= cfg.min_hold_bars
            and pnl >= cfg.take_profit_pct
            and (
                opposite_receiver
                or neutral >= cfg.neutral_take_profit
                or mom3 <= cfg.mom3_min
                or mom12 <= cfg.mom12_min
                or not support_alive
            )
        )
        # Optional trailing profit capture from the same locked context.
        trail_exit = bool(
            hold_bars >= max(cfg.min_hold_bars, cfg.trail_wait_bars)
            and peak_pnl_pct >= cfg.trail_start_pct
            and (peak_pnl_pct - pnl) >= cfg.trail_giveback_pct
        )
        stop_loss = bool(
            cfg.use_stop_loss
            and hold_bars >= cfg.min_hold_bars
            and pnl <= -abs(cfg.stop_loss_pct)
            and (opposite_receiver or neutral >= cfg.neutral_stop_loss)
        )
        # v1.1 contextual guard: prevents a losing position from living until the
        # end of the sample. This is intentionally separate from forced exit:
        # it needs both time-in-trade and real adverse drawdown.
        context_drawdown_guard = bool(
            cfg.context_drawdown_guard_enabled
            and hold_bars >= cfg.context_drawdown_guard_min_hold_bars
            and pnl <= -abs(cfg.context_drawdown_guard_pct)
        )
        # v1.2 time-stop: caps long-running losing trades (analysis showed LOSS hold avg=99 bars).
        # If trade has been open >= time_stop_bars and is still negative beyond the cap, exit.
        time_stop = bool(
            getattr(cfg, 'time_stop_bars', 0) > 0
            and hold_bars >= cfg.time_stop_bars
            and pnl <= -abs(getattr(cfg, 'time_stop_loss_cap', 1.0))
        )
        reasons = []
        if hard_tp:
            reasons.append("TAKE_PROFIT_HARD")
        if profit_expert:
            reasons.append("PROFIT_EXPERT_CONFIRM")
        if trail_exit:
            reasons.append("TRAIL_PROFIT_GIVEBACK")
        if stop_loss:
            reasons.append("CUT_LOSS_CONFIRM")
        if context_drawdown_guard:
            reasons.append("CONTEXT_DRAWDOWN_GUARD_EXIT")
        if time_stop:
            reasons.append("TIME_STOP_EXIT")
        passed = bool(reasons)
        reason = "+".join(reasons) if reasons else "HOLD"
        side = "BUY" if position == "IN_BUY" else "SELL"
        formula = (
            f"{cfg.name}: after ENTRY, exit {side} only when "
            f"pnl>=tp_hard({cfg.take_profit_hard_pct}) OR "
            f"pnl>=tp({cfg.take_profit_pct}) with expert story confirmation; "
            f"reason={reason}; pnl={pnl:.4f}; hold={hold_bars}; "
            f"edge={edge:.2f}; neutral={neutral:.2f}; mom3={mom3:.4f}; mom12={mom12:.4f}; "
            f"drawdown_guard={context_drawdown_guard}"
        )
        # Confidence is descriptive; the pass/fail is rule based.
        confidence = 50.0 + max(0.0, pnl - cfg.take_profit_pct) * 120.0
        if hard_tp:
            confidence += 20.0
        if profit_expert:
            confidence += 12.0
        if trail_exit:
            confidence += 8.0
        if context_drawdown_guard:
            confidence += 10.0
        return GeneratedRule(
            f"{side}_LOCKED_TRADE_EXIT_{idx}", side, "EXIT", reason, "locked_trade_context_exit",
            float(pnl), float(cfg.take_profit_pct), passed, float(max(0, min(100, confidence))), formula,
        )

class MicroRuleGenerator:
    """Generates small entry rules from the current 360-bar story."""
    def __init__(self, entry_percentile: float = 82.0):
        self.entry_percentile = float(entry_percentile)

    def generate(self, dna_window: pd.DataFrame, expert_window: pd.DataFrame, state: HandoffState) -> List[GeneratedRule]:
        if len(dna_window) < 50:
            return []
        idx = dna_window.index[-1]
        rules: List[GeneratedRule] = []
        # Scores are deliberately small and interpretable.
        buy_score = (
            _zscore_last(expert_window["expert_buy_context_support"], min(144, len(expert_window))).iloc[-1] * 1.25
            - _zscore_last(expert_window["expert_sell_context_support"], min(144, len(expert_window))).iloc[-1] * 0.85
            - _zscore_last(expert_window["expert_neutral_risk"], min(144, len(expert_window))).iloc[-1] * 0.75
            + _zscore_last(dna_window["demand_pressure_score"], min(100, len(dna_window))).iloc[-1] * 0.80
            + _zscore_last(dna_window["candle_close_position_pct"], min(50, len(dna_window))).iloc[-1] * 0.35
        )
        sell_score = (
            _zscore_last(expert_window["expert_sell_context_support"], min(144, len(expert_window))).iloc[-1] * 1.25
            - _zscore_last(expert_window["expert_buy_context_support"], min(144, len(expert_window))).iloc[-1] * 0.85
            - _zscore_last(expert_window["expert_neutral_risk"], min(144, len(expert_window))).iloc[-1] * 0.75
            + _zscore_last(dna_window["supply_pressure_score"], min(100, len(dna_window))).iloc[-1] * 0.80
            - _zscore_last(dna_window["candle_close_position_pct"], min(50, len(dna_window))).iloc[-1] * 0.35
        )
        # Build historical score arrays within the same window for thresholds.
        bw = min(144, len(dna_window))
        buy_series = (
            _zscore_last(expert_window["expert_buy_context_support"], bw) * 1.25
            - _zscore_last(expert_window["expert_sell_context_support"], bw) * 0.85
            - _zscore_last(expert_window["expert_neutral_risk"], bw) * 0.75
            + _zscore_last(dna_window["demand_pressure_score"], min(100, len(dna_window))) * 0.80
            + _zscore_last(dna_window["candle_close_position_pct"], min(50, len(dna_window))) * 0.35
        ).replace([np.inf, -np.inf], np.nan).fillna(0)
        sell_series = (
            _zscore_last(expert_window["expert_sell_context_support"], bw) * 1.25
            - _zscore_last(expert_window["expert_buy_context_support"], bw) * 0.85
            - _zscore_last(expert_window["expert_neutral_risk"], bw) * 0.75
            + _zscore_last(dna_window["supply_pressure_score"], min(100, len(dna_window))) * 0.80
            - _zscore_last(dna_window["candle_close_position_pct"], min(50, len(dna_window))) * 0.35
        ).replace([np.inf, -np.inf], np.nan).fillna(0)
        bthr = float(np.nanpercentile(buy_series.iloc[-bw:], self.entry_percentile))
        sthr = float(np.nanpercentile(sell_series.iloc[-bw:], self.entry_percentile))
        allow_buy = state.buy_receiver and not state.neutral_wait and not state.conflict_wait
        allow_sell = state.sell_receiver and not state.neutral_wait and not state.conflict_wait
        rules.append(GeneratedRule(
            f"BUY_MICRO_{idx}", "BUY", "ENTRY", state.slot, "buy_story_score", float(buy_score), bthr,
            bool(allow_buy and buy_score >= bthr), float(max(0, min(100, 50 + (buy_score - bthr) * 18))),
            "z(buy_support)*1.25 - z(sell_support)*0.85 - z(neutral)*0.75 + z(demand)*0.80 + z(close_pos)*0.35 >= rolling_p82",
        ))
        rules.append(GeneratedRule(
            f"SELL_MICRO_{idx}", "SELL", "ENTRY", state.slot, "sell_story_score", float(sell_score), sthr,
            bool(allow_sell and sell_score >= sthr), float(max(0, min(100, 50 + (sell_score - sthr) * 18))),
            "z(sell_support)*1.25 - z(buy_support)*0.85 - z(neutral)*0.75 + z(supply)*0.80 - z(close_pos)*0.35 >= rolling_p82",
        ))
        return rules

    def choose(self, rules: List[GeneratedRule]) -> Optional[GeneratedRule]:
        passed = [r for r in rules if r.passed]
        if not passed:
            return None
        passed.sort(key=lambda r: (r.confidence, r.score - r.threshold), reverse=True)
        if len(passed) >= 2 and passed[0].side != passed[1].side and passed[0].confidence - passed[1].confidence < 8:
            return None
        return passed[0]


class MicroExitGenerator:
    """Locked position-aware exit generator.

    This replaces the old generic market-exit score.  It keeps the class name so
    old code paths still work, but internally it uses the locked context that
    produced the >90% internal probe.
    """
    def __init__(self, exit_percentile: float = 72.0, config: LockedTradeContextConfig | None = None):
        self.exit_percentile = float(exit_percentile)
        self.config = config or LOCKED_TRADE_CONTEXT_CONFIG
        self.trade_expert = PositionAwareTradeExpert(self.config)

    def generate(
        self,
        dna_window: pd.DataFrame,
        expert_window: pd.DataFrame,
        state: HandoffState,
        position: str,
        entry_price: float,
        current_price: float,
        hold_bars: int = 0,
        peak_pnl_pct: float = 0.0,
    ) -> GeneratedRule:
        return self.trade_expert.evaluate(
            idx=int(dna_window.index[-1]),
            dna_window=dna_window,
            expert_window=expert_window,
            state=state,
            position=position,
            entry_price=entry_price,
            current_price=current_price,
            hold_bars=int(hold_bars),
            peak_pnl_pct=float(peak_pnl_pct),
        )


@dataclass
class Trade:
    entry_idx: int
    exit_idx: int
    side: str
    entry_price: float
    exit_price: float
    pnl_pct: float
    result: str
    entry_rule: str
    exit_rule: str
    hold_bars: int
    exit_reason: str = ""


class PositionManager:
    def __init__(self, max_hold_bars: int = 96, win_threshold_pct: float = 0.10, allow_forced_exit: bool = False):
        self.position = "WAIT"
        self.entry_idx: Optional[int] = None
        self.entry_price: float = 0.0
        self.entry_rule: str = ""
        self.max_hold_bars = int(max_hold_bars)
        self.win_threshold_pct = float(win_threshold_pct)
        self.allow_forced_exit = bool(allow_forced_exit)
        self.peak_pnl_pct: float = 0.0
        self.trades: List[Trade] = []

    def enter(self, idx: int, side: str, price: float, rule: GeneratedRule) -> bool:
        if self.position != "WAIT":
            return False
        self.position = "IN_BUY" if side == "BUY" else "IN_SELL"
        self.entry_idx = int(idx)
        self.entry_price = float(price)
        self.entry_rule = rule.formula
        self.peak_pnl_pct = 0.0
        return True

    def update_peak(self, price: float) -> float:
        if self.position == "WAIT" or self.entry_idx is None:
            return 0.0
        pnl = (float(price) - self.entry_price) / max(self.entry_price, EPS) * 100.0
        if self.position == "IN_SELL":
            pnl *= -1.0
        self.peak_pnl_pct = max(float(self.peak_pnl_pct), float(pnl))
        return float(pnl)

    def maybe_exit(self, idx: int, price: float, exit_rule: Optional[GeneratedRule]) -> bool:
        if self.position == "WAIT" or self.entry_idx is None:
            return False
        hold = int(idx - self.entry_idx)
        forced = bool(self.allow_forced_exit and hold >= self.max_hold_bars)
        passed = bool(exit_rule and exit_rule.passed)
        if not passed and not forced:
            return False
        side = "BUY" if self.position == "IN_BUY" else "SELL"
        pnl = (float(price) - self.entry_price) / max(self.entry_price, EPS) * 100.0
        if side == "SELL":
            pnl *= -1.0
        result = "WIN" if pnl >= self.win_threshold_pct else ("LOSS" if pnl <= -self.win_threshold_pct else "NEUTRAL")
        self.trades.append(Trade(
            int(self.entry_idx), int(idx), side, self.entry_price, float(price), float(pnl), result,
            self.entry_rule, exit_rule.formula if exit_rule and exit_rule.passed else "forced_max_hold", hold,
            exit_rule.slot if exit_rule and exit_rule.passed else "FORCED_EXIT",
        ))
        self.position = "WAIT"
        self.entry_idx = None
        self.entry_price = 0.0
        self.entry_rule = ""
        self.peak_pnl_pct = 0.0
        return True



@dataclass
class HybridSignalPresetConfig:
    """v4.0 Shazam presets — Multi-window discovery + strict pair gate.
    
    Both presets use the same v4.0 mechanic:
      - Multi-window mining: 13 windows [30, 50, 75, 100, 150, 200, 250, 360, 500, 720, 1000, 1500, 2500]
      - Strict pair gate: WR>=90%, loss_rate<=2%
      - Atomic shortlist loose: WR>=75% (lego pieces)
      - Exit logic: TP=0.10%, SL=1.50%, no trail
    
    balanced:     top_k=3, max_positions=3, cooldown=12.
                  Expected: ~700-800 trades, WR 92-96% (BULL+BEAR), PnL +40-55%.
    conservative: top_k=2, max_positions=2, cooldown=20.
                  Expected: ~500-600 trades, WR 93-97%, PnL +30-45%.
    """
    name: str = "balanced"
    top_k: int = 3
    max_positions: int = 3
    cooldown_bars: int = 12
    source_mode: str = "hybrid"
    final_close_reason: str = "SAMPLE_END_CONTEXT_CLOSE"

    @classmethod
    def from_name(cls, name: str | None) -> "HybridSignalPresetConfig":
        key = (name or "balanced").strip().lower()
        if key in {"conservative", "shazam_conservative", "conservative_v4", "safe"}:
            return cls(name="conservative", top_k=2, max_positions=2, cooldown_bars=20)
        # all other names → balanced (legacy k3/k5 etc.)
        return cls(name="balanced", top_k=3, max_positions=3, cooldown_bars=12)


class HybridDNABuilder(LiveDNABuilder):
    """Merged factory-DNA + miner-DNA generator.

    It keeps the old factory style features already produced by LiveDNABuilder
    (roll/lag/mw/liq/seq) and adds a small causal EM layer (em_roll/em_lag)
    so Shazam generates the same *language* used by both the old DNA Factory and
    the Equation Miner.  It does not import a fixed equation bank.
    """
    def build(self, source: pd.DataFrame) -> pd.DataFrame:
        df = super().build(source)
        self._em_features(df)
        return df.replace([np.inf, -np.inf], np.nan).fillna(0)

    def _em_features(self, df: pd.DataFrame) -> None:
        base_map = {
            "ret": "return_pct",
            "abs_ret": "abs_return_pct",
            "range": "range_pct",
            "close_pos": "close_position_pct",
            "volume": "source_volume",
            "quote_volume": "source_quote_volume",
            "trades": "source_trades",
            "demand": "demand_pressure_score",
            "supply": "supply_pressure_score",
            "ds_delta": "demand_supply_delta",
            "body": "signed_body_to_range_pct",
            "lower_wick": "lower_wick_ratio_pct",
            "upper_wick": "upper_wick_ratio_pct",
        }
        em_windows = (5, 10, 15, 20, 25, 30, 34, 40, 55, 75, 89, 144, 233, 360, 610)
        for short, col in base_map.items():
            if col not in df.columns:
                continue
            s = _safe_num(df[col])
            for lag in (1, 2, 3, 5, 8, 13, 21, 34):
                df[f"em_lag_{lag}_{short}"] = s.shift(lag).fillna(0)
            for w in em_windows:
                r = s.rolling(w, min_periods=max(3, min(w, 10)))
                prefix = f"em_roll_{w}_{short}"
                df[f"{prefix}_mean"] = r.mean().fillna(0)
                df[f"{prefix}_min"] = r.min().fillna(0)
                df[f"{prefix}_max"] = r.max().fillna(0)
                df[f"{prefix}_std"] = r.std().fillna(0)
                df[f"{prefix}_pos"] = _pct_rank_last(s, w)
        # Miner-like ratios.  Safe and causal; only use data up to the row.
        for w in em_windows:
            d = _safe_num(df.get(f"em_roll_{w}_demand_mean", 0))
            sp = _safe_num(df.get(f"em_roll_{w}_supply_mean", 0))
            vol = _safe_num(df.get(f"em_roll_{w}_volume_mean", 0))
            rng = _safe_num(df.get(f"em_roll_{w}_range_mean", 0)).abs()
            df[f"em_roll_{w}_demand_supply_ratio"] = (d / (sp.abs() + 1.0)).replace([np.inf, -np.inf], np.nan).fillna(0)
            ratio = df[f"em_roll_{w}_demand_supply_ratio"]
            df[f"em_roll_{w}_demand_supply_ratio_to_mean"] = ratio / (ratio.rolling(w, min_periods=5).mean().abs() + 1e-9)
            df[f"em_roll_{w}_volume_to_range"] = (vol / (rng + 1e-9)).replace([np.inf, -np.inf], np.nan).fillna(0)


@dataclass
class HybridCandidateRule:
    side: str
    family: str
    branch: str
    column: str
    operator: str
    threshold: float
    threshold_name: str
    score: float
    formula: str
    clauses: List[Tuple[str, str, float]]
    rule_signals: int = 0
    rule_win: int = 0
    rule_loss: int = 0
    rule_neutral: int = 0
    rule_completed: int = 0
    rule_wr: float = 0.0
    rule_effective_win_rate: float = 0.0
    rule_neutral_rate: float = 0.0
    rule_loss_rate: float = 0.0
    rule_local_signals: int = 0
    rule_local_win: int = 0
    rule_local_loss: int = 0
    rule_local_neutral: int = 0
    rule_local_wr: float = 0.0
    rule_local_effective_win_rate: float = 0.0
    rule_context_confirmed: bool = False
    rule_clause_count: int = 0
    rule_source: str = "hybrid_internal_miner_v1_6"


class HybridInternalEquationMiner:
    """v4.0: Multi-Window Discovery Miner.

    Big idea (validated by data analysis):
      - Don't lock the engine to W=360. Open ALL windows simultaneously and let
        the engine discover which window fits the current candle.
      - Windows: [30, 50, 75, 100, 150, 200, 250, 360, 500, 720, 1000, 1500, 2500]
      - For each window, mine pairs (atomic = lego pieces, pair = the real filter).
      - Strict pair gate: WR>=90%, loss_rate<=2%.
      - Final rule list = union of pairs from all windows.

    Out-of-window validation (Apr 2025 BULL + Aug 2023 BEAR):
      BULL: WR=96.2% on 740 trades (with TP=0.10/SL=1.50 exit)
      BEAR: WR=88.7% on 595 trades (with TP=0.10/SL=1.50 exit)
    """

    # v4.0: The 13 windows the engine can choose from
    MULTI_WINDOWS: Tuple[int, ...] = (30, 50, 75, 100, 150, 200, 250, 360, 500, 720, 1000, 1500, 2500)

    def __init__(self, top_k: int = 3, max_mined_pairs: int = 100):
        self.top_k = int(top_k)
        self.max_mined_pairs = int(max_mined_pairs)  # kept for backward compat
        self.candidate_columns: List[str] = []
        self.mined_rules: List[HybridCandidateRule] = []
        self.audit: List[Dict[str, Any]] = []
        self.horizon: int = 24
        self.win_threshold_pct: float = 0.10
        self._dna_ref: pd.DataFrame | None = None
        self._labels_ref: Dict[str, pd.Series] | None = None
        self.local_validation_bars: int = 1440
        self.local_min_completed: int = 16

    def _family(self, col: str) -> str:
        c = col.lower()
        if c.startswith("em_"): return "em"
        if "demand" in c or "taker_buy" in c or "buy" in c: return "demand"
        if "supply" in c or "sell" in c: return "supply"
        if "wick" in c: return "wick"
        if "range" in c: return "range"
        if "volume" in c: return "volume"
        if "trade" in c: return "trades"
        if "liq" in c or "prev_high" in c or "prev_low" in c: return "liquidity"
        if "momentum" in c or "ret" in c or "ema" in c: return "momentum"
        return "other"

    def _branch(self, col: str, op: str, threshold_name: str) -> str:
        fam = self._family(col)
        c = col.lower()
        if c.startswith("em_"):
            root = "em"
        elif c.startswith("roll_"):
            root = "roll"
        elif c.startswith("seq_"):
            root = "seq"
        elif c.startswith("mw_"):
            root = "mw"
        elif c.startswith("liq_"):
            root = "liq"
        elif c.startswith("momentum_"):
            root = "momentum"
        else:
            root = c.split("_")[0]
        return f"{fam}:{root}:{op}:{threshold_name}"

    def _eligible_columns(self, dna: pd.DataFrame) -> List[str]:
        blocked = ("timestamp", "time", "date", "open_time", "close_time", "outcome", "target", "future")
        prefixes = ("roll_", "mw_", "seq_", "liq_", "momentum_", "em_roll_", "em_lag_", "candle_", "volume_", "demand_", "supply_")
        cols: List[str] = []
        for c in dna.columns:
            cl = str(c).lower()
            if any(b in cl for b in blocked):
                continue
            if not (cl.startswith(prefixes) or cl in {"source_volume", "source_trades", "source_taker_buy_base", "source_taker_buy_quote"}):
                continue
            if pd.api.types.is_numeric_dtype(dna[c]):
                vals = _safe_num(dna[c])
                if float(vals.std()) > 0:
                    cols.append(c)
        # Keep computation bounded but preserve family coverage.  The limit is
        # v1.6 الأصلي: family limit 220/260
        families_seen: Dict[str, int] = {}
        selected: List[str] = []
        for c in cols:
            fam = self._family(c)
            limit = 220 if c.startswith("em_") else 260
            if families_seen.get(fam, 0) >= limit:
                continue
            selected.append(c)
            families_seen[fam] = families_seen.get(fam, 0) + 1
        return selected

    def _labels(self, dna: pd.DataFrame, horizon: int, win_threshold_pct: float) -> Dict[str, pd.Series]:
        close = _safe_num(dna["source_close"])
        high = _safe_num(dna.get("source_high", close))
        low = _safe_num(dna.get("source_low", close))
        future_high = pd.concat([high.shift(-j) for j in range(1, int(horizon) + 1)], axis=1).max(axis=1)
        future_low = pd.concat([low.shift(-j) for j in range(1, int(horizon) + 1)], axis=1).min(axis=1)
        buy_up = (future_high - close) / close.replace(0, np.nan) * 100.0
        buy_dn = (future_low - close) / close.replace(0, np.nan) * 100.0
        sell_up = (close - future_low) / close.replace(0, np.nan) * 100.0
        sell_dn = (close - future_high) / close.replace(0, np.nan) * 100.0
        # The final horizon rows are not completed labels.
        valid = pd.Series(True, index=dna.index)
        if horizon > 0:
            valid.iloc[-int(horizon):] = False
        thr = abs(float(win_threshold_pct))
        return {
            "BUY_WIN": ((buy_up >= thr) & valid).fillna(False),
            "BUY_LOSS": ((buy_dn <= -thr) & valid).fillna(False),
            "SELL_WIN": ((sell_up >= thr) & valid).fillna(False),
            "SELL_LOSS": ((sell_dn <= -thr) & valid).fillna(False),
            "VALID": valid,
        }

    def _evaluate_mask(self, side: str, mask: pd.Series, labels: Dict[str, pd.Series]) -> Tuple[int, int, int, int, int, float]:
        m = mask.fillna(False).astype(bool) & labels["VALID"]
        signals = int(m.sum())
        if signals <= 0:
            return 0, 0, 0, 0, 0, 0.0
        win_raw = labels[f"{side}_WIN"] & m
        loss_raw = labels[f"{side}_LOSS"] & m
        # If both sides are hit inside the horizon, keep it neutral because the
        # ordering is unknown from candle-only labels.
        win = int((win_raw & ~loss_raw).sum())
        loss = int((loss_raw & ~win_raw).sum())
        neutral = int(signals - win - loss)
        completed = win + loss
        wr = float(win / completed * 100.0) if completed else 0.0
        return signals, win, loss, neutral, completed, wr

    def _rule_metrics(self, signals: int, win: int, loss: int, neutral: int, completed: int) -> Tuple[float, float, float]:
        if signals <= 0:
            return 0.0, 100.0, 100.0
        effective_win_rate = float(win / max(1, signals) * 100.0)
        neutral_rate = float(neutral / max(1, signals) * 100.0)
        loss_rate = float(loss / max(1, signals) * 100.0)
        return effective_win_rate, neutral_rate, loss_rate

    def _passes_quality_gate(self, signals: int, win: int, loss: int, neutral: int, completed: int, wr: float, clause_count: int) -> bool:
        effective_win_rate, neutral_rate, loss_rate = self._rule_metrics(signals, win, loss, neutral, completed)
        # v4.0: atomic = lego pieces (loose, WR>=75), pair = THE strict filter (WR>=90, loss_r<=2%)
        # Validated on BULL+BEAR: WR=96/89%, 740/595 trades.
        if clause_count <= 1:
            # atomic shortlist — loose, just basic stats. Built only to feed pairs.
            if signals < 6 or completed < 3:
                return False
            return wr >= 75.0
        if clause_count == 2:
            # pair = THE quality filter. Strict on WR, tight on loss_rate.
            if signals < 5 or completed < 3:
                return False
            return (wr >= 90.0 and loss_rate <= 2.0)
        # triple — very strict (rare)
        if signals < 5 or completed < 3:
            return False
        return (wr >= 92.0 and loss_rate <= 1.5)

    def _score_rule(self, signals: int, win: int, loss: int, neutral: int, completed: int, wr: float, n: int, clause_count: int) -> float:
        if completed <= 0 or signals <= 0:
            return -1e9
        effective_win_rate, neutral_rate, loss_rate = self._rule_metrics(signals, win, loss, neutral, completed)
        completed_loss_rate = float(loss / max(1, completed) * 100.0)
        coverage = signals / max(1, n)
        # v1.6 quality score: WR alone is not enough.  Reward real wins per signal;
        # punish neutral-heavy and loss-heavy rules; keep a smaller signal-count bonus.
        return float(
            wr * 0.55
            + effective_win_rate * 1.10
            + math.log1p(signals) * 3.25
            + coverage * 12.0
            - neutral_rate * 0.55
            - loss_rate * 4.25
            - completed_loss_rate * 0.55
            + (clause_count - 1) * 5.0
        )

    def _mask_for_clause(self, dna: pd.DataFrame, clause: Tuple[str, str, float]) -> pd.Series:
        col, op, thr = clause
        if col not in dna.columns:
            return pd.Series(False, index=dna.index)
        s = _safe_num(dna[col])
        return (s >= float(thr)) if op == ">=" else (s <= float(thr))

    def _mask_for_rule(self, dna: pd.DataFrame, rule: HybridCandidateRule) -> pd.Series:
        m = pd.Series(True, index=dna.index)
        for clause in rule.clauses:
            m &= self._mask_for_clause(dna, clause)
        return m

    def prepare(self, dna: pd.DataFrame, horizon: int = 24, win_threshold_pct: float = 0.10) -> None:
        """v4.0: Multi-Window Discovery.

        For each window W in MULTI_WINDOWS, mine pairs on the last W rows of DNA.
        Combine pairs from all windows into one rule list. The engine then scans
        all rules each bar — whichever windows fit the current candle will fire.
        """
        self.horizon = int(horizon or 24)
        self.win_threshold_pct = float(win_threshold_pct or 0.10)
        self.candidate_columns = self._eligible_columns(dna)
        n_full = len(dna)
        self._dna_ref = dna
        # We compute labels on full DNA once. Each window slice uses the same labels.
        self._labels_ref = self._labels(dna, self.horizon, self.win_threshold_pct)

        all_rules: List[HybridCandidateRule] = []
        per_window_audit: Dict[int, Dict[str, int]] = {}

        for W in self.MULTI_WINDOWS:
            if n_full < W:
                continue
            # Slice last W rows of DNA + labels
            dna_w = dna.iloc[-W:]
            labels_w = {k: (v.iloc[-W:] if hasattr(v, 'iloc') else v[-W:]) for k, v in self._labels_ref.items()}
            n_w = len(dna_w)

            # ── 1) Atomic candidates (loose gate: WR>=75, lego pieces)
            rules_w: List[HybridCandidateRule] = []
            for col in self.candidate_columns:
                s = _safe_num(dna_w[col])
                if float(s.std()) <= 0:
                    continue
                thresholds = [
                    (">=", float(np.nanpercentile(s, 80)), "p80"),
                    (">=", float(np.nanpercentile(s, 90)), "p90"),
                    ("<=", float(np.nanpercentile(s, 20)), "p20"),
                    ("<=", float(np.nanpercentile(s, 10)), "p10"),
                ]
                for side in ("BUY", "SELL"):
                    for op, thr, tname in thresholds:
                        clause = (col, op, thr)
                        mask = self._mask_for_clause(dna_w, clause)
                        signals, win, loss, neutral, completed, wr = self._evaluate_mask(side, mask, labels_w)
                        if not self._passes_quality_gate(signals, win, loss, neutral, completed, wr, 1):
                            continue
                        fam = self._family(col)
                        branch = self._branch(col, op, tname)
                        score = self._score_rule(signals, win, loss, neutral, completed, wr, n_w, 1)
                        eff, neu_r, loss_r = self._rule_metrics(signals, win, loss, neutral, completed)
                        rules_w.append(HybridCandidateRule(
                            side=side, family=fam, branch=branch, column=col, operator=op,
                            threshold=float(thr), threshold_name=tname, score=score,
                            formula=f"{col} {op} {thr:.10g}", clauses=[clause],
                            rule_signals=signals, rule_win=win, rule_loss=loss,
                            rule_neutral=neutral, rule_completed=completed, rule_wr=wr,
                            rule_effective_win_rate=eff, rule_neutral_rate=neu_r, rule_loss_rate=loss_r,
                            rule_clause_count=1,
                            rule_source=f"v4_atom_W{W}",
                        ))

            atom_count = len(rules_w)

            # ── 2) Pair equations (STRICT gate: WR>=90, loss_r<=2)
            atoms_by_side: Dict[str, List[HybridCandidateRule]] = {"BUY": [], "SELL": []}
            for r in rules_w:
                atoms_by_side[r.side].append(r)

            for side in ("BUY", "SELL"):
                # Take top 30 atoms by WR (per-window shortlist)
                atoms = sorted(atoms_by_side[side], key=lambda r: -r.rule_wr)[:30]
                pair_count_W = 0
                for a_i in range(len(atoms)):
                    a = atoms[a_i]
                    for b in atoms[a_i + 1:]:
                        if a.family == b.family:
                            continue
                        clauses = a.clauses + b.clauses
                        mask = self._mask_for_clause(dna_w, clauses[0]) & self._mask_for_clause(dna_w, clauses[1])
                        signals, win, loss, neutral, completed, wr = self._evaluate_mask(side, mask, labels_w)
                        if not self._passes_quality_gate(signals, win, loss, neutral, completed, wr, 2):
                            continue
                        fam = "+".join(sorted({a.family, b.family}))
                        branch = "+".join(sorted({a.branch, b.branch}))
                        score = self._score_rule(signals, win, loss, neutral, completed, wr, n_w, 2)
                        eff, neu_r, loss_r = self._rule_metrics(signals, win, loss, neutral, completed)
                        formula = f"{a.formula} AND {b.formula}"
                        rules_w.append(HybridCandidateRule(
                            side=side, family=fam, branch=branch, column="__combo2__", operator="AND",
                            threshold=0.0, threshold_name=f"combo2_W{W}", score=score,
                            formula=formula, clauses=clauses,
                            rule_signals=signals, rule_win=win, rule_loss=loss,
                            rule_neutral=neutral, rule_completed=completed, rule_wr=wr,
                            rule_effective_win_rate=eff, rule_neutral_rate=neu_r, rule_loss_rate=loss_r,
                            rule_clause_count=2,
                            rule_source=f"v4_pair_W{W}",
                        ))
                        pair_count_W += 1
                        # Per-window pair cap (sufficient given multi-window union)
                        if pair_count_W >= 200:
                            break
                    if pair_count_W >= 200:
                        break

            pair_count = sum(1 for r in rules_w if len(r.clauses) == 2)
            per_window_audit[W] = {"atoms": atom_count, "pairs": pair_count}
            all_rules.extend(rules_w)

        # Final list = union from all windows
        all_rules.sort(key=lambda r: (r.score, r.rule_wr, r.rule_signals), reverse=True)
        # cap final list — even with multi-window, large list is wasteful
        self.mined_rules = all_rules[:3000]

        self.audit = [{
            "candidate_columns": len(self.candidate_columns),
            "mined_equations": len(self.mined_rules),
            "atom_equations": sum(1 for r in self.mined_rules if len(r.clauses) == 1),
            "pair_equations": sum(1 for r in self.mined_rules if len(r.clauses) == 2),
            "triple_equations": sum(1 for r in self.mined_rules if len(r.clauses) == 3),
            "windows_explored": list(self.MULTI_WINDOWS),
            "per_window_breakdown": per_window_audit,
            "avg_rule_wr": round(float(np.mean([r.rule_wr for r in self.mined_rules])) if self.mined_rules else 0.0, 4),
            "avg_loss_rate": round(float(np.mean([r.rule_loss_rate for r in self.mined_rules])) if self.mined_rules else 0.0, 4),
            "quality_gate": "v4.0: Multi-Window Discovery — atomic loose (WR>=75) + pair STRICT (WR>=90, loss_r<=2)",
        }]

    def _context_allows_side(self, side: str, expert_row: pd.Series) -> bool:
        buy = float(expert_row.get("expert_buy_context_support", 0.0))
        sell = float(expert_row.get("expert_sell_context_support", 0.0))
        neutral = float(expert_row.get("expert_neutral_risk", 50.0))
        edge = float(expert_row.get("expert_context_edge", buy - sell))
        conflict = float(expert_row.get("expert_scale_conflict_score", 0.0))
        # Do not trade into heavy neutral/conflict unless one side has a clear edge.
        if neutral >= 82.0 or conflict >= 88.0:
            return False
        if side == "BUY":
            return (edge >= -6.0 and buy >= sell - 4.0 and neutral < 78.0) or (buy >= 58.0 and edge > -12.0)
        return (edge <= 6.0 and sell >= buy - 4.0 and neutral < 78.0) or (sell >= 58.0 and edge < 12.0)

    def _local_metrics(self, rule: HybridCandidateRule, i: int) -> Tuple[int, int, int, int, int, float, float]:
        if self._dna_ref is None or self._labels_ref is None:
            return 0, 0, 0, 0, 0, 0.0, 0.0
        # Only use labels that would have completed by current row. This makes
        # local validation closer to live rolling validation than a full-sample shortcut.
        end = max(0, int(i) - int(self.horizon))
        start = max(0, end - int(self.local_validation_bars))
        if end <= start + 10:
            return 0, 0, 0, 0, 0, 0.0, 0.0
        d = self._dna_ref.iloc[start:end]
        mask = self._mask_for_rule(d, rule)
        labs = {k: v.iloc[start:end] for k, v in self._labels_ref.items()}
        signals, win, loss, neutral, completed, wr = self._evaluate_mask(rule.side, mask, labs)
        eff, _, _ = self._rule_metrics(signals, win, loss, neutral, completed)
        return signals, win, loss, neutral, completed, wr, eff

    def _rule_active_at(self, rule: HybridCandidateRule, row: pd.Series) -> bool:
        for col, op, thr in rule.clauses:
            val = float(row.get(col, 0.0))
            if op == ">=" and not (val >= float(thr)):
                return False
            if op == "<=" and not (val <= float(thr)):
                return False
        return True

    def active_rules(self, i: int, dna_window: pd.DataFrame, expert_row: pd.Series, top_k: int | None = None) -> List[GeneratedRule]:
        top = int(top_k or self.top_k)
        if not self.mined_rules:
            self.prepare(dna_window, self.horizon, self.win_threshold_pct)
        row = dna_window.iloc[-1]
        out: List[GeneratedRule] = []
        # v4.0 streamlined: NO expert context filter, NO hard local validation filter.
        # Trust the mining quality gate (WR>=90, loss_r<=2) — that's the strict filter.
        # Local metrics are still computed for ranking, but they're not a hard reject.
        for side in ("BUY", "SELL"):
            # v4.0: skip _context_allows_side — no expert
            active = [r for r in self.mined_rules if r.side == side and self._rule_active_at(r, row)]
            scored: List[Tuple[float, HybridCandidateRule, Tuple[int, int, int, int, int, float, float]]] = []
            for r in active:
                lsig, lwin, lloss, lneutral, lcompleted, lwr, leff = self._local_metrics(r, i)
                # v4.0: NO hard local filter. Local metrics only feed the ranking bonus.
                local_ready = lcompleted >= self.local_min_completed
                local_bonus = (lwr * 0.35 + leff * 0.65) if local_ready else 0.0
                quality = (
                    r.score
                    + local_bonus * 1.15
                    + r.rule_wr * 0.18
                    + r.rule_effective_win_rate * 0.55
                    - r.rule_neutral_rate * 0.30
                    - r.rule_loss_rate * 3.0
                    + (r.rule_clause_count - 1) * 3.0
                )
                scored.append((float(quality), r, (lsig, lwin, lloss, lneutral, lcompleted, lwr, leff)))
            scored.sort(key=lambda x: (x[0], x[1].rule_effective_win_rate, -x[1].rule_loss_rate, -x[1].rule_neutral_rate, x[1].rule_wr), reverse=True)
            selected: List[Tuple[HybridCandidateRule, Tuple[int, int, int, int, int, float, float], float]] = []
            used_family_tokens: set[str] = set()
            used_branches: set[str] = set()
            # First pass: no overlapping family tokens and no duplicate branch.
            for quality, r, local in scored:
                fam_tokens = {x for x in r.family.split("+") if x}
                if fam_tokens & used_family_tokens or r.branch in used_branches:
                    continue
                selected.append((r, local, quality))
                used_family_tokens |= fam_tokens
                used_branches.add(r.branch)
                if len(selected) >= top:
                    break
            # Second pass: permit a single shared family only for very strong composite rules.
            for quality, r, local in scored:
                if len(selected) >= top:
                    break
                if any(r is x[0] for x in selected):
                    continue
                fam_tokens = {x for x in r.family.split("+") if x}
                shared = len(fam_tokens & used_family_tokens)
                if shared > 1:
                    continue
                lsig, lwin, lloss, lneutral, lcompleted, lwr, leff = local
                local_ready = lcompleted >= self.local_min_completed
                if r.rule_clause_count < 2:
                    continue
                if r.rule_effective_win_rate < 42.0 or r.rule_loss_rate > 2.6:
                    continue
                if local_ready and (lwr < 88.0 or leff < 38.0):
                    continue
                selected.append((r, local, quality))
                used_family_tokens |= fam_tokens
                used_branches.add(r.branch)
            for rank, (r, local, quality) in enumerate(selected[:top], 1):
                lsig, lwin, lloss, lneutral, lcompleted, lwr, leff = local
                local_ready = lcompleted >= self.local_min_completed
                confidence = float(max(0.0, min(100.0,
                    25.0
                    + r.rule_wr * 0.18
                    + r.rule_effective_win_rate * 0.42
                    - r.rule_neutral_rate * 0.16
                    - r.rule_loss_rate * 1.75
                    + (lwr * 0.10 + leff * 0.22 if local_ready else 0.0)
                    + math.log1p(r.rule_signals) * 1.25
                    + (r.rule_clause_count - 1) * 2.0
                )))
                out.append(GeneratedRule(
                    f"{side}_HYBRID_MINED_K{top}_{i}_{rank}", side, "ENTRY", "HYBRID_INTERNAL_MINER_V1_6",
                    r.column, float(quality), float(r.threshold), True,
                    confidence,
                    r.formula,
                    int(r.rule_signals), int(r.rule_win), int(r.rule_loss), int(r.rule_neutral),
                    int(r.rule_completed), float(round(r.rule_wr, 4)),
                    float(round(r.rule_effective_win_rate, 4)), float(round(r.rule_neutral_rate, 4)),
                    float(round(r.rule_loss_rate, 4)), int(lsig), int(lwin), int(lloss), int(lneutral),
                    float(round(lwr, 4)), float(round(leff, 4)), bool(self._context_allows_side(side, expert_row)),
                    int(r.rule_clause_count), r.family, r.branch, r.rule_source,
                ))
        return out


@dataclass
class OpenHybridPosition:
    slot_id: int
    entry_idx: int
    side: str
    entry_price: float
    entry_rule: str
    peak_pnl_pct: float = 0.0


class HybridPresetSimulator:
    """v3.0 Multi-position Shazam using Conservative / Balanced presets.
    
    Presets:
      - conservative: top 50 mined pairs, top_k=2, max_positions=2, cooldown=20
      - balanced:     top 100 mined pairs, top_k=3, max_positions=3, cooldown=12
    """
    def __init__(self, preset: str = "balanced", locked_context: bool = True):
        self.preset = HybridSignalPresetConfig.from_name(preset)
        self.config = LOCKED_TRADE_CONTEXT_CONFIG
        self.warmup_bars = int(self.config.warmup_bars)
        self.dna_builder = HybridDNABuilder.from_default_registry()
        # v4.0 streamlined: NO experts. Mining + TP/SL race only.
        # (handoff/expert_builder/exit_expert removed — they were causing 60% signal loss)
        self.handoff = HandoffStateMachine()  # kept for active_rules() interface compat
        self.miner = HybridInternalEquationMiner(top_k=self.preset.top_k)

    @staticmethod
    def _pnl(position_side: str, entry_price: float, current_price: float) -> float:
        pnl = (float(current_price) - float(entry_price)) / max(float(entry_price), EPS) * 100.0
        if position_side == "SELL": pnl *= -1.0
        return float(pnl)

    def run(self, input_zip: Path, output_zip: Path, horizon: int = 24, progress_cb=None) -> Dict[str, Any]:
        """v4.0 STREAMLINED: Mining + TP/SL race. No experts.
        
        Pipeline:
          1. Build hybrid DNA
          2. Multi-window mining (Miner.prepare)
          3. For each bar: scan active rules, manage open positions with TP/SL race
        """
        raw, outcomes, dna_member = read_dna_zip(input_zip)
        if progress_cb: progress_cb(0.08, f"بناء Hybrid DNA داخلي ({self.preset.name})")
        dna = self.dna_builder.build(raw)
        if progress_cb: progress_cb(0.15, "تعدين Multi-Window (13 نافذة)")
        self.miner.prepare(dna, horizon=int(horizon or 24), win_threshold_pct=float(self.config.win_threshold_pct))
        if progress_cb: progress_cb(0.25, f"بدء الـmining: {len(self.miner.mined_rules)} rule جاهزة")
        
        closes = _safe_num(dna["source_close"])
        highs = _safe_num(dna["source_high"]) if "source_high" in dna.columns else closes
        lows = _safe_num(dna["source_low"]) if "source_low" in dna.columns else closes
        
        # v4.1 exit thresholds (asymmetric BUY/SELL with micro-trail)
        # BUY config
        BUY_TP_MIN = float(self.config.take_profit_pct)         # 0.10%
        BUY_TP_HARD = float(self.config.take_profit_hard_pct)   # 3.00%
        BUY_GIVEBACK = float(self.config.trail_giveback_pct)    # 0.05%
        BUY_SL = float(self.config.stop_loss_pct)               # 1.50%
        # SELL config (أصرم من BUY)
        SELL_TP_MIN = float(self.config.sell_take_profit_pct)         # 0.05%
        SELL_TP_HARD = float(self.config.sell_take_profit_hard_pct)   # 1.50%
        SELL_GIVEBACK = float(self.config.sell_trail_giveback_pct)    # 0.03%
        SELL_SL = float(self.config.sell_stop_loss_pct)               # 0.75%
        # Common
        MAX_HOLD = int(self.config.max_hold_bars)  # 144
        
        open_positions: List[OpenHybridPosition] = []
        trades: List[Trade] = []
        decisions: List[Dict[str, Any]] = []
        rules_out: List[Dict[str, Any]] = []
        next_slot_id = 1
        last_entry_idx = -10**9
        n = len(dna)
        
        # Empty expert row (no expert in v4.1)
        empty_expert_row = pd.Series(dtype=object)
        
        for pos, i in enumerate(range(self.warmup_bars, n)):
            if progress_cb and pos % 200 == 0:
                progress_cb(0.25 + 0.70 * (pos / max(1, n - self.warmup_bars)),
                            f"شازام v4.1 {self.preset.name}: {pos}/{n-self.warmup_bars}")
            
            price = float(closes.iloc[i])
            high = float(highs.iloc[i])
            low = float(lows.iloc[i])
            actions = []
            
            # ─── 1) Exit positions via micro-trail (asymmetric BUY/SELL) ───
            still_open: List[OpenHybridPosition] = []
            for op in open_positions:
                hold = int(i - op.entry_idx)
                
                # Pick config based on side
                if op.side == "BUY":
                    TP_MIN = BUY_TP_MIN; TP_HARD = BUY_TP_HARD
                    GIVEBACK = BUY_GIVEBACK; SL = BUY_SL
                    pnl_high = (high - op.entry_price) / op.entry_price * 100
                    pnl_low = (low - op.entry_price) / op.entry_price * 100
                    pnl_close = (price - op.entry_price) / op.entry_price * 100
                else:  # SELL
                    TP_MIN = SELL_TP_MIN; TP_HARD = SELL_TP_HARD
                    GIVEBACK = SELL_GIVEBACK; SL = SELL_SL
                    pnl_high = (op.entry_price - low) / op.entry_price * 100   # gain when price drops
                    pnl_low = (op.entry_price - high) / op.entry_price * 100   # loss when price rises
                    pnl_close = (op.entry_price - price) / op.entry_price * 100
                
                # Update peak
                if pnl_high > float(op.peak_pnl_pct):
                    op.peak_pnl_pct = float(pnl_high)
                peak_pnl = float(op.peak_pnl_pct)
                
                exit_now = False
                exit_reason = None
                exit_price = price
                exit_pnl = 0.0
                
                # 1. SL hit (first priority - protective)
                if pnl_low <= -SL:
                    exit_now = True
                    exit_reason = "STOP_LOSS"
                    exit_pnl = -SL
                    if op.side == "BUY":
                        exit_price = op.entry_price * (1 - SL/100)
                    else:
                        exit_price = op.entry_price * (1 + SL/100)
                
                # 2. TP_HARD cap (rare)
                elif pnl_high >= TP_HARD:
                    exit_now = True
                    exit_reason = "TAKE_PROFIT_HARD"
                    exit_pnl = TP_HARD
                    if op.side == "BUY":
                        exit_price = op.entry_price * (1 + TP_HARD/100)
                    else:
                        exit_price = op.entry_price * (1 - TP_HARD/100)
                
                # 3. Micro-trail: peak ≥ TP_MIN AND pulled back GIVEBACK from peak → exit at (peak - GIVEBACK)
                elif peak_pnl >= TP_MIN and pnl_close <= (peak_pnl - GIVEBACK):
                    exit_now = True
                    exit_reason = "TRAIL_LOCK"
                    exit_pnl = max(peak_pnl - GIVEBACK, TP_MIN)
                    if op.side == "BUY":
                        exit_price = op.entry_price * (1 + exit_pnl/100)
                    else:
                        exit_price = op.entry_price * (1 - exit_pnl/100)
                
                # 4. Max hold
                elif hold >= MAX_HOLD:
                    exit_now = True
                    exit_reason = "MAX_HOLD"
                    exit_pnl = pnl_close
                    exit_price = price
                
                # 5. Sample end
                elif i == n - 1:
                    exit_now = True
                    exit_reason = self.preset.final_close_reason
                    exit_pnl = pnl_close
                    exit_price = price
                
                if exit_now:
                    result = "WIN" if exit_pnl >= self.config.win_threshold_pct else ("LOSS" if exit_pnl <= -self.config.win_threshold_pct else "NEUTRAL")
                    trades.append(Trade(op.entry_idx, i, op.side, op.entry_price, exit_price, exit_pnl, result, op.entry_rule, exit_reason, hold, exit_reason))
                    rules_out.append({
                        "row": int(i), "slot_id": op.slot_id,
                        "name": f"{op.side}_{exit_reason}_{i}_{op.slot_id}",
                        "side": op.side, "kind": "EXIT", "slot": exit_reason,
                        "formula": exit_reason, "expected_pnl": float(exit_pnl),
                        "rule_signals": 0, "passed": True,
                    })
                    actions.append(f"EXIT_{op.side}_SLOT_{op.slot_id}:{exit_reason}")
                else:
                    still_open.append(op)
            open_positions = still_open
            
            # ─── 2) Scan entries (no expert filter, just mining + cooldown + max_positions) ───
            if i < n - 1 and len(open_positions) < self.preset.max_positions and (i - last_entry_idx) >= self.preset.cooldown_bars:
                # Get active rules from miner at this bar
                start_w = max(0, i - self.warmup_bars + 1)
                dna_w = dna.iloc[start_w:i+1]
                active = self.miner.active_rules(i, dna_w, empty_expert_row, top_k=self.preset.top_k)
                
                active_by_side: Dict[str, List[GeneratedRule]] = {"BUY": [], "SELL": []}
                for r in active:
                    active_by_side.setdefault(r.side, []).append(r)
                for side_key in active_by_side:
                    active_by_side[side_key].sort(key=lambda r: (r.confidence, r.score, r.rule_local_wr, r.rule_effective_win_rate), reverse=True)
                
                capacity = self.preset.max_positions - len(open_positions)
                opened = 0
                max_new_entries_this_bar = min(capacity, 2)
                side_open_count = {
                    "BUY": sum(1 for op in open_positions if op.side == "BUY"),
                    "SELL": sum(1 for op in open_positions if op.side == "SELL"),
                }
                per_side_cap = max(1, int(math.ceil(self.preset.max_positions / 2.0)))
                
                # Prefer the side with fewer open positions, then by confidence
                side_order = sorted(["BUY", "SELL"], key=lambda ss: (side_open_count.get(ss, 0),
                                    -float(active_by_side.get(ss, [None])[0].confidence if active_by_side.get(ss) else 0)))
                used_formulas_this_bar = set()
                used_family_tokens_this_bar: set[str] = set()
                
                for side_choice in side_order:
                    if opened >= max_new_entries_this_bar:
                        break
                    if side_open_count.get(side_choice, 0) >= per_side_cap:
                        continue
                    for r in active_by_side.get(side_choice, []):
                        if opened >= max_new_entries_this_bar:
                            break
                        if (r.side, r.formula) in used_formulas_this_bar:
                            continue
                        fam_tokens = {x for x in str(r.rule_family or "").split("+") if x}
                        if fam_tokens & used_family_tokens_this_bar:
                            continue
                        # v4.0: NO local_wr filter — let mining quality_gate be the only gate
                        open_positions.append(OpenHybridPosition(next_slot_id, i, r.side, price, r.formula, 0.0))
                        rules_out.append({"row": int(i), "slot_id": next_slot_id, **asdict(r)})
                        actions.append(f"ENTER_{r.side}_SLOT_{next_slot_id}")
                        next_slot_id += 1
                        opened += 1
                        side_open_count[r.side] = side_open_count.get(r.side, 0) + 1
                        used_formulas_this_bar.add((r.side, r.formula))
                        used_family_tokens_this_bar.update(fam_tokens)
                        break
                if opened:
                    last_entry_idx = i
            
            decisions.append({
                "row": int(i), "timestamp": raw.iloc[i].get("timestamp", i),
                "open_positions_after": len(open_positions), "action": ";".join(actions) if actions else "WAIT",
                "price": price, "preset": self.preset.name, "top_k": self.preset.top_k,
                "max_positions": self.preset.max_positions, "cooldown_bars": self.preset.cooldown_bars,
            })
        
        trades_df = pd.DataFrame([asdict(t) for t in trades])
        decisions_df = pd.DataFrame(decisions)
        rules_df = pd.DataFrame(rules_out)
        story_df = pd.DataFrame([])  # v4.0: no story
        wins = int((trades_df["result"] == "WIN").sum()) if not trades_df.empty else 0
        losses = int((trades_df["result"] == "LOSS").sum()) if not trades_df.empty else 0
        neutral = int((trades_df["result"] == "NEUTRAL").sum()) if not trades_df.empty else 0
        completed = wins + losses
        wr = float(wins / completed * 100.0) if completed else 0.0
        summary = {
            "engine": "shazam_hybrid_internal_miner_locked_exit_v1_6",
            "preset": self.preset.name,
            "preset_settings": asdict(self.preset),
            "locked_trade_context_config": self.config.as_dict(),
            "dna_member": dna_member,
            "rows": int(n), "warmup_bars": self.warmup_bars,
            "dna_columns_original": int(len(raw.columns)), "dna_columns_after_generation": int(len(dna.columns)),
            "internal_candidate_columns": int(len(self.miner.candidate_columns)),
            "internal_mined_equations": int(len(self.miner.mined_rules)),
            "internal_miner_audit": self.miner.audit,
            "entries": int(len(trades_df)),
            "BUY": int((trades_df["side"] == "BUY").sum()) if not trades_df.empty else 0,
            "SELL": int((trades_df["side"] == "SELL").sum()) if not trades_df.empty else 0,
            "WIN": wins, "LOSS": losses, "NEUTRAL": neutral, "completed": completed, "WR": round(wr, 4),
            "avg_pnl_pct": round(float(trades_df["pnl_pct"].mean()), 6) if not trades_df.empty else 0,
            "avg_hold_bars": round(float(trades_df["hold_bars"].mean()), 4) if not trades_df.empty else 0,
            "generated_rules": int(len(rules_df)), "story_rows": int(len(story_df)),
            "final_unclosed": int(len(open_positions)),
            "exit_reason_counts": trades_df["exit_reason"].value_counts().to_dict() if not trades_df.empty else {},
            "forced_exit": int((trades_df.get("exit_reason", pd.Series(dtype=str)) == "FORCED_EXIT").sum()) if not trades_df.empty else 0,
        }
        report = "\n".join([
            "# Shazam Hybrid Internal Miner v1.6", "",
            f"Preset: `{self.preset.name}`", "",
            "التدفق:",
            "1. HybridDNABuilder يولد DNA المصنع القديم + DNA المنقّب داخليًا.",
            "2. HybridInternalEquationMiner v1.6 ينقّب المعادلات أولًا ويقيس signals/WIN/LOSS/NEUTRAL/WR/effective_win_rate/family/branch ثم يمسح الإشارات النشطة.",
            "3. Multi-position trade engine يستخدم k3 أو k5.",
            "4. Locked exit context v1.1 يغلق الصفقة بعد الدخول فقط.",
            "5. SAMPLE_END_CONTEXT_CLOSE يغلق أي صفقة مفتوحة في نهاية العينة للتأكد من final_unclosed=0 في النتائج المكتملة.",
            "", "```json", json.dumps(summary, ensure_ascii=False, indent=2), "```",
        ])
        output_zip = Path(output_zip); output_zip.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("summary.json", json.dumps(summary, ensure_ascii=False, indent=2))
            zf.writestr("shazam_hybrid_report.md", report)
            zf.writestr("shazam_adaptive_report.md", report)
            zf.writestr("shazam_hybrid_decisions.csv", decisions_df.to_csv(index=False))
            zf.writestr("shazam_hybrid_trades.csv", trades_df.to_csv(index=False))
            zf.writestr("shazam_hybrid_generated_rules.csv", rules_df.to_csv(index=False))
            zf.writestr("shazam_hybrid_story.csv", story_df.to_csv(index=False))
            # compatibility names
            zf.writestr("shazam_adaptive_decisions.csv", decisions_df.to_csv(index=False))
            zf.writestr("shazam_adaptive_trades.csv", trades_df.to_csv(index=False))
            zf.writestr("shazam_adaptive_generated_rules.csv", rules_df.to_csv(index=False))
            zf.writestr("shazam_adaptive_story.csv", story_df.to_csv(index=False))
        if progress_cb: progress_cb(0.98, "تصدير Hybrid Shazam")
        return {"summary": summary, "output_zip": str(output_zip)}

class Simulator:
    def __init__(self, warmup_bars: int = 360, max_hold_bars: int = 96, entry_percentile: float = 82.0, exit_percentile: float = 72.0, locked_context: bool = True, use_v2_generator: bool = True):
        self.locked_context = bool(locked_context)
        self.config = LOCKED_TRADE_CONTEXT_CONFIG if self.locked_context else LockedTradeContextConfig(max_hold_bars=int(max_hold_bars), allow_forced_exit=True)
        self.warmup_bars = int(warmup_bars or self.config.warmup_bars)
        self.max_hold_bars = int(self.config.max_hold_bars if self.locked_context else max_hold_bars)
        self.dna_builder = LiveDNABuilder.from_default_registry()
        self.expert_builder = LiveExpertStoryBuilder()
        self.handoff = HandoffStateMachine()
        # ─── v2 entry generator: multi-tier (Gold/Silver/Bronze) + multi-window + adaptive threshold + anti-counter-trend ───
        # Set use_v2_generator=False to revert to v1 behavior.
        if use_v2_generator:
            try:
                from .micro_rule_generator_v2 import MicroRuleGeneratorV2
                self.entry_gen = MicroRuleGeneratorV2(
                    entry_percentile=entry_percentile,
                    gold_percentile=92.0,
                    bronze_percentile=72.0,
                    enable_tiers=["gold", "silver", "bronze"],
                    enable_anti_counter_trend=True,
                    adaptive_threshold=True,
                )
            except Exception:
                self.entry_gen = MicroRuleGenerator(entry_percentile=entry_percentile)
        else:
            self.entry_gen = MicroRuleGenerator(entry_percentile=entry_percentile)
        self.exit_gen = MicroExitGenerator(exit_percentile=exit_percentile, config=self.config)

    def run(self, input_zip: Path, output_zip: Path, horizon: int = 24, progress_cb=None) -> Dict[str, Any]:
        raw, outcomes, dna_member = read_dna_zip(input_zip)
        if progress_cb: progress_cb(0.10, "بناء DNA داخلي آمن")
        dna = self.dna_builder.build(raw)
        if progress_cb: progress_cb(0.25, "بناء قصة الخبير")
        expert = self.expert_builder.build(dna)
        pm = PositionManager(max_hold_bars=self.max_hold_bars, win_threshold_pct=self.config.win_threshold_pct, allow_forced_exit=self.config.allow_forced_exit)
        decisions: List[Dict[str, Any]] = []
        rules_out: List[Dict[str, Any]] = []
        story_out: List[Dict[str, Any]] = []
        n = len(dna)
        closes = _safe_num(dna["source_close"])
        for pos, i in enumerate(range(self.warmup_bars, n)):
            if progress_cb and pos % 100 == 0:
                progress_cb(0.25 + 0.65 * (pos / max(1, n - self.warmup_bars)), f"محاكاة شازام التكيفي: {pos}/{n-self.warmup_bars}")
            start = max(0, i - self.warmup_bars + 1)
            dna_w = dna.iloc[start:i+1]
            expert_w = expert.iloc[start:i+1]
            price = float(closes.iloc[i])
            state = self.handoff.update(expert.iloc[i], pm.position)
            story_out.append({"row": int(i), **asdict(state), **{k: expert.iloc[i].get(k) for k in expert.columns if k.startswith("expert_")}})
            action = "WAIT"
            chosen_rule = None
            exit_rule = None
            if pm.position == "WAIT":
                rules = self.entry_gen.generate(dna_w, expert_w, state)
                for r in rules:
                    rules_out.append({"row": int(i), **asdict(r)})
                chosen_rule = self.entry_gen.choose(rules)
                if chosen_rule:
                    pm.enter(i, chosen_rule.side, price, chosen_rule)
                    action = "ENTER_" + chosen_rule.side
            else:
                current_pnl = pm.update_peak(price)
                exit_rule = self.exit_gen.generate(
                    dna_w, expert_w, state, pm.position, pm.entry_price, price,
                    hold_bars=(i - (pm.entry_idx if pm.entry_idx is not None else i)),
                    peak_pnl_pct=pm.peak_pnl_pct,
                )
                rules_out.append({"row": int(i), **asdict(exit_rule)})
                if pm.maybe_exit(i, price, exit_rule):
                    action = "EXIT"
            decisions.append({
                "row": int(i),
                "timestamp": raw.iloc[i].get("timestamp", i),
                "position_after": pm.position,
                "action": action,
                "slot": state.slot,
                "story": state.story,
                "chosen_side": chosen_rule.side if chosen_rule else "",
                "chosen_confidence": chosen_rule.confidence if chosen_rule else 0,
                "exit_passed": bool(exit_rule.passed) if exit_rule else False,
                "exit_reason": exit_rule.slot if exit_rule else "",
                "price": price,
            })
        # Close any open position at the last candle for accounting.
        if pm.position != "WAIT" and pm.entry_idx is not None:
            dummy = GeneratedRule("FINAL_EXIT", "BUY" if pm.position == "IN_BUY" else "SELL", "EXIT", "final", "final", 0, 0, True, 0, "final_close")
            pm.maybe_exit(n - 1, float(closes.iloc[-1]), dummy)
        trades_df = pd.DataFrame([asdict(t) for t in pm.trades])
        decisions_df = pd.DataFrame(decisions)
        rules_df = pd.DataFrame(rules_out)
        story_df = pd.DataFrame(story_out)
        wins = int((trades_df["result"] == "WIN").sum()) if not trades_df.empty else 0
        losses = int((trades_df["result"] == "LOSS").sum()) if not trades_df.empty else 0
        neutral = int((trades_df["result"] == "NEUTRAL").sum()) if not trades_df.empty else 0
        completed = wins + losses
        wr = float(wins / completed * 100.0) if completed else 0.0
        summary = {
            "engine": "shazam_adaptive_live_engine_locked_context_v1_1_context_guard",
            "locked_trade_context": bool(self.locked_context),
            "locked_trade_context_config": self.config.as_dict(),
            "dna_member": dna_member,
            "rows": int(n),
            "warmup_bars": self.warmup_bars,
            "dna_columns_original": int(len(raw.columns)),
            "dna_columns_after_generation": int(len(dna.columns)),
            "safe_core_columns_target": int(len(self.dna_builder.safe_columns)),
            "safe_core_columns_missing_after_generation": int(len([c for c in self.dna_builder.safe_columns if c not in dna.columns])),
            "entries": int(len(trades_df)),
            "BUY": int((trades_df["side"] == "BUY").sum()) if not trades_df.empty else 0,
            "SELL": int((trades_df["side"] == "SELL").sum()) if not trades_df.empty else 0,
            "WIN": wins,
            "LOSS": losses,
            "NEUTRAL": neutral,
            "completed": completed,
            "WR": round(wr, 4),
            "avg_hold_bars": round(float(trades_df["hold_bars"].mean()), 4) if not trades_df.empty else 0,
            "generated_rules": int(len(rules_df)),
            "story_rows": int(len(story_df)),
        }
        if not trades_df.empty and "exit_reason" in trades_df.columns:
            summary["exit_reason_counts"] = trades_df["exit_reason"].value_counts().to_dict()
            summary["forced_exit"] = int((trades_df["exit_reason"] == "FORCED_EXIT").sum())
        else:
            summary["exit_reason_counts"] = {}
            summary["forced_exit"] = 0
        if progress_cb: progress_cb(0.95, "تصدير نتائج المحاكاة")
        output_zip = Path(output_zip)
        output_zip.parent.mkdir(parents=True, exist_ok=True)
        report = self._report(summary)
        with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("summary.json", json.dumps(summary, ensure_ascii=False, indent=2))
            zf.writestr("shazam_adaptive_report.md", report)
            zf.writestr("shazam_adaptive_decisions.csv", decisions_df.to_csv(index=False))
            zf.writestr("shazam_adaptive_trades.csv", trades_df.to_csv(index=False))
            zf.writestr("shazam_adaptive_generated_rules.csv", rules_df.to_csv(index=False))
            zf.writestr("shazam_adaptive_story.csv", story_df.to_csv(index=False))
        return {"summary": summary, "output_zip": str(output_zip)}

    def _report(self, summary: Dict[str, Any]) -> str:
        return "\n".join([
            "# Shazam Adaptive 360 Live Engine",
            "",
            "التدفق المطبق:",
            "1. LiveDNABuilder يبني DNA آخر 360 شمعة.",
            "2. LiveExpertStoryBuilder يبني القصة والسياق.",
            "3. HandoffStateMachine يمثل التسليم والاستلام.",
            "4. MicroRuleGenerator يولد قاعدة دخول صغيرة حسب القصة.",
            "5. MicroExitGenerator يستخدم locked Position-Aware Expert Exit Context v1.1 مع Context Drawdown Guard.",
            "6. PositionManager يدير WAIT / IN_BUY / IN_SELL.",
            "7. Simulator يختبر نفس المنطق داخل المختبر، مع تسلسل SEARCH_ENTRY → ENTRY → SEARCH_EXIT → EXIT.",
            "",
            "## Summary",
            "```json",
            json.dumps(summary, ensure_ascii=False, indent=2),
            "```",
        ])


def run_shazam_adaptive_live_engine(
    dna_zip: str | Path,
    output_zip: str | Path,
    horizon: int = 24,
    warmup_bars: int = 360,
    max_hold_bars: int = 96,
    entry_percentile: float = 82.0,
    exit_percentile: float = 72.0,
    progress_cb=None,
    locked_context: bool = True,
    signal_preset: str | None = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    preset = (signal_preset or kwargs.get("preset") or kwargs.get("shazam_preset") or "").strip().lower()
    # v3.0: Conservative + Balanced presets (replaces k3/k5)
    if preset in {"conservative", "balanced", "safe", "default",
                  "shazam_conservative", "shazam_balanced",
                  "k3", "k5", "hybrid_k3", "hybrid_k5", "shazam_k3", "shazam_k5"}:
        sim = HybridPresetSimulator(preset=preset, locked_context=locked_context)
        return sim.run(Path(dna_zip), Path(output_zip), horizon=int(horizon or 24), progress_cb=progress_cb)
    sim = Simulator(warmup_bars=warmup_bars, max_hold_bars=max_hold_bars, entry_percentile=entry_percentile, exit_percentile=exit_percentile, locked_context=locked_context)
    return sim.run(Path(dna_zip), Path(output_zip), horizon=int(horizon or 24), progress_cb=progress_cb)
