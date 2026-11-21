#!/usr/bin/env python3
"""
Bitget Stealth Volume Scanner
-----------------------------
بيدور على عملات حجم تداولها قفز فجأة بالنسبة لمتوسطها الطبيعي
لكن السعر لسه ما اتحركش (تجميع محتمل).

التشغيل:
    pip install requests
    python bitget_scanner.py                 # مسح مرة واحدة
    python bitget_scanner.py --loop 300      # مسح كل 5 دقايق
    python bitget_scanner.py --ratio 5 --max-move 1.5 --min-usd 1500

تنبيهات تيليجرام (اختياري):
    export TG_TOKEN="..."  TG_CHAT="..."
"""
import argparse
import json
import os
import smtplib
import ssl
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

from email.message import EmailMessage

import requests

BASE = "https://api.bitget.com"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "stealth-scanner/1.0"})


def api_get(path, params=None, retries=3):
    for i in range(retries):
        try:
            r = SESSION.get(BASE + path, params=params, timeout=10)
            if r.status_code == 429:
                time.sleep(1 + i)
                continue
            r.raise_for_status()
            j = r.json()
            if j.get("code") != "00000":
                raise RuntimeError(j.get("msg"))
            return j["data"]
        except Exception:
            if i == retries - 1:
                return None
            time.sleep(0.5 * (i + 1))
    return None


def get_candidates(a, data=None):
    """فلترة أولية من التيكرز: عملات USDT صغيرة الحجم وسعرها هادي في 24 ساعة."""
    if data is None:
        data = api_get("/api/v2/spot/market/tickers") or []
    out = []
    for t in data:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT") or sym in a.excl:
            continue
        try:
            vol24 = float(t.get("usdtVolume") or t.get("quoteVolume") or 0)
            chg24 = float(t.get("change24h") or 0) * 100
        except ValueError:
            continue
        if a.min_vol24 <= vol24 <= a.max_vol24 and abs(chg24) <= a.max_24h:
            out.append((sym, vol24, chg24))
    return out


def tf_minutes(tf):
    t = tf.lower()
    if t.endswith("min"):
        return int(t[:-3])
    if t.endswith("h"):
        return int(t[:-1]) * 60
    return 15


def analyze(item, a):
    """بيقيس قفزة الفوليوم على أكتر من مدة (من شمعة واحدة لحد ساعة)،
    ويرجّع المدة اللي فيها القفزة أقوى."""
    sym, vol24, chg24 = item
    raw = api_get(
        "/api/v2/spot/market/candles",
        {"symbol": sym, "granularity": a.tf, "limit": "200"},
    )
    kmax = max(a.wins)
    if not raw or len(raw) < kmax + 40:
        return None

    # [ts, open, high, low, close, baseVol, usdtVol, quoteVol]
    rows = sorted(raw, key=lambda c: int(c[0]))
    base_vols = [float(r[6]) for r in rows[:-kmax]]
    baseline = max(
        statistics.median(base_vols),
        statistics.mean(base_vols) * 0.5,
        a.floor,
    )
    best = None
    for k in a.wins:
        recent = rows[-k:]
        total = sum(float(r[6]) for r in recent)
        ratio = (total / k) / baseline
        o = float(recent[0][1])
        c = float(recent[-1][4])
        hi = max(float(r[2]) for r in recent)
        lo = min(float(r[3]) for r in recent)
        if o <= 0 or lo <= 0:
            continue
        move = (c / o - 1) * 100
        rng = (hi / lo - 1) * 100
        # عدد الشموع النشطة (حجم أكبر من ضعف الطبيعي)
        active = sum(1 for r in recent if float(r[6]) > 2 * baseline)
        # فلتر الاتجاه: مش عايزين فوليوم مع سعر نازل أو راجع لتحت إغلاق الشمعة اللي قبلها
        trend_ok = a.no_trend or (move >= a.min_move and c >= float(rows[-2][4]) * (1 - a.trend_tol / 100))
        hid = abs(move) <= a.h_move and rng <= a.h_range and abs(chg24) <= a.h_24
        # لو مش "مخفي" (يعني السعر مش هادي) لازم السعر يكون فعلاً طلع vis_min_move%% على الأقل
        vis_ok = hid or move >= a.vis_min_move
        if (
            trend_ok
            and vis_ok
            and ratio >= a.ratio
            and total >= a.min_usd
            and abs(move) <= a.max_move
            and rng <= a.max_range
            and active >= a.min_active
            and (best is None or ratio > best["ratio"])
        ):
            mins = k * a.tfm
            best = {
                "symbol": sym,
                "ratio": ratio,
                "recent_usd": total,
                "baseline_per_candle": baseline,
                "avg_recent": total / k,
                "price": c,
                "move": move,
                "range": rng,
                "active": active,
                "k": k,
                "mins": mins,
                "vol_rate": total / mins * 60,  # معدل الفوليوم في الساعة
                "vol24": vol24,
                "chg24": chg24,
                "hidden": hid,
            }
    return best


