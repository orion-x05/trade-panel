#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI六角色模拟交易 - GitHub Actions后台自动运行版（优化版）
优化项：大盘择时 + 连续亏损熔断 + 同板块限制 + 收盘总结推送 + 数据源备份
"""
import json
import os
import re
import math
import requests
from datetime import datetime, timedelta

# ==================== 配置 ====================
INIT_CASH = 100000
MAX_POS = 5
WECHAT_PUSH_KEY = os.environ.get("WECHAT_PUSH_KEY", "SCT419380Tp3TtzFORow9mBwTVWfBbZOu7")

# ===== WxPusher 推送配置（推荐）=====
# 在仓库 Settings → Secrets and variables → Actions 添加：
#   WXPUSHER_APP_TOKEN = AT_xxxxx
#   WXPUSHER_UID      = UID_xxxxx（多个用英文逗号分隔）
WXPUSHER_APP_TOKEN = os.environ.get("WXPUSHER_APP_TOKEN", "")
WXPUSHER_UID       = os.environ.get("WXPUSHER_UID", "")
WXPUSHER_TOPIC_ID  = os.environ.get("WXPUSHER_TOPIC_ID", "")

# 自适应优化边界（小幅度，防止乱调）
SL_MIN, SL_MAX, TP_MIN, TP_MAX = 3.0, 12.0, 5.0, 25.0

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

    # 应用历史自适应参数（上次调优后的止损/止盈）
    for r, v in (data.get("tuned_roles") or {}).items():
        if r in ROLES:
            ROLES[r]["sl"] = v.get("sl", ROLES[r]["sl"])
            ROLES[r]["tp"] = v.get("tp", ROLES[r]["tp"])
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
    sector_avg = {}
    for industry, chgs in sector_chg.items():
        if len(chgs) >= 2:
            sector_avg[industry] = sum(chgs) / len(chgs)
    sorted_sectors = sorted(sector_avg.items(), key=lambda x: x[1], reverse=True)
    return [s[0] for s in sorted_sectors[:5]]

def get_northbound_flow():
    """【北向资金监控】获取北向资金净流入，返回：bull(大幅流入)/bear(大幅流出)/neutral"""
    try:
        url = "https://push2.eastmoney.com/api/qt/kamt/get?fields1=f1,f2,f3,f4&fields2=f51,f52,f53,f54,f55,f56&ut=b2884a393a59ad64002292a3e90d46a5"
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        data = resp.json().get("data", {})
        sh_in = data.get("hk2sh", {}).get("dayNetAmtIn", 0) or 0
        sz_in = data.get("hk2sz", {}).get("dayNetAmtIn", 0) or 0
        total = (sh_in + sz_in) / 100000000  # 转换为亿
        print(f"  🌍 北向资金净流入：{total:+.2f}亿")
        if total > 30:
            return "bull"  # 大幅流入>30亿，看多
        elif total < -30:
            return "bear"  # 大幅流出>30亿，看空
        else:
            return "neutral"
    except Exception as e:
        print(f"  北向资金获取失败: {e}")
        return "neutral"

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

# ==================== 推送通道：WxPusher（主）+ Server酱（兜底）====================
def send_wxpusher(title, content, content_type=3):
    """WxPusher 推送。contentType: 1=文本 2=HTML 3=Markdown"""
    if not WXPUSHER_APP_TOKEN:
        print("  [warn] 未配置 WXPUSHER_APP_TOKEN，跳过 WxPusher")
        return False
    payload = {"appToken": WXPUSHER_APP_TOKEN, "content": content,
               "summary": title[:100], "contentType": content_type}
    uids = [u.strip() for u in WXPUSHER_UID.split(",") if u.strip()]
    if uids:
        payload["uids"] = uids
    if WXPUSHER_TOPIC_ID:
        try:
            payload["topicIds"] = [int(WXPUSHER_TOPIC_ID)]
        except ValueError:
            pass
    try:
        r = requests.post("https://wxpusher.zjiecode.com/api/send/message",
                          json=payload, timeout=15)
        j = r.json()
        ok = (j.get("code") == 1000)
        print(f"  {'[OK]' if ok else '[FAIL]'} WxPusher: {title} -> {'成功' if ok else j}")
        return ok
    except Exception as e:
        print(f"  [FAIL] WxPusher 异常: {e}")
        return False

def push_report(title, content):
    """统一推送入口：WxPusher 优先，失败则 Server酱 兜底"""
    if send_wxpusher(title, content):
        return True
    send_wechat(title, content)
    return True

# ==================== 五行干支（日 / 月 / 年）====================
TIANGAN = "甲乙丙丁戊己庚辛壬癸"
DIZHI   = "子丑寅卯辰巳午未申酉戌亥"
GAN_WX = {"甲":"木","乙":"木","丙":"火","丁":"火","戊":"土",
          "己":"土","庚":"金","辛":"金","壬":"水","癸":"水"}
ZHI_WX = {"子":"水","丑":"土","寅":"木","卯":"木","辰":"土","巳":"火",
          "午":"火","未":"土","申":"金","酉":"金","戌":"土","亥":"水"}
# 节气月边界（公历近似）：(月,日) -> 月支索引（寅=2 ... 丑=1）
_JIEQI = [((1,6),0),((2,4),2),((3,6),3),((4,5),4),((5,6),5),((6,6),6),
          ((7,7),7),((8,7),8),((9,8),9),((10,8),10),((11,7),11),((12,7),1)]
_ZHI_SEQ = {2:1,3:2,4:3,5:4,6:5,7:6,8:7,9:8,10:9,11:10,0:11,1:12}

def wx_of_gz(gz):
    """干支 -> 五行（如 己未 -> 土土）"""
    return GAN_WX.get(gz[0], "?") + ZHI_WX.get(gz[1], "?")

def gz_year(y):
    return TIANGAN[(y - 4) % 10] + DIZHI[(y - 4) % 12]

def _month_zhi(now):
    zhi = 0
    for (m, d), z in _JIEQI:
        if (now.month, now.day) >= (m, d):
            zhi = z
    return zhi

def gz_month(now):
    """月柱（按节气月，五虎遁年起月）"""
    zhi = _month_zhi(now)
    ygi = (now.year - 4) % 10
    zheng_yue_gan = [2, 4, 6, 8, 0][ygi % 5]   # 五虎遁
    gan = (zheng_yue_gan + _ZHI_SEQ[zhi] - 1) % 10
    return TIANGAN[gan] + DIZHI[zhi]

def gz_day(d):
    """日柱：基准 1900-01-31 = 甲辰（干支序号 40）"""
    delta = (d - datetime(1900, 1, 31)).days
    idx = (40 + delta) % 60
    return TIANGAN[idx % 10] + DIZHI[idx % 12]

def wuxing_block(now):
    gy, gm, gd = gz_year(now.year), gz_month(now), gz_day(now)
    return (f"- 年柱：{gy}（{wx_of_gz(gy)}）\n"
            f"- 月柱：{gm}（{wx_of_gz(gm)}）\n"
            f"- 日柱：{gd}（{wx_of_gz(gd)}）")

# ==================== 外部监控汇总 ====================
def build_external_monitor():
    """汇总外部监控：北向资金（实时）+ 大盘位置。失败自动降级，不影响推送"""
    lines = []
    try:
        nb = get_northbound_flow()
        if nb == "bull":
            lines.append("- 北向资金：净流入，偏多")
        elif nb == "bear":
            lines.append("- 北向资金：净流出，偏空")
        else:
            lines.append("- 北向资金：平稳")
    except Exception as e:
        lines.append(f"- 北向资金：获取失败（{e}）")
    try:
        r = requests.get("https://qt.gtimg.cn/q=sh000001", timeout=10)
        r.encoding = "gbk"
        arr = r.text.split("~")
        if len(arr) > 32:
            lines.append(f"- 上证指数：{arr[3]}（{arr[31]} {arr[32]}%）")
    except Exception:
        pass
    return "\n".join(lines) if lines else "- 外部监控数据暂不可用"

# ==================== 周期判断 ====================
def is_week_end(now):
    """本周最后交易日（周五收盘）"""
    return now.weekday() == 4

def is_month_end(now):
    """本月最后交易日：下一个交易日跨月"""
    nxt = now + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt.month != now.month

def build_rank_table(data):
    rows = ["| 角色 | 总资产 | 总收益 | 持仓 |", "|------|--------|--------|------|"]
    for i, p in enumerate(sorted(data["players"], key=lambda x: x.get("asset", INIT_CASH), reverse=True)):
        asset = asset_of(p)
        ret = (asset - INIT_CASH) / INIT_CASH * 100
        medal = "🥇" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else ""
        rows.append(f"| {medal}{p['id']}{p.get('name','')} | {asset:.0f} | {ret:+.2f}% | {len(p.get('holdings',[]))} |")
    return "\n".join(rows)

def trades_between(data, d1, d2):
    s1, s2 = d1.strftime("%Y-%m-%d"), d2.strftime("%Y-%m-%d")
    out = []
    for p in data["players"]:
        for t in p.get("trades", []):
            ds = trade_date(t)
            if s1 <= ds <= s2:
                out.append((p["id"], t))
    return out

# ==================== 自适应优化（小幅度）====================
def adaptive_tune(data):
    """根据近期成交表现，小幅微调止损/止盈（每次 ±0.5，受边界约束）"""
    notes = []
    for p in data["players"]:
        rid = p.get("id")
        if rid not in ROLES:
            continue
        sells = [t for t in p.get("trades", []) if t.get("type") == "sell"][-10:]
        if len(sells) < 4:
            continue
        wins = sum(1 for t in sells if t.get("profit", 0) > 0)
        wr = wins / len(sells)
        role = ROLES[rid]
        if wr < 0.4 and role["sl"] > SL_MIN:
            role["sl"] = round(role["sl"] - 0.5, 1)
            notes.append(f"{rid}{role['name']} 近{len(sells)}笔胜率{wr:.0%}偏低 → 止损收紧至 {role['sl']}%")
        elif wr > 0.6 and role["tp"] < TP_MAX:
            role["tp"] = round(role["tp"] + 0.5, 1)
            notes.append(f"{rid}{role['name']} 近{len(sells)}笔胜率{wr:.0%}较好 → 止盈放宽至 {role['tp']}%")
    data["tuned_roles"] = {r: {"sl": ROLES[r]["sl"], "tp": ROLES[r]["tp"]} for r in ROLES}
    return notes

def current_params_line():
    return "、".join(f"{r}{ROLES[r]['name']}(止损{ROLES[r]['sl']}%/止盈{ROLES[r]['tp']}%)" for r in ROLES)

# ==================== 周 / 月 总结 ====================
def send_period_summary(data, kind, market_trend):
    front = load_frontend_simdata()
    if front:
        data = front
    data = refresh_assets(data)
    now = datetime.now()
    if kind == "week":
        start = now - timedelta(days=6)
        title = f"📅 本周交易总结 {now.strftime('%Y-%m-%d')}"
    else:
        start = now - timedelta(days=29)
        title = f"🗓️ 本月交易总结 {now.strftime('%Y-%m')}"
    label = "本周" if kind == "week" else "本月"
    trades = trades_between(data, start, now)
    sells = [t for _, t in trades if t.get("type") == "sell"]
    wins = [t for t in sells if t.get("profit", 0) > 0]
    total_pnl = sum(t.get("profit", 0) for t in sells)
    wr = (len(wins) / len(sells) * 100) if sells else 0.0

    trend_txt = "多头📈" if market_trend == "bull" else "空头📉" if market_trend == "bear" else "震荡🔄"
    content = f"## {label}战绩（{start.strftime('%m-%d')} ~ {now.strftime('%m-%d')}）大盘{trend_txt}\n\n"
    content += build_rank_table(data) + "\n\n"
    content += (f"## {label}交易统计\n"
                f"- 区间成交：{len(trades)} 笔（卖出 {len(sells)} 笔）\n"
                f"- 胜率：{wr:.1f}%\n"
                f"- 已实现盈亏：{total_pnl:+.2f} 元\n\n")
    content += f"## 五行参考（年/月/日）\n{wuxing_block(now)}\n\n"
    content += f"## 外部监控\n{build_external_monitor()}\n\n"
    content += f"## 当前策略参数\n{current_params_line()}\n"
    push_report(title, content)
    print(f"  ✅ {label}总结已推送")

# ==================== 前端真实数据源 ====================
def load_frontend_simdata():
    """读取 index.html 内联的 embeddedSimData —— 页面显示的就是它，报告以此为准"""
    try:
        with open("index.html", encoding="utf-8") as f:
            html = f.read()
        m = re.search(r'id="embeddedSimData">([\s\S]*?)</script>', html)
        if m:
            d = json.loads(m.group(1))
            if d.get("players"):
                return d
    except Exception as e:
        print(f"  [warn] 读取前端数据失败: {e}")
    return None

def refresh_assets(data):
    """用实时行情重算各角色总资产（现金 + 持仓市值）——与前端页面算法一致"""
    codes = set()
    for p in data.get("players", []):
        for h in p.get("holdings", []):
            if h.get("code"):
                codes.add(h["code"])
    quotes = {}
    for c in codes:
        try:
            info = get_stock_info(c)
            if info and info.get("price"):
                quotes[c] = info["price"]
        except Exception:
            pass
    for p in data.get("players", []):
        total = p.get("cash", INIT_CASH)
        for h in p.get("holdings", []):
            px = quotes.get(h["code"])
            if px is None:                      # 行情取不到时用成本价兜底
                px = h.get("cost") or h.get("buyPrice") or 0
            total += px * h.get("qty", 0)
        p["asset"] = total
    return data

def asset_of(p):
    """总资产兼容取值：asset → assetHistory 末值 → 初始资金"""
    a = p.get("asset")
    if a is None:
        ah = p.get("assetHistory") or p.get("asset_history") or []
        a = ah[-1] if ah else INIT_CASH
    return a

def trade_date(t):
    """交易日期兼容：time / date"""
    return (t.get("time") or t.get("date") or "")[:10]

def send_daily_summary(data, market_trend):
    """【每日收盘总结推送】—— 数据以前端页面为准"""
    front = load_frontend_simdata()
    if front:
        data = front
    data = refresh_assets(data)   # 用实时行情重算，与页面显示一致
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
            if trade_date(t) == today_str:
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

    # 五行参考（年 / 月 / 日）
    content += f"\n## 五行参考（年/月/日）\n{wuxing_block(now)}\n"
    # 外部监控（北向资金 + 上证）
    content += f"\n## 外部监控\n{build_external_monitor()}\n"
    # 根据实际操作小幅优化策略
    notes = adaptive_tune(data)
    if notes:
        content += "\n## 策略微调（小幅）\n" + "\n".join(f"- {n}" for n in notes) + "\n"

    push_report(title, content)
    print("  ✅ 每日收盘总结已推送")

    # 周总结（周五收盘） / 月总结（月末最后交易日）
    if is_week_end(now):
        print("\n📅 周五收盘，追加每周总结...")
        send_period_summary(data, "week", market_trend)
    if is_month_end(now):
        print("\n🗓️ 月末最后交易日，追加每月总结...")
        send_period_summary(data, "month", market_trend)

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
    
    # 【北向资金监控】结合北向资金调整市场判断
    northbound = get_northbound_flow()
    if northbound == "bull" and market_trend == "neutral":
        market_trend = "bull"  # 北向大幅流入，震荡转多
        print("  🌍 北向资金大幅流入，市场判断调整为多头")
    elif northbound == "bear" and market_trend == "neutral":
        market_trend = "bear"  # 北向大幅流出，震荡转空
        print("  🌍 北向资金大幅流出，市场判断调整为空头")
    
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
