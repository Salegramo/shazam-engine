"""
Shazam Entry-Only v1.1
======================

محرك دخول فقط — يصدر signals بدون أي exit logic.

التحديثات في v1.1:
  ✓ Progressive First-Hit: اختر أصغر نافذة فيها rule WR≥98%، توقف هناك
  ✓ De-duplication: ما نصدر نفس الـrule في bars متتالية (transition-based)
  ✓ rule_window extraction صحيح من rule_source

Output:
  - shazam_entry_only_signals.csv (مع rule_window صحيح)
  - signals "transitions" فقط (rule جديدة نشطة) لمنع spam
"""
from __future__ import annotations

import math
import zipfile
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .v41_stable_engine import (
    HybridInternalEquationMiner,
    HybridSignalPresetConfig,
    HybridDNABuilder,
    LOCKED_TRADE_CONTEXT_CONFIG,
    read_dna_zip,
    _safe_num,
)


@dataclass
class EntrySignal:
    """One entry signal — NO exit info."""
    row: int
    timestamp: Any
    side: str
    price: float
    rule_window: int
    rule_wr: float
    rule_loss_rate: float
    rule_signals_in_window: int
    rule_formula: str
    rule_family: str
    confidence: str
    suggested_tp_pct: float
    suggested_sl_pct: float
    suggested_max_hold: int


@dataclass
class EntryOnlyConfig:
    """Entry-only engine config."""
    name: str = "shazam_entry_only_v1_1"
    warmup_bars: int = 2500
    horizon: int = 24
    win_threshold_pct: float = 0.10
    
    default_buy_tp: float = 0.10
    default_buy_sl: float = 1.50
    default_sell_tp: float = 0.05
    default_sell_sl: float = 0.75
    default_max_hold: int = 144
    
    # Progressive First-Hit settings
    progressive_first_hit: bool = True
    first_hit_wr_threshold: float = 98.0
    
    # De-duplication
    dedup_enabled: bool = True
    min_signal_cooldown: int = 0


def _extract_window_from_source(rule_source: str) -> int:
    """Extract window number from rule_source like 'v4_pair_W360'."""
    try:
        s = str(rule_source or "")
        if "_W" in s:
            return int(s.rsplit("_W", 1)[-1])
    except (ValueError, AttributeError):
        pass
    return 0