def enrich(r, a):
    """مرحلة التأكيد: اتجاه الشراء العدواني + عمق الأوردر بوك + فحص البوتات."""
    sym = r["symbol"]
    fills = api_get("/api/v2/spot/market/fills", {"symbol": sym, "limit": str(a.fills)}) or []
    buy = sell = 0.0
    sizes = []
    for f in fills:
        try:
            usd = float(f["price"]) * float(f["size"])
        except (KeyError, ValueError):
            continue
        sizes.append(usd)
        if str(f.get("side", "")).lower() == "buy":
            buy += usd
        else:
            sell += usd
    tot = buy + sell
    r["buy_pct"] = buy / tot * 100 if tot else 0.0
    r["buy_usd"], r["sell_usd"] = buy, sell
    r["trades"] = len(sizes)
    r["bid_usd"] = r["ask_usd"] = 0.0
    r["botlike"] = False
    if len(sizes) >= 30:
        m = statistics.mean(sizes)
        r["botlike"] = m > 0 and statistics.pstdev(sizes) / m < 0.15

    ob = api_get("/api/v2/spot/market/orderbook", {"symbol": sym, "type": "step0", "limit": "50"}) or {}
    try:
        bids = [(float(x[0]), float(x[1])) for x in ob.get("bids", [])]
        asks = [(float(x[0]), float(x[1])) for x in ob.get("asks", [])]
    except (ValueError, IndexError):
        bids, asks = [], []
    r["book_pct"], r["spread"] = 0.0, 99.0
    if bids and asks:
        mid = (bids[0][0] + asks[0][0]) / 2
        lo, hi = mid * (1 - a.depth / 100), mid * (1 + a.depth / 100)
        b = sum(p * q for p, q in bids if p >= lo)
        k = sum(p * q for p, q in asks if p <= hi)
        r["book_pct"] = b / (b + k) * 100 if (b + k) else 0.0
        r["bid_usd"], r["ask_usd"] = b, k
        r["spread"] = (asks[0][0] / bids[0][0] - 1) * 100

    r["strong"] = (
        r["buy_pct"] >= a.min_buy
        and r["buy_usd"] >= a.min_buy_usd
        and r["book_pct"] >= a.min_book
        and r["spread"] <= a.max_spread
        and not r["botlike"]
    )
    return r


def scan(a, tickers=None):
    cands = get_candidates(a, tickers)
    print(f"[{time.strftime('%H:%M:%S')}] عملات بعد الفلترة الأولية: {len(cands)}")
    results = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for res in ex.map(lambda it: analyze(it, a), cands):
            if res:
                results.append(res)
    if results:
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            results = list(ex.map(lambda r: enrich(r, a), results))
    if not a.weak:
        results = [r for r in results if r["strong"]]
    results.sort(key=lambda r: (r["strong"], r["ratio"]), reverse=True)
    return results


def print_table(results, a):
    if not results:
        print("مفيش حاجة مطابقة للشروط دلوقتي.\n")
        return
    print(
        f"{'SYMBOL':<14}{'VOLx':>7}{'RECENT$':>10}{'MOVE%':>8}"
        f"{'BUY%':>7}{'BOOK%':>7}{'SPRD%':>7}  FLAG"
    )
    for r in results[: a.top]:
        flag = "STRONG" if r["strong"] else ("BOT?" if r["botlike"] else "weak")
        print(
            f"{r['symbol']:<14}{r['ratio']:>7.1f}{r['recent_usd']:>10,.0f}"
            f"{r['move']:>8.2f}{r['buy_pct']:>7.0f}{r['book_pct']:>7.0f}"
            f"{r['spread']:>7.2f}  {flag}"
        )
    print()


def fmt_price(p):
    if p >= 1:
        s = f"{p:.4f}"
    elif p >= 0.01:
        s = f"{p:.5f}"
    else:
        s = f"{p:.8f}"
    return s.rstrip("0").rstrip(".")


