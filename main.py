# -*- coding: utf-8 -*-
import json, time, threading, traceback, math
import requests
from collections import deque, defaultdict
from websocket import WebSocketApp
from flask import Flask

# =========================
# إعدادات ثابتة (لا .env)
# =========================
SAQAR_WEBHOOK   = "https://saadisaadibot-saqarxbo-production.up.railway.app/"

# اختيار ديناميكي من Bitvavo (فريم 15m + 5m)
TOP_15M             = 5
TOP_5M              = 5
MIN_EUR_VOL_24H     = 50_000.0
CANDLES_LIMIT       = 20
EXCLUDE_BASES       = {"EUR", "USDT", "USDC"}

BITVAVO_BASE_URL    = "https://api.bitvavo.com/v2"

# =========================
# 🔥 كاشف التراكم/الضغط (على Binance bookTicker)
# =========================
SCAN_INTERVAL       = 180        # كل كم ثانية نعيد اختيار الأهداف
COOLDOWN_SEC        = 1200       # كولداون لكل زوج بعد إطلاق إشارة
DEDUP_WINDOW_SEC    = 6.0
MAX_ALERTS_PER_MIN  = 12
SUSTAIN_SEC         = 1.5        # لازم الشروط تبقى ≥ العتبة بهذه المدة

# فلاتر سيولة/سبريد
SPREAD_MAX_BP       = 25.0       # أقصى سبريد (bp)
MIN_BID_USDT        = 1000.0      # حد أدنى سيولة على أفضل Bid
IMB_MIN             = 1.25       # تفوق bids/asks الأدنى المقبول

# ضغط المشترين (EWMA)
PRESSURE_ALPHA      = 0.35       # نعومة EWMA
PRESSURE_TRIG       = 0.55       # عتبة إطلاق: ((bid-ask)/(bid+ask))_EWMA
PRESSURE_CLEAR      = 0.20       # لتفريغ الإشارة (hysteresis)

# اتجاه سعري قصير جداً
WINDOW_SEC          = 18         # نافذة تحليل upticks/الميل
UPTICKS_MIN         = 6          # كم مرّة يرتفع أفضل Bid داخل النافذة
MID_SLOPE_MIN_BP    = 4.5        # ميل midprice (bp) خلال النافذة

DEBUG_REJECTIONS    = False

# =========================
# متغيرات داخلية
# =========================
app = Flask(__name__)
_binance_symbols_ok = set()
_targets_lock = threading.Lock()
_targets      = set()

_ws           = None
_alert_bucket = deque()
_last_fire    = defaultdict(float)
_dedup_seen   = defaultdict(float)

# حالة لكل رمز
state = defaultdict(lambda: {
    "q": deque(maxlen=256),     # (ts, bid, ask, bqty, aqty, mid)
    "ewma_pressure": 0.0,
    "last_uptick_bid": None,
    "sustain_start": 0.0
})

# =========================
# أدوات مساعدة
# =========================
def log(*args):
    print(*args, flush=True)

def post_to_saqr(symbol_base):
    """إرسال أمر شراء لصقر مباشرة."""
    try:
        payload = {"text": f"اشتري {symbol_base}"}
        r = requests.post(SAQAR_WEBHOOK, json=payload, timeout=10)
        log(f"📨 SAQAR → {symbol_base} | status={r.status_code}")
    except Exception as e:
        log("❌ SAQAR error:", repr(e))
        traceback.print_exc()

def _rate_ok():
    now = time.time()
    while _alert_bucket and _alert_bucket[0] < now - 60:
        _alert_bucket.popleft()
    return len(_alert_bucket) < MAX_ALERTS_PER_MIN

def _mark_alert():
    _alert_bucket.append(time.time())

def _dedup(key):
    now = time.time()
    last = _dedup_seen.get(key, 0.0)
    if now - last < DEDUP_WINDOW_SEC:
        return False
    _dedup_seen[key] = now
    return True

def _reject(reason, symbol=None):
    if DEBUG_REJECTIONS:
        log(f"⏭️  skip {symbol or ''} | {reason}")

# =========================
# Bitvavo helpers (15m + 5m اختيار)
# =========================
def bitvavo_get_markets_eur():
    r = requests.get(f"{BITVAVO_BASE_URL}/markets", timeout=12)
    r.raise_for_status()
    markets = []
    for m in r.json():
        mk = m.get("market","")
        if mk.endswith("-EUR") and "-" in mk:
            base = mk.split("-")[0]
            if base not in EXCLUDE_BASES:
                markets.append((base, mk))
    return markets  # [(BASE, "BASE-EUR"), ...]

