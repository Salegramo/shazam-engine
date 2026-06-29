# Shazam Live v1 — دليل التركيب

## على Hostinger VPS مع Dokploy

### الطريقة الأولى: ملف ZIP مباشرة

1. ارفع `shazam_live_v1.zip` على الـVPS وفك ضغطه:
```bash
unzip shazam_live_v1.zip -d /opt/shazam-live
cd /opt/shazam-live
```

2. في Dokploy، أنشئ مشروع جديد:
- **Type**: Docker Compose
- **Source**: Local Path → `/opt/shazam-live`
- **Domain**: مثلاً `shazam-live.yourdomain.com`
- **Port**: 8000

3. اضغط Deploy.

### الطريقة الثانية: Docker Compose يدوياً

```bash
cd /opt/shazam-live
docker-compose up -d --build
```

ثم وصّل بـnginx/Caddy للـreverse proxy على port 8000.

## التشغيل المحلي

```bash
pip install -r requirements.txt
python -m uvicorn web_app:app --host 0.0.0.0 --port 8000
```

ثم افتح: http://localhost:8000

## ⚠️ مهم: بعد التشغيل

### عند البدء (warmup):
1. السيرفر يجلب 500-600 شمعة تاريخية من Binance
2. يبني DNA كامل (~4099 column)
3. يشغّل Mining (~30-60s ينتج ~3000 rule)
4. شريط `WARMING` يظهر في الواجهة
5. بعد اكتمال الـmining: `LIVE`

### كيف تتأكد إن كل شي شغّال:

في الـlogs:
```
📊 Fetching 600 historical candles for BTCUSDT 5m...
✓ Loaded 599 closed candles
🔨 Building DNA from 599 candles...
  DNA built in 3.2s (cols=4099)
⛏ Mining (Multi-Window) — this takes ~30-60s one-time...
  Mining done in 42.1s (2854 rules)
✅ Engine ready. Scanning new candles for signals...
📡 WebSocket started for btcusdt@kline_5m
```

في الـUI:
- Live pill: أخضر "LIVE"
- في settings: مكتوب عدد الـrules (e.g., "2854 rules")
- بعد أول candle جديد (5 دقائق): قد تظهر signal

## إعدادات قابلة للتعديل (env vars)

```yaml
environment:
  - SHAZAM_SYMBOL=BTCUSDT          # رمز التداول
  - SHAZAM_TIMEFRAME=5m            # 1m, 5m, 15m, 1h
  - SHAZAM_WARMUP=500              # عدد candles للـwarmup (500-2500)
```

## الواجهة

### Header
- Live pill: حالة الاتصال
- Settings (⚙): إعدادات SuperTrend

### Chart
- Candlestick chart
- SuperTrend line (أخضر صاعد / أحمر هابط)
- Entry markers (سهم BUY/SELL على الشمعة)
- Exit markers (دائرة EXIT)
- TP/SL lines (للـEntry-Only فقط عند الـopen position)

### Engine tabs
- اضغط على المحرك لتفعيله
- المحرك غير المفعّل يبقى متوقف (paused)
- بعد التبديل: signals الجديدة من المحرك الـactive فقط

### v4.1 Stable Card
- Balance, PnL%, Trades, Wins, Losses, WR
- Floating PnL لو في open position
- يعمل **تلقائياً** — لا تحكم يدوي

### Entry-Only Card
- نفس الإحصائيات
- إعدادات قابلة للتعديل:
  - BUY TP/SL %
  - SELL TP/SL %
  - Max Hold (candles)
  - الخروج التلقائي: مفعّل / يدوي فقط
- زر "إغلاق يدوي" لما في open position

## الأخطاء الشائعة

### الـMining لا يكتمل
- تحقق من logs
- تأكد إن العدد الكافي من candles (>500)
- إذا الـmining فشل، أعد تشغيل docker

### لا signals تظهر
- انتظر بعد اكتمال الـmining
- الـsignals تظهر فقط عند **closed candles**
- في 5m timeframe: signal كل 5 دقائق على الأكثر

### الـWebSocket ينقطع
- النظام يحاول إعادة الاتصال تلقائياً
- لو فشل تماماً: يستخدم REST polling fallback

## الـreverse proxy (nginx مثال)

```nginx
location / {
    proxy_pass http://localhost:8000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection 'upgrade';
    proxy_set_header Host $host;
    proxy_cache_bypass $http_upgrade;
}
```

## بعد التركيب — Quick test

```bash
# Health check
curl http://localhost:8000/health

# Status
curl http://localhost:8000/api/status | python -m json.tool

# Chart data
curl "http://localhost:8000/api/chart?n=50" | python -m json.tool
```

## Performance

- Mining one-time: ~30-60s
- Per-candle scan: ~50ms
- DNA rebuild on closed candle: ~3-5s (acceptable for 5m timeframe)
- WS reconnection: automatic with exponential backoff
