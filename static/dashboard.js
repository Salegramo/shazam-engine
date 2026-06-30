// ============================================================
// Shazam Live Dashboard v1.0
// ============================================================

let chart = null;
let candleSeries = null;
let supertrendSegments = [];  // array of line series, recreated on every update
let tpLineSeries = null;
let slLineSeries = null;

let currentEngine = "v41_stable";
let currentDisplayMode = "single";
let currentShowSignals = true;
let currentShowTrades = true;
let currentShowTpSl = true;
let currentShowSR = false;
let stThickness = 2;

const DEFAULT_BUY_LADDER = [
  [0.05, 0.040], [0.10, 0.085], [0.15, 0.130], [0.22, 0.195],
  [0.32, 0.290], [0.50, 0.460], [0.75, 0.700], [1.00, 0.930],
];
const DEFAULT_SELL_LADDER = [
  [0.05, 0.038], [0.10, 0.080], [0.15, 0.123], [0.22, 0.181],
  [0.32, 0.265], [0.50, 0.415], [0.75, 0.625], [1.00, 0.850],
];

// ----------- Helpers -----------
function $(id) { return document.getElementById(id); }
function fmtUsd(v) { return "$" + Number(v).toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ","); }
function fmtPct(v) {
  const sign = v >= 0 ? "+" : "";
  return sign + Number(v).toFixed(2) + "%";
}
function fmtTime(ts) {
  const d = new Date(ts * 1000);
  return String(d.getHours()).padStart(2,"0") + ":" + String(d.getMinutes()).padStart(2,"0");
}

// ----------- Collapsible sections -----------
function toggleSection(id) {
  const body = $(id);
  const chev = $("chev_" + id);
  if (!body) return;
  const isCollapsed = body.classList.toggle("collapsed");
  if (chev) chev.classList.toggle("collapsed", isCollapsed);
}

function toggleSettings() {
  $("settingsPanel").classList.toggle("hidden");
}

function closeSettings() {
  $("settingsPanel").classList.add("hidden");
}

// ----------- Chart Setup -----------
function initChart() {
  const container = $("chart");
  chart = LightweightCharts.createChart(container, {
    width: container.clientWidth, height: 320,
    layout: { background: { type: "solid", color: "#0a0e1a" }, textColor: "#9ca3af" },
    grid: { vertLines: { color: "rgba(40,48,68,.4)" }, horzLines: { color: "rgba(40,48,68,.4)" } },
    rightPriceScale: { borderColor: "#283044" },
    timeScale: { borderColor: "#283044", timeVisible: true, secondsVisible: false },
    crosshair: { mode: 1 },
  });
  candleSeries = chart.addCandlestickSeries({
    upColor: "#00ff88", downColor: "#ff4d4d",
    borderUpColor: "#00ff88", borderDownColor: "#ff4d4d",
    wickUpColor: "#00ff88", wickDownColor: "#ff4d4d",
  });
  // SuperTrend handled as dynamic segments (see updateSupertrend below)
  tpLineSeries = chart.addLineSeries({
    color: "#38bdf8", lineWidth: 1, lineStyle: 2,
    crosshairMarkerVisible: false, lastValueVisible: true, priceLineVisible: false, title: "TP"
  });
  slLineSeries = chart.addLineSeries({
    color: "#fb7185", lineWidth: 1, lineStyle: 2,
    crosshairMarkerVisible: false, lastValueVisible: true, priceLineVisible: false, title: "SL"
  });
  window.addEventListener("resize", () => chart.applyOptions({ width: container.clientWidth }));
}

// S/R price lines holder (we use candleSeries.createPriceLine)
let srPriceLines = [];

function clearSR() {
  for (const pl of srPriceLines) {
    try { candleSeries.removePriceLine(pl); } catch(e) {}
  }
  srPriceLines = [];
}

