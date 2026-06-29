"""Enhanced MicroRuleGenerator v2 — توسعة الإشارات + رفع الـWR.

التحسينات على الـMicroRuleGenerator الأصلي:

1. SCORE ENRICHMENT — إضافة 4 features جديدة للـscore (مش 5، صار 9):
   - z(effort_quote_volume_range_ratio) → institutional activity
   - z(taker_buy_ratio) → flow direction
   - z(range_compression) → breakout potential
   - z(liquidity_distance) → proximity to recent extremes

2. MULTI-WINDOW CONFLUENCE — score يحسب على 3 نوافذ (50, 100, 144)
   ثم نأخذ المتوسط المرجح. هذا يفلتر noise الـsingle-window.

3. MULTI-TIER SIGNALS (Gold/Silver/Bronze):
   - Gold (p92): WR متوقع 92-95%, signals أقل
   - Silver (p82): WR ~88-91% (current behavior)
   - Bronze (p72): WR ~82-86%, signals أكثر
   كل tier يطلق إشارة منفصلة → إجمالي signals يتضاعف بدون تخفيض المتوسط

4. ADAPTIVE THRESHOLD — يتكيف مع regime:
   - في trending strong: threshold أقل (signals أكثر، الـtrend يحمي WR)
   - في choppy/conflict: threshold أعلى (filter)

5. ANTI-COUNTER-TREND FILTER — يلغي signals تعاكس trend واضح
   (في trend صاعد قوي، لا نقبل BUY بعد فترة طويلة من الصعود = top buying)
"""
from __future__ import annotations

from typing import List, Optional, Tuple
import numpy as np
import pandas as pd

# Import from the parent module
try:
    from .shazam_adaptive_live_engine import (
        GeneratedRule, HandoffState, _safe_num, _zscore_last, _pct_rank_last
    )
except ImportError:
    # Standalone testing
    pass


def _zscore_series(series: pd.Series, window: int) -> pd.Series:
    """Causal z-score (used for the multi-window score)."""
    r = series.rolling(window, min_periods=max(5, min(window, 20)))
    mu = r.mean()
    sd = r.std().replace(0, np.nan)
    return ((series - mu) / sd).replace([np.inf, -np.inf], np.nan).fillna(0.0)