def format_alert(r, a):
    """شكل التنبيه: الحكم + الأرقام + الأسباب + لينك Bitget."""
    if r["strong"]:
        tag = "✅ محتمل حقيقي"
    elif r["botlike"]:
        tag = "🤖 شبهة بوت"
    else:
        tag = "⚠️ ضعيف"
    reasons = f"إيجابي: الشراء غالب {r['buy_pct']:.0f}%"
    if not r["botlike"]:
        reasons += f"، أحجام الصفقات متنوعة ({r['trades']} صفقة)"
    reasons += f"، طلبات شراء تحت السعر (${r['bid_usd']:,.0f})"
    reasons += f"، الفوليوم عالي في {r['active']} من آخر {r['k']} شموع"
    kind = "🕶 مخفي" if r["hidden"] else f"📢 ظاهر (السعر اتحرك {r['move']:+.1f}%)"
    if r.get("repeat"):
        kind += " 🔁 فوليوم مكمل"
    return "\n".join(
        [
            f"{tag}  {r['symbol']}  |  {kind}",
            f"الفوليوم في آخر {r['mins']} دقيقة: ${r['recent_usd']:,.0f} (الطبيعي لنفس المدة ${r['baseline_per_candle'] * r['k']:,.0f}) x{r['ratio']:.1f}",
            f"حركة السعر: {r['move']:+.1f}%  |  السعر: {fmt_price(r['price'])}",
            f"آخر {r['trades']} صفقة: شراء ${r['buy_usd']:,.0f} مقابل بيع ${r['sell_usd']:,.0f}",
            reasons,
            f"https://www.bitget.com/spot/{r['symbol']}",
        ]
    )


def candle_pump(sym, a, now):
    """صعود حقيقي بس (مش وهمي): واحد من شرطين على شموع مقفولة:
    1) شمعة وحدة أقفلت زيادة big_candle% أو أكتر.
    2) 3 شموع خضرا متتالية كل واحدة بتقفل أعلى من اللي قبلها، والعملة زادت pump_pct% عن أقل سعر في الفترة.
    ولازم فوليوم الشموع كفاية، وفتيلة الشمعة العلوية مش كبيرة (يعني مش ارتداد من قمة)."""
    m = a.ptfm
    n = a.pump_window // m + 1
    raw = api_get("/api/v2/spot/market/candles", {"symbol": sym, "granularity": a.pump_tf, "limit": str(max(n, 4) + 3)})
    if not raw or len(raw) < 5:
        return None
    rows = sorted(raw, key=lambda c: int(c[0]))
    if int(rows[-1][0]) / 1000 + m * 60 > now:  # آخر شمعة لسه بتتكون، نشيلها
        rows = rows[:-1]
    if len(rows) < 4:
        return None
    last = rows[-1]
    o, h, l, c, v = float(last[1]), float(last[2]), float(last[3]), float(last[4]), float(last[6])
    if o <= 0:
        return None
    rng = h - l
    wick_ok = rng <= 0 or (h - c) / rng <= a.max_wick  # الإغلاق قريب من القمة
    win = rows[-n:]
    low, low_ts = min((min(float(r[1]), float(r[4])), int(r[0])) for r in win)
    if low <= 0:
        return None
    rise = (c / low - 1) * 100
    gain = (c / o - 1) * 100
    big = gain >= a.big_candle and v >= a.pump_min_candle_usd and wick_ok
    r3 = rows[-3:]
    closes = [float(x[4]) for x in r3]
    three = (
        all(float(x[4]) > float(x[1]) for x in r3)
        and closes[0] < closes[1] < closes[2]
        and rise >= a.pump_pct
        and sum(float(x[6]) for x in r3) >= a.pump_min_candle_usd
        and wick_ok
    )
    # ارتداد: العملة نزلت نزول حقيقي (rebound_drop%) لقاع حديث، وبعدها طلعت من القاع rebound_pct% أو أكتر
    rebound, rb_txt, weak_rb = False, "", False
    if not a.no_rebound:
        lo_v, lo_i = min((float(x[3]), i) for i, x in enumerate(rows))
        if lo_v > 0 and lo_i >= 1 and len(rows) - 1 - lo_i <= a.rebound_candles:
            top = max(float(x[2]) for x in rows[:lo_i])
            drop = (top / lo_v - 1) * 100
            rb = (c / lo_v - 1) * 100
            vol_since = sum(float(x[6]) for x in rows[lo_i:])
            # استحواذ المشترين: الإغلاق لازم يغطي reclaim من جسم أكبر شمعة نزول (قبل القاع أو عنده)
            reclaim_ok = True
            fr = getattr(a, "reclaim", 0)
            if fr > 0:
                seg = rows[max(0, lo_i - 3): lo_i + 1]
                bd = max(seg, key=lambda x: float(x[1]) - float(x[4]))
                bo, bc = float(bd[1]), float(bd[4])
                if bo > bc:
                    reclaim_ok = c >= bc + fr * (bo - bc)
            if drop >= a.rebound_drop and not reclaim_ok:
                weak_rb = True  # ارتداد ضعيف بعد نزول حاد (المشترين مغطوش جسم شمعة النزول): نتجاهله بالكامل
            if drop >= a.rebound_drop and rb >= a.rebound_pct and c > o and vol_since >= a.pump_min_candle_usd and wick_ok and reclaim_ok:
                rebound = True
                rb_txt = f"ارتداد من القاع {fmt_price(lo_v)} (نزلت {drop:.1f}% قبلها وطلعت {rb:.1f}% من القاع)"
    if weak_rb and not rebound:
        return None
    if not (big or three or rebound):
        return None
    if rebound:
        why = rb_txt
    else:
        why = f"شمعة {m} دقايق أقفلت +{gain:.1f}%" if big else "3 شموع خضرا متتالية بإغلاق أعلى"
    return {
        "price": c,
        "rise": rise,
        "mins": (int(last[0]) - low_ts) / 60000 + m,
        "why": why,
        "closed_at": int(last[0]) / 1000 + m * 60,  # وقت إقفال الشمعة
        "rebound": rebound,
    }