function renderSR(sr) {
  clearSR();
  if (!currentShowSR || !sr) return;
  
  // Resistance (red)
  for (const r of (sr.resistance || [])) {
    const pl = candleSeries.createPriceLine({
      price: r.price,
      color: "rgba(251, 113, 133, 0.7)",
      lineWidth: 1,
      lineStyle: 2,  // dashed
      axisLabelVisible: true,
      title: "R" + (r.strength > 1 ? "×" + r.strength : ""),
    });
    srPriceLines.push(pl);
  }
  
  // Support (green)
  for (const s of (sr.support || [])) {
    const pl = candleSeries.createPriceLine({
      price: s.price,
      color: "rgba(52, 211, 153, 0.7)",
      lineWidth: 1,
      lineStyle: 2,
      axisLabelVisible: true,
      title: "S" + (s.strength > 1 ? "×" + s.strength : ""),
    });
    srPriceLines.push(pl);
  }
}


// ----------- SuperTrend (single line, multi-color segments) -----------
function updateSupertrend(points) {
  // Clear old segments
  for (const s of supertrendSegments) {
    try { chart.removeSeries(s); } catch(e) {}
  }
  supertrendSegments = [];
  if (!points || points.length === 0) return;
  
  // Build segments: a new segment starts on every direction change.
  // CRITICAL: the transition point is included in BOTH the old and new segment,
  // so the line is visually continuous (no gap), just color changes there.
  const segments = [];
  let curDir = points[0].direction;
  let curSeg = [points[0]];
  for (let i = 1; i < points.length; i++) {
    const pt = points[i];
    if (pt.direction === curDir) {
      curSeg.push(pt);
    } else {
      // Direction change — include the transition point in old seg, then start new from it
      curSeg.push(pt);  // close old segment at transition
      segments.push({ direction: curDir, points: curSeg });
      curSeg = [pt];    // new segment starts AT the transition point
      curDir = pt.direction;
    }
  }
  segments.push({ direction: curDir, points: curSeg });
  
  // Create a line series for each segment
  for (const seg of segments) {
    if (seg.points.length < 2) continue;  // skip 1-point segments
    const color = seg.direction === "bull" ? "#00ff88" : "#ff4d4d";
    const series = chart.addLineSeries({
      color: color,
      lineWidth: stThickness,
      lineStyle: 0,
      crosshairMarkerVisible: false,
      lastValueVisible: false,
      priceLineVisible: false,
    });
    series.setData(seg.points.map(p => ({ time: p.time, value: p.value })));
    supertrendSegments.push(series);
  }
}

// ----------- Chart Update -----------
function updateChart(data) {
  if (!data || !data.candles) return;
  candleSeries.setData(data.candles);
  
  // SuperTrend — single continuous line, color changes at trend transitions
  updateSupertrend(data.supertrend || []);
  
  // Markers — filtered by show toggles
  const markers = [];
  if (currentShowSignals) {
    for (const s of data.signals || []) {
      markers.push({
        time: s.time, position: s.side === "BUY" ? "belowBar" : "aboveBar",
        color: s.side === "BUY" ? "#00ff88" : "#ff4d4d",
        shape: s.side === "BUY" ? "arrowUp" : "arrowDown", text: s.side,
      });
    }
  }
  if (currentShowTrades) {
    for (const m of data.trade_markers || []) {
      if (m.type === "entry") {
        markers.push({
          time: m.time, position: m.side === "BUY" ? "belowBar" : "aboveBar",
          color: m.side === "BUY" ? "#34d399" : "#fb7185",
          shape: m.side === "BUY" ? "arrowUp" : "arrowDown",
          text: m.side + (m.open ? "●" : ""),
        });
      } else if (m.type === "exit") {
        markers.push({
          time: m.time, position: "inBar",
          color: m.pnl_pct >= 0 ? "#39c5cf" : "#fb7185",
          shape: "circle", text: "EXIT",
        });
      }
    }
  }
  markers.sort((a, b) => a.time - b.time);
  candleSeries.setMarkers(markers);
  
  // S/R levels
  renderSR(data.sr);
  
  // TP/SL lines (filtered by toggle)
  if (data.tp_line && currentShowTpSl) {
    const tp = data.tp_line;
    const lastTime = data.candles[data.candles.length - 1].time;
    const startTime = tp.entry_time;
    tpLineSeries.setData([{ time: startTime, value: tp.tp_price }, { time: lastTime + 60, value: tp.tp_price }]);
    slLineSeries.setData([{ time: startTime, value: tp.sl_price }, { time: lastTime + 60, value: tp.sl_price }]);
  } else {
    tpLineSeries.setData([]);
    slLineSeries.setData([]);
  }
  
  // Price display
  if (data.candles.length > 0) {
    const last = data.candles[data.candles.length - 1];
    $("latestPrice").textContent = "$" + Number(last.close).toLocaleString(undefined, { maximumFractionDigits: 2 });
    if (data.candles.length >= 2) {
      const prev = data.candles[0];
      const change = ((last.close - prev.close) / prev.close) * 100;
      const el = $("chartChange");
      el.textContent = fmtPct(change);
      el.className = "change " + (change >= 0 ? "up" : "down");
    }
  }
}

