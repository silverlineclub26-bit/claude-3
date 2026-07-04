# -*- coding: utf-8 -*-
"""
多商品 趨勢／盤整 自動判斷系統（台指期 + 比特幣）

抓每日開高低收 → 計算均線 / 發散度 / K 棒實體比例 / 連續小實體根數
→ 判斷「趨勢盤 / 盤整盤 / 轉折觀察」→ 產出單一 index.html。

- 台指期：FinMind TaiwanFuturesDaily（免費）
- 比特幣：Kraken 公開 OHLC API（免費、美國伺服器也可用）

同一頁用頁籤切換不同商品；每個商品可用日期選擇器查歷史（逐日回推判定）。
由 GitHub Actions 每日自動執行，index.html 推回 repo 後由 GitHub Pages 發布。
"""

import os
import sys
import json
import datetime

import requests

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "").strip()
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
KRAKEN_URL = "https://api.kraken.com/0/public/OHLC"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120 Safari/537.36")

DELTA_EXPAND = 0.02      # 發散度趨勢：delta > 0.02 視為擴大中
DELTA_CONTRACT = -0.02   # 發散度趨勢：delta < -0.02 視為收斂中

# 要判斷的商品清單（頁籤順序即此順序）
ASSETS = [
    {"key": "MTX", "name": "台指期", "kind": "futures", "id": "MTX"},
    {"key": "BTC", "name": "比特幣", "kind": "crypto", "pair": "XBTUSD"},
    {"key": "IXIC", "name": "那斯達克", "kind": "index", "symbol": "^IXIC"},
    {"key": "SOX", "name": "費半", "kind": "index", "symbol": "^SOX"},
]


# ---------------------------------------------------------------------------
# 資料抓取
# ---------------------------------------------------------------------------