def track_pumps(tickers, a, state, now):
    """تتبع الصعود: لما شمعة صعود حقيقية تتقفل (candle_pump) مش بنبعت فوراً،
    بنستنى confirm_sec ثانية ونتأكد إن السعر لسه ماسك (مش نزل أكتر من hold_pct%% عن إغلاق الشمعة).
    وتتكرر نفس العملة كل ما تكمل صعود pump_step%% فوق سعر آخر تنبيه."""
    if not tickers:
        return []
    hist = state.setdefault("hist", {})
    last = state.setdefault("pump", {})
    pend = state.setdefault("pend", {})
    window = a.pump_window * 60
    cands, live, prices = [], set(), {}
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT") or sym in a.excl:
            continue
        try:
            price = float(t.get("lastPr") or t.get("close") or 0)
            vol24 = float(t.get("usdtVolume") or t.get("quoteVolume") or 0)
            chg24 = float(t.get("change24h") or 0) * 100
        except ValueError:
            continue
        if price <= 0:
            continue
        prices[sym] = (price, vol24, chg24)
        if vol24 < a.pump_min_vol24:
            continue
        live.add(sym)
        h = [p for p in hist.get(sym, []) if now - p[0] <= window]
        if not h or now - h[-1][0] >= 55:  # نقطة سعر كل دقيقة تقريباً بس (يخفف حجم الملف)
            h.append([now, price])
        hist[sym] = h
        low = min(min(p[1] for p in h), price)
        hist_rise = (price / low - 1) * 100
        # فلتر أولي رخيص عشان منطلبش شموع لكل العملات
        if hist_rise >= a.pump_pct * 0.5 or sym in last:  # التنبيه النهائي محتاج صعود pump_pct%% على الأقل
            cands.append((sym, vol24, chg24, price))
    for s_ in list(hist):
        if s_ not in live:
            del hist[s_]
    if cands:
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            res = list(ex.map(lambda it: candle_pump(it[0], a, now), cands))
        for (sym, vol24, chg24, price), r in zip(cands, res):
            ref = last.get(sym)
            if ref and (now - ref["ts"] > 6 * 3600 or price <= ref["price"] * (1 - a.pump_pct / 100)):
                last.pop(sym, None)
                ref = None
            if (
                r
                and r["rise"] >= a.pump_pct  # الصعود لازم يكون pump_pct%% على الأقل (5%% افتراضياً)
                and (ref is None or r.get("rebound") or r["price"] >= ref["price"] * (1 + a.pump_step / 100))  # الارتداد بعد نزول حقيقي حدث جديد: مايتحجبش بقاعدة التكرار
                and (ref is None or now - ref["ts"] >= a.pump_gap)
                and now - r["closed_at"] <= a.pump_max_age  # الشمعة لسه طازة، مش قديمة
            ):
                p = pend.get(sym)
                if not p or abs(p["r"]["price"] - r["price"]) > 1e-12:
                    pend[sym] = {"t": now, "r": r, "chg24": chg24, "vol24": vol24}
    out = []
    for sym, p in list(pend.items()):
        cur = prices.get(sym)
        r = p["r"]
        if cur is None or now - p["t"] > 900:
            pend.pop(sym, None)
            continue
        price_now = cur[0]
        if price_now < r["price"] * (1 - a.hold_pct / 100):
            print(f"تجاهل {sym}: السعر رجع تحت إغلاق شمعة الصعود (انعكاس)")
            pend.pop(sym, None)
            continue
        if now - p["t"] < a.confirm_sec:
            continue
        ref = last.get(sym)
        out.append(
            {
                "symbol": sym,
                "price": r["price"],
                "now_price": price_now,
                "rise": r["rise"],
                "mins": r["mins"],
                "why": r["why"],
                "vol24": p["vol24"],
                "chg24": p["chg24"],
                "repeat": ref is not None,
                "from_alert": ref["price"] if ref else None,
            }
        )
        last[sym] = {"price": r["price"], "ts": now}
        pend.pop(sym, None)
    out.sort(key=lambda r: r["rise"], reverse=True)
    return out