// ----------- Status -----------
async function updateStatus() {
  try {
    const res = await fetch("/api/status");
    if (!res.ok) return;
    const data = await res.json();
    
    const pill = $("livePill");
    if (!data.mining_ready && data.mining_in_progress) {
      pill.textContent = "WARMING"; pill.className = "live-pill warming";
    } else if (data.ws_alive && data.mining_ready) {
      pill.textContent = "LIVE"; pill.className = "live-pill";
    } else {
      pill.textContent = "OFFLINE"; pill.className = "live-pill offline";
    }
    
    if (data.display_mode === "compare") {
      $("modeInfo").textContent = "COMPARE";
    } else {
      $("modeInfo").textContent = data.active_engine === "v41_stable" ? "v4.1 Stable" : "Entry-Only";
    }
    
    if (data.mining_audit && data.mining_audit.mined_equations) {
      $("warmupInfo").textContent = `${data.mining_audit.mined_equations} rules`;
    } else if (data.mining_in_progress) {
      $("warmupInfo").textContent = `mining...`;
    } else {
      $("warmupInfo").textContent = `${data.candles_loaded || 0} candles`;
    }
    
    // System info
    if ($("sysInfo")) {
      const audit = data.mining_audit || {};
      const lines = [];
      lines.push(`الرمز: ${data.symbol} @ ${data.timeframe}`);
      lines.push(`Warmup: ${data.warmup_bars} candle`);
      lines.push(`الـcandles المحمّلة: ${data.candles_loaded}`);
      if (audit.mined_equations) {
        lines.push(`المعادلات المُعدَّنة: ${audit.mined_equations} (${audit.atom_equations || 0} atoms + ${audit.pair_equations || 0} pairs)`);
      }
      lines.push(`الإشارات الكلية: ${data.signals_total}`);
      if (data.ws_last_event_age_sec !== null && data.ws_last_event_age_sec !== undefined) {
        lines.push(`آخر تحديث WS: ${data.ws_last_event_age_sec.toFixed(1)}s`);
      }
      $("sysInfo").innerHTML = lines.join("<br>");
    }
    
    // Engine cards status
    const inCompare = data.display_mode === "compare";
    const v41Active = inCompare || data.active_engine === "v41_stable";
    const eoActive = inCompare || data.active_engine === "entry_only";
    $("status_v41").textContent = v41Active && data.mining_ready ? "نشط" : "متوقف";
    $("status_v41").className = "status-pill " + (v41Active && data.mining_ready ? "active" : "paused");
    $("status_entry_only").textContent = eoActive && data.mining_ready ? "نشط" : "متوقف";
    $("status_entry_only").className = "status-pill " + (eoActive && data.mining_ready ? "active" : "paused");
    
    // Update settings UI from server state
    if (data.display_mode !== currentDisplayMode) {
      currentDisplayMode = data.display_mode;
      if ($("displayMode")) $("displayMode").value = data.display_mode;
    }
    if (data.show_signals !== undefined && data.show_signals !== currentShowSignals) {
      currentShowSignals = data.show_signals;
      if ($("showSignals")) $("showSignals").value = String(data.show_signals);
    }
    if (data.show_trades !== undefined && data.show_trades !== currentShowTrades) {
      currentShowTrades = data.show_trades;
      if ($("showTrades")) $("showTrades").value = String(data.show_trades);
    }
    if (data.show_tp_sl !== undefined && data.show_tp_sl !== currentShowTpSl) {
      currentShowTpSl = data.show_tp_sl;
      if ($("showTpSl")) $("showTpSl").value = String(data.show_tp_sl);
    }
    if (data.show_sr !== undefined && data.show_sr !== currentShowSR) {
      currentShowSR = data.show_sr;
      if ($("showSR")) $("showSR").value = String(data.show_sr);
    }
    
    // Sync entry-only settings if user hasn't been editing
    if (data.entry_only_settings) {
      syncEntryOnlySettings(data.entry_only_settings);
    }
  } catch (e) { console.error("status error", e); }
}

