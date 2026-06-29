// ============================================================
// Shazam Live Dashboard
// ============================================================

// Chart globals
let chart = null;
let candleSeries = null;
let supertrendLineUp = null;
let supertrendLineDown = null;
let tpLineSeries = null;
let slLineSeries = null;

// State
let currentEngine = "v41_stable";
let lastPrice = null;
let stThickness = 2;

// ----------- Helpers -----------
function $(id) { return document.getElementById(id); }
function fmtUsd(v) { return "$" + Number(v).toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ","); }
function fmtPct(v) {
  const sign = v >= 0 ? "+" : "";
  return sign + Number(v).toFixed(2) + "%";
}
function fmtTime(ts) {
  const d = new Date(ts * 1000);
  const h = String(d.getHours()).padStart(2, "0");
  const m = String(d.getMinutes()).padStart(2, "0");
  return `${h}:${m}`;
}

// ----------- Chart Setup -----------
function initChart() {
  const container = $("chart");
  chart = LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height: 320,
    layout: {
      background: { type: "solid", color: "#0a0e1a" },
      textColor: "#9ca3af",
    },
    grid: {
      vertLines: { color: "rgba(40,48,68,.4)" },
      horzLines: { color: "rgba(40,48,68,.4)" },
    },
    rightPriceScale: { borderColor: "#283044" },
    timeScale: {
      borderColor: "#283044",
      timeVisible: true,
      secondsVisible: false,
    },
    crosshair: {
      mode: 1,
    },
  });
  
  candleSeries = chart.addCandlestickSeries({
    upColor: "#00ff88",
    downColor: "#ff4d4d",
    borderUpColor: "#00ff88",
    borderDownColor: "#ff4d4d",
    wickUpColor: "#00ff88",
    wickDownColor: "#ff4d4d",
  });
  
  // Two supertrend series — one for bull (green), one for bear (red)
  supertrendLineUp = chart.addLineSeries({
    color: "#00ff88",
    lineWidth: 2,
    lineStyle: 0,
    crosshairMarkerVisible: false,
    lastValueVisible: false,
    priceLineVisible: false,
  });
  supertrendLineDown = chart.addLineSeries({
    color: "#ff4d4d",
    lineWidth: 2,
    lineStyle: 0,
    crosshairMarkerVisible: false,
    lastValueVisible: false,
    priceLineVisible: false,
  });
  
  // TP/SL lines (for entry-only with open position)
  tpLineSeries = chart.addLineSeries({
    color: "#38bdf8",
    lineWidth: 1,
    lineStyle: 2,  // dashed
    crosshairMarkerVisible: false,
    lastValueVisible: true,
    priceLineVisible: false,
    title: "TP"
  });
  slLineSeries = chart.addLineSeries({
    color: "#fb7185",
    lineWidth: 1,
    lineStyle: 2,
    crosshairMarkerVisible: false,
    lastValueVisible: true,
    priceLineVisible: false,
    title: "SL"
  });
  
  // Resize handler
  window.addEventListener("resize", () => {
    chart.applyOptions({ width: container.clientWidth });
  });
}