def format_pump(r):
    if r["why"].startswith("ارتداد"):
        head = "🔄 ارتداد بعد نزول"
    elif r["repeat"]:
        head = "🚀 كمّلت صعود"
    else:
        head = "🚀 صعود جديد"
    lines = [
        f"{head}  {r['symbol']}",
        f"صعدت {r['rise']:.1f}% في آخر {r['mins']:.0f} دقيقة  |  إغلاق الشمعة: {fmt_price(r['price'])}",
        f"السعر دلوقتي: {fmt_price(r['now_price'])} ({(r['now_price'] / r['price'] - 1) * 100:+.1f}% عن الإغلاق)",
        f"✅ {r['why']}",
    ]
    if r["repeat"] and not r["why"].startswith("ارتداد"):
        gain = (r["price"] / r["from_alert"] - 1) * 100
        lines.append(f"زادت {gain:.1f}% عن سعر التنبيه السابق ({fmt_price(r['from_alert'])})")
    if "buy_pct" in r:
        flow = f"آخر {r['trades']} صفقة: الشراء {r['buy_pct']:.0f}%  |  سيولة الشراء في الأوردر بوك {r['book_pct']:.0f}%"
        if r["botlike"]:
            flow += "  |  🤖 شبهة بوت"
        lines.append(flow)
        if r["book_pct"] < 40:
            lines.append("⚠️ طلبات البيع أكتر من الشراء، احتمال تصريف")
    lines.append("⚠️ الصعود حصل بالفعل، اتأكد من الشارت قبل أي قرار")
    lines.append(f"https://www.bitget.com/spot/{r['symbol']}")
    return "\n".join(lines)