let _eoSettingsSynced = false;
function syncEntryOnlySettings(s) {
  if (_eoSettingsSynced) return;
  _eoSettingsSynced = true;
  if ($("eo_exit_mode")) $("eo_exit_mode").value = s.exit_mode || "ladder";
  if ($("eo_enabled")) $("eo_enabled").value = String(s.enabled !== false);
  if ($("eo_buy_tp")) $("eo_buy_tp").value = s.buy_tp_pct || 0.10;
  if ($("eo_buy_sl")) $("eo_buy_sl").value = s.buy_sl_pct || 1.50;
  if ($("eo_sell_tp")) $("eo_sell_tp").value = s.sell_tp_pct || 0.05;
  if ($("eo_sell_sl")) $("eo_sell_sl").value = s.sell_sl_pct || 0.75;
  if ($("eo_buy_sl_global")) $("eo_buy_sl_global").value = s.buy_sl_pct || 1.50;
  if ($("eo_sell_sl_global")) $("eo_sell_sl_global").value = s.sell_sl_pct || 0.75;
  if ($("eo_max_hold")) $("eo_max_hold").value = s.max_hold_bars || 144;
  if ($("eo_cooldown")) $("eo_cooldown").value = s.cooldown_bars !== undefined ? s.cooldown_bars : 6;
  if ($("eo_smart_reverse")) $("eo_smart_reverse").value = String(s.smart_reverse !== false);
  const buyL = (s.buy_ladder && s.buy_ladder.length) ? s.buy_ladder : DEFAULT_BUY_LADDER;
  const sellL = (s.sell_ladder && s.sell_ladder.length) ? s.sell_ladder : DEFAULT_SELL_LADDER;
  renderBothLadders(buyL, sellL);
  if ($("eo_time_no_profit")) $("eo_time_no_profit").value = s.exit_after_no_profit_bars !== undefined ? s.exit_after_no_profit_bars : 8;
  if ($("eo_time_loss")) $("eo_time_loss").value = s.exit_after_loss_bars !== undefined ? s.exit_after_loss_bars : 15;
  updateExitModeUI();
}

// ----------- Paper Stats -----------
async function updatePaperStats() {
  try {
    const res = await fetch("/api/paper-stats");
    if (!res.ok) return;
    const data = await res.json();
    updateCard("v41", data.v41_stable);
    updateCard("eo", data.entry_only);
  } catch (e) { console.error(e); }
}