class ShazamEntryOnlyEngine:
    """Entry-only signal emitter with Progressive First-Hit + Dedup."""
    
    def __init__(self, preset: str = "balanced", config: Optional[EntryOnlyConfig] = None):
        self.preset = HybridSignalPresetConfig.from_name(preset)
        self.config = config or EntryOnlyConfig()
        self.dna_builder = HybridDNABuilder.from_default_registry()
        self.miner = HybridInternalEquationMiner(top_k=self.preset.top_k)
    
    def _confidence_tier(self, rule_wr: float, rule_signals: int) -> str:
        if rule_wr >= 95 and rule_signals >= 10:
            return "HIGH"
        if rule_wr >= 92 and rule_signals >= 7:
            return "MEDIUM"
        return "LOW"
    
    def _select_progressive(self, active_rules_for_side: List[Any]) -> Optional[Any]:
        """
        Progressive First-Hit:
        - Sort by window ASCENDING (smaller first)
        - Take first rule with rule_wr >= first_hit_wr_threshold
        - Fallback: highest WR if none qualify
        """
        if not active_rules_for_side:
            return None
        
        if not self.config.progressive_first_hit:
            return max(active_rules_for_side, key=lambda r: float(getattr(r, "rule_wr", 0.0)))
        
        rules_with_window = []
        for r in active_rules_for_side:
            w = _extract_window_from_source(getattr(r, "rule_source", ""))
            rules_with_window.append((w, r))
        
        # Sort: smaller window first, higher WR within window
        rules_with_window.sort(key=lambda x: (x[0], -float(getattr(x[1], "rule_wr", 0.0))))
        
        threshold = float(self.config.first_hit_wr_threshold)
        best_fallback = None
        best_fallback_wr = -1.0
        
        for window, rule in rules_with_window:
            wr = float(getattr(rule, "rule_wr", 0.0))
            if wr >= threshold:
                return rule  # First-hit stop
            if wr > best_fallback_wr:
                best_fallback = rule
                best_fallback_wr = wr
        
        return best_fallback
    
    def _rule_fingerprint(self, rule) -> str:
        return f"{getattr(rule, 'formula', '')}__{getattr(rule, 'rule_source', '')}"
    
    def run(self, input_zip: Path, output_zip: Path, horizon: int = 24, progress_cb=None) -> Dict[str, Any]:
        raw, outcomes, dna_member = read_dna_zip(input_zip)
        if progress_cb:
            progress_cb(0.08, f"بناء Hybrid DNA لـEntry-Only v1.1 ({self.preset.name})")
        
        dna = self.dna_builder.build(raw)
        if progress_cb:
            progress_cb(0.15, "تعدين Multi-Window (13 نافذة)")
        
        self.miner.prepare(dna, horizon=int(horizon or self.config.horizon),
                           win_threshold_pct=float(self.config.win_threshold_pct))
        
        if progress_cb:
            progress_cb(0.25, f"Mining جاهز: {len(self.miner.mined_rules)} rule. Scanning (Progressive First-Hit)...")
        
        closes = _safe_num(dna["source_close"])
        n = len(dna)
        signals: List[EntrySignal] = []
        bars_scanned = 0
        empty_expert_row = pd.Series(dtype=object)
        
        # De-duplication state
        last_rule_fp = {"BUY": None, "SELL": None}
        last_signal_bar = {"BUY": -10**9, "SELL": -10**9}
        
        # Stats
        first_hit_count = 0
        fallback_count = 0
        window_distribution = {}
        
        for pos, i in enumerate(range(self.config.warmup_bars, n)):
            if progress_cb and pos % 200 == 0:
                progress_cb(
                    0.25 + 0.70 * (pos / max(1, n - self.config.warmup_bars)),
                    f"Entry-Only v1.1 ({self.preset.name}): {pos}/{n - self.config.warmup_bars} → {len(signals)} signals"
                )
            
            bars_scanned += 1
            price = float(closes.iloc[i])
            
            start_w = max(0, i - self.config.warmup_bars + 1)
            dna_w = dna.iloc[start_w:i + 1]
            active = self.miner.active_rules(i, dna_w, empty_expert_row, top_k=99)
            
            active_by_side: Dict[str, List] = {"BUY": [], "SELL": []}
            for r in active:
                if r.side in active_by_side:
                    active_by_side[r.side].append(r)
            
            for side, side_rules in active_by_side.items():
                if not side_rules:
                    last_rule_fp[side] = None
                    continue
                
                top_rule = self._select_progressive(side_rules)
                if top_rule is None:
                    continue
                
                rule_wr = float(getattr(top_rule, "rule_wr", 0.0))
                if rule_wr >= self.config.first_hit_wr_threshold:
                    first_hit_count += 1
                else:
                    fallback_count += 1
                window = _extract_window_from_source(getattr(top_rule, "rule_source", ""))
                window_distribution[window] = window_distribution.get(window, 0) + 1
                
                # De-duplication
                if self.config.dedup_enabled:
                    fp = self._rule_fingerprint(top_rule)
                    if fp == last_rule_fp[side]:
                        continue
                    last_rule_fp[side] = fp
                
                if self.config.min_signal_cooldown > 0:
                    if (i - last_signal_bar[side]) < self.config.min_signal_cooldown:
                        continue
                last_signal_bar[side] = i
                
                if side == "BUY":
                    sug_tp = self.config.default_buy_tp
                    sug_sl = self.config.default_buy_sl
                else:
                    sug_tp = self.config.default_sell_tp
                    sug_sl = self.config.default_sell_sl
                
                ts = raw.iloc[i].get("timestamp", i) if i < len(raw) else i
                signals.append(EntrySignal(
                    row=int(i),
                    timestamp=ts,
                    side=side,
                    price=price,
                    rule_window=int(window),
                    rule_wr=float(rule_wr),
                    rule_loss_rate=float(getattr(top_rule, "rule_loss_rate", 0.0)),
                    rule_signals_in_window=int(getattr(top_rule, "rule_signals", 0)),
                    rule_formula=str(getattr(top_rule, "formula", "")),
                    rule_family=str(getattr(top_rule, "rule_family", "?")),
                    confidence=self._confidence_tier(rule_wr, int(getattr(top_rule, "rule_signals", 0))),
                    suggested_tp_pct=sug_tp,
                    suggested_sl_pct=sug_sl,
                    suggested_max_hold=self.config.default_max_hold,
                ))
        
        if progress_cb:
            progress_cb(0.95, f"كتابة {len(signals)} signals")
        
        signals_df = pd.DataFrame([asdict(s) for s in signals])
        
        # ⭐ تقرير المعادلات (مثل المصنع)
        # لكل معادلة فريدة: عدد signals + WR + losses + window + confidence
        equations_report = []
        if len(signals_df) > 0:
            for (formula, side), group in signals_df.groupby(['rule_formula', 'side']):
                eq = {
                    'rule_formula': formula,
                    'side': side,
                    'rule_window': int(group['rule_window'].iloc[0]),
                    'rule_family': group['rule_family'].iloc[0],
                    # إحصائيات تاريخية من الـmining (داخل النافذة)
                    'mining_signals': int(group['rule_signals_in_window'].iloc[0]),  # signals في الـwindow
                    'mining_wr': float(group['rule_wr'].iloc[0]),                     # WR على الـwindow
                    'mining_loss_rate': float(group['rule_loss_rate'].iloc[0]),       # loss rate
                    # signals الفعلية المُصدرة في هذا الـrun
                    'live_signals_emitted': int(len(group)),
                    'first_bar': int(group['row'].min()),
                    'last_bar': int(group['row'].max()),
                    'confidence': group['confidence'].iloc[0],
                }
                # Estimate WIN/LOSS counts من mining
                # WR % × completed_signals = wins
                wr = eq['mining_wr'] / 100.0
                loss_r = eq['mining_loss_rate'] / 100.0
                total_completed = eq['mining_signals']
                eq['mining_wins'] = int(round(wr * total_completed))
                eq['mining_losses'] = int(round(loss_r * total_completed))
                equations_report.append(eq)
        
        # Sort by signals emitted (الأكثر إصداراً أولاً)
        equations_df = pd.DataFrame(equations_report)
        if len(equations_df) > 0:
            equations_df = equations_df.sort_values(['side', 'live_signals_emitted'], ascending=[True, False])
        
        buy_signals = [s for s in signals if s.side == "BUY"]
        sell_signals = [s for s in signals if s.side == "SELL"]
        confidence_counts = {
            "HIGH": sum(1 for s in signals if s.confidence == "HIGH"),
            "MEDIUM": sum(1 for s in signals if s.confidence == "MEDIUM"),
            "LOW": sum(1 for s in signals if s.confidence == "LOW"),
        }
        
        window_dist_sorted = dict(sorted(window_distribution.items()))
        total_picks = first_hit_count + fallback_count
        
        summary = {
            "engine": self.config.name,
            "preset": self.preset.name,
            "rows": int(n),
            "warmup_bars": self.config.warmup_bars,
            "bars_scanned": bars_scanned,
            "total_signals": len(signals),
            "buy_signals": len(buy_signals),
            "sell_signals": len(sell_signals),
            "unique_equations_buy": int(len(equations_df[equations_df['side']=='BUY'])) if len(equations_df) > 0 else 0,
            "unique_equations_sell": int(len(equations_df[equations_df['side']=='SELL'])) if len(equations_df) > 0 else 0,
            "confidence_breakdown": confidence_counts,
            "progressive_first_hit_stats": {
                "enabled": self.config.progressive_first_hit,
                "wr_threshold": self.config.first_hit_wr_threshold,
                "first_hit_count": first_hit_count,
                "fallback_count": fallback_count,
                "first_hit_pct": round(first_hit_count / max(1, total_picks) * 100, 2),
            },
            "window_distribution_picked": window_dist_sorted,
            "dedup_enabled": self.config.dedup_enabled,
            "min_signal_cooldown": self.config.min_signal_cooldown,
            "mining_audit": self.miner.audit[0] if self.miner.audit else {},
            "default_suggestions": {
                "buy_tp_pct": self.config.default_buy_tp,
                "buy_sl_pct": self.config.default_buy_sl,
                "sell_tp_pct": self.config.default_sell_tp,
                "sell_sl_pct": self.config.default_sell_sl,
                "max_hold_bars": self.config.default_max_hold,
            },
            "note": (
                "Entry-only v1.1: Progressive First-Hit + De-duplication. "
                "Signals only, NO exits. Bot manages exits."
            ),
        }
        
        import json
        output_zip = Path(output_zip)
        output_zip.parent.mkdir(parents=True, exist_ok=True)
        
        with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("shazam_entry_only_signals.csv", signals_df.to_csv(index=False))
            # ⭐ تقرير المعادلات (مثل المصنع)
            if len(equations_df) > 0:
                zf.writestr("equations_report.csv", equations_df.to_csv(index=False))
            zf.writestr("summary.json", json.dumps(summary, indent=2, default=str))
            zf.writestr("README.md", _BOT_INTEGRATION_README)
        
        if progress_cb:
            progress_cb(1.0, f"اكتمل: {len(signals)} signal ({len(buy_signals)} BUY / {len(sell_signals)} SELL)")
        
        return {"summary": summary, "signals_count": len(signals)}


