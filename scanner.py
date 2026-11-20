"""
Bitget Spot volume-spike scanner
- Finds small USDT spot coins whose volume jumps suddenly while price stays flat
- Checks whether the spike looks real or like wash trading
- Sends results by ntfy (phone push) + email
"""
import json
import os
import smtplib
import statistics
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from email.mime.text import MIMEText

import requests

# ================== Settings (edit freely) ==================
CANDLE = "5min"            # candle size
SPIKE_WINDOW = 3           # last N closed candles checked for a spike (covers GitHub delays)
BASELINE = 24              # previous candles used as "normal" volume (24 x 5min = 2 hours)
SPIKE_RATIO = 5.0          # volume must be at least 5x normal (500$ -> 2,500$+)
MIN_SPIKE_USDT = 1500      # ignore tiny spikes below this
MAX_PRICE_MOVE = 3.0       # % — price must not have moved more than this (still "hidden")
MAX_24H_VOL = 3_000_000    # small coins only: 24h volume below this (USDT)
MIN_24H_VOL = 5_000        # skip completely dead coins
COOLDOWN_HOURS = 2         # don't alert the same coin again within this time
STATE_FILE = "state.json"
# ============================================================

API = "https://api.bitget.com"
session = requests.Session()


def get(path, params=None, retries=3):
    for i in range(retries):
        try:
            r = session.get(API + path, params=params, timeout=10)
            if r.status_code == 429:
                time.sleep(1 + i)
                continue
            r.raise_for_status()
            j = r.json()
            if j.get("code") != "00000":
                raise RuntimeError(j.get("msg"))
            return j["data"]
        except Exception as e:
            if i == retries - 1:
                raise RuntimeError(f"{path} {params}: {e}")
            time.sleep(1 + i)


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


# ---------------- Step 1: pick small coins ----------------
def small_coins():
    out = []
    for t in get("/api/v2/spot/market/tickers"):
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        vol = f(t.get("usdtVolume") or t.get("quoteVolume"))
        if MIN_24H_VOL <= vol <= MAX_24H_VOL:
            out.append((sym, vol))
    return out


# ---------------- Step 2: detect a hidden spike ----------------
def check_spike(sym):
    rows = get("/api/v2/spot/market/candles",
               {"symbol": sym, "granularity": CANDLE, "limit": BASELINE + SPIKE_WINDOW + 2})
    if not rows or len(rows) < BASELINE + SPIKE_WINDOW + 1:
        return None
    # [ts, open, high, low, close, baseVol, usdtVol, quoteVol]
    c = sorted(([int(r[0]), f(r[1]), f(r[2]), f(r[3]), f(r[4]), f(r[6]) or f(r[7])]
                for r in rows), key=lambda x: x[0])
    c = c[:-1]  # drop the candle that is still open
    recent = c[-SPIKE_WINDOW:]
    base = c[-(SPIKE_WINDOW + BASELINE):-SPIKE_WINDOW]
    normal = statistics.median(x[5] for x in base) or 1.0

    best = max(recent, key=lambda x: x[5])
    ratio = best[5] / normal
    if ratio < SPIKE_RATIO or best[5] < MIN_SPIKE_USDT:
        return None

    start_price = base[-1][4]
    now_price = c[-1][4]
    move = abs(now_price - start_price) / start_price * 100 if start_price else 99
    if move > MAX_PRICE_MOVE:
        return None  # price already moved — not hidden anymore

    elevated = sum(1 for x in recent if x[5] >= 2 * normal)
    return {
        "normal": normal, "spike": best[5], "ratio": ratio,
        "move": move, "price": now_price, "elevated": elevated,
        "since_ms": recent[0][0],
    }