// ----------- Chart Update -----------
function updateChart(data) {
  if (!data || !data.candles) return;
  
  // Candles
  candleSeries.setData(data.candles);
  
  // SuperTrend — split into bull and bear segments
  const bullPoints = [];
  const bearPoints = [];
  for (const pt of data.supertrend || []) {
    if (pt.direction === "bull") {
      bullPoints.push({ time: pt.time, value: pt.value });
    } else {
      bearPoints.push({ time: pt.time, value: pt.value });
    }
  }
  // Apply thickness
  supertrendLineUp.applyOptions({ lineWidth: stThickness });
  supertrendLineDown.applyOptions({ lineWidth: stThickness });
  // Need to use whitespace to break the line at direction changes
  // Build unified series with `null` values at transitions for clean breaks
  const upSeries = [];
  const downSeries = [];
  for (const pt of data.supertrend || []) {
    if (pt.direction === "bull") {
      upSeries.push({ time: pt.time, value: pt.value });
      downSeries.push({ time: pt.time }); // gap on down line
    } else {
      downSeries.push({ time: pt.time, value: pt.value });
      upSeries.push({ time: pt.time }); // gap on up line
    }
  }
  supertrendLineUp.setData(upSeries);
  supertrendLineDown.setData(downSeries);
  
  // Trade markers
  const markers = [];
  // Entry signals (just visual markers on chart)
  for (const s of data.signals || []) {
    markers.push({
      time: s.time,
      position: s.side === "BUY" ? "belowBar" : "aboveBar",
      color: s.side === "BUY" ? "#00ff88" : "#ff4d4d",
      shape: s.side === "BUY" ? "arrowUp" : "arrowDown",
      text: s.side,
    });
  }
  // Trade markers (from paper bot): entry + exit
  for (const m of data.trade_markers || []) {
    if (m.type === "entry") {
      markers.push({
        time: m.time,
        position: m.side === "BUY" ? "belowBar" : "aboveBar",
        color: m.side === "BUY" ? "#34d399" : "#fb7185",
        shape: m.side === "BUY" ? "arrowUp" : "arrowDown",
        text: m.side + (m.open ? "●" : ""),
      });
    } else if (m.type === "exit") {
      markers.push({
        time: m.time,
        position: "inBar",
        color: m.pnl_pct >= 0 ? "#39c5cf" : "#fb7185",
        shape: "circle",
        text: "EXIT",
      });
    }
  }
  // Sort by time
  markers.sort((a, b) => a.time - b.time);
  candleSeries.setMarkers(markers);
  
  // TP/SL lines for entry-only open position
  if (data.tp_line) {
    const tp = data.tp_line;
    const lastTime = data.candles[data.candles.length - 1].time;
    const startTime = tp.entry_time;
    // Single horizontal line from entry to last candle
    tpLineSeries.setData([
      { time: startTime, value: tp.tp_price },
      { time: lastTime + 60, value: tp.tp_price },
    ]);
    slLineSeries.setData([
      { time: startTime, value: tp.sl_price },
      { time: lastTime + 60, value: tp.sl_price },
    ]);
  } else {
    tpLineSeries.setData([]);
    slLineSeries.setData([]);
  }
  
  // Update price display
  if (data.candles.length > 0) {
    const last = data.candles[data.candles.length - 1];
    $("latestPrice").textContent = "$" + Number(last.close).toLocaleString(undefined, { maximumFractionDigits: 2 });
    if (data.candles.length >= 2) {
      const prev = data.candles[0]; // use first candle in view for change
      const change = ((last.close - prev.close) / prev.close) * 100;
      const el = $("chartChange");
      el.textContent = fmtPct(change);
      el.className = "change " + (change >= 0 ? "up" : "down");
    }
    lastPrice = last.close;
  }
}

// ----------- Status -----------
async function updateStatus() {
  try {
    const res = await fetch("/api/status");
    const data = await res.json();
    
    // Live pill
    const pill = $("livePill");
    if (!data.mining_ready && data.mining_in_progress) {
      pill.textContent = "WARMING";
      pill.className = "live-pill warming";
    } else if (data.ws_alive && data.mining_ready) {
      pill.textContent = "LIVE";
      pill.className = "live-pill";
    } else {
      pill.textContent = "OFFLINE";
      pill.className = "live-pill offline";
    }
    
    // Info chips
    $("modeInfo").textContent = data.active_engine === "v41_stable" ? "v4.1 Stable" : "Entry-Only";
    if (data.mining_audit && data.mining_audit.mined_equations) {
      $("warmupInfo").textContent = `${data.mining_audit.mined_equations} rules`;
    } else if (data.mining_in_progress) {
      $("warmupInfo").textContent = `mining...`;
    } else {
      $("warmupInfo").textContent = `${data.candles_loaded || 0} candles`;
    }
    
    // Update engine cards status
    const isV41Active = data.active_engine === "v41_stable";
    $("status_v41").textContent = isV41Active && data.mining_ready ? "نشط" : "متوقف";
    $("status_v41").className = "status-pill " + (isV41Active && data.mining_ready ? "active" : "paused");
    $("status_entry_only").textContent = !isV41Active && data.mining_ready ? "نشط" : "متوقف";
    $("status_entry_only").className = "status-pill " + (!isV41Active && data.mining_ready ? "active" : "paused");
  } catch (e) {
    console.error("status error", e);
  }
}

// ----------- Paper Stats -----------
async function updatePaperStats() {
  try {
    const res = await fetch("/api/paper-stats");
    const data = await res.json();
    
    const v41 = data.v41_stable;
    const eo = data.entry_only;
    
    updateCard("v41", v41);
    updateCard("eo", eo);
  } catch (e) {
    console.error("paper stats error", e);
  }
}