function updateCard(prefix, stats) {
  if (!stats) return;
  $(`${prefix}_balance`).textContent = fmtUsd(stats.balance);
  const pnlEl = $(`${prefix}_pnl_pct`);
  pnlEl.textContent = fmtPct(stats.total_pnl_pct);
  pnlEl.className = "stat-value " + (stats.total_pnl_pct >= 0 ? "up" : "down");
  $(`${prefix}_trades`).textContent = stats.trades_total;
  if ($(`${prefix}_signals`)) $(`${prefix}_signals`).textContent = stats.signals_received_count || 0;
  $(`${prefix}_wins`).textContent = stats.wins;
  $(`${prefix}_losses`).textContent = stats.losses;
  $(`${prefix}_wr`).textContent = stats.win_rate_pct.toFixed(1) + "%";
  
  const floatBox = $(`${prefix}_floating_box`);
  if (stats.has_open_position && stats.open_position) {
    floatBox.style.display = "block";
    $(`${prefix}_open_side`).textContent = stats.open_position.side;
    $(`${prefix}_open_side`).className = stats.open_position.side;
    const fEl = $(`${prefix}_floating_pnl`);
    fEl.textContent = fmtPct(stats.floating_pnl_pct);
    fEl.className = "floating-value " + (stats.floating_pnl_pct >= 0 ? "up" : "down");
    // Lock info for entry-only
    if (prefix === "eo" && $("eo_lock_info")) {
      const p = stats.open_position;
      let info = `Peak: ${fmtPct(p.peak_pnl_pct || 0)}`;
      if (p.current_lock_pct !== null && p.current_lock_pct !== undefined) {
        info += ` | 🔒 Lock: ${fmtPct(p.current_lock_pct)}`;
      } else {
        info += ` | لا lock بعد`;
      }
      $("eo_lock_info").textContent = info;
    }
  } else {
    floatBox.style.display = "none";
  }
}

// ----------- Chart Tick -----------
async function tickChart() {
  try {
    const res = await fetch("/api/chart?n=200");
    if (!res.ok) return;
    const data = await res.json();
    updateChart(data);
    updateSignalsList(data.signals || []);
  } catch (e) { console.error(e); }
}

function updateSignalsList(signals) {
  const list = $("signalsList");
  if (!signals || signals.length === 0) {
    list.innerHTML = '<div style="text-align:center;color:var(--muted);padding:14px;font-size:12px">لا توجد إشارات بعد</div>';
    return;
  }
  const recent = signals.slice(-15).reverse();
  list.innerHTML = recent.map(s => `
    <div class="signal-item">
      <span class="signal-side ${s.side}">${s.side}</span>
      <span class="signal-meta">$${s.price.toFixed(2)} · W=${s.rule_window} · WR ${s.rule_wr.toFixed(1)}%</span>
      <span class="signal-conf ${s.confidence}">${s.confidence}</span>
      <span class="signal-time">${fmtTime(s.time)}</span>
    </div>
  `).join("");
}

// ----------- Engine Selection -----------
async function selectEngine(engine) {
  currentEngine = engine;
  document.querySelectorAll(".engine-tab").forEach(t => {
    t.classList.toggle("active", t.dataset.engine === engine);
  });
  $("card_v41_stable").style.display = engine === "v41_stable" ? "block" : "none";
  $("card_entry_only").style.display = engine === "entry_only" ? "block" : "none";
  try {
    await fetch("/api/active-engine", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ engine: engine }),
    });
  } catch (e) { console.error(e); }
  await Promise.all([tickChart(), updateStatus(), updatePaperStats()]);
}

// ----------- Display mode + Show markers -----------
async function applyDisplayMode() {
  const mode = $("displayMode").value;
  currentDisplayMode = mode;
  try {
    await fetch("/api/display-mode", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    });
    showToast(mode === "compare" ? "وضع مقارنة: كلا المحركين يعملان" : "وضع واحد: المحرك المختار فقط");
    await tickChart();
  } catch (e) { console.error(e); }
}

