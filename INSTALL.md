# Shazam Live v1.0 — التركيب والاستخدام

## ما الجديد في النسخة v1.0

```
✓ FIX: إصلاح 404 على /api/* (router مسجل قبل lifespan)
✓ NEW: Compare Mode — كلا المحركين يعملان بالخلفية للمقارنة
✓ NEW: Toggle إظهار/إخفاء markers على الشارت
✓ NEW: قائمة إعدادات منسدلة (Display / SuperTrend / Reports / System)
✓ NEW: Step Ladder Protection للـEntry-Only
✓ NEW: Manual Mode للـTP/SL الثابت
✓ NEW: Disabled Mode (للتحكم اليدوي 100%)
✓ NEW: تصدير تقارير ZIP لكل محرك (signals + trades + equations + DNA)
```

## نظام الحماية المتدرج (Step Ladder)

```
Trigger    Lock        Giveback   Ratio
─────────────────────────────────────────
0.10%   →  0.06%       0.040%     60%
0.18%   →  0.11%       0.070%     61%
0.28%   →  0.18%       0.100%     64%
0.40%   →  0.27%       0.130%     68%
0.55%   →  0.38%       0.170%     69%
0.75%   →  0.52%       0.230%     69%
1.00%   →  0.72%       0.280%     72%
> 1.00%:   Peak × 0.72  (overflow ratio)
```

**كيف يشتغل**:
1. BUY entry @ $60,000
2. السعر +0.05% → لا lock (SL يحمي عند -1.50%)
3. السعر +0.12% → triggers 0.10% → lock@0.06%
4. السعر +0.30% → triggers 0.28% → lock@0.18%
5. السعر يرتد لـ+0.18% → 🔒 EXIT @ lock = +0.18%

## التركيب

```bash
# 1. ارفع للـVPS
scp shazam_live_v1.zip user@vps:/opt/

# 2. على الـVPS
cd /opt && unzip shazam_live_v1.zip && cd shazam_live_v1

# 3. شغّل
docker-compose up -d --build

# 4. افتح
# http://your-vps-ip:8000
```

## استخدام الـUI

### ⚙ قائمة الإعدادات (4 أقسام)

**🖥 العرض**:
- وضع التشغيل: واحد / مقارنة
- إظهار الإشارات: نعم / إخفاء

**📈 SuperTrend**:
- الفترة (ATR), المضاعف, البعد عن الشموع, سماكة الخط

**📊 التقارير**:
- زر تصدير v4.1 Stable
- زر تصدير Entry-Only
- التقرير ZIP يحتوي: signals.csv + trades.csv + equations_report.csv + summary.json + live_dna_snapshot.csv

**ℹ النظام**:
- معلومات الـsymbol/timeframe/warmup/mined rules/الإشارات الكلية

### الكروت

كل محرك له كرت منفصل مع:
- الرصيد + PnL% + عدد الصفقات
- الإشارات المُستلمة + Wins/Losses/WR
- Floating PnL للـopen position (مع Lock info للـEntry-Only)
- مبلغ التداول الافتراضي (قابل للتعديل + reset)

### Entry-Only: 3 أوضاع خروج

**Step Ladder (افتراضي)**:
- جدول قابل للضبط من UI
- إضافة/حذف مستويات
- زر "افتراضي" لاستعادة الإعدادات

**Manual**:
- TP/SL ثابت لكل من BUY و SELL

**Disabled**:
- البوت لا يفتح صفقات تلقائياً
- المستخدم يقرر يدوياً

## التقارير

كل تقرير ZIP يحتوي:

```
report_engine_YYYYMMDD_HHMMSS.zip
├── signals.csv         كل الإشارات (timestamp/side/price/window/wr/formula)
├── trades.csv          الصفقات الفعلية مع outcomes
├── equations_report.csv المعادلات + WR + signals + wins/losses
├── summary.json        إحصائيات شاملة + audit
├── live_dna_snapshot.csv الـDNA الخام (للتدقيق)
└── README.md           شرح المحتويات
```

## env vars

```yaml
environment:
  - SHAZAM_SYMBOL=BTCUSDT
  - SHAZAM_TIMEFRAME=5m
  - SHAZAM_WARMUP=500       # 200-2500
```

## Workflow المُقترح

```
1. شغّل المشروع → انتظر اكتمال Mining (~30-60s)
2. اختر Compare Mode من الإعدادات
3. اترك المحركين يعملان لـ24-48 ساعة
4. صدّر التقارير لكل محرك
5. حلّل: أي محرك أفضل؟ أي إعدادات تحتاج تعديل؟
6. عدّل الـladder/TP لو لزم، ثم استمر
```
