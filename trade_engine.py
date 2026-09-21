#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI六角色模拟交易 - GitHub Actions后台自动运行版
每天交易时间自动运行，结果存到data.json，面板读取显示
"""
import json
import os
import math
import requests
from datetime import datetime

# ==================== 配置 ====================
INIT_CASH = 100000  # 每角色初始资金
MAX_POS = 5  # 每角色最多持仓数
WECHAT_PUSH_KEY = "SCT419380Tp3TtzFORow9mBwTVWfBbZOu7"  # Server酱推送KEY

# 六角色参数（止损/止盈百分比）
ROLES = {
    "D1": {"name": "破锋", "sl": 7, "tp": 12},   # 趋势突破：宽止损宽止盈
    "D2": {"name": "衡", "sl": 6, "tp": 8},       # 多因子量化：紧止损紧止盈
    "D3": {"name": "镜", "sl": 5, "tp": 6},       # 多空辩论：最紧
    "D4": {"name": "啸", "sl": 7, "tp": 10},      # 量价共振
    "D5": {"name": "渊", "sl": 6, "tp": 9},       # 趋势回踩
    "D6": {"name": "焰", "sl": 8, "tp": 15},      # 涨停回踩：最宽
}

# 候选股票池（各行业龙头，简化版）
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

# ==================== 工具函数 ====================
def load_data():
    """加载持仓数据"""
    if os.path.exists("data.json"):
        with open("data.json", "r", encoding="utf-8") as f:
            return json.load(f)
    # 初始化数据
    data = {"players": [], "last_update": datetime.now().isoformat()}
    for role_id, role in ROLES.items():
        data["players"].append({
            "id": role_id, "name": role["name"],
            "cash": INIT_CASH, "holdings": [], "trades": [],
            "asset_history": [INIT_CASH]
        })
    return data

def save_data(data):
    """保存持仓数据"""
    data["last_update"] = datetime.now().isoformat()
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def is_trade_time():
    """判断是否在交易时间（北京时间）"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 100 + now.minute
    return (930 <= hm <= 1130) or (1300 <= hm <= 1500)

def get_stock_info(code):
    """获取股票实时行情+近期K线（腾讯接口）"""
    try:
        prefix = "sh" if code.startswith("6") else "sz"
        # 实时行情
        url = f"https://qt.gtimg.cn/q={prefix}{code}"
        resp = requests.get(url, timeout=10)
        resp.encoding = "gbk"
        arr = resp.text.split("~")
        if len(arr) < 40:
            return None
        price = float(arr[3])
        prev_close = float(arr[4])
        if price <= 0:
            return None
        # 日K线（近60天）
        kline_url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{code},day,,,60,qfq"
        kresp = requests.get(kline_url, timeout=10)
        kdata = kresp.json()
        klines = kdata.get("data", {}).get(f"{prefix}{code}", {}).get("qfqday") or \
                 kdata.get("data", {}).get(f"{prefix}{code}", {}).get("day") or []
        closes = [float(k[2]) for k in klines[-60:]] if klines else []
        # 计算均线
        ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else price
        ma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else price
        ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else price
        # 计算RSI6
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
        # 计算KDJ
        k, d, j = 50, 50, 50
        if len(closes) >= 9:
            highs = [float(k[3]) for k in klines[-9:]]
            lows = [float(k[4]) for k in klines[-9:]]
            rsv = (price - min(lows)) / (max(highs) - min(lows) + 0.001) * 100
            k = 2/3 * 50 + 1/3 * rsv
            d = 2/3 * 50 + 1/3 * k
            j = 3 * k - 2 * d
        return {
            "code": code, "name": arr[1], "price": price,
            "prev_close": prev_close,
            "chg": float(arr[32]) or ((price - prev_close) / prev_close * 100),
            "open": float(arr[5]), "high": float(arr[33]), "low": float(arr[34]),
            "volume": float(arr[6]), "turnover": float(arr[38]),
            "vol_ratio": float(arr[49]) if len(arr) > 49 and arr[49] else 1,
            "ma5": ma5, "ma10": ma10, "ma20": ma20,
            "rsi6": rsi6, "kdj_k": k, "kdj_d": d, "kdj_j": j,
        }
    except Exception as e:
        print(f"  获取{code}失败: {e}")
        return None

def send_wechat(title, content):
    """微信推送"""
    try:
        url = f"https://sctapi.ftqq.com/{WECHAT_PUSH_KEY}.send"
        requests.post(url, data={"title": title, "desp": content}, timeout=10)
        print(f"  推送: {title}")
    except Exception as e:
        print(f"  推送失败: {e}")

# ==================== 交易逻辑 ====================
def can_sell_t1(hold):
    """T+1规则：当天买的不能卖"""
    buy_date = hold["buyTime"][:10]
    today = datetime.now().strftime("%Y-%m-%d")
    return buy_date != today