def bitvavo_get_24h_map_eur_volume():
    try:
        r = requests.get(f"{BITVAVO_BASE_URL}/ticker/24h", timeout=12)
        r.raise_for_status()
        res = {}
        for x in r.json():
            mk = x.get("market","")
            if not mk.endswith("-EUR"): continue
            base = mk.split("-")[0]
            if base in EXCLUDE_BASES: continue
            vol_base = float(x.get("volume") or 0.0)
            last_eur = float(x.get("last") or x.get("price") or 0.0)
            res[base] = vol_base * last_eur
        return res
    except Exception:
        return {}

def bitvavo_change_pct(market_eur: str, interval: str) -> float:
    try:
        url = f"{BITVAVO_BASE_URL}/{market_eur}/candles?interval={interval}&limit={CANDLES_LIMIT}"
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        c = r.json()  # [ts, open, high, low, close, volume]
        if not c or len(c) < 3: return 0.0
        first_close = float(c[0][4]); last_close = float(c[-1][4])
        if first_close <= 0: return 0.0
        return (last_close - first_close) / first_close * 100.0
    except Exception:
        return 0.0

def fetch_bitvavo_momentum_targets():
    markets = bitvavo_get_markets_eur()
    vol_map = bitvavo_get_24h_map_eur_volume()
    rows_15, rows_5 = [], []
    for base, mk in markets:
        eur_vol = vol_map.get(base, 0.0)
        if eur_vol < MIN_EUR_VOL_24H: continue
        ch15 = bitvavo_change_pct(mk, "15m")
        ch05 = bitvavo_change_pct(mk, "5m")
        rows_15.append((base, ch15, eur_vol))
        rows_5.append((base, ch05, eur_vol))
    rows_15.sort(key=lambda t: (t[1], t[2]), reverse=True)
    rows_5.sort(key=lambda t: (t[1], t[2]), reverse=True)
    top15 = [b for (b, _, _) in rows_15[:TOP_15M]]
    top5  = [b for (b, _, _) in rows_5[:TOP_5M]]

    merged, seen = [], set()
    for b in top15 + top5:
        if b not in seen:
            seen.add(b); merged.append(b)
    return set(merged)

# =========================
# Binance
# =========================
def fetch_binance_exchange_info():
    global _binance_symbols_ok
    try:
        url = "https://api.binance.com/api/v3/exchangeInfo"
        data = requests.get(url, timeout=15).json()
        ok = {s["symbol"] for s in data.get("symbols", []) if s.get("status")=="TRADING"}
        _binance_symbols_ok = ok
        log(f"✅ exchangeInfo loaded: {len(ok)} symbols")
    except Exception:
        pass

def refresh_targets_loop():
    while True:
        try:
            bases = fetch_bitvavo_momentum_targets()
            with _targets_lock:
                new_targets = {f"{b}USDT" for b in bases if f"{b}USDT" in _binance_symbols_ok}
                if new_targets:
                    _targets.clear()
                    _targets.update(new_targets)
                    log("🎯 targets (5m+15m):", ", ".join(sorted(_targets)) or "-")
        except Exception as e:
            log("refresh_targets_loop error:", repr(e))
        time.sleep(SCAN_INTERVAL)

# =========================
# WebSocket
# =========================
def build_stream_url(symbols):
    parts = [f"{s.lower()}@bookTicker" for s in symbols]
    return "wss://stream.binance.com:9443/stream?streams=" + "/".join(parts)