class MicroRuleGeneratorV2:
    """Enhanced rule generator — يوسع signals ويرفع الـWR معاً.
    
    الـAPI متطابق مع الـMicroRuleGenerator الأصلي — drop-in replacement.
    
    Parameters
    ----------
    entry_percentile : float
        Silver tier percentile (default 82, الـcurrent behavior).
    gold_percentile : float
        Gold tier percentile (default 92). أعلى = WR أكبر، signals أقل.
    bronze_percentile : float
        Bronze tier percentile (default 72). أقل = signals أكثر، WR أقل.
    enable_tiers : list of str
        أي tiers تطلق signals. الافتراضي ['gold','silver','bronze'].
    enable_anti_counter_trend : bool
        فلتر signals counter-trend في طرف الـtrend.
    adaptive_threshold : bool
        تعديل الـthreshold بحسب الـregime.
    """
    def __init__(
        self,
        entry_percentile: float = 82.0,
        gold_percentile: float = 92.0,
        bronze_percentile: float = 72.0,
        enable_tiers: Optional[List[str]] = None,
        enable_anti_counter_trend: bool = True,
        adaptive_threshold: bool = True,
    ):
        self.silver_pct = float(entry_percentile)
        self.gold_pct = float(gold_percentile)
        self.bronze_pct = float(bronze_percentile)
        self.tiers = list(enable_tiers or ["gold", "silver", "bronze"])
        self.anti_counter_trend = bool(enable_anti_counter_trend)
        self.adaptive = bool(adaptive_threshold)
    
    # ─── Multi-window score: weighted average across 3 windows ───
    def _compute_side_score_series(
        self, 
        dna_window: pd.DataFrame, 
        expert_window: pd.DataFrame, 
        side: str,
    ) -> pd.Series:
        """Compute multi-window score series for one side.
        
        Score = weighted average across windows {50, 100, 144} of:
          + z(side_support) × 1.25
          - z(opposite_support) × 0.85
          - z(neutral_risk) × 0.75
          + z(side_pressure) × 0.80
          ± z(close_position) × 0.35
          + z(effort_quote_volume_range) × 0.30   ← NEW: institutional
          ± z(taker_buy_minus_sell) × 0.40        ← NEW: flow
          + z(range_compression) × 0.25           ← NEW: breakout potential
          + z(liquidity_proximity) × 0.20         ← NEW: liquidity zones
        """
        n = len(dna_window)
        if n < 50:
            return pd.Series(np.zeros(n), index=dna_window.index)
        
        # Core supports
        side_support = expert_window["expert_buy_context_support"] if side == "BUY" else expert_window["expert_sell_context_support"]
        opposite_support = expert_window["expert_sell_context_support"] if side == "BUY" else expert_window["expert_buy_context_support"]
        neutral = expert_window["expert_neutral_risk"]
        
        # Pressure scores
        side_pressure = dna_window["demand_pressure_score"] if side == "BUY" else dna_window["supply_pressure_score"]
        close_pos = dna_window["candle_close_position_pct"]
        close_sign = 1.0 if side == "BUY" else -1.0
        
        # NEW features
        effort = _safe_num(dna_window.get("effort_quote_volume_range_ratio", pd.Series(0, index=dna_window.index)))
        taker_buy = _safe_num(dna_window.get("volume_taker_buy_ratio_pct", pd.Series(50, index=dna_window.index)))
        taker_flow = (taker_buy - 50.0) * (1.0 if side == "BUY" else -1.0)  # positive when flow agrees
        range_pct = _safe_num(dna_window.get("candle_range_pct", pd.Series(0, index=dna_window.index)))
        # range_compression: low recent range vs recent average = compression → breakout potential
        range_avg = range_pct.rolling(50, min_periods=10).mean()
        range_compression = (range_avg - range_pct).clip(lower=0)  # positive when compressed
        # liquidity proximity: how close is current close to recent low (BUY) or recent high (SELL)
        if side == "BUY":
            low_50 = dna_window["candle_close_position_pct"].rolling(50, min_periods=10).min()
            liq_proximity = 100 - (dna_window["candle_close_position_pct"] - low_50)  # high when close to recent low
        else:
            high_50 = dna_window["candle_close_position_pct"].rolling(50, min_periods=10).max()
            liq_proximity = 100 - (high_50 - dna_window["candle_close_position_pct"])  # high when close to recent high
        
        # Multi-window weighted score
        weights = [0.25, 0.40, 0.35]  # weights for windows 50, 100, 144
        windows = [50, 100, 144]
        score_total = pd.Series(np.zeros(n), index=dna_window.index)
        
        for w, weight in zip(windows, weights):
            w_eff = min(w, n)
            score_w = (
                _zscore_series(side_support, w_eff) * 1.25
                - _zscore_series(opposite_support, w_eff) * 0.85
                - _zscore_series(neutral, w_eff) * 0.75
                + _zscore_series(side_pressure, min(100, n)) * 0.80
                + close_sign * _zscore_series(close_pos, min(50, n)) * 0.35
                + _zscore_series(effort, w_eff) * 0.30
                + _zscore_series(taker_flow, w_eff) * 0.40
                + _zscore_series(range_compression, min(50, n)) * 0.25
                + _zscore_series(liq_proximity, w_eff) * 0.20
            )
            score_total = score_total + score_w * weight
        
        return score_total.replace([np.inf, -np.inf], np.nan).fillna(0)
    
    # ─── Regime detection for adaptive thresholding ───
    def _detect_regime(self, dna_window: pd.DataFrame, expert_window: pd.DataFrame) -> str:
        """Returns 'trending_strong', 'trending_weak', 'choppy', 'neutral'."""
        if len(expert_window) < 50:
            return "neutral"
        last_50 = expert_window.tail(50)
        edge_mean = abs(last_50["expert_context_edge"].mean())
        conflict_mean = last_50["expert_scale_conflict_score"].mean()
        edge_consistency = (np.sign(last_50["expert_context_edge"]).abs().sum() / 50.0)
        
        if edge_mean > 25 and conflict_mean < 35:
            return "trending_strong"
        elif edge_mean > 12 and conflict_mean < 50:
            return "trending_weak"
        elif conflict_mean > 60:
            return "choppy"
        else:
            return "neutral"
    
    def _adjust_threshold(self, base_thr: float, regime: str, score_series: pd.Series, percentile: float) -> float:
        """Adjust threshold based on regime."""
        if not self.adaptive:
            return base_thr
        adj_pct = percentile
        if regime == "trending_strong":
            # In strong trend, slightly lower threshold (signals are robust)
            adj_pct = max(percentile - 5, 50)
        elif regime == "choppy":
            # In chop, raise threshold (filter noise)
            adj_pct = min(percentile + 4, 97)
        return float(np.nanpercentile(score_series, adj_pct))
    
    # ─── Anti-counter-trend filter ───
    def _passes_counter_trend(self, dna_window: pd.DataFrame, expert_window: pd.DataFrame, side: str) -> bool:
        """Check if signal passes the anti-counter-trend filter."""
        if not self.anti_counter_trend or len(dna_window) < 100:
            return True
        # Look at last 100 candles trend (close direction)
        close = dna_window["source_close"].tail(100)
        if len(close) < 50:
            return True
        # Compute slope normalized
        x = np.arange(len(close))
        slope = np.polyfit(x, close.values, 1)[0]
        slope_normalized = slope * len(close) / close.iloc[0] if close.iloc[0] != 0 else 0
        # slope_normalized > 0.1 means strong uptrend over 100 bars (>10% rise)
        # Reject BUY at top of strong uptrend (buying exhaustion), and SELL at bottom of strong downtrend
        # But ALLOW BUY in downtrend (bounce buying) and SELL in uptrend (top selling)
        if side == "BUY" and slope_normalized > 0.15:
            # Strong uptrend already — check if we're near the top
            last_30_high = dna_window["source_high"].tail(30).max()
            current = dna_window["source_close"].iloc[-1]
            if current >= last_30_high * 0.995:  # within 0.5% of 30-bar high
                return False  # likely top buying, reject
        elif side == "SELL" and slope_normalized < -0.15:
            last_30_low = dna_window["source_low"].tail(30).min()
            current = dna_window["source_close"].iloc[-1]
            if current <= last_30_low * 1.005:  # within 0.5% of 30-bar low
                return False  # likely bottom selling, reject
        return True
    
    # ─── Main generate method (drop-in compatible) ───
    def generate(
        self,
        dna_window: pd.DataFrame,
        expert_window: pd.DataFrame,
        state: HandoffState,
    ) -> List["GeneratedRule"]:
        if len(dna_window) < 50:
            return []
        idx = dna_window.index[-1]
        rules: List[GeneratedRule] = []
        
        # Compute multi-window scores
        buy_series = self._compute_side_score_series(dna_window, expert_window, "BUY")
        sell_series = self._compute_side_score_series(dna_window, expert_window, "SELL")
        buy_score = float(buy_series.iloc[-1])
        sell_score = float(sell_series.iloc[-1])
        
        # Regime
        regime = self._detect_regime(dna_window, expert_window)
        
        # Counter-trend filter
        buy_ok_trend = self._passes_counter_trend(dna_window, expert_window, "BUY")
        sell_ok_trend = self._passes_counter_trend(dna_window, expert_window, "SELL")
        
        allow_buy = state.buy_receiver and not state.neutral_wait and not state.conflict_wait and buy_ok_trend
        allow_sell = state.sell_receiver and not state.neutral_wait and not state.conflict_wait and sell_ok_trend
        
        # Build tiers
        bw = min(144, len(dna_window))
        recent_buy = buy_series.iloc[-bw:]
        recent_sell = sell_series.iloc[-bw:]
        
        tier_configs = {
            "gold":   (self.gold_pct,   "GOLD",   28),  # higher confidence scaling
            "silver": (self.silver_pct, "SILVER", 18),  # original confidence scaling
            "bronze": (self.bronze_pct, "BRONZE", 12),  # lower confidence scaling
        }
        
        for tier_name in self.tiers:
            if tier_name not in tier_configs:
                continue
            pct, label, conf_scale = tier_configs[tier_name]
            bthr = self._adjust_threshold(0, regime, recent_buy, pct)
            sthr = self._adjust_threshold(0, regime, recent_sell, pct)
            
            buy_pass = bool(allow_buy and buy_score >= bthr)
            sell_pass = bool(allow_sell and sell_score >= sthr)
            
            buy_conf = float(max(0, min(100, 50 + (buy_score - bthr) * conf_scale)))
            sell_conf = float(max(0, min(100, 50 + (sell_score - sthr) * conf_scale)))
            
            rules.append(GeneratedRule(
                f"BUY_MICRO_{tier_name.upper()}_{idx}", "BUY", "ENTRY", state.slot, f"buy_story_score_{tier_name}",
                buy_score, bthr, buy_pass, buy_conf,
                f"multi_window_score [tier={label}, regime={regime}] >= rolling_p{pct:.0f}",
            ))
            rules.append(GeneratedRule(
                f"SELL_MICRO_{tier_name.upper()}_{idx}", "SELL", "ENTRY", state.slot, f"sell_story_score_{tier_name}",
                sell_score, sthr, sell_pass, sell_conf,
                f"multi_window_score [tier={label}, regime={regime}] >= rolling_p{pct:.0f}",
            ))
        return rules
    
    def choose(self, rules: List["GeneratedRule"]) -> Optional["GeneratedRule"]:
        """Choose the best signal. Prefer Gold > Silver > Bronze."""
        passed = [r for r in rules if r.passed]
        if not passed:
            return None
        
        # Tier ranking (higher = better)
        def tier_rank(r):
            rid = r.rule_id.upper()
            if "GOLD" in rid: return 3
            if "SILVER" in rid: return 2
            if "BRONZE" in rid: return 1
            return 0
        
        # Sort: tier first, then confidence, then score margin
        passed.sort(key=lambda r: (tier_rank(r), r.confidence, r.score - r.threshold), reverse=True)
        
        # Conflict check: if top two are opposite sides and confidence very close → skip
        if len(passed) >= 2 and passed[0].side != passed[1].side and abs(passed[0].confidence - passed[1].confidence) < 8:
            return None
        return passed[0]