def check_sell_signals(player, stock_data):
    """检查卖出信号"""
    signals = []
    role = ROLES[player["id"]]
    for hold in player["holdings"]:
        if hold["code"] not in stock_data:
            continue
        if not can_sell_t1(hold):
            continue  # T+1，今天不能卖
        st = stock_data[hold["code"]]
        chg = (st["price"] - hold["buyPrice"]) / hold["buyPrice"] * 100
        
        # 1. 止损
        if chg <= -role["sl"]:
            signals.append((hold, f"止损-{abs(chg):.1f}%", 1.0))
            continue
        # 2. 止盈
        if chg >= role["tp"]:
            signals.append((hold, f"止盈+{chg:.1f}%", 1.0))
            continue
        # 3. 跌破MA20
        if st["price"] < st["ma20"] and st["ma5"] < st["ma10"]:
            signals.append((hold, "跌破MA20+均线走弱", 1.0))
            continue
        # 4. KDJ死叉+高位
        if st["kdj_k"] < st["kdj_d"] and st["kdj_k"] > 70:
            signals.append((hold, "KDJ高位死叉", 1.0))
            continue
        # 5. 放量下跌（量比>1.5，跌幅>2%）
        if st["vol_ratio"] > 1.5 and st["chg"] < -2:
            signals.append((hold, "放量下跌主力出逃", 1.0))
    return signals

def check_buy_signals(player, stock_data, all_codes_data):
    """检查买入信号（每个角色策略不同）"""
    if len(player["holdings"]) >= MAX_POS:
        return None  # 持仓已满
    role_id = player["id"]
    candidates = []
    
    for code, st in all_codes_data.items():
        # 跳过已持仓
        if any(h["code"] == code for h in player["holdings"]):
            continue
        # 跳过涨跌停
        if st["chg"] >= 9.8 or st["chg"] <= -9.8:
            continue
        # 跳过ST（简单判断：名字含ST）
        if "ST" in st["name"]:
            continue
            
        score = 0
        reason_parts = []
        
        # 各角色差异化策略
        if role_id == "D1":  # 破锋：趋势突破
            if st["price"] > st["ma20"] and st["ma5"] > st["ma10"] > st["ma20"]:
                score += 30; reason_parts.append("均线多头")
            if st["chg"] > 3 and st["vol_ratio"] > 1.2:
                score += 25; reason_parts.append("放量突破")
            if st["rsi6"] > 55 and st["rsi6"] < 75:
                score += 20; reason_parts.append("RSI健康")
            if st["kdj_k"] > st["kdj_d"]:
                score += 15; reason_parts.append("KDJ金叉")
                
        elif role_id == "D2":  # 衡：多因子量化
            if st["price"] > st["ma20"]:
                score += 20; reason_parts.append("站上MA20")
            if st["ma5"] > st["ma10"]:
                score += 15; reason_parts.append("短期多头")
            if 45 < st["rsi6"] < 65:
                score += 25; reason_parts.append("RSI中性偏强")
            if st["kdj_k"] > st["kdj_d"] and st["kdj_k"] < 70:
                score += 20; reason_parts.append("KDJ低位金叉")
            if 0.8 < st["vol_ratio"] < 2.0:
                score += 10; reason_parts.append("量比适中")
                
        elif role_id == "D3":  # 镜：多空辩论（最谨慎）
            if st["price"] > st["ma20"] and st["ma20"] > st["ma20"] * 0.98:
                score += 15; reason_parts.append("站稳MA20")
            if 50 < st["rsi6"] < 60:
                score += 30; reason_parts.append("RSI完美区间")
            if st["kdj_k"] > st["kdj_d"] and 40 < st["kdj_k"] < 60:
                score += 30; reason_parts.append("KDJ中位金叉")
            if st["chg"] > 0 and st["chg"] < 3:
                score += 15; reason_parts.append("温和上涨")
                
        elif role_id == "D4":  # 啸：量价共振
            if st["vol_ratio"] > 1.3 and st["chg"] > 1:
                score += 35; reason_parts.append("量价齐升")
            if st["price"] > st["ma5"] > st["ma10"]:
                score += 25; reason_parts.append("短期多头")
            if st["kdj_k"] > st["kdj_d"]:
                score += 20; reason_parts.append("KDJ金叉")
            if st["rsi6"] > 50:
                score += 10; reason_parts.append("RSI偏强")
                
        elif role_id == "D5":  # 渊：趋势回踩
            if st["ma5"] > st["ma10"] > st["ma20"]:
                score += 25; reason_parts.append("中期上涨趋势")
            if abs(st["price"] - st["ma10"]) / st["ma10"] < 0.03:
                score += 30; reason_parts.append("回踩MA10")
            if st["chg"] > -1 and st["chg"] < 2:
                score += 15; reason_parts.append("缩量企稳")
            if st["kdj_k"] > st["kdj_d"] and st["kdj_k"] < 65:
                score += 20; reason_parts.append("KDJ金叉")
                
        elif role_id == "D6":  # 焰：涨停回踩（最激进）
            if st["chg"] > 5:
                score += 30; reason_parts.append("强势上涨")
            if st["vol_ratio"] > 1.5:
                score += 25; reason_parts.append("放量")
            if st["price"] > st["ma5"]:
                score += 20; reason_parts.append("站上MA5")
            if st["rsi6"] > 60:
                score += 15; reason_parts.append("强势")
        
        if score >= 60:
            candidates.append((code, st, score, " + ".join(reason_parts)))
    
    if not candidates:
        return None
    # 按评分排序，取最高的
    candidates.sort(key=lambda x: x[2], reverse=True)
    return candidates[0]