def debug_coin(sym, a, tickers, now):
    """تشخيص: بيطبع في اللوج ليه عملة معينة اتبعتت أو اتلغت، وعند أنهي شرط بالظبط."""
    t = next((x for x in tickers if x.get("symbol") == sym), None)
    if not t:
        print(f"[DEBUG {sym}] مش موجودة في التيكرز")
        return
    try:
        price = float(t.get("lastPr") or t.get("close") or 0)
        vol24 = float(t.get("usdtVolume") or t.get("quoteVolume") or 0)
        chg24 = float(t.get("change24h") or 0) * 100
    except ValueError:
        return
    print(f"[DEBUG {sym}] السعر {fmt_price(price)} | فوليوم 24س ${vol24:,.0f} | تغير 24س {chg24:+.1f}%")
    if sym in a.excl:
        print(f"[DEBUG {sym}] ❌ مستبعدة بـ --exclude")
        return
    if not a.no_vol:
        if not (a.min_vol24 <= vol24 <= a.max_vol24):
            print(f"[DEBUG {sym}] ❌ فوليوم 24س خارج الحدود ({a.min_vol24:,.0f} - {a.max_vol24:,.0f})")
        raw = api_get("/api/v2/spot/market/candles", {"symbol": sym, "granularity": a.tf, "limit": "200"})
        kmax = max(a.wins)
        if not raw or len(raw) < kmax + 40:
            print(f"[DEBUG {sym}] ❌ [فوليوم] شموع {a.tf} مش كفاية ({len(raw) if raw else 0} من {kmax + 40})")
        else:
            rows = sorted(raw, key=lambda c: int(c[0]))
            bv = [float(r[6]) for r in rows[:-kmax]]
            base = max(statistics.median(bv), statistics.mean(bv) * 0.5, a.floor)
            ok_any = None
            for k in a.wins:
                rec = rows[-k:]
                tot = sum(float(r[6]) for r in rec)
                ratio = (tot / k) / base
                o, c = float(rec[0][1]), float(rec[-1][4])
                mv = (c / o - 1) * 100 if o > 0 else 0
                fails = []
                if ratio < a.ratio:
                    fails.append(f"الفوليوم x{ratio:.1f} أقل من {a.ratio:g}")
                if tot < a.min_usd:
                    fails.append(f"الحجم ${tot:,.0f} أقل من ${a.min_usd:,.0f}")
                if not a.no_trend and mv < a.min_move:
                    fails.append(f"الحركة {mv:+.1f}% أقل من {a.min_move:g}")
                if not a.no_trend and c < float(rows[-2][4]) * (1 - a.trend_tol / 100):
                    fails.append("السعر تحت إغلاق الشمعة اللي قبلها")
                hid = abs(mv) <= a.h_move and (max(float(r[2]) for r in rec) / min(float(r[3]) for r in rec) - 1) * 100 <= a.h_range and abs(chg24) <= a.h_24
                if not hid and mv < a.vis_min_move:
                    fails.append(f"مش مخفي (السعر مش هادي) والحركة {mv:+.1f}% أقل من {a.vis_min_move:g}%")
                print(f"[DEBUG {sym}] [فوليوم] نافذة {k * a.tfm} دقيقة: x{ratio:.1f}، ${tot:,.0f}، حركة {mv:+.1f}% -> {'✅ عدّت' if not fails else '❌ ' + '، '.join(fails)}")
                if not fails and ok_any is None:
                    ok_any = k
            if ok_any is not None:
                r = enrich({"symbol": sym}, a)
                bad = []
                if r["buy_pct"] < a.min_buy:
                    bad.append(f"الشراء {r['buy_pct']:.0f}% أقل من {a.min_buy:g}%")
                if r["buy_usd"] < a.min_buy_usd:
                    bad.append(f"قيمة الشراء ${r['buy_usd']:,.0f} أقل من ${a.min_buy_usd:,.0f}")
                if r["book_pct"] < a.min_book:
                    bad.append(f"الأوردر بوك {r['book_pct']:.0f}% أقل من {a.min_book:g}%")
                if r["spread"] > a.max_spread:
                    bad.append(f"السبريد {r['spread']:.2f}% أكبر من {a.max_spread:g}%")
                if r["botlike"]:
                    bad.append("شبهة بوت")
                print(f"[DEBUG {sym}] [تأكيد] {r['trades']} صفقة: شراء {r['buy_pct']:.0f}% (${r['buy_usd']:,.0f})، أوردر بوك {r['book_pct']:.0f}%، سبريد {r['spread']:.2f}% -> {'✅ قوية' if not bad else '❌ ' + '، '.join(bad)}")
    if not a.no_pump:
        if vol24 < a.pump_min_vol24:
            print(f"[DEBUG {sym}] ❌ [صعود] فوليوم 24س أقل من ${a.pump_min_vol24:,.0f}")
            return
        m = a.ptfm
        n = a.pump_window // m + 1
        raw = api_get("/api/v2/spot/market/candles", {"symbol": sym, "granularity": a.pump_tf, "limit": str(max(n, 4) + 3)})
        if not raw or len(raw) < 5:
            print(f"[DEBUG {sym}] ❌ [صعود] شموع مش كفاية")
            return
        rows = sorted(raw, key=lambda c: int(c[0]))
        if int(rows[-1][0]) / 1000 + m * 60 > now:
            rows = rows[:-1]
        last = rows[-1]
        o, h, l, c, v = float(last[1]), float(last[2]), float(last[3]), float(last[4]), float(last[6])
        rng = h - l
        wick = (h - c) / rng if rng > 0 else 0
        gain = (c / o - 1) * 100 if o > 0 else 0
        win = rows[-n:]
        low = min(min(float(r[1]), float(r[4])) for r in win)
        rise = (c / low - 1) * 100 if low > 0 else 0
        age = now - (int(last[0]) / 1000 + m * 60)
        res = candle_pump(sym, a, now)
        fails = []
        if gain < a.big_candle:
            fails.append(f"آخر شمعة {gain:+.1f}% أقل من {a.big_candle:g}%")
        if v < a.pump_min_candle_usd:
            fails.append(f"حجمها ${v:,.0f} أقل من ${a.pump_min_candle_usd:,.0f}")
        if wick > a.max_wick:
            fails.append(f"الفتيلة العلوية {wick * 100:.0f}% أكبر من {a.max_wick * 100:.0f}%")
        print(f"[DEBUG {sym}] [صعود] آخر شمعة مقفولة: {gain:+.1f}%، حجم ${v:,.0f}، فتيلة {wick * 100:.0f}%، صعود من أدنى نقطة {rise:+.1f}%، عمرها {age:.0f}ث -> {'✅ شرط الشمعة اتحقق' if res else '❌ ' + ('، '.join(fails) or 'مفيش 3 شموع خضرا متتالية ولا ارتداد من قاع')}")
        if res and res["rise"] < a.pump_pct:
            print(f"[DEBUG {sym}] ❌ [صعود] الصعود {res['rise']:.1f}% أقل من {a.pump_pct:g}% (الحد الأدنى للتنبيه)")
        elif res:
            if age > a.pump_max_age:
                print(f"[DEBUG {sym}] ❌ [صعود] الشمعة قديمة (أكتر من {a.pump_max_age}ث)")
            elif price < res["price"] * (1 - a.hold_pct / 100):
                print(f"[DEBUG {sym}] ❌ [صعود] السعر الحالي {fmt_price(price)} نزل تحت إغلاق الشمعة {fmt_price(res['price'])} بأكتر من {a.hold_pct:g}% (انعكاس)")
            else:
                print(f"[DEBUG {sym}] ✅ [صعود] المفروض يتبعت بعد {a.confirm_sec} ثانية تأكيد")


def log_alerts(st, items, now):
    """بنسجل كل تنبيه (العملة، النوع، السعر) عشان نقيس بعدها بربع ساعة وساعة نتيجته فعلاً."""
    lg = st.setdefault("log", [])
    for sym, kind, price in items:
        if price and price > 0:
            lg.append({"s": sym, "k": kind, "p": price, "t": now})
    del lg[:-300]


