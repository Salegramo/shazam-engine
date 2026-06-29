# Shazam Live v1

محرك تداول حي مع شارت Binance + محركان (v4.1 Stable + Entry-Only) + paper trading.

## المكونات

```
shazam_live_v1/
├── core/                          # المحركات + الـMining
│   ├── v41_stable_engine.py       # المحرك الأول: دخول+خروج تلقائي
│   ├── entry_only_engine.py       # المحرك الثاني: دخول فقط
│   ├── micro_rule_generator_v2.py # micro rule generator (dependency)
│   └── supertrend.py              # مؤشر SuperTrend
├── live/                          # تكامل Binance + إدارة المحركين
│   ├── binance_provider.py        # REST + WebSocket
│   ├── dna_builder_live.py        # تحويل candles → DNA (~4000 column)
│   ├── engine_manager.py          # تنسيق كامل
│   └── paper_bot.py               # تداول وهمي لكل محرك
├── api/                           # REST API
│   └── live_api.py
├── templates/dashboard.html       # الواجهة
├── static/
│   ├── dashboard.css              # نفس theme الأصلي (dark + gradient)
│   └── dashboard.js               # شارت + UI logic
├── web_app.py                     # FastAPI main
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

## المميزات

- ✅ شارت candlestick مع SuperTrend (متغير لون: أخضر/أحمر)
- ✅ كرت لكل محرك مع stats منفصلة
- ✅ Toggle بين المحركين (واحد active، الثاني paused)
- ✅ Paper trading (مبلغ وهمي قابل للضبط)
- ✅ TP/SL lines على الشارت (للـEntry-Only)
- ✅ BUY/SELL/EXIT markers
- ✅ Entry-Only بإعدادات قابلة للضبط من UI
- ✅ خروج يدوي للصفقات (Entry-Only)
- ✅ Binance WebSocket (auto-reconnect) + REST fallback

## كيف يعمل

```
Binance WS
   ↓ (candles closed)
   buffer of candles (5000)
   ↓
   HybridDNABuilder (نفس المختبر — 4099 column)
   ↓
   Mining (مرة واحدة عند warmup ~30-60s)
   ↓ (3000 rule)
   كل candle جديد:
      ↓ active_rules() — أي rules نشطة الآن
      ↓ selection (Quality OR Progressive First-Hit)
      ↓ signal لـactive engine فقط
      ↓ paper bot (auto exit في v4.1 / fixed TP في Entry-Only)
   ↓
   UI polling كل 2-3 ثانية
```

## التشغيل

عبر Docker:
```bash
docker-compose up -d
# افتح: http://localhost:8000
```

أو محلياً:
```bash
pip install -r requirements.txt
python -m uvicorn web_app:app --host 0.0.0.0 --port 8000
```

## ملاحظات

- الـwarmup_bars الافتراضي = 500 (للسرعة في الـlive). يمكن زيادته لـ2500 (نفس المختبر) عبر env var.
- الـmining يحدث **مرة واحدة عند البدء** (~30-60s). بعدها كل candle جديد = ~50ms للـscan.
- المحرك الذي ليس active يبقى متوقفاً (لا يصدر signals)، لكن الـmining مشترك بينهما.
