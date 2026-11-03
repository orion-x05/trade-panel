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


def index_regime_map():
    """返回 {日期: '多'/'空'/'震'}，按上证指数收盘与 MA20/MA60 关系逐日判定。

    口径简单可解释：收盘在两条均线之上=多；之下=空；夹在中间=震。
    """
    k = fetch_kline("000001", BARS)
    if not k:
        return {}
    out = {}
    closes = [x["c"] for x in k]
    for i in range(60, len(k)):
        c = closes[i]
        m20 = sum(closes[i - 19:i + 1]) / 20
        m60 = sum(closes[i - 59:i + 1]) / 60
        out[k[i]["d"]] = "多" if c > m20 and c > m60 else ("空" if c < m20 and c < m60 else "震")
    return out


def hot_universe(days=45, limit=60):
    """从最近若干个交易日的涨停池里挑“最常涨停”的票，作为短线类策略的标的池。

    为什么需要：龙头首阴/龙头回踩/中军回踩都要求连板≥4，用大盘股池一笔都跑不出来。
    涨停池按日拉（每个交易日 1 次请求），按出现次数排序取前 N 只，控制请求量。
    """
    import datetime as _dt
    from collections import Counter
    hit = Counter()
    base = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).replace(tzinfo=None)
    ok_days = 0
    for k in range(days):
        d = base - _dt.timedelta(days=k)
        if d.weekday() >= 5:
            continue
        ds = d.strftime("%Y%m%d")
        url = ("https://push2ex.eastmoney.com/getTopicZTPool?ut=7eea3edcaed734bea9cbfc24409ed989"
               "&dpt=wz.ztzt&Pageindex=0&pagesize=100&sort=fbt:asc&date=" + ds)
        try:
            req_ = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            j = json.loads(urllib.request.urlopen(req_, timeout=12).read().decode("utf-8"))
            data_ = j.get("data") or {}
            qdate = str(data_.get("qdate") or "")
            if qdate and qdate < ds:      # 兜底返回了更早的数据，跳过该日
                continue
            pool = data_.get("pool") or []
            if not pool:
                continue
            ok_days += 1
            for s in pool:
                c = s.get("c")
                if c:
                    hit[c] += 1
        except Exception:
            continue
    if not hit:
        return []
    print(f"  历史涨停池：扫了 {ok_days} 个交易日，覆盖 {len(hit)} 只票")
    return [c for c, _ in hit.most_common(limit)]


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


def rr_at_entry(k, i):
    """开仓时的赔率：目标位取前 20 日最高收盘与 price+2ATR 的较大者，止损取 price-1.5ATR。"""
    if i < 25:
        return None
    closes = [x["c"] for x in k[:i + 1]]
    px = closes[-1]
    trs = []
    for j in range(i - 13, i + 1):
        h_, l_, pc_ = k[j]["h"], k[j]["l"], k[j - 1]["c"]
        trs.append(max(h_ - l_, abs(h_ - pc_), abs(l_ - pc_)))
    atr = sum(trs) / len(trs)
    hi20 = max(closes[-20:])
    target = max(hi20, px + 2 * atr)
    stop = px - 1.5 * atr
    if px - stop <= 0:
        return None
    return round((target - px) / (px - stop), 2)


def board_count(k, i):
    """从第 i 根往前数连续涨停天数（≥9.5% 视为涨停，主板口径近似）。"""
    n = 0
    j = i
    while j > 0:
        if k[j]["c"] / k[j - 1]["c"] - 1 >= 0.095:
            n += 1
            j -= 1
        else:
            break
    return n


DIP_TYPES = ["龙头首阴", "假阴真阳", "龙头回踩", "中军回踩", "后排二波"]


def dip_signal(name, k, i):
    """五类低吸位置的可量化复刻（只用 i 及之前的数据）。"""
    if i < 30:
        return False
    cur, prev = k[i], k[i - 1]
    closes = [x["c"] for x in k[:i + 1]]
    vols = [x["v"] for x in k[:i + 1]]
    m5, m10, m20 = ma(closes, 5), ma(closes, 10), ma(closes, 20)
    if None in (m10, m20):
        return False
    chg = (cur["c"] / prev["c"] - 1) * 100
    vr = cur["v"] / (sum(vols[-6:-1]) / 5) if len(vols) >= 6 and sum(vols[-6:-1]) else 0
    bc = board_count(k, i - 1)                     # 截至昨日的连板数
    zt_lows = [k[j]["l"] for j in range(max(0, i - 10), i)
               if j > 0 and k[j]["c"] / k[j - 1]["c"] - 1 >= 0.095]

    if name == "龙头首阴":
        return bc >= 4 and cur["c"] < cur["o"] and chg < 0 and vr < 1.5
    if name == "假阴真阳":
        return cur["c"] < cur["o"] and chg > 0 and vr >= 1.5
    if name == "龙头回踩":
        return bc >= 4 and (abs(cur["c"] / m10 - 1) <= 0.025 or abs(cur["c"] / m20 - 1) <= 0.025)
    if name == "中军回踩":
        return 2 <= bc <= 3 and abs(cur["c"] / m10 - 1) <= 0.025
    if name == "后排二波":
        return 1 <= bc <= 2 and bool(zt_lows) and cur["l"] >= min(zt_lows) * 0.99 and cur["c"] > cur["o"]
    return False