def update_log(st, tickers, now):
    """بيسجل سعر العملة بعد 15 و60 دقيقة من التنبيه ويطبع النتيجة في اللوج."""
    lg = st.get("log", [])
    if not lg:
        return
    px = {}
    for t in tickers:
        try:
            px[t.get("symbol", "")] = float(t.get("lastPr") or t.get("close") or 0)
        except ValueError:
            continue
    for e in lg:
        cur = px.get(e["s"])
        if not cur:
            continue
        for m in (15, 60):
            key = f"r{m}"
            if key not in e and now - e["t"] >= m * 60:
                e[key] = (cur / e["p"] - 1) * 100
                print(f"نتيجة {e['s']} ({e['k']}): بعد {m} دقيقة {e[key]:+.1f}% عن سعر التنبيه")
    st["log"] = [e for e in lg if now - e["t"] < 48 * 3600]


KIND_NAMES = {
    "pump": "🚀 صعود",
    "rebound": "🔄 ارتداد",
    "vol-hidden": "🕶 فوليوم مخفي",
    "vol-visible": "📢 فوليوم ظاهر",
}


def summary_text(st, now):
    """ملخص نتايج التنبيهات اللي عدّى عليها ساعة (آخر 24 ساعة)، لكل نوع تنبيه لوحده."""
    done = [e for e in st.get("log", []) if "r60" in e and now - e["t"] < 24 * 3600]
    if not done:
        return None
    lines = ["📊 ملخص نتايج التنبيهات (آخر 24 ساعة، القياس بعد ساعة من التنبيه)"]
    for kind, name in KIND_NAMES.items():
        g = [e for e in done if e["k"] == kind]
        if not g:
            continue
        up = sum(1 for e in g if e["r60"] > 0)
        avg60 = sum(e["r60"] for e in g) / len(g)
        g15 = [e["r15"] for e in g if "r15" in e]
        avg15 = f" | متوسط بعد 15 دقيقة {sum(g15) / len(g15):+.1f}%" if g15 else ""
        lines.append(f"{name}: {len(g)} تنبيه | {up} منهم أعلى بعد ساعة | متوسط بعد ساعة {avg60:+.1f}%{avg15}")
    lines.append("القياس لسعر التنبيه بس، مش ربح فعلي (من غير رسوم ولا انزلاق). ومش نصيحة مالية.")
    return "\n".join(lines)


def telegram(msg):
    tok, chat = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT")
    if not tok or not chat:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            data={"chat_id": chat, "text": msg[:4000]},
            timeout=10,
        )
    except Exception:
        pass


def send_email(body, subject="Bitget scanner: عملات حجمها قفز"):
    user, pwd = os.getenv("EMAIL_USER"), os.getenv("EMAIL_PASS")
    if not user or not pwd:
        return
    to = os.getenv("EMAIL_TO") or user
    host = os.getenv("EMAIL_SMTP", "smtp.gmail.com")
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = user
        msg["To"] = to
        msg.set_content(body)
        with smtplib.SMTP_SSL(host, 465, context=ssl.create_default_context(), timeout=20) as srv:
            srv.login(user, pwd)
            srv.send_message(msg)
    except Exception as e:
        print("email failed:", e)


def send_ntfy(body, title="Bitget scanner", tags="mag"):
    topic = os.getenv("NTFY_TOPIC")
    if not topic:
        print("ntfy: NTFY_TOPIC مش متضبط")
        return
    data = body.encode("utf-8")[:3800].decode("utf-8", "ignore").encode("utf-8")
    for attempt in range(3):
        try:
            r = requests.post(
                f"https://ntfy.sh/{topic}",
                data=data,
                headers={"Title": title, "Priority": "high", "Tags": tags},
                timeout=15,
            )
            if r.status_code == 200:
                print(f"ntfy: اتبعت ({len(data)} بايت)")
                return
            print(f"ntfy: فشل HTTP {r.status_code} {r.text[:150]}")
            if r.status_code != 429:
                return
            time.sleep(6)
        except Exception as e:
            print("ntfy failed:", e)
            time.sleep(2)


def notify(body, subject="Bitget scanner: عملات حجمها قفز", title="Bitget scanner", tags="mag"):
    telegram(body)
    send_email(body, subject)
    send_ntfy(body, title, tags)


