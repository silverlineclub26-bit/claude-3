# -*- coding: utf-8 -*-
"""
台指期趨勢／盤整 自動判斷系統

抓 FinMind 免費日 K 資料 → 計算均線 / 發散度 / K 棒實體比例 / 連續小實體根數
→ 判斷「趨勢盤 / 盤整盤 / 轉折觀察」→ 產出 index.html。

由 GitHub Actions 每個交易日自動執行，index.html 推回 repo 後由 GitHub Pages 發布，
手機瀏覽器加書籤即可查看，不需自己跑程式。
"""

import os
import sys
import datetime

import requests

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "").strip()
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"

# 各判定門檻的預設值（會由 run() / analyze() 帶入）
DELTA_EXPAND = 0.02      # 發散度趨勢：delta > 0.02 視為擴大中
DELTA_CONTRACT = -0.02   # 發散度趨勢：delta < -0.02 視為收斂中


# ---------------------------------------------------------------------------
# 資料抓取
# ---------------------------------------------------------------------------

def fetch_futures_daily(futures_id, start_date, end_date):
    """
    呼叫 FinMind TaiwanFuturesDaily，回傳一份「一天一筆」的日 K 資料。

    FinMind 同一天會回傳多個合約（近月 / 次月）與多個交易時段（一般盤 / 盤後），
    這裡：
      1. 優先只留一般交易時段 (trading_session == 'position')
      2. 同一天取「成交量最大」的那筆 → 近月主力合約
      3. 依日期去重、由舊到新排序

    回傳 list[dict]，每筆含 date, open, max, min, close（皆為 float，date 為字串）。
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
            "FinMind 沒有回傳任何資料（data_id=%s, %s ~ %s）。"
            "請確認商品代碼與日期區間。" % (futures_id, start_date, end_date)
        )

    # 1. 優先只留一般交易時段
    if any("trading_session" in r for r in rows):
        day_rows = [r for r in rows if str(r.get("trading_session", "")).lower() == "position"]
        if day_rows:
            rows = day_rows

    # 2. 同一天取成交量最大的一筆
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

    # 3. 整理成乾淨欄位、由舊到新排序
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
            # 某些停牌日欄位可能為空，跳過
            continue
        # 過濾掉開高低收都是 0 的異常列
        if bar["max"] <= 0 and bar["min"] <= 0 and bar["close"] <= 0:
            continue
        bars.append(bar)

    if not bars:
        raise RuntimeError("FinMind 資料整理後為空，無有效日 K。")

    return bars


# ---------------------------------------------------------------------------
# 指標計算
# ---------------------------------------------------------------------------

def sma(values, period):
    """
    簡單移動平均。回傳與輸入等長的陣列；前面不足週期的位置填 None。
    """
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


# ---------------------------------------------------------------------------
# 核心分析
# ---------------------------------------------------------------------------

def analyze(bars, periods, body_thresh, streak_thresh, lookback):
    """
    執行趨勢／盤整判斷。

    參數
      bars          : fetch_futures_daily() 的輸出（由舊到新）
      periods       : 均線週期清單，例 [5, 10, 20, 60]
      body_thresh   : 實體比例警戒值（0~1），例 0.40
      streak_thresh : 連續小實體根數門檻，例 3
      lookback      : 發散度與前幾根相比，例 6

    回傳 dict，含判定結果與前端所需資料。
    """
    periods = sorted(periods)
    max_period = max(periods)

    # 資料量檢查：至少要 最長均線週期 + 回看根數 + 2
    required = max_period + lookback + 2
    if len(bars) < required:
        raise RuntimeError(
            "資料不足：需要至少 %d 根日 K（最長均線 %d + 回看 %d + 2），目前只有 %d 根。"
            % (required, max_period, lookback, len(bars))
        )

    closes = [b["close"] for b in bars]

    # 1. 各週期均線（用收盤價）
    ma_series = {p: sma(closes, p) for p in periods}

    # 2. 每根 K 棒的均線發散度 spread（%）
    #    = (最大均線值 - 最小均線值) / 均線平均值 * 100
    spreads = [None] * len(bars)
    for i in range(len(bars)):
        vals = [ma_series[p][i] for p in periods if ma_series[p][i] is not None]
        if len(vals) == len(periods):  # 需所有均線都有值
            avg = sum(vals) / len(vals)
            if avg != 0:
                spreads[i] = (max(vals) - min(vals)) / avg * 100.0

    # 3. 發散度趨勢：最新一根 spread 對比 lookback 根前的 spread
    latest_spread = spreads[-1]
    prev_spread = spreads[-1 - lookback]
    if latest_spread is None or prev_spread is None:
        raise RuntimeError("發散度資料不足，無法比較趨勢（可能是均線暖機期）。")
    delta = latest_spread - prev_spread

    if delta > DELTA_EXPAND:
        spread_trend = "expanding"      # 擴大中
    elif delta < DELTA_CONTRACT:
        spread_trend = "contracting"    # 收斂中
    else:
        spread_trend = "flat"           # 持平

    # 4. 每根 K 棒實體比例 = |close - open| / (max - min)
    body_ratios = []
    for b in bars:
        rng = b["max"] - b["min"]
        ratio = 0.0 if rng <= 0 else abs(b["close"] - b["open"]) / rng
        body_ratios.append(ratio)

    # 5. 連續小實體根數 streak：從最新一根往回數，連續幾根實體比例 < 警戒值
    streak = 0
    for ratio in reversed(body_ratios):
        if ratio < body_thresh:
            streak += 1
        else:
            break

    # 6. 最終判定
    if spread_trend == "expanding" and streak < 2:
        verdict = "trend"
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

    # 前端要用的近期實體比例（最近 16 根）
    recent_n = min(16, len(bars))
    recent_bodies = [
        {
            "date": bars[i]["date"],
            "ratio": round(body_ratios[i], 4),
            "below_thresh": body_ratios[i] < body_thresh,
            "strong": body_ratios[i] > 0.70,
        }
        for i in range(len(bars) - recent_n, len(bars))
    ]

    # 目前各均線數值
    ma_now = {p: ma_series[p][-1] for p in periods}

    last = bars[-1]
    return {
        "futures_id": None,  # 由 run() 補上
        "verdict": verdict,
        "verdict_label": verdict_label,
        "verdict_desc": verdict_desc,
        "last_date": last["date"],
        "last_close": last["close"],
        "spread_now": round(latest_spread, 3),
        "spread_prev": round(prev_spread, 3),
        "spread_delta": round(delta, 3),
        "spread_trend": spread_trend,
        "lookback": lookback,
        "streak": streak,
        "streak_thresh": streak_thresh,
        "body_thresh": body_thresh,
        "periods": periods,
        "ma_now": ma_now,
        "recent_bodies": recent_bodies,
        "bar_count": len(bars),
    }


# ---------------------------------------------------------------------------
# 產生 HTML 報告
# ---------------------------------------------------------------------------

_VERDICT_COLOR = {
    "trend": "#D4A73C",   # 金色：趨勢盤
    "range": "#8B7EC8",   # 紫色：盤整盤
    "watch": "#D98A3D",   # 橙色：轉折觀察
}

_SPREAD_TREND_LABEL = {
    "expanding": "擴大中 ↑",
    "contracting": "收斂中 ↓",
    "flat": "持平 →",
}


def _fmt(v, digits=0):
    if v is None:
        return "—"
    if digits == 0:
        return "{:,.0f}".format(v)
    return ("{:,.%df}" % digits).format(v)


def generate_html_report(result, futures_id):
    """把 analyze() 的結果套進深色交易終端機風格的手機頁面，回傳完整 HTML 字串。"""
    result = dict(result)
    result["futures_id"] = futures_id
    accent = _VERDICT_COLOR.get(result["verdict"], "#8B919B")

    gen_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    # 均線數值表
    ma_rows = "".join(
        '<div class="ma-row"><span class="ma-label">MA{p}</span>'
        '<span class="ma-val">{v}</span></div>'.format(p=p, v=_fmt(result["ma_now"][p], 0))
        for p in result["periods"]
    )

    # 近期實體比例長條
    bar_cells = ""
    for b in result["recent_bodies"]:
        pct = max(2, round(b["ratio"] * 100))
        if b["below_thresh"]:
            color = "#8B7EC8"   # 紫：低於警戒值
        elif b["strong"]:
            color = "#D4A73C"   # 金：超過 70%
        else:
            color = "#3A4049"   # 灰：其餘
        mmdd = b["date"][5:]
        bar_cells += (
            '<div class="body-col" title="{date}：{ratio:.0%}">'
            '<div class="body-bar-track">'
            '<div class="body-bar-fill" style="height:{pct}%;background:{color};"></div>'
            '</div>'
            '<div class="body-date">{mmdd}</div>'
            '</div>'
        ).format(date=b["date"], ratio=b["ratio"], pct=pct, color=color, mmdd=mmdd)

    stat_cards = """
      <div class="stat"><div class="stat-k">均線發散度</div><div class="stat-v">{spread_now}%</div>
        <div class="stat-sub">{trend_label}</div></div>
      <div class="stat"><div class="stat-k">近 {lookback} 根變動</div>
        <div class="stat-v">{delta:+.2f}</div><div class="stat-sub">Δ 發散度</div></div>
      <div class="stat"><div class="stat-k">連續小實體</div><div class="stat-v">{streak}</div>
        <div class="stat-sub">門檻 {streak_thresh} 根</div></div>
      <div class="stat"><div class="stat-k">實體警戒值</div><div class="stat-v">{body_thresh:.0%}</div>
        <div class="stat-sub">低於即計數</div></div>
    """.format(
        spread_now=result["spread_now"],
        trend_label=_SPREAD_TREND_LABEL.get(result["spread_trend"], "—"),
        lookback=result["lookback"],
        delta=result["spread_delta"],
        streak=result["streak"],
        streak_thresh=result["streak_thresh"],
        body_thresh=result["body_thresh"],
    )

    html = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>台指期 趨勢／盤整 判斷 · {futures_id}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=Noto+Sans+TC:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg:#0B0D10; --panel:#14171C; --line:#262B33;
    --text:#EDEAE3; --muted:#8B919B;
    --accent:{accent};
    --up:#3DAE73; --down:#E5484D;
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{
    background:var(--bg); color:var(--text);
    font-family:"Noto Sans TC",-apple-system,sans-serif;
    -webkit-font-smoothing:antialiased; line-height:1.5;
    padding:24px 16px 40px;
  }}
  .wrap {{ max-width:520px; margin:0 auto; }}
  .mono {{ font-family:"IBM Plex Mono",monospace; }}

  .eyebrow {{ font-family:"IBM Plex Mono",monospace; font-size:12px; letter-spacing:.18em;
    color:var(--muted); text-transform:uppercase; }}
  h1 {{ font-size:22px; font-weight:700; margin:6px 0 14px; letter-spacing:.02em; }}
  .meta {{ display:flex; flex-wrap:wrap; gap:6px 16px; font-family:"IBM Plex Mono",monospace;
    font-size:12px; color:var(--muted); margin-bottom:22px; }}
  .meta b {{ color:var(--text); font-weight:500; }}

  .verdict {{ background:var(--panel); border:1px solid var(--line);
    border-left:4px solid var(--accent); border-radius:12px; padding:22px 20px; margin-bottom:20px; }}
  .verdict-tag {{ font-family:"IBM Plex Mono",monospace; font-size:12px; letter-spacing:.14em;
    color:var(--muted); text-transform:uppercase; }}
  .verdict-title {{ font-size:34px; font-weight:700; color:var(--accent); margin:8px 0 12px; letter-spacing:.03em; }}
  .verdict-desc {{ font-size:14.5px; color:var(--text); }}

  .stats {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:22px; }}
  .stat {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px; }}
  .stat-k {{ font-size:12px; color:var(--muted); margin-bottom:6px; }}
  .stat-v {{ font-family:"IBM Plex Mono",monospace; font-size:22px; font-weight:600; }}
  .stat-sub {{ font-family:"IBM Plex Mono",monospace; font-size:11px; color:var(--muted); margin-top:2px; }}

  .section-title {{ font-size:13px; color:var(--muted); letter-spacing:.06em; margin:0 0 12px; }}

  .body-chart {{ background:var(--panel); border:1px solid var(--line); border-radius:10px;
    padding:16px 12px 10px; margin-bottom:22px; }}
  .body-bars {{ display:flex; align-items:flex-end; gap:5px; height:120px; }}
  .body-col {{ flex:1; display:flex; flex-direction:column; align-items:center; height:100%; }}
  .body-bar-track {{ flex:1; width:100%; display:flex; align-items:flex-end; }}
  .body-bar-fill {{ width:100%; border-radius:3px 3px 0 0; }}
  .body-date {{ font-family:"IBM Plex Mono",monospace; font-size:9px; color:var(--muted);
    margin-top:6px; transform:rotate(-45deg); transform-origin:center; white-space:nowrap; }}
  .legend {{ display:flex; gap:16px; font-size:11px; color:var(--muted); margin-top:16px; flex-wrap:wrap; }}
  .legend i {{ display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:5px; vertical-align:middle; }}

  .ma-table {{ background:var(--panel); border:1px solid var(--line); border-radius:10px;
    padding:6px 16px; margin-bottom:22px; }}
  .ma-row {{ display:flex; justify-content:space-between; padding:10px 0; border-bottom:1px solid var(--line); }}
  .ma-row:last-child {{ border-bottom:none; }}
  .ma-label {{ font-family:"IBM Plex Mono",monospace; color:var(--muted); font-size:13px; }}
  .ma-val {{ font-family:"IBM Plex Mono",monospace; font-size:15px; font-weight:500; }}

  footer {{ font-family:"IBM Plex Mono",monospace; font-size:11px; color:var(--muted);
    text-align:center; margin-top:8px; line-height:1.7; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="eyebrow">{futures_id} · 日K自動判斷</div>
  <h1>趨勢／盤整判斷</h1>
  <div class="meta">
    <span>更新 <b>{gen_time}</b></span>
    <span>收盤日 <b class="mono">{last_date}</b></span>
    <span>收盤價 <b class="mono">{last_close}</b></span>
  </div>

  <div class="verdict">
    <div class="verdict-tag">判定結果</div>
    <div class="verdict-title">{verdict_label}</div>
    <div class="verdict-desc">{verdict_desc}</div>
  </div>

  <div class="stats">{stat_cards}</div>

  <div class="section-title">近期 K 棒實體比例（最近 {recent_n} 根）</div>
  <div class="body-chart">
    <div class="body-bars">{bar_cells}</div>
    <div class="legend">
      <span><i style="background:#8B7EC8;"></i>低於警戒值</span>
      <span><i style="background:#D4A73C;"></i>&gt;70% 強實體</span>
      <span><i style="background:#3A4049;"></i>其餘</span>
    </div>
  </div>

  <div class="section-title">目前均線數值</div>
  <div class="ma-table">{ma_rows}</div>

  <footer>
    資料來源 FinMind TaiwanFuturesDaily · 每交易日 16:30 後更新<br>
    本頁由 GitHub Actions 自動產生 · 僅供研究參考，非投資建議
  </footer>
</div>
</body>
</html>
""".format(
        futures_id=futures_id,
        accent=accent,
        gen_time=gen_time,
        last_date=result["last_date"],
        last_close=_fmt(result["last_close"], 0),
        verdict_label=result["verdict_label"],
        verdict_desc=result["verdict_desc"],
        stat_cards=stat_cards,
        recent_n=len(result["recent_bodies"]),
        bar_cells=bar_cells,
        ma_rows=ma_rows,
    )
    return html


