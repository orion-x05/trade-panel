#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI六角色模拟交易 - GitHub Actions版
每天交易时间自动运行，结果存到data.json
"""
import json
import os
import time
import requests
from datetime import datetime, timedelta

# ==================== 配置 ====================
INIT_CASH = 100000  # 每角色初始资金
MAX_POS = 5  # 每角色最多持仓数
WECHAT_PUSH_KEY = "SCT419380Tp3TtzFORow9mBwTVWfBbZOu7"  # Server酱推送KEY

# 六角色参数
ROLES = {
    "D1": {"name": "破锋", "sl": 7, "tp": 8, "tpMult": 4, "slMult": 3},
    "D2": {"name": "衡", "sl": 7, "tp": 8, "tpMult": 2, "slMult": 1.5},
    "D3": {"name": "镜", "sl": 7, "tp": 8, "tpMult": 1.5, "slMult": 1},
    "D4": {"name": "啸", "sl": 7, "tp": 8, "tpMult": 3, "slMult": 2},
    "D5": {"name": "渊", "sl": 7, "tp": 8, "tpMult": 2.5, "slMult": 2},
    "D6": {"name": "焰", "sl": 7, "tp": 8, "tpMult": 5, "slMult": 3.5},
}

# ==================== 工具函数 ====================
def load_data():
    """加载持仓数据"""
    if os.path.exists("data.json"):
        with open("data.json", "r", encoding="utf-8") as f:
            return json.load(f)
    # 初始化数据
    data = {
        "players": [],
        "last_update": datetime.now().isoformat(),
        "trades": []
    }
    for role_id, role in ROLES.items():
        data["players"].append({
            "id": role_id,
            "name": role["name"],
            "cash": INIT_CASH,
            "holdings": [],
            "trades": [],
            "asset_history": [INIT_CASH]
        })
    return data

def save_data(data):
    """保存持仓数据"""
    data["last_update"] = datetime.now().isoformat()
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def is_trade_time():
    """判断是否在交易时间"""
    now = datetime.now()
    # 周末不交易
    if now.weekday() >= 5:
        return False
    hour_minute = now.hour * 100 + now.minute
    # 9:30-11:30, 13:00-15:00
    if (930 <= hour_minute <= 1130) or (1300 <= hour_minute <= 1500):
        return True
    return False

def get_stock_price(code):
    """获取股票实时价格（用新浪接口）"""
    try:
        # 新浪行情接口
        prefix = "sh" if code.startswith("6") else "sz"
        url = f"http://hq.sinajs.cn/list={prefix}{code}"
        headers = {"Referer": "http://finance.sina.com.cn"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.encoding = "gbk"
        line = resp.text.split("\n")[0]
        parts = line.split('"')[1].split(",")
        if len(parts) < 32:
            return None
        return {
            "name": parts[0],
            "open": float(parts[1]),
            "close": float(parts[3]),  # 当前价
            "high": float(parts[4]),
            "low": float(parts[5]),
            "volume": float(parts[8]),
            "chg": float(parts[3]) - float(parts[2]),  # 涨跌
            "chg_pct": (float(parts[3]) - float(parts[2])) / float(parts[2]) * 100 if float(parts[2]) > 0 else 0
        }
    except Exception as e:
        print(f"获取{code}价格失败: {e}")
        return None

def send_wechat(title, content):
    """微信推送"""
    try:
        url = f"https://sctapi.ftqq.com/{WECHAT_PUSH_KEY}.send"
        data = {"title": title, "desp": content}
        requests.post(url, data=data, timeout=10)
        print(f"微信推送: {title}")
    except Exception as e:
        print(f"推送失败: {e}")

# ==================== 交易逻辑 ====================
def check_sell_signals(player, stock_data):
    """检查卖出信号"""
    signals = []
    for hold in player["holdings"]:
        if hold["code"] not in stock_data:
            continue
        st = stock_data[hold["code"]]
        buy_price = hold["buyPrice"]
        chg = (st["close"] - buy_price) / buy_price * 100
        
        # 止损：跌7%
        if chg <= -7:
            signals.append((hold, f"止损-{abs(chg):.1f}%", 1.0))
        
        # 止盈：涨8%
        elif chg >= 8:
            signals.append((hold, f"止盈+{chg:.1f}%", 1.0))
        
        # 高位放量下跌（买入次日保护）
        hold_days = (datetime.now() - datetime.fromisoformat(hold["buyTime"])).days
        if hold_days <= 1 and st["chg_pct"] < -1.5:
            signals.append((hold, "买入次日放量下跌", 1.0))
    
    return signals

def do_sell(player, hold, price, reason, ratio=1.0):
    """执行卖出"""
    sell_qty = int(hold["qty"] * ratio / 100) * 100 if ratio < 1 else hold["qty"]
    sell_qty = min(sell_qty, hold["qty"])
    if sell_qty < 100:
        return
    
    money = price * sell_qty
    fee = max(money * 0.00025, 5)  # 佣金
    stamp_tax = money * 0.001  # 印花税
    total_fee = fee + stamp_tax
    
    player["cash"] += money - total_fee
    profit = money - hold["buyPrice"] * sell_qty - total_fee
    
    # 记录交易
    player["trades"].append({
        "type": "sell",
        "code": hold["code"],
        "name": hold["name"],
        "qty": sell_qty,
        "price": price,
        "profit": profit,
        "time": datetime.now().isoformat(),
        "reason": reason
    })
    
    # 更新持仓
    hold["qty"] -= sell_qty
    if hold["qty"] <= 0:
        player["holdings"] = [h for h in player["holdings"] if h["code"] != hold["code"]]
    
    # 微信推送
    push_key = f"push_{player['id']}_{hold['code']}_sell_{datetime.now().strftime('%Y%m%d')}"
    if not os.path.exists(f".{push_key}"):
        open(f".{push_key}", "w").close()
        send_wechat(
            f"📉 {player['id']} 清仓 {hold['name']} {'盈利' if profit>=0 else '亏损'}{abs(profit):.2f}元",
            f"股票：{hold['name']}({hold['code']})\n数量：{sell_qty}股\n价格：{price:.2f}元\n盈亏：{profit:.2f}元\n原因：{reason}\n时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

# ==================== 主函数 ====================
def main():
    print(f"=== 交易引擎启动 {datetime.now()} ===")
    
    if not is_trade_time():
        print("非交易时间，退出")
        return
    
    data = load_data()
    
    # 获取所有持仓股票的最新价格
    all_codes = set()
    for player in data["players"]:
        for hold in player["holdings"]:
            all_codes.add(hold["code"])
    
    stock_data = {}
    for code in all_codes:
        info = get_stock_price(code)
        if info:
            stock_data[code] = info
            print(f"  {code} {info['name']}: {info['close']}元 ({info['chg_pct']:+.2f}%)")
    
    # 检查卖出信号
    for player in data["players"]:
        signals = check_sell_signals(player, stock_data)
        for hold, reason, ratio in signals:
            if hold["code"] in stock_data:
                price = stock_data[hold["code"]]["close"]
                print(f"  卖出信号: {player['id']} {hold['name']} @{price:.2f} - {reason}")
                do_sell(player, hold, price, reason, ratio)
    
    # 更新资产
    for player in data["players"]:
        total_value = player["cash"]
        for hold in player["holdings"]:
            if hold["code"] in stock_data:
                total_value += stock_data[hold["code"]]["close"] * hold["qty"]
        player["asset_history"].append(total_value)
        if len(player["asset_history"]) > 252:
            player["asset_history"] = player["asset_history"][-252:]
    
    save_data(data)
    print(f"=== 交易引擎完成 {datetime.now()} ===")

if __name__ == "__main__":
    main()
