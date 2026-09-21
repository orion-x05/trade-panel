#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI六角色模拟交易 - GitHub Actions后台自动运行版（优化版）
优化项：大盘择时 + 连续亏损熔断 + 同板块限制 + 收盘总结推送 + 数据源备份
"""
import json
import os
import math
import requests
from datetime import datetime, timedelta

# ==================== 配置 ====================
INIT_CASH = 100000
MAX_POS = 5
WECHAT_PUSH_KEY = "SCT419380Tp3TtzFORow9mBwTVWfBbZOu7"

ROLES = {
    "D1": {"name": "破锋", "sl": 7, "tp": 12},
    "D2": {"name": "衡", "sl": 6, "tp": 8},
    "D3": {"name": "镜", "sl": 5, "tp": 6},
    "D4": {"name": "啸", "sl": 7, "tp": 10},
    "D5": {"name": "渊", "sl": 6, "tp": 9},
    "D6": {"name": "焰", "sl": 8, "tp": 15},
}

STOCK_POOL = [
    "600519", "000858", "601318", "600036", "000001",
    "601012", "002594", "300750", "600900", "601899",
    "000333", "000651", "600276", "300015", "603259",
    "600887", "000568", "000596", "600809", "002304",
    "601888", "600030", "601211", "000776", "600999",
    "601668", "601390", "601186", "600585", "000895",
    "600009", "601111", "600029", "601021", "002120",
    "300059", "601519", "002230", "300496", "600588",
]

STOCK_INDUSTRY = {
    "600519": "白酒", "000858": "白酒", "000568": "白酒", "000596": "白酒", "600809": "白酒", "002304": "白酒",
    "601318": "保险", "600036": "银行", "000001": "银行",
    "601012": "光伏", "300750": "新能源", "002594": "新能源",
    "600900": "电力", "601899": "有色",
    "000333": "家电", "000651": "家电",
    "600276": "医药", "300015": "医药", "603259": "医药",
    "600887": "食品", "000895": "食品", "601888": "免税",
    "600030": "券商", "601211": "券商", "000776": "券商", "600999": "券商", "300059": "券商",
    "601668": "建筑", "601390": "建筑", "601186": "建筑", "600585": "建材",
    "600009": "机场", "601111": "航空", "600029": "航空", "601021": "航空",
    "002120": "物流", "601519": "金融科技", "002230": "AI", "300496": "AI", "600588": "软件",
}

# ==================== 工具函数 ====================
def load_data():
    if os.path.exists("data.json"):
        with open("data.json", "r", encoding="utf-8") as f:
            return json.load(f)
    data = {"players": [], "last_update": datetime.now().isoformat(),
            "consecutive_losses": {}, "market_trend": "neutral"}
    for role_id, role in ROLES.items():
        data["players"].append({
            "id": role_id, "name": role["name"],
            "cash": INIT_CASH, "holdings": [], "trades": [],
            "asset_history": [INIT_CASH]
        })
        data["consecutive_losses"][role_id] = 0
    return data

def save_data(data):
    data["last_update"] = datetime.now().isoformat()
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def is_trade_time():
    now = datetime.now()
    if now.weekday() >= 5: return False
    hm = now.hour * 100 + now.minute
    return (930 <= hm <= 1130) or (1300 <= hm <= 1500)

def is_close_time():
    now = datetime.now()
    if now.weekday() >= 5: return False
    hm = now.hour * 100 + now.minute
    return 1500 <= hm <= 1510

def get_market_trend():
    """【大盘择时】上证指数趋势判断"""
    try:
        url = "https://qt.gtimg.cn/q=sh000001"
        resp = requests.get(url, timeout=10)
        resp.encoding = "gbk"
        arr = resp.text.split("~")
        if len(arr) < 40: return "neutral"
        price = float(arr[3])
        
        kline_url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh000001,day,,,30,qfq"
        kresp = requests.get(kline_url, timeout=10)
        kdata = kresp.json()
        klines = kdata.get("data", {}).get("sh000001", {}).get("qfqday") or \
                 kdata.get("data", {}).get("sh000001", {}).get("day") or []
        closes = [float(k[2]) for k in klines[-30:]] if klines else []
        if len(closes) < 20: return "neutral"
        
        ma20 = sum(closes[-20:]) / 20
        ma5 = sum(closes[-5:]) / 5
        if price > ma20 and ma5 > ma20: return "bull"
        elif price < ma20 and ma5 < ma20: return "bear"
        else: return "neutral"
    except Exception as e:
        print(f"  大盘趋势获取失败: {e}")
        return "neutral"

def get_sector_strength(all_stock_data):
    """【板块轮动】计算各板块平均涨幅，返回强势板块排序"""
    sector_chg = {}
    for code, st in all_stock_data.items():
        industry = STOCK_INDUSTRY.get(code, "其他")
        if industry not in sector_chg:
            sector_chg[industry] = []
        sector_chg[industry].append(st["chg"])
    # 计算各板块平均涨幅
    sector_avg = {}
    for industry, chgs in sector_chg.items():
        if len(chgs) >= 2:  # 至少2只股票才算
            sector_avg[industry] = sum(chgs) / len(chgs)
    # 按涨幅排序，返回强势板块列表
    sorted_sectors = sorted(sector_avg.items(), key=lambda x: x[1], reverse=True)
    return [s[0] for s in sorted_sectors[:5]]  # 返回前5个强势板块

def get_stock_info_tx(code):
    """【数据源1】腾讯行情"""
    try:
        prefix = "sh" if code.startswith("6") else "sz"
        url = f"https://qt.gtimg.cn/q={prefix}{code}"
        resp = requests.get(url, timeout=8)
        resp.encoding = "gbk"
        arr = resp.text.split("~")
        if len(arr) < 40 or float(arr[3]) <= 0: return None
        return {"code": code, "name": arr[1], "price": float(arr[3]),
                "prev_close": float(arr[4]), "chg": float(arr[32]) or 0,
                "open": float(arr[5]), "high": float(arr[33]), "low": float(arr[34]),
                "volume": float(arr[6]), "turnover": float(arr[38]),
                "vol_ratio": float(arr[49]) if len(arr) > 49 and arr[49] else 1}
    except: return None

def get_stock_info_em(code):
    """【数据源2-备份】东方财富行情"""
    try:
        secid = f"1.{code}" if code.startswith("6") else f"0.{code}"
        url = f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields=f43,f44,f45,f46,f47,f48,f50,f51,f52,f55,f57,f58,f60,f168,f170"
        resp = requests.get(url, timeout=8)
        d = resp.json().get("data", {})
        if not d or not d.get("f43"): return None
        price = d["f43"] / 100
        prev_close = d["f60"] / 100
        return {"code": code, "name": d.get("f58", code), "price": price,
                "prev_close": prev_close, "chg": d.get("f170", 0) / 100,
                "open": d.get("f46", 0) / 100, "high": d.get("f44", 0) / 100,
                "low": d.get("f45", 0) / 100, "volume": d.get("f47", 0),
                "turnover": d.get("f168", 0), "vol_ratio": d.get("f50", 100) / 100}
    except: return None

def get_stock_info(code):
    """获取股票行情（主源腾讯，备份东方财富）+ 技术指标"""
    info = get_stock_info_tx(code) or get_stock_info_em(code)
    if not info: return None
    
    try:
        prefix = "sh" if code.startswith("6") else "sz"
        kline_url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{code},day,,,60,qfq"
        kresp = requests.get(kline_url, timeout=8)
        kdata = kresp.json()
        klines = kdata.get("data", {}).get(f"{prefix}{code}", {}).get("qfqday") or \
                 kdata.get("data", {}).get(f"{prefix}{code}", {}).get("day") or []
        closes = [float(k[2]) for k in klines[-60:]] if klines else []
        price = info["price"]
        ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else price
        ma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else price
        ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else price
        
        rsi6 = 50
        if len(closes) >= 7:
            gains, losses = [], []
            for i in range(-6, 0):
                diff = closes[i] - closes[i-1]
                if diff > 0: gains.append(diff)
                else: losses.append(abs(diff))
            avg_gain = sum(gains) / 6 if gains else 0
            avg_loss = sum(losses) / 6 if losses else 0.001
            rsi6 = 100 - 100 / (1 + avg_gain / avg_loss)
        
        k, d, j = 50, 50, 50
        if len(closes) >= 9:
            highs = [float(kline[3]) for kline in klines[-9:]]
            lows = [float(kline[4]) for kline in klines[-9:]]
            rsv = (price - min(lows)) / (max(highs) - min(lows) + 0.001) * 100
            k = 2/3 * 50 + 1/3 * rsv
            d = 2/3 * 50 + 1/3 * k
            j = 3 * k - 2 * d
        
        info.update({"ma5": ma5, "ma10": ma10, "ma20": ma20,
                     "rsi6": rsi6, "kdj_k": k, "kdj_d": d, "kdj_j": j})
    except:
        info.update({"ma5": info["price"], "ma10": info["price"], "ma20": info["price"],
                     "rsi6": 50, "kdj_k": 50, "kdj_d": 50, "kdj_j": 50})
    return info

def send_wechat(title, content):
    try:
        url = f"https://sctapi.ftqq.com/{WECHAT_PUSH_KEY}.send"
        requests.post(url, data={"title": title, "desp": content}, timeout=10)
        print(f"  推送: {title}")
    except Exception as e:
        print(f"  推送失败: {e}")

def send_daily_summary(data, market_trend):
    """【每日收盘总结推送】"""
    now = datetime.now()
    title = f"📊 每日收盘总结 {now.strftime('%m-%d')} 大盘:{('多头' if market_trend=='bull' else '空头' if market_trend=='bear' else '震荡')}"
    
    content = "## 各角色战绩\n\n| 角色 | 总资产 | 总收益 | 持仓数 |\n|------|--------|--------|--------|\n"
    sorted_players = sorted(data["players"], key=lambda p: p.get("asset", INIT_CASH), reverse=True)
    for i, player in enumerate(sorted_players):
        asset = player.get("asset", INIT_CASH)
        total_ret = (asset - INIT_CASH) / INIT_CASH * 100
        medal = "🥇" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else "  "
        content += f"| {medal}{player['id']} {player['name']} | {asset:.0f} | {total_ret:+.2f}% | {len(player['holdings'])} |\n"
    
    today_str = now.strftime("%Y-%m-%d")
    today_trades = []
    for player in data["players"]:
        for t in player.get("trades", []):
            if t.get("time", "").startswith(today_str):
                today_trades.append((player["id"], t))
    
    if today_trades:
        content += "\n## 今日交易\n\n"
        for role_id, t in today_trades:
            if t["type"] == "buy":
                content += f"- 📈 {role_id} 买入 {t['name']} {t['qty']}股 @{t['price']:.2f}\n"
            else:
                pnl = t.get("profit", 0)
                content += f"- 📉 {role_id} 卖出 {t['name']} {t['qty']}股 @{t['price']:.2f} 盈亏{pnl:+.2f}\n"
    
    content += "\n## 明日计划\n\n"
    if market_trend == "bear":
        content += "⚠️ 大盘空头趋势，明日建议降低仓位，谨慎操作\n"
    elif market_trend == "bull":
        content += "✅ 大盘多头趋势，明日可积极操作\n"
    else:
        content += "🔄 大盘震荡，明日建议高抛低吸，控制仓位\n"
    
    send_wechat(title, content)
    print("  ✅ 每日收盘总结已推送")

# ==================== 交易逻辑 ====================
def can_sell_t1(hold):
    return hold["buyTime"][:10] != datetime.now().strftime("%Y-%m-%d")

def check_sell_signals(player, stock_data):
    signals = []
    role = ROLES[player["id"]]
    for hold in player["holdings"]:
        if hold["code"] not in stock_data or not can_sell_t1(hold): continue
        st = stock_data[hold["code"]]
        chg = (st["price"] - hold["buyPrice"]) / hold["buyPrice"] * 100
        if chg <= -role["sl"]: signals.append((hold, f"止损-{abs(chg):.1f}%", 1.0)); continue
        if chg >= role["tp"]: signals.append((hold, f"止盈+{chg:.1f}%", 1.0)); continue
        if st["price"] < st["ma20"] and st["ma5"] < st["ma10"]: signals.append((hold, "跌破MA20+均线走弱", 1.0)); continue
        if st["kdj_k"] < st["kdj_d"] and st["kdj_k"] > 70: signals.append((hold, "KDJ高位死叉", 1.0)); continue
        if st["vol_ratio"] > 1.5 and st["chg"] < -2: signals.append((hold, "放量下跌主力出逃", 1.0)); continue
    return signals

def check_buy_signals(player, stock_data, all_codes_data, market_trend, consecutive_losses, strong_sectors):
    role_id = player["id"]
    # 【连续亏损熔断】连续3笔亏损，冷却
    if consecutive_losses.get(role_id, 0) >= 3:
        print(f"  ⏸️  {role_id} 连续亏损{consecutive_losses[role_id]}笔，熔断冷却中")
        return None
    # 【大盘择时】空头市场，最多2只持仓（D6除外）
    if market_trend == "bear" and role_id != "D6" and len(player["holdings"]) >= 2:
        return None
    if len(player["holdings"]) >= MAX_POS: return None
    
    candidates = []
    for code, st in all_codes_data.items():
        if any(h["code"] == code for h in player["holdings"]): continue
        if st["chg"] >= 9.8 or st["chg"] <= -9.8: continue
        if "ST" in st["name"]: continue
        # 【同板块持仓限制】同一板块最多2只
        industry = STOCK_INDUSTRY.get(code, "其他")
        same_count = sum(1 for h in player["holdings"] if STOCK_INDUSTRY.get(h["code"], "其他") == industry)
        if same_count >= 2: continue
        
        score = 0
        reason_parts = []
        # 【板块轮动】强势板块加分（前5名强势板块+10分）
        if industry in strong_sectors:
            score += 10
            reason_parts.append(f"{industry}板块强势")
        if role_id == "D1":
            if st["price"] > st["ma20"] and st["ma5"] > st["ma10"] > st["ma20"]: score += 30; reason_parts.append("均线多头")
            if st["chg"] > 3 and st["vol_ratio"] > 1.2: score += 25; reason_parts.append("放量突破")
            if 55 < st["rsi6"] < 75: score += 20; reason_parts.append("RSI健康")
            if st["kdj_k"] > st["kdj_d"]: score += 15; reason_parts.append("KDJ金叉")
        elif role_id == "D2":
            if st["price"] > st["ma20"]: score += 15
            if st["ma5"] > st["ma10"]: score += 15
            if 45 < st["rsi6"] < 70: score += 20; reason_parts.append("RSI健康")
            if st["kdj_k"] > st["kdj_d"] and st["kdj_k"] < 70: score += 20; reason_parts.append("KDJ金叉")
            if 0.8 < st["vol_ratio"] < 2.5: score += 15; reason_parts.append("量比适中")
            if st["chg"] > 0: score += 15
        elif role_id == "D3":
            if st["price"] > st["ma20"]: score += 15
            if 50 < st["rsi6"] < 60: score += 30; reason_parts.append("RSI完美")
            if st["kdj_k"] > st["kdj_d"] and 40 < st["kdj_k"] < 60: score += 30; reason_parts.append("KDJ中位金叉")
            if 0 < st["chg"] < 3: score += 15
        elif role_id == "D4":
            if st["vol_ratio"] > 1.3 and st["chg"] > 1: score += 35; reason_parts.append("量价齐升")
            if st["price"] > st["ma5"] > st["ma10"]: score += 25; reason_parts.append("短期多头")
            if st["kdj_k"] > st["kdj_d"]: score += 20
            if st["rsi6"] > 50: score += 10
        elif role_id == "D5":
            if st["ma5"] > st["ma10"] > st["ma20"]: score += 25; reason_parts.append("中期上涨")
            if abs(st["price"] - st["ma10"]) / st["ma10"] < 0.03: score += 30; reason_parts.append("回踩MA10")
            if -1 < st["chg"] < 2: score += 15
            if st["kdj_k"] > st["kdj_d"] and st["kdj_k"] < 65: score += 20
        elif role_id == "D6":
            if st["chg"] > 5: score += 30; reason_parts.append("强势上涨")
            if st["vol_ratio"] > 1.5: score += 25; reason_parts.append("放量")
            if st["price"] > st["ma5"]: score += 20
            if st["rsi6"] > 60: score += 15
        
        if score >= 60:
            candidates.append((code, st, score, " + ".join(reason_parts) or "综合评分达标"))
    
    if not candidates: return None
    candidates.sort(key=lambda x: x[2], reverse=True)
    return candidates[0]

def do_buy(player, code, st, score, reason):
    """执行买入（含仓位动态调整：胜率高的角色加仓，胜率低的减仓）"""
    # 【仓位动态调整】根据历史胜率调整单笔仓位比例
    trades = player.get("trades", [])
    sells = [t for t in trades if t["type"] == "sell"]
    wins = [t for t in sells if t.get("profit", 0) > 0]
    win_rate = len(wins) / len(sells) if sells else 0.5
    # 胜率>60%：仓位提升到25%；胜率<40%：仓位降低到15%；其他：20%
    if win_rate > 0.6:
        position_ratio = 0.25
    elif win_rate < 0.4:
        position_ratio = 0.15
    else:
        position_ratio = 0.20
    budget = player["cash"] * position_ratio
    qty = int(budget / st["price"] / 100) * 100
    if qty < 100:
        print(f"  {player['id']} 资金不足，跳过 {st['name']}")
        return
    amount = qty * st["price"]
    fee = max(amount * 0.00025, 5)
    cost_per = (amount + fee) / qty
    player["cash"] -= (amount + fee)
    player["holdings"].append({
        "code": code, "name": st["name"], "qty": qty,
        "buyPrice": cost_per, "buyTime": datetime.now().isoformat(),
        "reason": reason, "score": score
    })
    player["trades"].append({
        "type": "buy", "code": code, "name": st["name"],
        "qty": qty, "price": st["price"], "fee": fee,
        "time": datetime.now().isoformat(), "reason": reason
    })
    push_key = f".push_{player['id']}_{code}_buy_{datetime.now().strftime('%Y%m%d')}"
    if not os.path.exists(push_key):
        open(push_key, "w").close()
        send_wechat(
            f"📈 {player['id']} 建仓 {st['name']}",
            f"股票：{st['name']}({code})\n数量：{qty}股\n价格：{st['price']:.2f}元\n成本：{cost_per:.2f}元\n评分：{score}分\n原因：{reason}\n⏰ T+1：明日可卖出\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
    print(f"  ✅ {player['id']} 买入 {st['name']} {qty}股 @{st['price']:.2f}")

def do_sell(player, hold, st, reason, data, ratio=1.0):
    sell_qty = hold["qty"] if ratio >= 1 else int(hold["qty"] * ratio / 100) * 100
    sell_qty = min(sell_qty, hold["qty"])
    if sell_qty < 100: return
    money = sell_qty * st["price"]
    fee = max(money * 0.00025, 5)
    stamp = money * 0.001
    total_fee = fee + stamp
    player["cash"] += money - total_fee
    profit = money - hold["buyPrice"] * sell_qty - total_fee
    player["trades"].append({
        "type": "sell", "code": hold["code"], "name": hold["name"],
        "qty": sell_qty, "price": st["price"], "fee": total_fee,
        "profit": profit, "time": datetime.now().isoformat(), "reason": reason
    })
    # 【连续亏损熔断】更新计数
    if profit < 0:
        data["consecutive_losses"][player["id"]] = data["consecutive_losses"].get(player["id"], 0) + 1
    else:
        data["consecutive_losses"][player["id"]] = 0
    
    hold["qty"] -= sell_qty
    if hold["qty"] <= 0:
        player["holdings"] = [h for h in player["holdings"] if h["code"] != hold["code"]]
    push_key = f".push_{player['id']}_{hold['code']}_sell_{datetime.now().strftime('%Y%m%d')}"
    if not os.path.exists(push_key):
        open(push_key, "w").close()
        send_wechat(
            f"📉 {player['id']} 清仓 {hold['name']} {'盈利' if profit>=0 else '亏损'}{abs(profit):.2f}元",
            f"股票：{hold['name']}({hold['code']})\n数量：{sell_qty}股\n价格：{st['price']:.2f}元\n盈亏：{profit:.2f}元\n收益率：{profit/(hold['buyPrice']*sell_qty)*100:.2f}%\n原因：{reason}\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
    print(f"  ✅ {player['id']} 卖出 {hold['name']} {sell_qty}股 @{st['price']:.2f} 盈亏{profit:.2f}")

# ==================== 主函数 ====================
def main():
    print(f"{'='*50}")
    print(f"  AI六角色交易引擎启动 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}")
    
    data = load_data()
    market_trend = get_market_trend()
    data["market_trend"] = market_trend
    trend_text = "多头📈" if market_trend == "bull" else "空头📉" if market_trend == "bear" else "震荡🔄"
    print(f"\n🌐 大盘趋势：{trend_text}")
    
    # 【每日收盘总结】
    if is_close_time():
        print("\n📊 收盘时间，发送每日总结...")
        all_codes = set()
        for player in data["players"]:
            for hold in player["holdings"]:
                all_codes.add(hold["code"])
        all_stock_data = {}
        for code in all_codes:
            info = get_stock_info(code)
            if info: all_stock_data[code] = info
        for player in data["players"]:
            total_value = player["cash"]
            for hold in player["holdings"]:
                if hold["code"] in all_stock_data:
                    total_value += all_stock_data[hold["code"]]["price"] * hold["qty"]
            player["asset"] = total_value
        send_daily_summary(data, market_trend)
        save_data(data)
        return
    
    if not is_trade_time():
        print("  ⏸️  非交易时间，退出")
        return
    
    all_codes = set(STOCK_POOL)
    for player in data["players"]:
        for hold in player["holdings"]:
            all_codes.add(hold["code"])
    
    print(f"\n📊 正在获取 {len(all_codes)} 只股票行情...")
    all_stock_data = {}
    for i, code in enumerate(all_codes):
        info = get_stock_info(code)
        if info: all_stock_data[code] = info
        if (i + 1) % 20 == 0:
            print(f"  已获取 {i+1}/{len(all_codes)}")
    print(f"  ✅ 成功获取 {len(all_stock_data)} 只股票行情")
    
    # 【板块轮动】计算强势板块
    strong_sectors = get_sector_strength(all_stock_data)
    print(f"  🔥 强势板块：{'、'.join(strong_sectors)}")
    
    for player in data["players"]:
        print(f"\n🎯 {player['id']} {player['name']} (现金:{player['cash']:.0f} 持仓:{len(player['holdings'])})")
        sell_signals = check_sell_signals(player, all_stock_data)
        for hold, reason, ratio in sell_signals:
            if hold["code"] in all_stock_data:
                do_sell(player, hold, all_stock_data[hold["code"]], reason, data, ratio)
        buy_signal = check_buy_signals(player, all_stock_data, all_stock_data,
                                        market_trend, data.get("consecutive_losses", {}), strong_sectors)
        if buy_signal:
            code, st, score, reason = buy_signal
            do_buy(player, code, st, score, reason)
    
    print(f"\n💰 更新资产...")
    for player in data["players"]:
        total_value = player["cash"]
        for hold in player["holdings"]:
            if hold["code"] in all_stock_data:
                total_value += all_stock_data[hold["code"]]["price"] * hold["qty"]
        player["asset"] = total_value
        player["asset_history"].append(total_value)
        if len(player["asset_history"]) > 252:
            player["asset_history"] = player["asset_history"][-252:]
        total_return = (total_value - INIT_CASH) / INIT_CASH * 100
        print(f"  {player['id']}: 总资产{total_value:.0f} 收益{total_return:+.2f}%")
    
    save_data(data)
    print(f"\n{'='*50}")
    print(f"  ✅ 交易引擎完成，数据已保存")
    print(f"{'='*50}")

if __name__ == "__main__":
    main()