_BOT_INTEGRATION_README = """# Shazam Entry-Only v1.1 — Bot Guide

## v1.1 Improvements

- Progressive First-Hit: smaller windows tried first, stops at WR>=98%
- De-duplication: same rule firing in consecutive bars is suppressed
- rule_window now correctly extracted (was bugged in v1.0)

## CSV columns

- row, timestamp, side, price
- rule_window: window that found this rule (30/50/.../2500)
- rule_wr: rule WR
- rule_loss_rate, rule_signals_in_window
- rule_formula, rule_family
- confidence: HIGH/MEDIUM/LOW
- suggested_tp_pct, suggested_sl_pct, suggested_max_hold

## Confidence

- HIGH:    WR >= 95% AND signals >= 10
- MEDIUM:  WR >= 92% AND signals >= 7
- LOW:     else
"""


def run_shazam_entry_only(
    dna_zip: Path,
    output_zip: Path,
    horizon: int = 24,
    warmup_bars: int = 2500,
    signal_preset: str = "balanced",
    progressive_first_hit: bool = True,
    first_hit_wr_threshold: float = 98.0,
    dedup_enabled: bool = True,
    min_signal_cooldown: int = 0,
    progress_cb=None,
    **kwargs
) -> Dict[str, Any]:
    """Entry-point compatible with engine_runner."""
    config = EntryOnlyConfig(
        warmup_bars=int(warmup_bars),
        horizon=int(horizon),
        progressive_first_hit=bool(progressive_first_hit),
        first_hit_wr_threshold=float(first_hit_wr_threshold),
        dedup_enabled=bool(dedup_enabled),
        min_signal_cooldown=int(min_signal_cooldown),
    )
    engine = ShazamEntryOnlyEngine(preset=signal_preset, config=config)
    return engine.run(Path(dna_zip), Path(output_zip), horizon=horizon, progress_cb=progress_cb)