def do_buy(player, code, st, score, reason):
    """执行买入"""
    # 仓位管理：用可用资金的20%
    budget = player["cash"] * 0.2
    qty = int(budget / st["price"] / 100) * 100
    if qty < 100:
        print(f"  {player['id']} 资金不足，跳过 {st['name']}")
        return
    amount = qty * st["price"]
    fee = max(amount * 0.00025, 5)
    cost_per = (amount + fee) / qty
    
    player["cash"] -= (amount + fee)
    player["holdings"].append({
        "code": code, "name": st["name"],
        "qty": qty, "buyPrice": cost_per,
        "buyTime": datetime.now().isoformat(),
        "reason": reason, "score": score
    })
    player["trades"].append({
        "type": "buy", "code": code, "name": st["name"],
        "qty": qty, "price": st["price"], "fee": fee,
        "time": datetime.now().isoformat(), "reason": reason
    })
    
    # 微信推送（当天同股票只推一次）
    push_key = f".push_{player['id']}_{code}_buy_{datetime.now().strftime('%Y%m%d')}"
    if not os.path.exists(push_key):
        open(push_key, "w").close()
        send_wechat(
            f"📈 {player['id']} 建仓 {st['name']}",
            f"股票：{st['name']}({code})\n数量：{qty}股\n价格：{st['price']:.2f}元\n成本：{cost_per:.2f}元\n评分：{score}分\n原因：{reason}\n⏰ T+1：明日可卖出\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
    print(f"  ✅ {player['id']} 买入 {st['name']} {qty}股 @{st['price']:.2f}")

def do_sell(player, hold, st, reason, ratio=1.0):
    """执行卖出"""
    sell_qty = hold["qty"] if ratio >= 1 else int(hold["qty"] * ratio / 100) * 100
    sell_qty = min(sell_qty, hold["qty"])
    if sell_qty < 100:
        return
    
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
    
    hold["qty"] -= sell_qty
    if hold["qty"] <= 0:
        player["holdings"] = [h for h in player["holdings"] if h["code"] != hold["code"]]
    
    # 微信推送（当天同股票只推一次）
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
    
    if not is_trade_time():
        print("  ⏸️  非交易时间，退出")
        return
    
    data = load_data()
    
    # 1. 获取所有需要的股票行情（持仓 + 候选池）
    all_codes = set(STOCK_POOL)
    for player in data["players"]:
        for hold in player["holdings"]:
            all_codes.add(hold["code"])
    
    print(f"\n📊 正在获取 {len(all_codes)} 只股票行情...")
    all_stock_data = {}
    for i, code in enumerate(all_codes):
        info = get_stock_info(code)
        if info:
            all_stock_data[code] = info
        if (i + 1) % 20 == 0:
            print(f"  已获取 {i+1}/{len(all_codes)}")
    
    print(f"  ✅ 成功获取 {len(all_stock_data)} 只股票行情")
    
    # 2. 每个角色：先检查卖出，再检查买入
    for player in data["players"]:
        print(f"\n🎯 {player['id']} {player['name']} (现金:{player['cash']:.0f} 持仓:{len(player['holdings'])})")
        
        # 卖出
        sell_signals = check_sell_signals(player, all_stock_data)
        for hold, reason, ratio in sell_signals:
            if hold["code"] in all_stock_data:
                do_sell(player, hold, all_stock_data[hold["code"]], reason, ratio)
        
        # 买入（卖出后再买，资金更准确）
        buy_signal = check_buy_signals(player, all_stock_data, all_stock_data)
        if buy_signal:
            code, st, score, reason = buy_signal
            do_buy(player, code, st, score, reason)
    
    # 3. 更新资产
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
    
    # 4. 保存数据
    save_data(data)
    print(f"\n{'='*50}")
    print(f"  ✅ 交易引擎完成，数据已保存")
    print(f"{'='*50}")

if __name__ == "__main__":
    main()
