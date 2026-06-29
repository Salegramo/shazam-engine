// ============================================================
// Shazam Live Dashboard v1.0
// ============================================================

let chart = null;
let candleSeries = null;
let supertrendLineUp = null;
let supertrendLineDown = null;
let tpLineSeries = null;
let slLineSeries = null;

let currentEngine = "v41_stable";
let currentDisplayMode = "single";
let currentShowMarkers = true;
let stThickness = 2;

const DEFAULT_LADDER = [
  [0.10, 0.06], [0.18, 0.11], [0.28, 0.18],
  [0.40, 0.27], [0.55, 0.38], [0.75, 0.52], [1.00, 0.72],
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
  supertrendLineUp = chart.addLineSeries({
    color: "#00ff88", lineWidth: 2, lineStyle: 0,
    crosshairMarkerVisible: false, lastValueVisible: false, priceLineVisible: false,
  });
  supertrendLineDown = chart.addLineSeries({
    color: "#ff4d4d", lineWidth: 2, lineStyle: 0,
    crosshairMarkerVisible: false, lastValueVisible: false, priceLineVisible: false,
  });
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

// ----------- Chart Update -----------
function updateChart(data) {
  if (!data || !data.candles) return;
  candleSeries.setData(data.candles);
  
  // SuperTrend
  supertrendLineUp.applyOptions({ lineWidth: stThickness });
  supertrendLineDown.applyOptions({ lineWidth: stThickness });
  const upSeries = [];
  const downSeries = [];
  for (const pt of data.supertrend || []) {
    if (pt.direction === "bull") {
      upSeries.push({ time: pt.time, value: pt.value });
      downSeries.push({ time: pt.time });
    } else {
      downSeries.push({ time: pt.time, value: pt.value });
      upSeries.push({ time: pt.time });
    }
  }
  supertrendLineUp.setData(upSeries);
  supertrendLineDown.setData(downSeries);
  
  // Markers
  const markers = [];
  for (const s of data.signals || []) {
    markers.push({
      time: s.time, position: s.side === "BUY" ? "belowBar" : "aboveBar",
      color: s.side === "BUY" ? "#00ff88" : "#ff4d4d",
      shape: s.side === "BUY" ? "arrowUp" : "arrowDown", text: s.side,
    });
  }
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
  markers.sort((a, b) => a.time - b.time);
  candleSeries.setMarkers(markers);
  
  // TP/SL lines
  if (data.tp_line) {
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
    if (data.show_markers !== currentShowMarkers) {
      currentShowMarkers = data.show_markers;
      if ($("showMarkers")) $("showMarkers").value = String(data.show_markers);
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
  if (s.ladder && s.ladder.length) {
    renderLadder(s.ladder);
  } else {
    renderLadder(DEFAULT_LADDER);
  }
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

async function applyShowMarkers() {
  const show = $("showMarkers").value === "true";
  currentShowMarkers = show;
  try {
    await fetch("/api/show-markers", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ show }),
    });
    showToast(show ? "تم إظهار الإشارات" : "تم إخفاء الإشارات");
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
    await tickChart();
  } catch (e) { console.error(e); }
}

// ----------- Step Ladder editor -----------
function renderLadder(ladder) {
  const container = $("ladderRows");
  if (!container) return;
  container.innerHTML = "";
  for (let i = 0; i < ladder.length; i++) {
    const row = ladder[i];
    const div = document.createElement("div");
    div.className = "ladder-row";
    div.innerHTML = `
      <input type="number" step="0.01" min="0" value="${row[0]}" data-idx="${i}" data-field="trigger">
      <span class="arrow">→</span>
      <input type="number" step="0.01" min="0" value="${row[1]}" data-idx="${i}" data-field="lock">
      <button onclick="removeLadderRow(${i})">✕</button>
    `;
    container.appendChild(div);
  }
  const addBtn = document.createElement("button");
  addBtn.className = "add-ladder-btn";
  addBtn.textContent = "+ إضافة مستوى";
  addBtn.onclick = addLadderRow;
  container.appendChild(addBtn);
}

function getLadderFromUI() {
  const inputs = $("ladderRows").querySelectorAll(".ladder-row");
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

function removeLadderRow(idx) {
  const current = getLadderFromUI();
  current.splice(idx, 1);
  renderLadder(current);
}

function addLadderRow() {
  const current = getLadderFromUI();
  // Suggest a new row above the last
  let newTrigger = 1.5, newLock = 1.0;
  if (current.length > 0) {
    const last = current[current.length - 1];
    newTrigger = last[0] * 1.5;
    newLock = newTrigger * 0.72;
  }
  current.push([newTrigger, newLock]);
  renderLadder(current);
}

function resetLadderDefault() {
  renderLadder(DEFAULT_LADDER);
  showToast("استرجاع الجدول الافتراضي");
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
  };
  
  if (exitMode === "manual") {
    body.buy_tp_pct = parseFloat($("eo_buy_tp").value);
    body.sell_tp_pct = parseFloat($("eo_sell_tp").value);
    // Override SL with manual section's SL
    body.buy_sl_pct = parseFloat($("eo_buy_sl").value);
    body.sell_sl_pct = parseFloat($("eo_sell_sl").value);
  } else if (exitMode === "ladder") {
    const ladder = getLadderFromUI();
    if (ladder.length === 0) {
      showToast("⚠ الـladder فاضي — يجب إضافة مستوى واحد على الأقل");
      return;
    }
    body.ladder = ladder;
    body.use_ladder = true;
  }
  
  try {
    await fetch("/api/entry-only-settings", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    showToast("✓ تم تطبيق الإعدادات");
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
  renderLadder(DEFAULT_LADDER);
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