# ---------------- Step 3: real or fake (wash trading)? ----------------
def analyze_quality(sym, spike):
    fills = get("/api/v2/spot/market/fills", {"symbol": sym, "limit": 500}) or []
    fills = [x for x in fills if int(x.get("ts", 0)) >= spike["since_ms"]] or fills
    book = get("/api/v2/spot/market/orderbook", {"symbol": sym, "type": "step0", "limit": 50})

    score, good, bad = 0, [], []

    # 1) buy vs sell
    buy = sum(f(x["price"]) * f(x["size"]) for x in fills if x.get("side") == "buy")
    total = sum(f(x["price"]) * f(x["size"]) for x in fills) or 1.0
    buy_pct = buy / total * 100
    if buy_pct >= 65:
        score += 2; good.append(f"الشراء غالب {buy_pct:.0f}%")
    elif 42 <= buy_pct <= 58:
        score -= 2; bad.append(f"شراء وبيع متساويين تقريباً ({buy_pct:.0f}%)")
    else:
        bad.append(f"الشراء {buy_pct:.0f}% بس") if buy_pct < 42 else good.append(f"الشراء {buy_pct:.0f}%")

    # 2) repeated identical trade sizes
    sizes = Counter(x["size"] for x in fills)
    top_share = (sizes.most_common(1)[0][1] / len(fills) * 100) if fills else 0
    if len(fills) >= 10 and top_share >= 30:
        score -= 2; bad.append(f"نفس حجم الصفقة متكرر {top_share:.0f}%")
    elif len(fills) >= 10:
        score += 1; good.append("أحجام الصفقات متنوعة")

    # 3) number of trades + buy/sell ping-pong at same price & size
    n = len(fills)
    if n < 8:
        score -= 1; bad.append(f"صفقات قليلة جداً ({n})")
    else:
        good.append(f"{n} صفقة")
    s = sorted(fills, key=lambda x: int(x.get("ts", 0)))
    pairs = sum(1 for a, b in zip(s, s[1:])
                if a.get("side") != b.get("side") and a["price"] == b["price"] and a["size"] == b["size"])
    if n >= 10 and pairs / (n - 1) >= 0.2:
        score -= 2; bad.append("شراء وبيع بالتبادل على نفس السعر والحجم")

    # 4) order book: thin book with big volume = contradiction
    bids = [(f(p), f(q)) for p, q in (book.get("bids") or [])]
    asks = [(f(p), f(q)) for p, q in (book.get("asks") or [])]
    if bids and asks:
        bid, ask = bids[0][0], asks[0][0]
        mid = (bid + ask) / 2
        spread = (ask - bid) / mid * 100
        depth = sum(p * q for p, q in bids if p >= mid * 0.98)
        if spread > 1.5:
            score -= 1; bad.append(f"سبريد واسع {spread:.1f}%")
        if spike["spike"] > 5 * max(depth, 1):
            score -= 1; bad.append(f"دفتر الأوامر ضعيف (${depth:,.0f}) مقارنة بالفوليوم")
        elif depth >= spike["spike"]:
            score += 1; good.append(f"طلبات شراء حقيقية تحت السعر (${depth:,.0f})")
    else:
        score -= 1; bad.append("دفتر الأوامر فاضي")

    # 5) persistence
    if spike["elevated"] >= 2:
        score += 1; good.append(f"الفوليوم عالي في {spike['elevated']} شمعات ورا بعض")
    else:
        bad.append("قفزة في شمعة واحدة بس")

    verdict = "✅ محتمل حقيقي" if score >= 2 else ("⚠️ مشبوه" if score <= -1 else "🔸 غير واضح")
    return verdict, score, good, bad


# ---------------- State (avoid repeat alerts) ----------------
def load_state():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_state(st):
    now = time.time()
    st = {k: v for k, v in st.items() if now - v < 24 * 3600}
    with open(STATE_FILE, "w") as fh:
        json.dump(st, fh)


# ---------------- Alerts: ntfy (phone push) + Email ----------------
def send_ntfy(subject, body, urgent=False):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    try:
        r = requests.post("https://ntfy.sh/", json={
            "topic": topic, "title": subject, "message": body[:3900],
            "priority": 5 if urgent else 3, "tags": ["chart_with_upwards_trend"],
        }, timeout=10)
        print("ntfy:", r.status_code)
    except Exception as e:
        print("ntfy failed:", e)


def send_email(subject, body, urgent=False):
    send_ntfy(subject, body, urgent)
    user, pw = os.environ.get("EMAIL_USER"), os.environ.get("EMAIL_PASS")
    to = os.environ.get("EMAIL_TO") or user
    if not user or not pw:
        print("No email secrets set — printing instead:\n" + body)
        return
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"], msg["From"], msg["To"] = subject, user, to
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(user, pw.replace(" ", ""))
            s.sendmail(user, [to], msg.as_string())
        print("Email sent to", to)
    except Exception as e:
        print("Email failed:", e)


def main():
    t0 = time.time()
    state = load_state()
    coins = small_coins()
    print(f"Scanning {len(coins)} small spot coins...")
    hits = []

    def work(item):
        sym, vol24 = item
        try:
            sp = check_spike(sym)
            if not sp or time.time() - state.get(sym, 0) < COOLDOWN_HOURS * 3600:
                return None
            verdict, score, good, bad = analyze_quality(sym, sp)
            return (score, sym, vol24, sp, verdict, good, bad)
        except Exception as e:
            print("skip", sym, e)
            return None

    # 5 coins in parallel — much faster, still under Bitget's 20 requests/sec limit
    with ThreadPoolExecutor(max_workers=5) as pool:
        for res in pool.map(work, coins):
            if res:
                hits.append(res)
                state[res[1]] = time.time()
    print(f"Done in {time.time() - t0:.0f}s, {len(hits)} hits")

    save_state(state)
    if not hits:
        print("No spikes this run.")
        if os.environ.get("TEST_EMAIL") == "1":
            send_email("Bitget Scanner: test", "السكانر شغال ✅ — مفيش إشارات دلوقتي.")
        return

    hits.sort(key=lambda h: -h[0])
    lines = []
    for score, sym, vol24, sp, verdict, good, bad in hits:
        lines.append(
            f"{verdict}  {sym}\n"
            f"الفوليوم: ${sp['normal']:,.0f} ← ${sp['spike']:,.0f}  (x{sp['ratio']:.1f})\n"
            f"حركة السعر: {sp['move']:.1f}%  |  السعر: {sp['price']:g}  |  فوليوم 24س: ${vol24:,.0f}\n"
            + (f"إيجابي: {'، '.join(good)}\n" if good else "")
            + (f"سلبي: {'، '.join(bad)}\n" if bad else "")
            + f"https://www.bitget.com/spot/{sym}\n"
        )
    body = "\n".join(lines) + "\nتنبيه: مش نصيحة استثمارية — راجع الشارت بنفسك قبل الدخول."
    real = sum(1 for h in hits if h[4].startswith("✅"))
    send_email(f"Bitget: {len(hits)} قفزة فوليوم ({real} محتمل حقيقي)", body, urgent=real > 0)


if __name__ == "__main__":
    main()