async function applyShowToggles() {
  currentShowSignals = $("showSignals").value === "true";
  currentShowTrades = $("showTrades").value === "true";
  currentShowTpSl = $("showTpSl").value === "true";
  currentShowSR = $("showSR").value === "true";
  try {
    await fetch("/api/show-markers", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        show_signals: currentShowSignals,
        show_trades: currentShowTrades,
        show_tp_sl: currentShowTpSl,
        show_sr: currentShowSR,
      }),
    });
    await tickChart();
  } catch (e) { console.error(e); }
}

// ----------- SuperTrend settings -----------
async function applySupertrendSettings() {
  const body = {
    period: parseInt($("stPeriod").value),
    multiplier: parseFloat($("stMultiplier").value),
    offset_pct: parseFloat($("stOffset").value),
    thickness: parseInt($("stThickness").value),
  };
  stThickness = body.thickness;
  try {
    await fetch("/api/supertrend-settings", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    showToast("تم تطبيق SuperTrend");
    closeSettings();
    await tickChart();
  } catch (e) { console.error(e); }
}

// ----------- Step Ladder editor -----------
function renderLadderRows(containerId, ladder, sideKey) {
  const container = $(containerId);
  if (!container) return;
  container.innerHTML = "";
  for (let i = 0; i < ladder.length; i++) {
    const row = ladder[i];
    const div = document.createElement("div");
    div.className = "ladder-row";
    div.innerHTML = `
      <input type="number" step="0.01" min="0" value="${row[0]}" data-side="${sideKey}" data-idx="${i}" data-field="trigger">
      <span class="arrow">→</span>
      <input type="number" step="0.01" min="0" value="${row[1]}" data-side="${sideKey}" data-idx="${i}" data-field="lock">
      <button onclick="removeLadderRow('${sideKey}', ${i})">✕</button>
    `;
    container.appendChild(div);
  }
  const addBtn = document.createElement("button");
  addBtn.className = "add-ladder-btn";
  addBtn.textContent = "+ إضافة مستوى";
  addBtn.onclick = () => addLadderRow(sideKey);
  container.appendChild(addBtn);
}

function renderBothLadders(buyLadder, sellLadder) {
  renderLadderRows("buyLadderRows", buyLadder, "buy");
  renderLadderRows("sellLadderRows", sellLadder, "sell");
}

function getLadderFromUI(sideKey) {
  const containerId = sideKey === "buy" ? "buyLadderRows" : "sellLadderRows";
  const inputs = $(containerId).querySelectorAll(".ladder-row");
  const ladder = [];
  inputs.forEach(row => {
    const trigger = parseFloat(row.querySelector('[data-field="trigger"]').value);
    const lock = parseFloat(row.querySelector('[data-field="lock"]').value);
    if (!isNaN(trigger) && !isNaN(lock) && trigger > 0 && lock > 0 && lock < trigger) {
      ladder.push([trigger, lock]);
    }
  });
  ladder.sort((a, b) => a[0] - b[0]);
  return ladder;
}

function removeLadderRow(sideKey, idx) {
  const current = getLadderFromUI(sideKey);
  current.splice(idx, 1);
  const containerId = sideKey === "buy" ? "buyLadderRows" : "sellLadderRows";
  renderLadderRows(containerId, current, sideKey);
}

function addLadderRow(sideKey) {
  const current = getLadderFromUI(sideKey);
  let newTrigger = 1.5, newLock = 1.4;
  if (current.length > 0) {
    const last = current[current.length - 1];
    newTrigger = last[0] * 1.5;
    newLock = newTrigger * (sideKey === "buy" ? 0.93 : 0.85);
  }
  current.push([newTrigger, newLock]);
  const containerId = sideKey === "buy" ? "buyLadderRows" : "sellLadderRows";
  renderLadderRows(containerId, current, sideKey);
}

function resetLadderDefault() {
  renderBothLadders(DEFAULT_BUY_LADDER, DEFAULT_SELL_LADDER);
  showToast("استرجاع الجدول الافتراضي للاثنين");
}

function updateExitModeUI() {
  const mode = $("eo_exit_mode").value;
  $("ladderSection").style.display = mode === "ladder" ? "block" : "none";
  $("manualSection").style.display = mode === "manual" ? "block" : "none";
}

// ----------- Entry-Only settings -----------
async function applyEntryOnlySettings() {
  const exitMode = $("eo_exit_mode").value;
  const body = {
    exit_mode: exitMode,
    enabled: $("eo_enabled").value === "true",
    buy_sl_pct: parseFloat($("eo_buy_sl_global").value),
    sell_sl_pct: parseFloat($("eo_sell_sl_global").value),
    max_hold_bars: parseInt($("eo_max_hold").value),
    cooldown_bars: parseInt($("eo_cooldown").value),
    smart_reverse: $("eo_smart_reverse").value === "true",
  };
  
  if (exitMode === "manual") {
    body.buy_tp_pct = parseFloat($("eo_buy_tp").value);
    body.sell_tp_pct = parseFloat($("eo_sell_tp").value);
    // Override SL with manual section's SL
    body.buy_sl_pct = parseFloat($("eo_buy_sl").value);
    body.sell_sl_pct = parseFloat($("eo_sell_sl").value);
  } else if (exitMode === "ladder") {
    const buyLadder = getLadderFromUI("buy");
    const sellLadder = getLadderFromUI("sell");
    if (buyLadder.length === 0 || sellLadder.length === 0) {
      showToast("⚠ الـladder فاضي — يجب إضافة مستوى لكل من BUY و SELL");
      return;
    }
    body.buy_ladder = buyLadder;
    body.sell_ladder = sellLadder;
    body.use_ladder = true;
  }
  
  // Add time-stops
  body.exit_after_no_profit_bars = parseInt($("eo_time_no_profit").value) || 0;
  body.exit_after_loss_bars = parseInt($("eo_time_loss").value) || 0;
  
  try {
    await fetch("/api/entry-only-settings", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    showToast("✓ تم تطبيق الإعدادات");
    closeSettings();
  } catch (e) { console.error(e); showToast("خطأ في التطبيق"); }
}

// ----------- Paper -----------
async function resetPaper(engine) {
  const balanceInput = engine === "v41_stable" ? "v41_balance_input" : "eo_balance_input";
  const balance = parseFloat($(balanceInput).value);
  try {
    await fetch("/api/paper-reset", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ engine, initial_balance: balance }),
    });
    await updatePaperStats();
    showToast("تم إعادة التعيين");
  } catch (e) { console.error(e); }
}

async function closeManually(engine) {
  try {
    await fetch("/api/close-position", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ engine }),
    });
    await updatePaperStats();
    showToast("تم إغلاق الصفقة");
  } catch (e) { console.error(e); }
}

// ----------- Reports -----------
function downloadReport(engineName) {
  showToast("📥 تحضير التقرير...");
  window.location.href = `/api/report/${engineName}`;
}

// ----------- Toast -----------
function showToast(msg) {
  let t = document.createElement("div");
  t.textContent = msg;
  t.style.cssText = "position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#151a2b;color:#fff;padding:10px 18px;border-radius:8px;border:1px solid #283044;z-index:9999;font-size:13px;box-shadow:0 4px 12px rgba(0,0,0,.4);max-width:90%;text-align:center;direction:rtl";
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2500);
}

// ----------- Init -----------
window.addEventListener("DOMContentLoaded", () => {
  initChart();
  renderBothLadders(DEFAULT_BUY_LADDER, DEFAULT_SELL_LADDER);
  updateExitModeUI();
  
  // Initial load
  updateStatus();
  tickChart();
  updatePaperStats();
  
  // Polling
  setInterval(updateStatus, 3000);
  setInterval(tickChart, 2000);
  setInterval(updatePaperStats, 1500);
});