def fetch_futures_daily(futures_id, start_date, end_date):
    """
    呼叫 FinMind TaiwanFuturesDaily，回傳「一天一筆」日 K（由舊到新）。
    同一天取一般盤、成交量最大的合約（近月主力）。
    """
    params = {
        "dataset": "TaiwanFuturesDaily",
        "data_id": futures_id,
        "start_date": start_date,
        "end_date": end_date,
    }
    headers = {}
    if FINMIND_TOKEN:
        headers["Authorization"] = "Bearer " + FINMIND_TOKEN

    try:
        resp = requests.get(FINMIND_URL, params=params, headers=headers, timeout=30)
    except requests.RequestException as e:
        raise RuntimeError("呼叫 FinMind API 失敗：%s" % e) from e

    if resp.status_code != 200:
        raise RuntimeError("FinMind API 回傳 HTTP %s：%s" % (resp.status_code, resp.text[:300]))

    payload = resp.json()
    if payload.get("status") != 200 and payload.get("msg", "").lower() not in ("success", ""):
        raise RuntimeError("FinMind API 回傳非成功狀態：%s" % str(payload)[:300])

    rows = payload.get("data", []) or []
    if not rows:
        raise RuntimeError(
            "FinMind 沒有回傳任何資料（data_id=%s, %s ~ %s）。" % (futures_id, start_date, end_date)
        )

    if any("trading_session" in r for r in rows):
        day_rows = [r for r in rows if str(r.get("trading_session", "")).lower() == "position"]
        if day_rows:
            rows = day_rows

    def _vol(r):
        v = r.get("trading_volume", r.get("volume", 0))
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    best_by_date = {}
    for r in rows:
        d = r.get("date")
        if not d:
            continue
        if d not in best_by_date or _vol(r) > _vol(best_by_date[d]):
            best_by_date[d] = r

    bars = []
    for d in sorted(best_by_date):
        r = best_by_date[d]
        try:
            bar = {
                "date": d,
                "open": float(r["open"]),
                "max": float(r["max"]),
                "min": float(r["min"]),
                "close": float(r["close"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
        if bar["max"] <= 0 and bar["min"] <= 0 and bar["close"] <= 0:
            continue
        bars.append(bar)

    if not bars:
        raise RuntimeError("FinMind 資料整理後為空，無有效日 K。")
    return bars


def fetch_crypto_daily(pair="XBTUSD", interval=1440):
    """
    呼叫 Kraken 公開 OHLC API，回傳「一天一筆」日 K（由舊到新）。
    interval=1440 分鐘 = 日線。回傳欄位 [time, open, high, low, close, vwap, volume, count]。
    """
    params = {"pair": pair, "interval": interval}
    try:
        resp = requests.get(KRAKEN_URL, params=params, timeout=30)
    except requests.RequestException as e:
        raise RuntimeError("呼叫 Kraken API 失敗：%s" % e) from e

    if resp.status_code != 200:
        raise RuntimeError("Kraken API 回傳 HTTP %s：%s" % (resp.status_code, resp.text[:300]))

    payload = resp.json()
    if payload.get("error"):
        raise RuntimeError("Kraken API 回傳錯誤：%s" % payload["error"])

    result = payload.get("result", {})
    keys = [k for k in result if k != "last"]
    if not keys:
        raise RuntimeError("Kraken 回傳沒有 OHLC 資料。")
    rows = result[keys[0]]

    bars = []
    for row in rows:
        try:
            ts = int(row[0])
            d = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d")
            bar = {
                "date": d,
                "open": float(row[1]),
                "max": float(row[2]),
                "min": float(row[3]),
                "close": float(row[4]),
            }
        except (IndexError, TypeError, ValueError):
            continue
        bars.append(bar)

    if not bars:
        raise RuntimeError("Kraken 資料整理後為空，無有效日 K。")
    return bars


def fetch_index_daily(symbol, rng="2y"):
    """
    呼叫 Yahoo Finance 圖表 API，回傳指數「一天一筆」日 K（由舊到新）。
    symbol 例：^IXIC（那斯達克綜合）、^SOX（費城半導體）。需帶瀏覽器 User-Agent。
    """
    headers = {"User-Agent": _UA}
    try:
        resp = requests.get(YAHOO_URL + symbol, params={"range": rng, "interval": "1d"},
                            headers=headers, timeout=30)
    except requests.RequestException as e:
        raise RuntimeError("呼叫 Yahoo Finance 失敗：%s" % e) from e

    if resp.status_code != 200:
        raise RuntimeError("Yahoo Finance 回傳 HTTP %s：%s" % (resp.status_code, resp.text[:200]))

    payload = resp.json()
    chart = payload.get("chart", {})
    if chart.get("error"):
        raise RuntimeError("Yahoo Finance 回傳錯誤：%s" % chart["error"])

    results = chart.get("result") or []
    if not results:
        raise RuntimeError("Yahoo Finance 沒有回傳資料（symbol=%s）。" % symbol)
    result = results[0]

    ts = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    opens, highs, lows, closes = (quote.get("open"), quote.get("high"),
                                  quote.get("low"), quote.get("close"))
    if not ts or not all([opens, highs, lows, closes]):
        raise RuntimeError("Yahoo Finance 資料結構不完整（symbol=%s）。" % symbol)

    bars = []
    for i in range(len(ts)):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        if None in (o, h, l, c):
            continue
        d = datetime.datetime.fromtimestamp(int(ts[i]), datetime.timezone.utc).strftime("%Y-%m-%d")
        bars.append({"date": d, "open": float(o), "max": float(h), "min": float(l), "close": float(c)})

    if not bars:
        raise RuntimeError("Yahoo Finance 資料整理後為空，無有效日 K。")
    return bars


# ---------------------------------------------------------------------------
# 指標計算
# ---------------------------------------------------------------------------

def sma(values, period):
    """簡單移動平均；前面不足週期的位置填 None。"""
    out = [None] * len(values)
    if period <= 0:
        return out
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= period:
            running -= values[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


def analyze(bars, periods, body_thresh, streak_thresh, lookback):
    """以 bars 的最後一根為「當日」執行趨勢／盤整判斷，回傳結果 dict。"""
    periods = sorted(periods)
    max_period = max(periods)

    required = max_period + lookback + 2
    if len(bars) < required:
        raise RuntimeError(
            "資料不足：需要至少 %d 根日 K，目前只有 %d 根。" % (required, len(bars))
        )

    closes = [b["close"] for b in bars]
    ma_series = {p: sma(closes, p) for p in periods}

    spreads = [None] * len(bars)
    for i in range(len(bars)):
        vals = [ma_series[p][i] for p in periods if ma_series[p][i] is not None]
        if len(vals) == len(periods):
            avg = sum(vals) / len(vals)
            if avg != 0:
                spreads[i] = (max(vals) - min(vals)) / avg * 100.0

    latest_spread = spreads[-1]
    prev_spread = spreads[-1 - lookback]
    if latest_spread is None or prev_spread is None:
        raise RuntimeError("發散度資料不足，無法比較趨勢。")
    delta = latest_spread - prev_spread

    if delta > DELTA_EXPAND:
        spread_trend = "expanding"
    elif delta < DELTA_CONTRACT:
        spread_trend = "contracting"
    else:
        spread_trend = "flat"

    body_ratios = []
    for b in bars:
        rng = b["max"] - b["min"]
        ratio = 0.0 if rng <= 0 else abs(b["close"] - b["open"]) / rng
        body_ratios.append(ratio)

    streak = 0
    for ratio in reversed(body_ratios):
        if ratio < body_thresh:
            streak += 1
        else:
            break

    fast_p = periods[0]
    fast_ma = ma_series[fast_p][-1]

    # 徽章1「均線收斂」：5 日與 10 日（兩條最短）均線的距離，近 3 根是收斂還是發散
    p_a, p_b = periods[0], (periods[1] if len(periods) > 1 else periods[0])
    sa, sb = ma_series[p_a], ma_series[p_b]

    # 波動：近5根平均振幅 vs 近20根平均振幅（自適應各商品），放大代表大K交替洗盤
    ranges = [b["max"] - b["min"] for b in bars]
    vol_recent = sum(ranges[-5:]) / min(5, len(ranges))
    vol_base = sum(ranges[-20:]) / min(20, len(ranges))
    choppy = vol_base > 0 and vol_recent > 1.2 * vol_base

    conv, conv_label = "flat", "—"
    if (len(sa) > 3 and sa[-1] is not None and sb[-1] is not None
            and sa[-4] is not None and sb[-4] is not None):
        dist_now = abs(sa[-1] - sb[-1])
        dist_ref = abs(sa[-4] - sb[-4])
        if dist_now < dist_ref:
            # 收斂細分：高波動洗盤 → 震盪；低波動 → 盤整
            if choppy:
                conv, conv_label = "chop", "震盪"
            else:
                conv, conv_label = "range", "盤整"
        else:
            conv, conv_label = "diverge", "發散"

    # 徽章2「短線方向」：跌破5日且為實體黑K → 偏空；站上5日 → 偏多；其餘 → 中性
    last_open = bars[-1]["open"]
    last_close = closes[-1]
    is_black_solid = (last_close < last_open) and (body_ratios[-1] >= 0.5)
    if fast_ma is None:
        momentum, momentum_label = "flat", "中性"
    elif last_close > fast_ma:
        momentum, momentum_label = "up", "偏多"
    elif last_close < fast_ma and is_black_solid:
        momentum, momentum_label = "down", "偏空"
    else:
        momentum, momentum_label = "flat", "中性"

    # 趨勢盤的方向：MA5 相對 MA60 的排列（之上多方、之下空方）
    slow_ma = ma_series[periods[-1]][-1]
    if fast_ma is not None and slow_ma is not None and fast_ma != slow_ma:
        trend_dir = "long" if fast_ma > slow_ma else "short"
    else:
        trend_dir = "flat"

    if spread_trend == "expanding" and streak < 2:
        verdict = "trend"
        if trend_dir == "long":
            verdict_label = "多方趨勢"
            verdict_desc = ("均線間距擴大且呈多頭排列（MA%d 在 MA%d 之上），"
                            "無連續小實體 K 棒，順勢偏多操作。" % (fast_p, periods[-1]))
        elif trend_dir == "short":
            verdict_label = "空方趨勢"
            verdict_desc = ("均線間距擴大且呈空頭排列（MA%d 在 MA%d 之下），"
                            "無連續小實體 K 棒，順勢偏空操作。" % (fast_p, periods[-1]))
        else:
            verdict_label = "趨勢盤"
            verdict_desc = "均線間距正在擴大，且沒有連續小實體 K 棒，適合波段順勢邏輯操作。"
    elif spread_trend == "contracting" and streak >= streak_thresh:
        verdict = "range"
        verdict_label = "盤整盤"
        verdict_desc = (
            "均線間距正在收斂，且已連續 %d 根實體比例低於警戒值，"
            "建議縮小部位或改用高賣低買區間邏輯。" % streak
        )
    else:
        verdict = "watch"
        verdict_label = "轉折觀察"
        verdict_desc = "訊號不一致，建議先縮小部位試單。"

    # 強訊號箭頭：三個條件（方向趨勢 / 均線發散 / 短線同向）累計，滿足 2 項給 1 箭頭、3 項給 2 箭頭
    up_conds = ((1 if (verdict == "trend" and trend_dir == "long") else 0)
                + (1 if conv == "diverge" else 0)
                + (1 if momentum == "up" else 0))
    down_conds = ((1 if (verdict == "trend" and trend_dir == "short") else 0)
                  + (1 if conv == "diverge" else 0)
                  + (1 if momentum == "down" else 0))
    if up_conds > down_conds and up_conds >= 2:
        signal_dir, n = "up", up_conds
    elif down_conds > up_conds and down_conds >= 2:
        signal_dir, n = "down", down_conds
    else:
        signal_dir, n = "none", 0
    signal_n = 2 if n >= 3 else (1 if n == 2 else 0)

    # 操作建議：趨勢還在→續抱（順勢抱單）；趨勢不在→短做（縮部位、快進快出）
    if verdict == "trend" and trend_dir in ("long", "short"):
        side = "多單" if trend_dir == "long" else "空單"
        action = "hold_long" if trend_dir == "long" else "hold_short"
        if conv == "converge":
            action_label = "趨勢還在但轉弱 · 續抱%s、可分批減碼" % side
        else:
            action_label = "趨勢還在 · 續抱%s" % side
    else:
        action = "scalp"
        action_label = "趨勢不在 · 短做（快進快出、縮小部位）"

    recent_n = min(16, len(bars))
    recent_bodies = [
        {"date": bars[i]["date"], "ratio": round(body_ratios[i], 3)}
        for i in range(len(bars) - recent_n, len(bars))
    ]

    ma_now = {p: ma_series[p][-1] for p in periods}
    last = bars[-1]
    return {
        "verdict": verdict,
        "verdict_label": verdict_label,
        "verdict_desc": verdict_desc,
        "conv": conv,
        "conv_label": conv_label,
        "momentum": momentum,
        "momentum_label": momentum_label,
        "trend_dir": trend_dir,
        "signal_dir": signal_dir,
        "signal_n": signal_n,
        "action": action,
        "action_label": action_label,
        "last_date": last["date"],
        "last_close": round(last["close"], 2),
        "spread_now": round(latest_spread, 3),
        "spread_prev": round(prev_spread, 3),
        "spread_delta": round(delta, 3),
        "spread_trend": spread_trend,
        "lookback": lookback,
        "streak": streak,
        "streak_thresh": streak_thresh,
        "body_thresh": body_thresh,
        "ma_now": {str(p): (round(ma_now[p], 2) if ma_now[p] is not None else None) for p in periods},
        "recent_bodies": recent_bodies,
    }


# 持續性趨勢狀態表：state → (標題, 說明, 操作建議, 主色, 橫幅樣式)
_STATE_META = {
    "up_hold":    ("多方趨勢延續", "收盤仍站在5日線上，多方趨勢延續，順勢續抱多單。",
                   "趨勢還在 · 續抱多單", "#E5484D", "act-hold-long"),
    "down_hold":  ("空方趨勢延續", "收盤仍在5日線下，空方趨勢延續，順勢續抱空單。",
                   "趨勢還在 · 續抱空單", "#3DAE73", "act-hold-short"),
    "up_range":   ("盤整 · 跌破5日", "跌破5日線但守住10日線，趨勢暫歇進入盤整。",
                   "趨勢暫歇 · 改短做、縮小部位", "#D98A3D", "act-scalp"),
    "down_range": ("盤整 · 站上5日", "站上5日線但未過10日線，趨勢暫歇進入盤整。",
                   "趨勢暫歇 · 改短做、縮小部位", "#D98A3D", "act-scalp"),
    "turn_down":  ("短期轉空 · 跌破10日", "跌破10日線，多方趨勢告一段落、短期轉空。",
                   "多單出場 · 偏空短做", "#3DAE73", "act-hold-short"),
    "turn_up":    ("短期轉多 · 站上10日", "站上10日線，空方趨勢告一段落、短期轉多。",
                   "空單回補 · 偏多短做", "#E5484D", "act-hold-long"),
    "none":       ("無明確趨勢", "尚未形成明確趨勢，區間短做或觀望。",
                   "觀望／短做", "#8B919B", "act-scalp"),
}


def build_history(bars, periods, body_thresh, streak_thresh, lookback, max_days=180):
    """逐日回推：對每個可計算交易日，用當日（含）之前資料算判定，並跑持續性趨勢狀態機。"""
    periods = sorted(periods)
    required = max(periods) + lookback + 2
    records = []
    for i in range(required - 1, len(bars)):
        try:
            records.append(analyze(bars[:i + 1], periods, body_thresh, streak_thresh, lookback))
        except RuntimeError:
            continue

    # 持續性趨勢狀態機：趨勢形成後，只有跌破5日(轉盤整)、跌破10日(轉空/轉多)才改變狀態
    p5 = str(periods[0])
    p10 = str(periods[1]) if len(periods) > 1 else str(periods[0])
    side = "none"
    strong_up = dipped_up = strong_dn = dipped_dn = False  # 加碼追蹤：本波是否確認過兩箭頭、之後是否回檔
    for rec in records:
        close = rec["last_close"]
        ma5 = rec["ma_now"].get(p5)
        ma10 = rec["ma_now"].get(p10)
        if side == "none":
            if rec["verdict"] == "trend" and rec["trend_dir"] == "long":
                side = "long"
            elif rec["verdict"] == "trend" and rec["trend_dir"] == "short":
                side = "short"

        if side == "long" and ma5 is not None and ma10 is not None:
            if close < ma10:
                state, side = "turn_down", "none"
            elif close < ma5:
                state = "up_range"
            else:
                state = "up_hold"
        elif side == "short" and ma5 is not None and ma10 is not None:
            if close > ma10:
                state, side = "turn_up", "none"
            elif close > ma5:
                state = "down_range"
            else:
                state = "down_hold"
        else:
            state = "none"

        headline, desc, act_label, accent, act_class = _STATE_META[state]
        rec["state"] = state
        rec["state_label"] = headline
        rec["state_desc"] = desc
        rec["action_label"] = act_label
        rec["accent"] = accent
        rec["action_class"] = act_class

        # 箭頭：續抱狀態 + 均線發散 + 短線同向，累計 2 項給 1 箭頭、3 項給 2 箭頭
        up_c = ((1 if state == "up_hold" else 0)
                + (1 if rec["conv"] == "diverge" else 0)
                + (1 if rec["momentum"] == "up" else 0))
        dn_c = ((1 if state == "down_hold" else 0)
                + (1 if rec["conv"] == "diverge" else 0)
                + (1 if rec["momentum"] == "down" else 0))
        if up_c > dn_c and up_c >= 2:
            rec["signal_dir"], nn = "up", up_c
        elif dn_c > up_c and dn_c >= 2:
            rec["signal_dir"], nn = "down", dn_c
        else:
            rec["signal_dir"], nn = "none", 0
        rec["signal_n"] = 2 if nn >= 3 else (1 if nn == 2 else 0)

        # 趨勢加碼點：兩箭頭確認後短線回檔(轉空/持平)，趨勢仍在時短線再度轉多/空 → 加碼
        add_signal = "none"
        if side == "long":
            if rec["signal_dir"] == "up" and rec["signal_n"] >= 2:
                strong_up = True
            if strong_up and rec["momentum"] != "up":
                dipped_up = True
            if strong_up and dipped_up and state == "up_hold" and rec["momentum"] == "up":
                add_signal, dipped_up = "add_long", False
        elif side == "short":
            if rec["signal_dir"] == "down" and rec["signal_n"] >= 2:
                strong_dn = True
            if strong_dn and rec["momentum"] != "down":
                dipped_dn = True
            if strong_dn and dipped_dn and state == "down_hold" and rec["momentum"] == "down":
                add_signal, dipped_dn = "add_short", False
        else:  # 離開趨勢 → 本波作廢
            strong_up = dipped_up = strong_dn = dipped_dn = False
        rec["add_signal"] = add_signal
        rec["add_label"] = ("趨勢加碼點 · 順勢加碼多單" if add_signal == "add_long"
                            else "趨勢加碼點 · 順勢加碼空單" if add_signal == "add_short" else "")

    if max_days and len(records) > max_days:
        records = records[-max_days:]
    return records


# ---------------------------------------------------------------------------
# 產生 HTML 報告（多商品頁籤 + JS 日期選擇器）
# ---------------------------------------------------------------------------

def generate_html_report(assets, periods):
    """assets: list[{key, name, history:[...]}]。產生含頁籤與日期選擇器的單一頁面。"""
    gen_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    data_json = json.dumps(assets, ensure_ascii=False)
    periods_json = json.dumps(sorted(periods))

    html = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>趨勢／盤整 自動判斷</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=Noto+Sans+TC:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:#0B0D10; --panel:#14171C; --line:#262B33;
    --text:#EDEAE3; --muted:#8B919B; --accent:#8B919B;
    --up:#3DAE73; --down:#E5484D;
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--text);
    font-family:"Noto Sans TC",-apple-system,sans-serif;
    -webkit-font-smoothing:antialiased; line-height:1.5; padding:24px 16px 40px; }
  .wrap { max-width:520px; margin:0 auto; }
  .mono { font-family:"IBM Plex Mono",monospace; }

  .eyebrow { font-family:"IBM Plex Mono",monospace; font-size:12px; letter-spacing:.18em;
    color:var(--muted); text-transform:uppercase; }
  h1 { font-size:22px; font-weight:700; margin:6px 0 16px; letter-spacing:.02em; }

  .tabs { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:16px; }
  .tab { flex:1 1 0; min-width:64px; text-align:center; padding:11px 6px; border:1px solid var(--line);
    border-radius:9px; background:var(--panel); color:var(--muted); font-size:14px;
    font-weight:500; cursor:pointer; user-select:none; white-space:nowrap;
    transition:color .12s,border-color .12s; }
  .tab.active { color:var(--text); border-color:var(--accent);
    box-shadow:inset 0 0 0 1px var(--accent); }

  .picker { display:flex; align-items:center; gap:10px; margin-bottom:16px; }
  .picker label { font-size:12px; color:var(--muted); white-space:nowrap; }
  .picker input[type=date] { flex:1; min-width:0; background:var(--panel); color:var(--text);
    border:1px solid var(--line); border-radius:8px; padding:10px 12px;
    font-family:"IBM Plex Mono",monospace; font-size:14px; }
  .picker input[type=date]::-webkit-calendar-picker-indicator { filter:invert(.65); cursor:pointer; }
  .nav { display:flex; gap:8px; }
  .nav button { background:var(--panel); color:var(--text); border:1px solid var(--line);
    border-radius:8px; width:40px; height:40px; font-size:16px; cursor:pointer; }
  .nav button:disabled { opacity:.35; cursor:default; }

  .meta { display:flex; flex-wrap:wrap; gap:6px 16px; font-family:"IBM Plex Mono",monospace;
    font-size:12px; color:var(--muted); margin-bottom:18px; }
  .meta b { color:var(--text); font-weight:500; }
  .latest-badge { color:var(--up); }
  .hist-badge { color:var(--muted); }

  .verdict { background:var(--panel); border:1px solid var(--line);
    border-left:4px solid var(--accent); border-radius:12px; padding:22px 20px; margin-bottom:20px; }
  .verdict-tag { font-family:"IBM Plex Mono",monospace; font-size:12px; letter-spacing:.14em;
    color:var(--muted); text-transform:uppercase; }
  .verdict-head { display:flex; align-items:center; justify-content:space-between; gap:12px; margin:8px 0 12px; }
  .verdict-title { font-size:34px; font-weight:700; color:var(--accent); letter-spacing:.03em; }
  .signal-arrow { font-size:46px; font-weight:800; line-height:1; }
  .sig-up { color:#E5484D; }
  .sig-down { color:#3DAE73; }
  .verdict-desc { font-size:14.5px; color:var(--text); }
  .action { margin-top:14px; padding:12px 14px; border-radius:8px; font-weight:700; font-size:15.5px; }
  .act-hold-long { background:rgba(229,72,77,.12); color:#E5484D; border:1px solid rgba(229,72,77,.45); }
  .act-hold-short { background:rgba(61,174,115,.12); color:#3DAE73; border:1px solid rgba(61,174,115,.45); }
  .act-scalp { background:rgba(217,138,61,.12); color:#D98A3D; border:1px solid rgba(217,138,61,.45); }
  .addon { display:none; margin-top:10px; padding:11px 14px; border-radius:8px; font-weight:700;
    font-size:15px; border:1px dashed; }
  .add-long { color:#E5484D; border-color:#E5484D; background:rgba(229,72,77,.10); }
  .add-short { color:#3DAE73; border-color:#3DAE73; background:rgba(61,174,115,.10); }
  .dirs { display:flex; gap:10px; margin-top:16px; flex-wrap:wrap; }
  .chip { font-size:12px; color:var(--muted); background:#1B1F26; border:1px solid var(--line);
    border-radius:20px; padding:6px 12px; }
  .chip b { font-weight:600; margin-left:5px; font-size:13px; }
  .dir-up { color:var(--down); }   /* 多／偏多＝紅（台股慣例 漲紅） */
  .dir-down { color:var(--up); }   /* 空／偏空＝綠（跌綠） */
  .dir-warn { color:#D98A3D; }
  .dir-flat { color:var(--muted); }
  .dir-soft { color:var(--muted); }                     /* 盤整：柔和 */
  .dir-chop { color:#0B0D10; background:#F5A623; padding:2px 9px; border-radius:6px; font-weight:700; }  /* 震盪：醒目 */

  .stats { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:22px; }
  .stat { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px; }
  .stat-k { font-size:12px; color:var(--muted); margin-bottom:6px; }
  .stat-v { font-family:"IBM Plex Mono",monospace; font-size:22px; font-weight:600; }
  .stat-sub { font-family:"IBM Plex Mono",monospace; font-size:11px; color:var(--muted); margin-top:2px; }

  .section-title { font-size:13px; color:var(--muted); letter-spacing:.06em; margin:0 0 12px; }

  .body-chart { background:var(--panel); border:1px solid var(--line); border-radius:10px;
    padding:16px 12px 10px; margin-bottom:22px; }
  .body-bars { display:flex; align-items:flex-end; gap:5px; height:120px; }
  .body-col { flex:1; display:flex; flex-direction:column; align-items:center; height:100%; }
  .body-bar-track { flex:1; width:100%; display:flex; align-items:flex-end; }
  .body-bar-fill { width:100%; border-radius:3px 3px 0 0; }
  .body-date { font-family:"IBM Plex Mono",monospace; font-size:9px; color:var(--muted);
    margin-top:6px; transform:rotate(-45deg); transform-origin:center; white-space:nowrap; }
  .legend { display:flex; gap:16px; font-size:11px; color:var(--muted); margin-top:16px; flex-wrap:wrap; }
  .legend i { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:5px; vertical-align:middle; }

  .ma-table { background:var(--panel); border:1px solid var(--line); border-radius:10px;
    padding:6px 16px; margin-bottom:22px; }
  .ma-row { display:flex; justify-content:space-between; padding:10px 0; border-bottom:1px solid var(--line); }
  .ma-row:last-child { border-bottom:none; }
  .ma-label { font-family:"IBM Plex Mono",monospace; color:var(--muted); font-size:13px; }
  .ma-val { font-family:"IBM Plex Mono",monospace; font-size:15px; font-weight:500; }

  footer { font-family:"IBM Plex Mono",monospace; font-size:11px; color:var(--muted);
    text-align:center; margin-top:8px; line-height:1.7; }
</style>
</head>
<body>
<div class="wrap">
  <div class="eyebrow" id="eyebrow">日K自動判斷</div>
  <h1>趨勢／盤整判斷</h1>

  <div class="tabs" id="tabs"></div>

  <div class="picker">
    <label>查看日期</label>
    <input type="date" id="dateInput">
    <div class="nav">
      <button id="prevBtn" title="前一交易日">‹</button>
      <button id="nextBtn" title="後一交易日">›</button>
    </div>
  </div>

  <div class="meta" id="meta"></div>
  <div class="verdict">
    <div class="verdict-tag">判定結果</div>
    <div class="verdict-head">
      <div class="verdict-title"><span id="verdictLabel">—</span></div>
      <div class="signal-arrow" id="signalArrow"></div>
    </div>
    <div class="verdict-desc" id="verdictDesc"></div>
    <div class="action" id="actionBox"></div>
    <div class="addon" id="addonBox"></div>
    <div class="dirs">
      <span class="chip">均線型態<b class="dir-val" id="alignVal"></b></span>
      <span class="chip">短線方向<b class="dir-val" id="momVal"></b></span>
    </div>
  </div>

  <div class="stats" id="stats"></div>

  <div class="section-title">近期 K 棒實體比例（最近 16 根）</div>
  <div class="body-chart">
    <div class="body-bars" id="bodyBars"></div>
    <div class="legend">
      <span><i style="background:#8B7EC8;"></i>低於警戒值</span>
      <span><i style="background:#D4A73C;"></i>&gt;70% 強實體</span>
      <span><i style="background:#3A4049;"></i>其餘</span>
    </div>
  </div>

  <div class="section-title">當日均線數值</div>
  <div class="ma-table" id="maTable"></div>

  <footer>
    資料來源 FinMind（台指）／ Kraken（比特幣）／ Yahoo Finance（那斯達克・費半）<br>
    本頁產生時間 __GEN__ · 歷史判定為依當日（含）之前資料回推計算<br>
    僅供研究參考，非投資建議
  </footer>
</div>

<script>
const ASSETS = __DATA__;
const PERIODS = __PERIODS__;
const VC = { trend:"#D4A73C", range:"#8B7EC8", watch:"#D98A3D" };
const ST = { expanding:"擴大中 ↑", contracting:"收斂中 ↓", flat:"持平 →" };

let curAsset = 0, curIdx = 0, DATES = [];

const tabsEl = document.getElementById("tabs");
const eyebrowEl = document.getElementById("eyebrow");
const dateInput = document.getElementById("dateInput");
const prevBtn = document.getElementById("prevBtn");
const nextBtn = document.getElementById("nextBtn");

function fmt(v, d) {
  if (v === null || v === undefined) return "—";
  return Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}
function pct(v) { return (v * 100).toFixed(0) + "%"; }
function card(k, v, sub) {
  return '<div class="stat"><div class="stat-k">' + k + '</div>' +
         '<div class="stat-v">' + v + '</div><div class="stat-sub">' + sub + '</div></div>';
}
function setDir(id, dir, label) {
  const el = document.getElementById(id);
  el.textContent = label;
  const cls = (dir === "up") ? "dir-up"
            : (dir === "down") ? "dir-down"
            : (dir === "converge") ? "dir-warn" : "dir-flat";
  el.className = "dir-val " + cls;
}
function nearestIdx(dateStr) {
  let found = -1;
  for (let i = 0; i < DATES.length; i++) { if (DATES[i] <= dateStr) found = i; else break; }
  return found >= 0 ? found : 0;
}

// 建立頁籤
ASSETS.forEach(function (a, i) {
  const t = document.createElement("div");
  t.className = "tab";
  t.textContent = a.name;
  t.addEventListener("click", function () { switchAsset(i); });
  tabsEl.appendChild(t);
});

function switchAsset(i) {
  curAsset = i;
  const hist = ASSETS[i].history;
  DATES = hist.map(function (r) { return r.last_date; });
  dateInput.min = DATES[0];
  dateInput.max = DATES[DATES.length - 1];
  eyebrowEl.textContent = ASSETS[i].name + " · 日K自動判斷";
  for (let j = 0; j < tabsEl.children.length; j++) {
    tabsEl.children[j].classList.toggle("active", j === i);
  }
  render(hist.length - 1);
}

function render(idx) {
  curIdx = idx;
  const hist = ASSETS[curAsset].history;
  const r = hist[idx];
  const isLatest = idx === hist.length - 1;
  dateInput.value = r.last_date;
  document.documentElement.style.setProperty("--accent", r.accent || "#8B919B");

  document.getElementById("meta").innerHTML =
    '<span>收盤日 <b class="mono">' + r.last_date + '</b></span>' +
    '<span>收盤價 <b class="mono">' + fmt(r.last_close, 0) + '</b></span>' +
    (isLatest ? '<span class="latest-badge">● 最新</span>'
              : '<span class="hist-badge">○ 歷史回推</span>');

  document.getElementById("verdictLabel").textContent = r.state_label;
  document.getElementById("verdictDesc").textContent = r.state_desc;

  const act = document.getElementById("actionBox");
  act.textContent = r.action_label;
  act.className = "action " + (r.action_class || "act-scalp");

  const addon = document.getElementById("addonBox");
  if (r.add_signal === "add_long" || r.add_signal === "add_short") {
    addon.textContent = "🎯 " + r.add_label;
    addon.className = "addon " + (r.add_signal === "add_long" ? "add-long" : "add-short");
    addon.style.display = "block";
  } else {
    addon.style.display = "none";
  }

  // 均線型態：發散跟著趨勢方向上色（多紅空綠），收斂為橘色警訊
  const alignEl = document.getElementById("alignVal");
  alignEl.textContent = r.conv_label;
  if (r.conv === "chop") {
    alignEl.className = "dir-val dir-chop";            // 震盪：醒目
  } else if (r.conv === "range") {
    alignEl.className = "dir-val dir-soft";            // 盤整：柔和
  } else if (r.conv === "diverge") {
    alignEl.className = "dir-val " + (r.trend_dir === "long" ? "dir-up"
                        : r.trend_dir === "short" ? "dir-down" : "dir-flat");
  } else {
    alignEl.className = "dir-val dir-flat";
  }
  setDir("momVal", r.momentum, r.momentum_label);

  // 強訊號箭頭：2 項→1 箭頭、3 項→2 箭頭
  const arrow = document.getElementById("signalArrow");
  if (r.signal_dir === "up" && r.signal_n > 0) {
    arrow.textContent = "↑".repeat(r.signal_n); arrow.className = "signal-arrow sig-up";
  } else if (r.signal_dir === "down" && r.signal_n > 0) {
    arrow.textContent = "↓".repeat(r.signal_n); arrow.className = "signal-arrow sig-down";
  } else {
    arrow.textContent = ""; arrow.className = "signal-arrow";
  }

  document.getElementById("stats").innerHTML =
    card("均線發散度", r.spread_now + "%", ST[r.spread_trend] || "—") +
    card("近 " + r.lookback + " 根變動", (r.spread_delta >= 0 ? "+" : "") + r.spread_delta.toFixed(2), "Δ 發散度") +
    card("連續小實體", r.streak, "門檻 " + r.streak_thresh + " 根") +
    card("實體警戒值", pct(r.body_thresh), "低於即計數");

  let bars = "";
  r.recent_bodies.forEach(function (b) {
    const h = Math.max(2, Math.round(b.ratio * 100));
    const color = b.ratio < r.body_thresh ? "#8B7EC8" : (b.ratio > 0.70 ? "#D4A73C" : "#3A4049");
    bars += '<div class="body-col" title="' + b.date + '：' + pct(b.ratio) + '">' +
            '<div class="body-bar-track"><div class="body-bar-fill" style="height:' + h + '%;background:' + color + ';"></div></div>' +
            '<div class="body-date">' + b.date.slice(5) + '</div></div>';
  });
  document.getElementById("bodyBars").innerHTML = bars;

  let ma = "";
  PERIODS.forEach(function (p) {
    ma += '<div class="ma-row"><span class="ma-label">MA' + p + '</span>' +
          '<span class="ma-val">' + fmt(r.ma_now[String(p)], 0) + '</span></div>';
  });
  document.getElementById("maTable").innerHTML = ma;

  prevBtn.disabled = idx === 0;
  nextBtn.disabled = isLatest;
}

dateInput.addEventListener("change", function () {
  if (dateInput.value) render(nearestIdx(dateInput.value));
});
prevBtn.addEventListener("click", function () { if (curIdx > 0) render(curIdx - 1); });
nextBtn.addEventListener("click", function () {
  if (curIdx < ASSETS[curAsset].history.length - 1) render(curIdx + 1);
});

// 預設顯示第一個商品的最新一天
switchAsset(0);
</script>
</body>
</html>
"""
    html = (html
            .replace("__GEN__", gen_time)
            .replace("__PERIODS__", periods_json)
            .replace("__DATA__", data_json))
    return html


# ---------------------------------------------------------------------------
# 串接：抓所有商品 → 各自建歷史 → 產出單一 HTML → 寫檔
# ---------------------------------------------------------------------------

def build_asset(cfg, periods, body_thresh, streak_thresh, lookback, history_days, hist_max):
    if cfg["kind"] == "futures":
        end = datetime.date.today()
        start = end - datetime.timedelta(days=history_days * 2)
        bars = fetch_futures_daily(cfg["id"], start.isoformat(), end.isoformat())
    elif cfg["kind"] == "crypto":
        bars = fetch_crypto_daily(cfg["pair"])
    elif cfg["kind"] == "index":
        bars = fetch_index_daily(cfg["symbol"])
    else:
        raise RuntimeError("未知商品類型：%s" % cfg["kind"])

    history = build_history(bars, periods, body_thresh, streak_thresh, lookback, max_days=hist_max)
    if not history:
        raise RuntimeError("資料不足，無法建立任何一天的判定。")
    return {"key": cfg["key"], "name": cfg["name"], "history": history}


def run_all(assets_cfg=None, periods=None, body_thresh_pct=40, streak_thresh=3,
            lookback=6, history_days=200, hist_max=180, output_path="index.html"):
    if assets_cfg is None:
        assets_cfg = ASSETS
    if periods is None:
        periods = [5, 10, 20, 60]
    body_thresh = body_thresh_pct / 100.0

    if not FINMIND_TOKEN:
        print("（未設定 FINMIND_TOKEN，台指以匿名額度抓取，仍可運作）")

    results = []
    for cfg in assets_cfg:
        print("抓取並分析 %s（%s）..." % (cfg["name"], cfg["key"]))
        try:
            res = build_asset(cfg, periods, body_thresh, streak_thresh, lookback, history_days, hist_max)
        except Exception as e:  # 單一商品失敗不影響其他商品
            print("  ! %s 失敗，略過：%s" % (cfg["key"], e), file=sys.stderr)
            continue
        latest = res["history"][-1]
        print("  %s：%d 天可查，最新 %s → %s"
              % (cfg["key"], len(res["history"]), latest["last_date"], latest["verdict_label"]))
        results.append(res)

    if not results:
        raise RuntimeError("所有商品都抓取失敗，無法產生報告。")

    print("產生 HTML 報告 ...")
    html = generate_html_report(results, periods)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print("完成！已寫入 %s（%d 個商品）。" % (output_path, len(results)))
    return results


if __name__ == "__main__":
    try:
        run_all(
            assets_cfg=ASSETS,
            periods=[5, 10, 20, 60],
            body_thresh_pct=40,
            streak_thresh=3,
            lookback=6,
            history_days=200,
            hist_max=180,
        )
    except Exception as e:
        print("執行失敗：%s" % e, file=sys.stderr)
        sys.exit(1)