def _update_state_and_check(symbol, bid, ask, bqty, aqty):
    st = state[symbol]
    now = time.time()
    if bid <= 0 or ask <= 0 or ask <= bid: 
        _reject("invalid quotes", symbol); 
        return False, {}

    mid = (bid + ask) / 2.0
    spread_bp = (ask - bid) / mid * 10000.0

    # فلتر سبريد/سيولة أولي (شدّ السبريد)
    bid_usdt = bqty * bid
    ask_usdt = aqty * ask
    if spread_bp > 25.0:   # ← بدل 35.0
        _reject(f"spread {spread_bp:.1f}bp", symbol); 
        return False, {}
    if bid_usdt < MIN_BID_USDT: 
        _reject(f"bid_usdt {bid_usdt:.0f}<min", symbol); 
        return False, {}

    imb = (bid_usdt / max(1e-9, ask_usdt)) if ask_usdt>0 else 999.0
    if imb < IMB_MIN:
        _reject(f"imb {imb:.2f}<min", symbol)
        return False, {}

    # سجل العينة
    st["q"].append((now, bid, ask, bqty, aqty, mid))

    # EWMA للضغط: (B-A)/(B+A)
    raw_press = (bid_usdt - ask_usdt) / max(1e-9, (bid_usdt + ask_usdt))
    st["ewma_pressure"] = (1 - PRESSURE_ALPHA) * st["ewma_pressure"] + PRESSURE_ALPHA * raw_press

    # upticks للـ bid خلال نافذة
    if st["last_uptick_bid"] is None:
        st["last_uptick_bid"] = bid
    upticks = 0; downticks = 0
    if bid > st["last_uptick_bid"]: upticks += 1
    elif bid < st["last_uptick_bid"]: downticks += 1
    st["last_uptick_bid"] = bid

    # حساب داخل النافذة
    q = st["q"]
    b_prev = None; upt_in_win = 0
    for ts, b, a, BQ, AQ, m in list(q)[::-1]:
        if now - ts > WINDOW_SEC: break
        if b_prev is not None and b > b_prev: upt_in_win += 1
        b_prev = b

    # ميل midprice داخل النافذة
    m_old = None
    for ts, b, a, BQ, AQ, m in q:
        if now - ts <= WINDOW_SEC:
            m_old = m; break
    slope_bp = 0.0
    if m_old and mid:
        slope_bp = (mid - m_old) / ((mid + m_old)/2.0) * 10000.0

    # شرط الاستدامة (تشغيل/إيقاف) — صار AND بدل OR
    press_ok = st["ewma_pressure"] >= PRESSURE_TRIG
    trend_ok = (slope_bp >= MID_SLOPE_MIN_BP) and (upt_in_win >= UPTICKS_MIN)

    if press_ok and trend_ok:
        if st["sustain_start"] == 0.0:
            st["sustain_start"] = now
        sustained = (now - st["sustain_start"]) >= SUSTAIN_SEC
    else:
        st["sustain_start"] = 0.0
        sustained = False
        if st["ewma_pressure"] < PRESSURE_CLEAR:
            pass

    metrics = {
        "spread_bp": spread_bp,
        "bid_usdt": bid_usdt,
        "ask_usdt": ask_usdt,
        "imb": imb,
        "press_ewma": st["ewma_pressure"],
        "upticks": upt_in_win,
        "slope_bp": slope_bp
    }
    return sustained, metrics
def on_message(ws, message):
    try:
        data = json.loads(message)
        d = data.get("data", {})
        s = d.get("s")
        if not s: return
        with _targets_lock:
            if s not in _targets: return

        bid = float(d.get("b","0")); ask = float(d.get("a","0"))
        bqty = float(d.get("B","0")); aqty = float(d.get("A","0"))

        ok, M = _update_state_and_check(s, bid, ask, bqty, aqty)
        if not ok:
            return

        now = time.time()
        if now - _last_fire[s] < COOLDOWN_SEC: 
            return
        if not _rate_ok(): 
            return
        key = f"{s}:{int(M['press_ewma']*1000)}:{int(M['slope_bp'])}"
        if not _dedup(key): 
            return

        _mark_alert(); _last_fire[s] = now
        base = s.replace("USDT","")
        log(f"🚀 ACCUMULATION {s} | spread={M['spread_bp']:.1f}bp | bid${M['bid_usdt']:.0f} | "
            f"imb={M['imb']:.2f} | press={M['press_ewma']:.2f} | upticks={M['upticks']} | slope={M['slope_bp']:.1f}bp")
        post_to_saqr(base)

    except Exception:
        # لا نكسر الحلقة
        pass

def ws_loop():
    global _ws
    current_set = set()
    while True:
        try:
            with _targets_lock:
                t = sorted(_targets)
            if not t:
                time.sleep(3); continue
            if t != sorted(current_set):
                current_set = set(t)
                if _ws:
                    try: _ws.close()
                    except: pass
                url = build_stream_url(t)
                _ws = WebSocketApp(url, on_message=on_message)
                threading.Thread(
                    target=_ws.run_forever,
                    kwargs={"ping_interval":20,"ping_timeout":10},
                    daemon=True
                ).start()
            time.sleep(5)
        except Exception:
            time.sleep(5)

# =========================
# Flask (صحة فقط)
# =========================
@app.get("/")
def health():
    with _targets_lock:
        ts = ",".join(sorted(_targets)) or "-"
    return {"ok": True, "targets": ts}

# =========================
# الإقلاع
# =========================
def boot():
    log("🚀 Accumulation Sniper is alive ✅")
    fetch_binance_exchange_info()
    threading.Thread(target=refresh_targets_loop, daemon=True).start()
    threading.Thread(target=ws_loop, daemon=True).start()

boot()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)