function updateCard(prefix, stats) {
  $(`${prefix}_balance`).textContent = fmtUsd(stats.balance);
  const pnlEl = $(`${prefix}_pnl_pct`);
  pnlEl.textContent = fmtPct(stats.total_pnl_pct);
  pnlEl.className = "stat-value " + (stats.total_pnl_pct >= 0 ? "up" : "down");
  $(`${prefix}_trades`).textContent = stats.trades_total;
  $(`${prefix}_wins`).textContent = stats.wins;
  $(`${prefix}_losses`).textContent = stats.losses;
  $(`${prefix}_wr`).textContent = stats.win_rate_pct.toFixed(1) + "%";
  
  // Floating PnL
  const floatBox = $(`${prefix}_floating_box`);
  if (stats.has_open_position && stats.open_position) {
    floatBox.style.display = "block";
    $(`${prefix}_open_side`).textContent = stats.open_position.side;
    $(`${prefix}_open_side`).className = stats.open_position.side;
    const fEl = $(`${prefix}_floating_pnl`);
    fEl.textContent = fmtPct(stats.floating_pnl_pct);
    fEl.className = "floating-value " + (stats.floating_pnl_pct >= 0 ? "up" : "down");
  } else {
    floatBox.style.display = "none";
  }
}

// ----------- Chart Tick -----------
async function tickChart() {
  try {
    const res = await fetch("/api/chart?n=200");
    const data = await res.json();
    updateChart(data);
    updateSignalsList(data.signals || []);
  } catch (e) {
    console.error("chart error", e);
  }
}

function updateSignalsList(signals) {
  const list = $("signalsList");
  if (!signals || signals.length === 0) {
    list.innerHTML = '<div style="text-align:center;color:var(--muted);padding:14px;font-size:12px">لا توجد إشارات بعد</div>';
    return;
  }
  // Latest first
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
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({engine: engine}),
    });
  } catch (e) { console.error(e); }
  
  // Refresh
  await Promise.all([tickChart(), updateStatus(), updatePaperStats()]);
}

// ----------- Settings -----------
function toggleSettings() {
  $("settingsPanel").classList.toggle("hidden");
}

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
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
    await tickChart();
  } catch (e) { console.error(e); }
}

async function applyEntryOnlySettings() {
  const body = {
    buy_tp_pct: parseFloat($("eo_buy_tp").value),
    buy_sl_pct: parseFloat($("eo_buy_sl").value),
    sell_tp_pct: parseFloat($("eo_sell_tp").value),
    sell_sl_pct: parseFloat($("eo_sell_sl").value),
    max_hold_bars: parseInt($("eo_max_hold").value),
    enabled: $("eo_enabled").value === "true",
  };
  try {
    await fetch("/api/entry-only-settings", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
    showToast("تم تطبيق الإعدادات");
  } catch (e) { console.error(e); }
}

async function resetPaper(engine) {
  const balanceInput = engine === "v41_stable" ? "v41_balance_input" : "eo_balance_input";
  const balance = parseFloat($(balanceInput).value);
  try {
    await fetch("/api/paper-reset", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({engine: engine, initial_balance: balance}),
    });
    await updatePaperStats();
    showToast("تم إعادة التعيين");
  } catch (e) { console.error(e); }
}

async function closeManually(engine) {
  try {
    await fetch("/api/close-position", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({engine: engine}),
    });
    await updatePaperStats();
    showToast("تم إغلاق الصفقة");
  } catch (e) { console.error(e); }
}

// Simple toast
function showToast(msg) {
  let t = document.createElement("div");
  t.textContent = msg;
  t.style.cssText = "position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#151a2b;color:#fff;padding:10px 18px;border-radius:8px;border:1px solid #283044;z-index:9999;font-size:13px;box-shadow:0 4px 12px rgba(0,0,0,.4)";
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2200);
}

// ----------- Init -----------
window.addEventListener("DOMContentLoaded", () => {
  initChart();
  
  // Initial load
  updateStatus();
  tickChart();
  updatePaperStats();
  
  // Polling
  setInterval(updateStatus, 3000);
  setInterval(tickChart, 2000);
  setInterval(updatePaperStats, 1500);
});