def signal(role, k, i):
    """第 i 根 K 线的开仓信号（只用 i 及之前的数据，防未来函数）。"""
    if role in DIP_TYPES:
        return dip_signal(role, k, i)
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
             max_hold=20, max_pos=5, pos_pct=0.2, stop_mult=1.0, regime_map=None):
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
                trades.append({"code": c, "ret": round(net_ret, 2), "reason": reason,
                               "days": h["day"], "regime": h.get("regime"), "rr": h.get("rr")})
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
                rr_in = rr_at_entry(data[c], idx)
                holdings[c] = {"shares": shares, "buy": px, "day": 0, "cost": budget + fee, "rr": rr_in,
                               "stop": stop_pct * stop_mult, "take": take_pct * stop_mult,
                               "atr": round(ap, 2),
                               "regime": (regime_map or {}).get(data[c][idx]["d"], "?")}

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
    by_reg = {}
    for t in trades:
        g = t.get("regime") or "?"
        d = by_reg.setdefault(g, {"n": 0, "win": 0, "sum": 0.0})
        d["n"] += 1
        d["win"] += 1 if t["ret"] > 0 else 0
        d["sum"] = round(d["sum"] + t["ret"], 2)
    for g, d in by_reg.items():
        d["avg"] = round(d["sum"] / d["n"], 2) if d["n"] else None
        d["win_rate"] = round(d["win"] / d["n"], 3) if d["n"] else None
    # 赔率分档：高赔率到底好不好
    buckets = {"<2": {"n": 0, "win": 0, "sum": 0.0}, "2-3": {"n": 0, "win": 0, "sum": 0.0},
               "3-4": {"n": 0, "win": 0, "sum": 0.0}, ">4": {"n": 0, "win": 0, "sum": 0.0}}
    for t in trades:
        rr = t.get("rr")
        if rr is None:
            continue
        key = "<2" if rr < 2 else ("2-3" if rr < 3 else ("3-4" if rr < 4 else ">4"))
        b = buckets[key]
        b["n"] += 1
        b["win"] += 1 if t["ret"] > 0 else 0
        b["sum"] = round(b["sum"] + t["ret"], 2)
    for k2, b in buckets.items():
        b["avg"] = round(b["sum"] / b["n"], 2) if b["n"] else None
        b["win_rate"] = round(b["win"] / b["n"], 3) if b["n"] else None
    return {"n": len(trades), "regime": by_reg, "rr_buckets": buckets,
            "win_rate": round(len(wins) / len(trades), 3) if trades else None,
            "avg": round(sum(rets) / len(rets), 2) if trades else None,
            "total": round((equity[-1] - 1) * 100, 2),
            "maxdd": round(mdd * 100, 2),
            "days": len(equity)}


def main():
    regime_map = index_regime_map()
    print(f"大盘环境覆盖 {len(regime_map)} 个交易日")

    pools = {}
    hot = hot_universe()
    if hot:
        pools["涨停热票"] = hot
    big = load_universe()
    if big:
        pools["大盘股"] = big
    if hot and big:
        pools["合并"] = sorted(set(hot) | set(big))[:110]
    if not pools:
        print("⚠ 没有可用标的池")
        return

    all_data = {}
    for name, codes in pools.items():
        data = {}
        for code in codes:
            if code in all_data:
                k = all_data[code]
            else:
                k = fetch_kline(code)
                all_data[code] = k
            if len(k) >= 80:
                data[code] = k
        pools[name] = data
        print(f"池 {name}：{len(data)} 只有效 K 线")

    ROLES_ALL = ["D1", "D2", "D3", "D4", "D5", "D6", "D7"] + DIP_TYPES
    out = {"generated_by": "bt_replay.py", "asof": time.strftime("%Y-%m-%d %H:%M"),
           "bars": BARS, "pools": {}, "sensitivity": {}, "regime_days": len(regime_map),
           "note": "简化规则复刻，不含 AI 判断与滑点；组合口径最多5只/单只20%；止损止盈按 ATR 动态"}

    for name, data in pools.items():
        if not data:
            continue
        res = {}
        print(f"\n== 池 {name}（{len(data)} 只）==")
        for r in ROLES_ALL:
            v = run_role(r, data, regime_map=regime_map)
            res[r] = v
            wr = "—" if v["win_rate"] is None else f"{v['win_rate']:.0%}"
            print(f"  {r}: {v['n']:3} 笔 · 胜率 {wr} · 组合 {v['total']:>7}% · 回撤 {v['maxdd']}%")
            reg = v.get("regime") or {}
            if reg:
                print("      环境分层: " + " · ".join(
                    f"{g} {d['n']}笔 均{d['avg']}%" for g, d in sorted(reg.items()) if d.get('n')))
        out["pools"][name] = {"universe": len(data), "roles": res}

    # 参数敏感性：止损止盈整体 ×0.8 / ×1.0 / ×1.2，看结论会不会翻
    sens = {}
    focus = ["D2", "D4", "D7"] + DIP_TYPES
    for name, data in pools.items():
        if not data:
            continue
        for r in focus:
            row = {}
            for m in (0.8, 1.0, 1.2):
                v = run_role(r, data, regime_map=regime_map, stop_mult=m)
                row[f"x{m}"] = v["total"]
            sens.setdefault(r, {})[name] = row
    out["sensitivity"] = sens

    with open("bt_result.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n💾 已生成 bt_result.json（含分池 / 环境分层 / 参数敏感性）")


if __name__ == "__main__":
    main()
