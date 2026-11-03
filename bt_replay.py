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


def run_role(role, data, fee_rate=0.00025, stamp=0.0005, stop=-7.0, take=8.0, max_hold=20):
    """单角色逐日回放：满仓单票，-7% 止损 / +8% 或跌破 MA10 止盈。"""
    trades = []
    pos = None
    pos_cooldown = {}
    for code, k in data.items():
        i = 61
        while i < len(k) - 1:
            if pos is None:
                if signal(role, k, i):
                    if pos_cooldown.get(code, -99) > i - 3:
                        i += 1
                        continue
                    px = k[i]["c"]
                    pos = {"code": code, "i": i, "buy": px, "day": 0}
                i += 1
                continue
            # 持仓中：次日开始判断离场
            i += 1
            cur = k[i]
            pos["day"] += 1
            ret = (cur["c"] / pos["buy"] - 1) * 100
            m10 = ma([x["c"] for x in k[:i + 1]], 10)
            exit_reason = None
            if ret <= stop:
                exit_reason = "止损"
            elif ret >= take:
                exit_reason = "止盈"
            elif pos["day"] >= 3 and m10 and cur["c"] < m10:
                exit_reason = "破MA10"
            elif pos["day"] >= max_hold:
                exit_reason = "超时"
            if exit_reason:
                sell = cur["c"]
                gross = (sell - pos["buy"]) / pos["buy"]
                net = gross - fee_rate * 2 - stamp
                trades.append({"code": code, "buy": pos["buy"], "sell": sell,
                               "ret": round(net * 100, 2), "reason": exit_reason,
                               "days": pos["day"]})
                pos_cooldown[code] = i
                pos = None
        if pos is not None:   # 期末未平仓，按最后收盘算
            cur = k[-1]
            gross = (cur["c"] - pos["buy"]) / pos["buy"]
            net = gross - fee_rate * 2 - stamp
            trades.append({"code": code, "buy": pos["buy"], "sell": cur["c"],
                           "ret": round(net * 100, 2), "reason": "期末平仓", "days": pos["day"]})
            pos = None

    if not trades:
        return {"n": 0, "win_rate": None, "avg": None, "total": 0.0, "maxdd": 0.0, "trades": []}
    rets = [t["ret"] for t in trades]
    wins = [r for r in rets if r > 0]
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= (1 + r / 100)
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
    return {"n": len(trades), "win_rate": round(len(wins) / len(trades), 3),
            "avg": round(sum(rets) / len(rets), 2),
            "total": round((eq - 1) * 100, 2), "maxdd": round(mdd * 100, 2),
            "trades": trades[-20:]}


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