def load_state(path):
    if not path:
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(path, state):
    if not path:
        return
    try:
        with open(path, "w") as f:
            json.dump(state, f)
    except Exception:
        pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tf", default="5min", help="1min,5min,15min,30min,1h")
    p.add_argument("--windows", default="1,3,6,12", help="مدد القياس بعدد الشموع (مع 5min: 5 و15 و30 و60 دقيقة)")
    p.add_argument("--ratio", type=float, default=4.0, help="أقل نسبة قفزة حجم عن الطبيعي")
    p.add_argument("--min-usd", type=float, default=1000, help="أقل حجم $ في الفترة الحديثة")
    p.add_argument("--min-move", type=float, default=-0.3, help="أقل حركة سعر%% مسموحة في الفترة (فلتر الاتجاه، يمنع الفوليوم مع نزول)")
    p.add_argument("--trend-tol", type=float, default=0.3, help="السعر الحالي لازم ميبقاش تحت إغلاق الشمعة اللي قبل كده بأكتر من %%")
    p.add_argument("--vis-min-move", type=float, default=5.0, help="التنبيه الظاهر (السعر مش هادي): أقل حركة سعر%% في الفترة عشان يتبعت (التنبيه المخفي مش متأثر)")
    p.add_argument("--no-trend", action="store_true", help="إيقاف فلتر الاتجاه")
    p.add_argument("--max-move", type=float, default=2.0, help="أقصى حركة سعر%% في الفترة")
    p.add_argument("--max-range", type=float, default=4.0, help="أقصى مدى high-low%% في الفترة")
    p.add_argument("--min-active", type=int, default=1, help="أقل عدد شموع نشطة")
    p.add_argument("--floor", type=float, default=5.0, help="حد أدنى للحجم الطبيعي لكل شمعة $ (يمنع القسمة على صفر)")
    p.add_argument("--min-vol24", type=float, default=5_000)
    p.add_argument("--max-vol24", type=float, default=3_000_000)
    p.add_argument("--max-24h", type=float, default=15.0, help="استبعد العملات اللي اتحركت أكتر من كده في 24س")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--top", type=int, default=25)
    p.add_argument("--loop", type=int, default=0, help="ثواني بين كل مسح (0 = مرة واحدة)")
    p.add_argument("--duration", type=int, default=0, help="مع --loop: أقصى مدة تشغيل بالثواني قبل ما يقفل لوحده (0 = بلا حد)")
    p.add_argument("--cooldown", type=int, default=900, help="ثواني قبل تكرار تنبيه نفس العملة لو الفوليوم لسه عالي")
    p.add_argument("--vol-step", type=float, default=1.5, help="يكرر التنبيه فوراً لو الفوليوم الحديث زاد بالمعامل ده عن آخر تنبيه")
    p.add_argument("--vol-price-step", type=float, default=3.0, help="يكرر تنبيه الفوليوم لو السعر كمل صعود كده%% فوق سعر آخر تنبيه (0 = إيقاف)")
    p.add_argument("--hidden-only", action="store_true", help="الرجوع للوضع القديم: العملات الهادية سعرياً بس")
    p.add_argument("--min-buy", type=float, default=60, help="أقل نسبة شراء عدواني %%")
    p.add_argument("--fills", type=int, default=300, help="عدد آخر الصفقات اللي بنحسب منها نسبة الشراء (حد أقصى 500)")
    p.add_argument("--min-buy-usd", type=float, default=500, help="أقل قيمة $ لصفقات الشراء العدواني في العينة")
    p.add_argument("--pump-min-buy", type=float, default=50, help="تنبيه الصعود يتلغي لو نسبة الشراء أقل من كده%%")
    p.add_argument("--min-book", type=float, default=52, help="أقل نسبة سيولة bid في الأوردر بوك %%")
    p.add_argument("--max-spread", type=float, default=1.5, help="أقصى سبريد bid/ask %%")
    p.add_argument("--depth", type=float, default=2.0, help="عمق الأوردر بوك المحسوب حوالين السعر %%")
    p.add_argument("--weak", action="store_true", help="اعرض كمان العملات الضعيفة (للتجربة)")
    p.add_argument("--pump-pct", type=float, default=5.0, help="نسبة الصعود اللي تطلّع تنبيه %%")
    p.add_argument("--pump-step", type=float, default=3.0, help="كل كام %% صعود إضافي يتكرر التنبيه لنفس العملة")
    p.add_argument("--pump-window", type=int, default=60, help="مدة قياس الصعود بالدقايق")
    p.add_argument("--pump-tf", default="5min", help="فريم شموع تتبع الصعود: 1min أو 5min")
    p.add_argument("--big-candle", type=float, default=3.0, help="شمعة وحدة مقفولة زادت كده%% = صعود لحظي")
    p.add_argument("--pump-min-candle-usd", type=float, default=300, help="أقل فوليوم $ لشمعة الصعود (يمنع الصعود الوهمي على صفقة واحدة)")
    p.add_argument("--max-wick", type=float, default=0.4, help="أقصى نسبة للفتيلة العلوية من الشمعة (أكتر منها = ارتداد/فخ)")
    p.add_argument("--confirm-sec", type=int, default=30, help="استنى كام ثانية بعد إقفال شمعة الصعود وتأكد إن
