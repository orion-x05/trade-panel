#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest.py —— 基于实盘日志的六角色参数 walk-forward 评估（回测闭环）

设计说明（诚实边界）：
- 引擎是实盘/日内引擎，无外部历史K线数据管道，完整K线重放不可行。
- 本脚本基于 data.json 中【已真实发生的 trades】（每笔买卖价、盈亏、触发原因、
  开仓市况），对每角色做【保守的参数敏感性评估】：
    · 止盈单(tp触发)：更紧的 tp 能更早锁定收益；更松的 tp 不改变已实现的（不假设更好）
    · 止损单(sl触发)：更紧的 sl 能少亏；更松的 sl 不改变已发生的（不假设更好）
    · 其他触发(跌破MA/KDJ死叉/放量下跌)：保持原 profit
- 网格搜索 (sl, tp) 组合，选历史累计净盈亏最高且胜率不过低者，生成 bt_tune.json。
- 单角色卖出样本 < MIN_SELLS 时跳过（保留出厂/上次值），不强行调参，防过拟合。

由 .github/workflows/trade.yml 的 backtest job 每周定时运行并提交 bt_tune.json，
把"面板回测→人工落盘"的半自动闭环升级为全自动。
"""

import json
import os

# 与 trade_engine.py 保持一致的出厂基准（防止单独运行缺常量）
ROLES_BASE = {
    "D1": {"sl": 7, "tp": 12}, "D2": {"sl": 6, "tp": 8},
    "D3": {"sl": 5, "tp": 6},  "D4": {"sl": 7, "tp": 10},
    "D5": {"sl": 6, "tp": 9},  "D6": {"sl": 8, "tp": 15},
    "D7": {"sl": 10, "tp": 25},
}
DATA_FILE = "data.json"
BT_TUNE_FILE = "bt_tune.json"
MIN_SELLS = 8           # 单角色最少卖出样本，低于则跳过
SL_MIN, SL_MAX = 3.0, 15.0
TP_MIN, TP_MAX = 4.0, 35.0


def load_data():
    if not os.path.exists(DATA_FILE):
        return None
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def is_tp(reason):
    return "止盈" in (reason or "") or "tp" in (reason or "").lower()


def is_sl(reason):
    return "止损" in (reason or "") or "移动止损" in (reason or "")


def eval_params(sells, sl, tp):
    """对某角色一组 (sl, tp) 重算假设累计净盈亏与胜率（保守反事实）"""
    total = 0.0
    wins = 0
    n = 0
    for s in sells:
        price = s.get("price", 0) or 0
        qty = s.get("qty", 0) or 0
        profit = s.get("profit", 0) or 0
        fee = s.get("fee", 0) or 0
        if price <= 0 or qty <= 0:
            continue
        cost = price * qty - profit - fee   # 反推成本基数
        if cost <= 0:
            continue
        r = profit / cost                   # 实际收益率
        if is_tp(s.get("reason", "")):
            new_r = min(r, tp / 100.0)       # 更紧 tp 锁定更早收益；更松不改变
        elif is_sl(s.get("reason", "")):
            new_r = max(r, -sl / 100.0)      # 更紧 sl 少亏；更松不改变已发生
        else:
            new_r = r                        # 其他触发保持原样
        hyp = new_r * cost
        total += hyp
        if hyp > 0:
            wins += 1
        n += 1
    wr = wins / n if n else 0.0
    return total, wr, n


def best_params(sells, base_sl, base_tp):
    best = None
    sl_lo = max(SL_MIN, round(base_sl - 3, 1))
    sl_hi = min(SL_MAX, round(base_sl + 1, 1))
    tp_lo = max(TP_MIN, round(base_tp - 4, 1))
    tp_hi = min(TP_MAX, round(base_tp + 8, 1))
    for sl in [round(x * 0.5, 1) for x in range(int(sl_lo * 2), int(sl_hi * 2) + 1)]:
        for tp in range(int(tp_lo), int(tp_hi) + 1):
            tot, wr, n = eval_params(sells, sl, tp)
            score = tot if wr >= 0.4 else tot * 0.5   # 胜率过低惩罚
            if best is None or score > best["score"]:
                best = {"sl": sl, "tp": tp, "score": score, "wr": wr, "n": n, "tot": tot}
    return best


def main():
    data = load_data()
    if not data:
        print(f"  ⚠️  无 {DATA_FILE}，跳过回测（新仓库/首次运行）")
        return
    players = data.get("players", [])
    existing = {}
    if os.path.exists(BT_TUNE_FILE):
        try:
            existing = json.load(open(BT_TUNE_FILE, "r", encoding="utf-8")).get("roles", {})
        except Exception:
            existing = {}

    roles_out = {}
    for p in players:
        rid = p.get("id")
        if rid not in ROLES_BASE:
            continue
        sells = [t for t in p.get("trades", []) if t.get("type") == "sell" and "profit" in t]
        base = ROLES_BASE[rid]
        if len(sells) < MIN_SELLS:
            print(f"  ⏭️  {rid} 卖出样本 {len(sells)}<{MIN_SELLS}，保留现有参数")
            if rid in existing:
                roles_out[rid] = existing[rid]
            continue
        best = best_params(sells, base["sl"], base["tp"])
        if not best:
            continue
        cap = 0.25 if best["wr"] > 0.55 else 0.20 if best["wr"] > 0.45 else 0.15
        rec = {"sl": best["sl"], "tp": best["tp"], "pos_cap": cap}
        if rid in existing and existing[rid].get("enabled") is False:
            rec["enabled"] = False   # 尊重手动暂停
        roles_out[rid] = rec
        print(f"  ✅ {rid} 回测推荐 sl={best['sl']}% tp={best['tp']}% cap={int(cap*100)}% "
              f"(样本{best['n']} 胜率{best['wr']:.0%} 假设净盈亏{best['tot']:.0f})")

    if not roles_out:
        print("  ℹ️  无角色达到回测样本阈值，未生成 bt_tune.json")
        return
    out = {"generated_by": "backtest.py", "roles": roles_out}
    with open(BT_TUNE_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"  💾 已生成 {BT_TUNE_FILE}（{len(roles_out)} 个角色）")


if __name__ == "__main__":
    main()
