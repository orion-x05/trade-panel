#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bt_replay.py —— 历史 K 线回放回测（逐日、按角色规则）

诚实的边界（写在最前面）：
  1. 规则是「可量化复刻」，不是原策略逐条照搬。原策略里带 AI 判断、群里讨论、
     资金流打分等主观环节，这里一律用可计算的替代判据，宁可简单也不编。
  2. 不含滑点与手续费摩擦的精细建模，只扣双边费用 0.05% + 佣金万 1.15（最低 5 元）。
  3. 单票满仓、不加仓、不做两段式；同时只持 1 只，避免回放里过度交易。
  4. 数据来自腾讯前复权日 K，250 根，遇停牌/缺数据直接跳过该票。

输出 bt_result.json：
  {asof, universe, days, roles: {D1: {...}}, benchmark: {...}, note}
"""
import json
import re
import time
import urllib.request

UNIVERSE_FALLBACK = [
    "600519", "600036", "601318", "600030", "600887", "000858", "000333", "000651",
    "600276", "300750", "002594", "601012", "600585", "601899", "600028", "601088",
]
BARS = 250

# 各角色的 ATR 止损倍数与盈亏比（与引擎 sl_atr 思路一致：波动大的票止损更宽）
ROLE_SL_ATR = {"D1": 2.0, "D2": 2.0, "D3": 2.2, "D4": 1.8, "D5": 2.0, "D6": 2.5, "D7": 2.5}
ROLE_RR = {"D1": 2.0, "D2": 2.0, "D3": 2.0, "D4": 2.5, "D5": 2.0, "D6": 2.0, "D7": 3.0}


def atr_pct(k, i, n=14):
    """第 i 根处的 ATR 占价格百分比。"""
    if i < n + 1:
        return 2.0
    trs = []
    for j in range(i - n + 1, i + 1):
        h, l, pc = k[j]["h"], k[j]["l"], k[j - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return (sum(trs) / len(trs)) / k[i]["c"] * 100


def fetch_kline(code, n=BARS):
    sym = ("sh" if str(code).startswith("6") else "sz") + str(code)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{n},qfq"
    for _ in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            j = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))
            obj = (j.get("data") or {}).get(sym) or {}
            rows = obj.get("qfqday") or obj.get("day") or []
            out = []
            for r in rows:
                try:
                    out.append({"d": r[0], "o": float(r[1]), "c": float(r[2]),
                                "h": float(r[3]), "l": float(r[4]), "v": float(r[5])})
                except Exception:
                    continue
            return out
        except Exception:
            time.sleep(1)
    return []


def load_universe():
    """从 trade_engine.py 里抠 STOCK_POOL；失败用兜底池。"""
    try:
        src = open("trade_engine.py", encoding="utf-8").read()
        m = re.search(r"STOCK_POOL\s*=\s*[\[{](.*?)[\]}]", src, re.S)
        if m:
            codes = re.findall(r'["\'](\d{6})["\']', m.group(1))
            if codes:
                return sorted(set(codes))
    except Exception:
        pass
    return UNIVERSE_FALLBACK


def ma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def signal(role, k, i):
    """第 i 根 K 线的开仓信号（只用 i 及之前的数据，防未来函数）。"""
    if i < 61:
        return False
    closes = [x["c"] for x in k[:i + 1]]
    vols = [x["v"] for x in k[:i + 1]]
    cur, prev = k[i], k[i - 1]
    m5, m20, m60 = ma(closes, 5), ma(closes, 20), ma(closes, 60)
    if None in (m5, m20, m60):
        return False
    hi20 = max(closes[-21:-1])
    v5 = sum(vols[-6:-1]) / 5 if len(vols) >= 6 else vols[-1]
    vr = cur["v"] / v5 if v5 else 0
    chg = (cur["c"] / prev["c"] - 1) * 100
    up_trend = m20 > m60

    if role == "D1":      # 趋势突破：多头 + 收盘破前 20 日最高 + 放量
        return up_trend and cur["c"] >= hi20 and vr >= 1.2 and chg > 1
    if role == "D2":      # 多头共识：均线多头排列且当日收红
        return up_trend and cur["c"] > m5 > m20 and chg > 0
    if role == "D3":      # 多空辩论（简化）：站上 MA20、近三日不破、且当日有力度
        return (cur["c"] > m20 and min(x["c"] for x in k[i - 2:i + 1]) > m20 * 0.98
                and chg > 1.5 and vr >= 1.2)
    if role == "D4":      # 假阴真阳：收<开但收>昨收，且放量
        return cur["c"] < cur["o"] and chg > 0 and vr >= 1.5
    if role == "D5":      # 趋势回踩：多头趋势 + 贴近 MA20 + 收阳
        return up_trend and abs(cur["c"] / m20 - 1) <= 0.025 and cur["c"] > cur["o"]
    if role == "D6":      # 涨停回调：近 10 日有涨停，今日缩量回踩不破其低点
        zt_days = [x for j, x in enumerate(k[i - 9:i + 1]) if j > 0 and x["c"] / k[i - 9 + j - 1]["c"] - 1 >= 0.095]
        if not zt_days:
            return False
        low = min(x["l"] for x in zt_days)
        return cur["l"] >= low and vr < 1.2 and cur["c"] > cur["o"]
    if role == "D7":      # 周线超卖（简化为 20 日深跌后收阳）
        ret20 = (cur["c"] / closes[-21] - 1) * 100
        return ret20 <= -15 and cur["c"] > cur["o"]
    return False


def run_role(role, data, fee_rate=0.00025, stamp=0.0005,
             stop=-7.0, take=8.0, max_hold=20, max_pos=5, pos_pct=0.2):
    """组合口径回放：与实盘一致——最多 5 只、单只 20% 仓位、逐日遍历。

    每个交易日先处理离场，再按信号补仓（现金够、仓位未满才买）。
    净值按「现金 + 持仓市值」逐日计算，避免满仓单票的复利失真。
    """
    codes = list(data.keys())
    n_days = min(len(v) for v in data.values())
    offs = {c: len(data[c]) - n_days for c in codes}   # 右对齐到同一末端
    cash, holdings = 1.0, {}                            # holdings: code -> {shares, buy, day, cost}
    trades, equity = [], []

    for t in range(61, n_days):
        # 1) 离场
        for c in list(holdings.keys()):
            h = holdings[c]
            idx = offs[c] + t
            cur = data[c][idx]
            h["day"] += 1
            ret = (cur["c"] / h["buy"] - 1) * 100
            m10 = ma([x["c"] for x in data[c][:idx + 1]], 10)
            reason = None
            if ret <= -h.get("stop", 7.0):
                reason = "止损"
            elif ret >= h.get("take", 8.0):
                reason = "止盈"
            elif h["day"] >= 3 and m10 and cur["c"] < m10:
                reason = "破MA10"
            elif h["day"] >= max_hold:
                reason = "超时"
            if reason:
                gross = h["shares"] * cur["c"]
                fee = max(gross * fee_rate, 5e-5) + gross * stamp
                cash += gross - fee
                net_ret = ((gross - fee) / h["cost"] - 1) * 100
                trades.append({"code": c, "ret": round(net_ret, 2), "reason": reason, "days": h["day"]})
                holdings.pop(c)

        # 2) 补仓
        for c in codes:
            if len(holdings) >= max_pos or cash < pos_pct * 0.9:
                continue
            if c in holdings:
                continue
            idx = offs[c] + t
            if idx < 61:
                continue
            if signal(role, data[c], idx):
                px = data[c][idx]["c"]
                budget = min(cash, pos_pct)
                shares = budget / px
                fee = max(budget * fee_rate, 5e-5)
                cash -= (budget + fee)
                ap = atr_pct(data[c], idx)
                sl_mult = ROLE_SL_ATR.get(role, 2.0)
                stop_pct = max(3.0, min(12.0, sl_mult * ap))          # 3%~12% 夹住，防 ATR 极端
                take_pct = stop_pct * ROLE_RR.get(role, 2.0)
                holdings[c] = {"shares": shares, "buy": px, "day": 0, "cost": budget + fee,
                               "stop": stop_pct, "take": take_pct, "atr": round(ap, 2)}

        # 3) 记净值
        mv = sum(h["shares"] * data[c][offs[c] + t]["c"] for c, h in holdings.items())
        equity.append(cash + mv)

    if not equity:
        return {"n": 0, "win_rate": None, "avg": None, "total": 0.0, "maxdd": 0.0, "days": 0}
    peak, mdd = equity[0], 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    rets = [x["ret"] for x in trades]
    wins = [r for r in rets if r > 0]
    return {"n": len(trades),
            "win_rate": round(len(wins) / len(trades), 3) if trades else None,
            "avg": round(sum(rets) / len(rets), 2) if trades else None,
            "total": round((equity[-1] - 1) * 100, 2),
            "maxdd": round(mdd * 100, 2),
            "days": len(equity)}


def main():
    universe = load_universe()
    print(f"回测标的 {len(universe)} 只 · 每只 {BARS} 根日 K")
    data = {}
    for code in universe:
        k = fetch_kline(code)
        if len(k) >= 80:
            data[code] = k
    if not data:
        print("⚠ 没有可用 K 线，退出")
        return
    days = min(len(k) for k in data.values())
    print(f"可用 {len(data)} 只 · 最短 {days} 根")
    roles = {}
    for r in ["D1", "D2", "D3", "D4", "D5", "D6", "D7"]:
        res = run_role(r, data)
        roles[r] = {k: v for k, v in res.items() if k != "trades"}
        print(f"  {r}: {res['n']} 笔 · 胜率 {res['win_rate']} · 累计 {res['total']}% · 回撤 {res['maxdd']}%")
    out = {"generated_by": "bt_replay.py", "asof": time.strftime("%Y-%m-%d %H:%M"),
           "universe": len(data), "bars": days, "roles": roles,
           "note": "简化规则复刻，不含 AI 判断与滑点；单票满仓，扣双边费用"}
    with open("bt_result.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("💾 已生成 bt_result.json")


if __name__ == "__main__":
    main()