# ---------------------------------------------------------------------------
# 串接：抓資料 → 分析 → 產出 HTML → 寫檔
# ---------------------------------------------------------------------------

def run(futures_id="MTX", periods=None, body_thresh_pct=40, streak_thresh=3,
        lookback=6, history_days=200, output_path="index.html"):
    if periods is None:
        periods = [5, 10, 20, 60]
    body_thresh = body_thresh_pct / 100.0

    end = datetime.date.today()
    start = end - datetime.timedelta(days=history_days * 2)  # 抓寬一點以扣除假日
    start_date = start.isoformat()
    end_date = end.isoformat()

    print("[1/4] 抓取 FinMind 資料 %s (%s ~ %s) ..." % (futures_id, start_date, end_date))
    if not FINMIND_TOKEN:
        print("      （未設定 FINMIND_TOKEN，以匿名額度執行，仍可運作）")
    bars = fetch_futures_daily(futures_id, start_date, end_date)
    print("      取得 %d 根日 K，最新 %s。" % (len(bars), bars[-1]["date"]))

    print("[2/4] 計算指標與判斷 ...")
    result = analyze(bars, periods, body_thresh, streak_thresh, lookback)
    print("      判定：%s（發散度趨勢=%s, streak=%d）"
          % (result["verdict_label"], result["spread_trend"], result["streak"]))

    print("[3/4] 產生 HTML 報告 ...")
    html = generate_html_report(result, futures_id)

    print("[4/4] 寫入 %s ..." % output_path)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print("完成！判定結果：%s。" % result["verdict_label"])
    return result


if __name__ == "__main__":
    try:
        run(
            futures_id="MTX",
            periods=[5, 10, 20, 60],
            body_thresh_pct=40,
            streak_thresh=3,
            lookback=6,
            history_days=200,
        )
    except Exception as e:
        print("執行失敗：%s" % e, file=sys.stderr)
        sys.exit(1)
