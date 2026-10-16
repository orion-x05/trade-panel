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
from datetime import datetime, timedelta, timezone

# ==================== 配置 ====================
# ===== 时区修正 =====
# GitHub Actions runner 的 TZ 是 UTC，cn_now() 会比北京时间慢 8 小时，
# 导致交易时段(9:30-15:00)与收盘窗口(15:00+)判定全部错位 —— 后台引擎每次运行都秒退。
# 统一用北京时间取"现在"；返回 naive（不带 tzinfo），与 data.json 里历史时间戳
# （datetime.fromisoformat 解析出的同样是 naive）做算术时不会触发 TypeError。
CN_TZ = timezone(timedelta(hours=8))

def cn_now():
    """当前北京时间（naive datetime）"""
    return datetime.now(CN_TZ).replace(tzinfo=None)

# 手动强制发送每日收盘总结（workflow_dispatch 勾选 force_summary 时置 1）
FORCE_SUMMARY = os.environ.get("FORCE_SUMMARY", "").strip().lower() in ("1", "true", "yes", "on")

INIT_CASH = 100000
MAX_POS = 5
MAX_OVERLAP = 2  # 全局：同一只股票最多允许 2 个角色持有，禁止六角色持仓大幅重复
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
# 前端「回测 → 实盘调参」落盘文件（面板点「复制 bt_tune.json」→ 放到仓库根目录）
BT_TUNE_FILE = "bt_tune.json"

ROLES = {
    "D1": {"name": "破锋", "sl": 7, "tp": 12},
    "D2": {"name": "衡", "sl": 6, "tp": 8},
    "D3": {"name": "镜", "sl": 5, "tp": 6},
    "D4": {"name": "啸", "sl": 7, "tp": 10},
    "D5": {"name": "渊", "sl": 6, "tp": 9},
    "D6": {"name": "焰", "sl": 8, "tp": 15},
    # D7 周线超卖反转（与前端 ROLE_PARAMS 对齐：sl10/tp25，atrParamsMap sl1.5/tp3）
    "D7": {"name": "沉", "sl": 10, "tp": 25},
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
def ensure_players(data):
    """对齐前端 ensurePlayerD7：data.json 历史快照缺新角色时幂等补齐，
       不动 D1-D6 的持仓/成交/历史。返回是否有新增。"""
    existing = {p.get("id") for p in data.get("players", [])}
    added = False
    for role_id, role in ROLES.items():
        if role_id in existing:
            continue
        data["players"].append({
            "id": role_id, "name": role["name"],
            "cash": INIT_CASH, "holdings": [], "trades": [],
            "asset_history": [INIT_CASH]
        })
        data.setdefault("consecutive_losses", {})[role_id] = 0
        added = True
        print(f"  ➕ 已补齐新角色 {role_id} {role['name']}（初始资金 {INIT_CASH}）")
    return added

def load_data():
    if os.path.exists("data.json"):
        with open("data.json", "r", encoding="utf-8") as f:
            data = json.load(f)
        ensure_players(data)
        return data
    data = {"players": [], "last_update": cn_now().isoformat(),
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

def load_bt_tune():
    """读取前端回测调参文件 bt_tune.json（面板「复制 bt_tune.json」→ 放到仓库根目录）。
    这是"历史回测 → 实盘"的闭环通道：面板跑完六角色回测后，按回撤/盈亏比/胜率
    自动换算出的止损、止盈、单笔仓位上限、角色开关，会在这里作用到真实交易。
    文件不存在时静默跳过，行为与之前完全一致。"""
    if not os.path.exists(BT_TUNE_FILE):
        return []
    try:
        with open(BT_TUNE_FILE, "r", encoding="utf-8") as f:
            t = json.load(f)
    except Exception as e:
        print(f"  ⚠️  {BT_TUNE_FILE} 解析失败，忽略：{e}")
        return []
    notes = []
    for rid, v in (t.get("roles") or {}).items():
        if rid not in ROLES or not isinstance(v, dict):
            continue
        role = ROLES[rid]
        if isinstance(v.get("sl"), (int, float)):
            role["sl"] = round(min(max(float(v["sl"]), SL_MIN), SL_MAX), 1)
        if isinstance(v.get("tp"), (int, float)):
            role["tp"] = round(min(max(float(v["tp"]), TP_MIN), TP_MAX), 1)
        if isinstance(v.get("pos_cap"), (int, float)):
            role["pos_cap"] = round(min(max(float(v["pos_cap"]), 5.0), 30.0), 1) / 100.0
        if "enabled" in v:
            role["enabled"] = bool(v["enabled"])
        notes.append(f"{rid}{role['name']} 止损{role['sl']}% / 止盈{role['tp']}% / 单笔≤{int((role.get('pos_cap') or 0.2)*100)}%"
                     + ("" if role.get("enabled", True) else "（暂停开仓）"))
    if notes:
        print("  ⚙️  回测调参已生效（bt_tune.json）：" + "；".join(notes))
    return notes


def save_data(data):
    data["last_update"] = cn_now().isoformat()
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def is_trade_time():
    now = cn_now()
    if now.weekday() >= 5: return False
    hm = now.hour * 100 + now.minute
    return (930 <= hm <= 1130) or (1300 <= hm <= 1500)

def is_close_time():
    now = cn_now()
    if now.weekday() >= 5: return False
    hm = now.hour * 100 + now.minute
    # 收盘总结窗口放宽到 15:00-15:55：GitHub Actions 定时任务排队延迟常见
    # （原 10 分钟窗口内跑晚就直接改走交易逻辑，每日小结被静默跳过）
    # 窗口 15:00-16:00：GitHub Actions cron 排队延迟常见（实测 14~46 分钟），
    # 原 10 分钟窗口一旦排晚就改走交易逻辑，每日小结被静默跳过。
    # 有 data.json 的 last_daily_push 去重标记，放宽不会重复推送。
    return 1500 <= hm <= 1600

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

def get_index_kline(index_code="sh000001", count=60):
    """【大盘K线走势】抓取指数日K（开收高低），用于判断趋势/形态，不只看收盘"""
    try:
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={index_code},day,,,{count},qfq"
        kdata = requests.get(url, timeout=10).json()
        klines = kdata.get("data", {}).get(index_code, {}).get("qfqday") or \
                 kdata.get("data", {}).get(index_code, {}).get("day") or []
        rows = []
        for k in klines[-count:]:
            rows.append({"date": k[0], "open": float(k[1]), "close": float(k[2]),
                         "high": float(k[3]), "low": float(k[4]), "vol": float(k[5]) if len(k) > 5 else 0})
        return rows
    except Exception as e:
        print(f"  大盘K线获取失败: {e}")
        return []

def get_market_volume(count=20):
    """【大盘总体成交量】两市成交量（上证+深证日K成交量，单位亿手）。数据源：新浪（数据中心友好）"""
    def sina_vol(symbol):
        url = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=5&datalen={count}"
        j = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}).json()
        # 新浪 volume 单位：股 → /1e10 得 亿手
        return [{"date": k["day"], "vol": float(k["volume"]) / 1e10} for k in (j or [])]
    try:
        sh = sina_vol("sh000001")
        sz = sina_vol("sz399001")
    except Exception as e:
        print(f"  大盘成交量获取失败: {e}")
        return None
    n = min(len(sh), len(sz))
    if n == 0:
        return None
    dates = [sh[-n + i]["date"] for i in range(n)]
    total = [sh[-n + i]["vol"] + sz[-n + i]["vol"] for i in range(n)]
    ma5 = [sum(total[max(0, i - 4):i + 1]) / min(5, i + 1) for i in range(n)]
    return {"dates": dates, "total": total, "ma5": ma5, "latest": total[-1] if total else 0,
            "ma5_latest": ma5[-1] if ma5 else 0, "vs_ma5": (total[-1] / ma5[-1] - 1) * 100 if ma5 and ma5[-1] else 0}

def _em_ulist(secids):
    """东方财富批量实时报价（数字字段）"""
    url = "https://push2.eastmoney.com/api/qt/ulist.np/get?secids=" + ",".join(secids) + \
          "&fields=f2,f3,f12,f14,f57,f62&fltt=2&invt=2&_=" + str(int(__import__("time").time() * 1000))
    try:
        j = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"}).json()
        return {it.get("f12"): it for it in (j.get("data", {}) or {}).get("diff", []) if it}
    except Exception as e:
        print(f"  ETF实时报价失败: {e}")
        return {}

def _tx_quotes(codes):
    """腾讯 qt.gtimg.cn 批量实时报价（codes 带交易所前缀，如 sh510300），东方财富不可用时的兜底"""
    import urllib.request
    try:
        url = "https://qt.gtimg.cn/q=" + ",".join(codes)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
        text = raw.decode("gbk", "ignore")
        out = {}
        for line in text.split(";"):
            m = re.match(r'v_(\w+)="(.+)"', line)
            if not m:
                continue
            arr = m.group(2).split("~")
            if len(arr) < 33:
                continue
            code = m.group(1)[2:]
            out[code] = {"f12": code, "f2": float(arr[3]),
                         "f3": float(arr[32]) if arr[32] else 0,
                         "f14": arr[1], "f57": float(arr[37]) if arr[37] else 0}
        return out
    except Exception as e:
        print(f"  腾讯报价兜底失败: {e}")
        return {}

def detect_national_team():
    """【国家队资金动向·最高优先级】探测宽基ETF异动+尾盘异动+沪深300/中证2000强弱，猜测并验证意图"""
    try:
        etf = {"510300": "沪深300ETF", "510050": "上证50ETF", "510500": "中证500ETF",
               "588000": "科创50ETF", "159915": "创业板ETF"}
        tx_codes = ["sh" + c for c in etf.keys()] + ["sh000300", "sh932000", "sh000001"]
        # 主源东方财富；不可用时兜底腾讯实时报价
        q = _em_ulist(["1." + c for c in etf.keys()] + ["1.000300", "1.932000", "1.000001"]) or _tx_quotes(tx_codes)
        etf_rows, score = [], 0
        for code, name in etf.items():
            d = q.get(code)
            if not d: continue
            chg = (d.get("f3") or 0) / 100
            active = chg > 0.25
            if active: score += 1
            etf_rows.append({"name": name, "chg": chg, "active": active})
        # 尾盘异动：腾讯分时
        tail_desc, gap_desc = "尾盘平稳", ""
        try:
            mr = requests.get("https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=sh000001", timeout=10).json()
            m = (mr.get("data", {}).get("sh000001", {}).get("data", {}) or {}).get("data", []) or []
            if len(m) > 30:
                arr = [float(str(x).split(" ")[1]) for x in m]
                last = sum(arr[-30:]) / 30
                first = sum(arr[:-30]) / max(1, len(arr) - 30)
                tail_chg = (last - first) / first * 100
                if tail_chg > 0.25:
                    tail_desc = f"尾盘拉升 +{tail_chg:.2f}%"; score += 1
                elif tail_chg < -0.25:
                    tail_desc = f"尾盘跳水 {tail_chg:.2f}%"
                else:
                    tail_desc = f"尾盘平稳 {tail_chg:.2f}%"
        except Exception:
            pass
        h300 = q.get("000300"); z2k = q.get("932000")
        # 护盘特征更精准的代理：上证50ETF(权重) vs 中证500ETF(中盘) 强弱
        h50 = next((e["chg"] for e in etf_rows if e["name"] == "上证50ETF"), None)
        mid = next((e["chg"] for e in etf_rows if e["name"] == "中证500ETF"), None)
        if h50 is not None and mid is not None:
            gap = h50 - mid
            if gap > 0.4:
                gap_desc = f"上证50(权重)强于中证500(中盘) {gap:.2f}pct（护盘特征）"; score += 1
            else:
                gap_desc = f"上证50 vs 中证500 强弱差 {gap:.2f}pct"
        elif h300 and z2k:
            gap = (h300.get("f3") or 0) / 100 - (z2k.get("f3") or 0) / 100
            gap_desc = f"沪深300 vs 中证2000 强弱差 {gap:.2f}pct"
        if score >= 3:
            intent = "主动护盘/拉升：国家队明确进场，托底意图强"
            verify = "验证信号：观察未来1-3日是否放量企稳/反转，若沪深300持续强于中证2000则确认护盘成立"
        elif score >= 1:
            intent = "试探性托底：出现国家队异动迹象，力度待确认"
            verify = "验证信号：紧盯宽基ETF是否持续放量、尾盘是否再出现拉升，确认后再加仓"
        else:
            intent = "未见明显动作：宽基ETF平静，国家队暂未出手"
            verify = "持续观察宽基ETF放量是否延续、沪深300是否持续强于中证2000"
        return {"etfs": etf_rows, "tail": tail_desc, "gap": gap_desc,
                "intent": intent, "verify": verify, "score": score}
    except Exception as e:
        print(f"  国家队探测失败: {e}")
        return {"etfs": [], "tail": "--", "gap": "--", "intent": "探测失败", "verify": "--", "score": 0}

def national_team_factor():
    """【agent 领先指标】国家队异动强度 → 仓位系数（先于普通情绪反应）"""
    nt = detect_national_team()
    if nt["score"] >= 3:
        return 1.15   # 国家队明确进场，可适度领先加仓
    if nt["score"] >= 1:
        return 1.0    # 试探，维持中性
    return 0.9        # 未见动作，略偏防御

def build_national_team_block():
    """推送：国家队资金动向段"""
    nt = detect_national_team()
    etf_lines = " / ".join(f"{e['name']}{e['chg']:+.2f}%" + ("↑异动" if e["active"] else "") for e in nt["etfs"]) or "（无）"
    return (f"- 【国家队·最高优先级】意图：{nt['intent']}\n"
            f"  · 宽基ETF：{etf_lines}\n"
            f"  · {nt['tail']}；{nt['gap']}\n"
            f"  · 🔎 验证：{nt['verify']}（异动评分 {nt['score']}/5）")

def build_sentiment_block():
    """推送：大盘走势过程（K线形态 + 成交量），不只看收盘"""
    lines = []
    try:
        k = get_index_kline("sh000001", 20)
        if k and len(k) >= 2:
            last, prev = k[-1], k[-2]
            up = last["close"] >= last["open"]
            body = (last["close"] - last["open"]) / last["open"] * 100
            recent = sum(x["close"] for x in k[-5:]) / 5
            near_high = last["close"] >= recent * 1.005
            near_low = last["close"] <= recent * 0.995
            shape = "光头光脚阳线(强势)" if (up and last["close"] >= last["high"] * 0.998) else \
                    "光头光脚阴线(弱势)" if (not up and last["close"] <= last["low"] * 1.002) else \
                    "阳线" if up else "阴线"
            lines.append(f"- 上证日K：收 {last['close']:.2f}（{shape}，实体 {body:+.2f}%），"
                         f"{'逼近5日高点' if near_high else '逼近5日低点' if near_low else '居中'}（近20日{'震荡' if abs(body)<1.5 else '有趋势'}）")
            v = get_market_volume(20)
        if v:
            rng = "放量" if v["vs_ma5"] > 8 else "缩量" if v["vs_ma5"] < -8 else "持平"
            lines.append(f"- 大盘总体成交量：两市成交量 {v['latest']:.2f}亿手（{rng}，较MA5 {v['vs_ma5']:+.1f}%）")
    except Exception as e:
        lines.append(f"- 走势过程获取失败（{e}）")
    return "\n".join(lines) if lines else "- 走势过程数据暂不可用"

# ==================== 底部探测器：政策底 + 技术底 ====================
POLICY_DICT = {
    "降准": 3, "降息": 3, "印花税": 3, "汇金": 3, "中央汇金": 3, "平准基金": 2.5,
    "国家队": 2, "逆回购": 1, "MLF": 1.5, "LPR": 1.5, "流动性": 1,
    "政治局": 2, "国常会": 2, "证监会": 1.5, "央行": 1.5, "稳增长": 1.5,
    "活跃资本市场": 2, "限制减持": 2, "减持新规": 2, "暂停IPO": 2, "再融资": 1,
    "引导长期资金": 1.5, "中长线资金": 1.5,
    "回购": 1, "增持潮": 1.2, "破净": 1, "估值底": 1,
}
POLICY_STRONG = ["降准", "降息", "印花税", "汇金", "中央汇金", "平准基金",
                 "国家队", "逆回购", "MLF", "LPR", "流动性"]
POLICY_MID = ["政治局", "国常会", "证监会", "央行", "稳增长", "活跃资本市场",
              "限制减持", "减持新规", "暂停IPO", "再融资", "引导长期资金", "中长线资金"]
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"}


def _strip_html(s):
    return re.sub(r"<[^>]*>", " ", s or "")


def fetch_policy_signals():
    """【专门政策源·多源】抓政策要闻，命中政策词库返回证据。
    源：①东财7×24快讯 ②央行公开市场操作 ③证监会要闻（不可达则自动降级，不影响运行）。"""
    texts, sources = [], []
    try:
        txt = requests.get("https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_50_1_.html",
                           headers=_UA, timeout=12).text
        items = re.findall(r'"(?:title|digest)":"((?:[^"\\]|\\.)*)"', txt)
        body = " ".join(_strip_html(i) for i in items)
        if len(body) > 50:
            texts.append(body)
            sources.append("东财7×24快讯")
    except Exception:
        pass
    for name, url in (("央行公开市场操作", "http://www.pbc.gov.cn/zhengcehuobisi/125207/125213/125431/125475/index.html"),
                      ("证监会要闻", "http://www.csrc.gov.cn/csrc/c100028/common_list.shtml")):
        try:
            r = requests.get(url, headers=_UA, timeout=12, allow_redirects=True)
            if r.status_code != 200:
                continue
            r.encoding = r.apparent_encoding or "utf-8"
            body = re.sub(r"\s+", " ", _strip_html(r.text))
            if len(body) > 200:
                texts.append(body)
                sources.append(name)
        except Exception:
            continue
    if not texts:
        return {"score": 0, "level": "政策源不可达", "verdict": "政策面数据源暂不可达，请稍后重试",
                "hits": [], "sources": []}
    hits, seen = [], set()
    for t in texts:
        for w, wt in POLICY_DICT.items():
            if w in t and w not in seen:
                seen.add(w)
                cat = "强" if w in POLICY_STRONG else ("中" if w in POLICY_MID else "弱")
                hits.append({"word": w, "weight": wt, "cat": cat})
    total = sum(h["weight"] for h in hits)
    score = min(100, int(total * 4))
    if score >= 60:
        level, verdict = "政策底明确", "强政策信号密集出现，政策底大概率已现，关注市场底传导（常滞后1-3月）。"
    elif score >= 30:
        level, verdict = "政策底渐显", "出现政策托底信号，力度待积累，密切跟踪是否加码。"
    elif score >= 10:
        level, verdict = "政策暖风", "零星政策信号，尚未形成底部合力。"
    else:
        level, verdict = "政策平静", "消息面暂无明确政策托底信号。"
    return {"score": score, "level": level, "verdict": verdict, "hits": hits, "sources": sources}


def calc_tech_bottom(kline):
    """【技术底】上证日K 7维量价加权 0-100（与前端 calcTechBottom 同口径）"""
    if not kline or len(kline) < 25:
        return {"score": None, "level": "数据不足", "verdict": "上证日K数据不足，无法计算技术底", "details": {}}
    closes = [float(x["close"]) for x in kline]
    vols = [float(x.get("vol") or 0) for x in kline]
    last = closes[-1]

    def clamp(v, a, b):
        return max(a, min(b, v))

    def ma(a, n):
        return sum(a[-n:]) / n

    def rsi(n):
        g = l = 0.0
        st = max(1, len(closes) - n)
        for i in range(st, len(closes)):
            d = closes[i] - closes[i - 1]
            if d > 0:
                g += d
            else:
                l -= d
        cnt = max(1, len(closes) - st)
        rs = (g / cnt) / (l / cnt or 1)
        return 100 - 100 / (1 + rs)

    hh = max(closes)
    dd = (last - hh) / hh * 100
    dd_s = clamp(int((dd + 8) / (8 - 38) * 100), 0, 100)
    ma20, ma60 = ma(closes, 20), ma(closes, 60)
    d20, d60 = (last - ma20) / ma20 * 100, (last - ma60) / ma60 * 100
    d20_s = clamp(int((d20 + 8) / (8 - 18) * 100), 0, 100)
    d60_s = clamp(int((d60 + 5) / (5 - 25) * 100), 0, 100)
    r6, r14 = rsi(6), rsi(14)
    rsi_s = clamp(int((25 - (r14 + r6) / 2) / 25 * 100), 0, 100)
    streak = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] < closes[i - 1]:
            streak += 1
        else:
            break
    streak_s = clamp(int(streak / 7 * 100), 0, 100)
    v5, v20 = ma(vols[-5:], 5), ma(vols, 20)
    vr = v5 / (v20 or 1)
    vol_s = clamp(int((1.1 - vr) / 1.1 * 100), 0, 100)
    hammer = 0
    for x in kline[-3:]:
        o, c, h, l = float(x["open"]), float(x["close"]), float(x["high"]), float(x["low"])
        body, rng = abs(c - o), (h - l) or 1
        if body / rng < 0.3 and ((h - max(o, c)) / rng > 0.5 or (max(o, c) - l) / rng > 0.5):
            hammer += 1
    hammer_s = clamp(int(hammer / 3 * 100), 0, 100)
    m = ma(closes, 20)
    sd = (sum((v - m) ** 2 for v in closes[-20:]) / 20) ** 0.5
    lo, hi = m - 2 * sd, m + 2 * sd
    bp = clamp((last - lo) / ((hi - lo) or 1), 0, 1)
    boll_s = clamp(int((0.15 - bp) / 0.15 * 100), 0, 100)
    score = int(round(dd_s * 0.22 + d20_s * 0.15 + d60_s * 0.13 + rsi_s * 0.15 +
                      streak_s * 0.10 + vol_s * 0.12 + hammer_s * 0.05 + boll_s * 0.08))
    if score >= 70:
        level, verdict = "技术底确认", "量价超跌特征明显（深回撤+负乖离+超卖+缩量），配合政策底可左侧布局。"
    elif score >= 50:
        level, verdict = "接近技术底", "出现多项超跌信号，接近技术底，尚需缩量企稳确认。"
    elif score >= 30:
        level, verdict = "偏弱", "部分指标超跌，整体尚未见底，反弹易反复。"
    else:
        level, verdict = "未见技术底", "量价尚未进入超跌区，继续等待。"
    return {"score": score, "level": level, "verdict": verdict,
            "details": {"回撤": f"{dd:.1f}%", "MA20乖离": f"{d20:.1f}%", "MA60乖离": f"{d60:.1f}%",
                        "RSI14": f"{r14:.0f}", "连跌": f"{streak}日", "量比": f"{vr:.2f}"}}


def build_bottom_block():
    """推送：底部探测（政策底 + 技术底 + 阶段传导）"""
    try:
        tech = calc_tech_bottom(get_index_kline("sh000001", 120))
    except Exception as e:
        tech = {"score": None, "level": "数据不足", "verdict": f"技术底获取失败（{e}）", "details": {}}
    pol = fetch_policy_signals()
    ts, ps = tech.get("score"), pol.get("score", 0)
    p_on, t_on = ps >= 30, (ts is not None and ts >= 50)
    if p_on and t_on:
        phase = "政策底+技术底共振：底部区域，可左侧布局"
    elif p_on:
        phase = "政策底领先技术底：政策已出手，等待市场底（常滞后1-3月），左侧窗口"
    elif t_on:
        phase = "技术底先行：量价已超跌但缺政策确认，反弹易反复，等右侧信号"
    else:
        phase = "双弱：政策未出手、技术未超跌，未见底部"
    lines = [f"- 【政策底】{ps}/100 · {pol['level']}（来源：{'、'.join(pol['sources']) or '暂不可达'}）"]
    if pol.get("hits"):
        ev = " / ".join(f"{h['word']}({h['cat']}+{h['weight']})" for h in pol["hits"][:8])
        lines.append(f"  · 命中：{ev}")
    lines.append(f"- 【技术底】{'--' if ts is None else str(ts)}/100 · {tech['level']}")
    d = tech.get("details") or {}
    if d:
        lines.append(f"  · 距高点回撤{d.get('回撤', '--')} · MA20乖离{d.get('MA20乖离', '--')} · "
                     f"MA60乖离{d.get('MA60乖离', '--')} · RSI14 {d.get('RSI14', '--')} · "
                     f"连跌{d.get('连跌', '--')} · 量比(5/20){d.get('量比', '--')}")
    lines.append(f"- 【阶段】{phase}")
    lines.append("- 【验证】政策底需连续加码；技术底需地量+长下影企稳+不创新低；二者共振且放量收复MA20方为右侧加仓信号")
    return "\n".join(lines)


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
        # ===== v2 策略升级新增字段 =====
        ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else ma20
        ma20_prev = sum(closes[-25:-5]) / 20 if len(closes) >= 25 else ma20  # 5日前的MA20→趋势方向
        highs20 = [float(k[3]) for k in klines[-20:]]
        hi20 = max(highs20) if highs20 else price                          # 前20日高点→突破确认
        lows20 = [float(k[4]) for k in klines[-20:]]
        lo20 = min(lows20) if lows20 else price                           # 前20日低点→反弹幅度/低位判断
        # 全局风控闸历史基准（与前端 liveGlobalGuard 同口径）
        close5 = closes[-6] if len(closes) >= 6 else 0                    # 5日前收盘→追涨拦截
        close10 = closes[-11] if len(closes) >= 11 else 0                 # 10日前收盘→追涨拦截
        up_days = 0                                                       # 截至昨日连续收阳天数
        for idx in range(len(closes) - 1, 0, -1):
            if closes[idx] > closes[idx - 1]: up_days += 1
            else: break
        has_zt5, zt_low, high5 = False, 0.0, price                          # 近5日涨停信息（D6用）
        if len(klines) >= 6:
            highs5 = [float(k[3]) for k in klines[-5:]]
            high5 = max(highs5) if highs5 else price
            for j in range(max(1, len(klines) - 5), len(klines)):
                day_chg = (float(klines[j][2]) / float(klines[j - 1][2]) - 1) * 100
                if day_chg >= 9.8:
                    has_zt5 = True
                    zt_low = float(klines[j][4])
                    break
        
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
                     "ma60": ma60, "ma20_prev": ma20_prev, "hi20": hi20, "lo20": lo20,
                     "close5": close5, "close10": close10, "up_days": up_days,
                     "has_zt5": has_zt5, "zt_low": zt_low, "high5": high5,
                     "rsi6": rsi6, "kdj_k": k, "kdj_d": d, "kdj_j": j})
    except:
        info.update({"ma5": info["price"], "ma10": info["price"], "ma20": info["price"],
                     "ma60": info["price"], "ma20_prev": info["price"], "hi20": info["price"],
                     "lo20": info["price"], "close5": 0, "close10": 0, "up_days": 0,
                     "has_zt5": False, "zt_low": 0.0, "high5": info["price"],
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
    """汇总外部监控：以【大盘情绪·主参考】为首，北向/外围为辅助。失败自动降级"""
    lines = []
    # —— 大盘情绪（主参考）：以上证指数涨跌幅为核心，与前端情绪温度同算法 ——
    try:
        r = requests.get("https://qt.gtimg.cn/q=sh000001", timeout=10)
        r.encoding = "gbk"
        arr = r.text.split("~")
        if len(arr) > 32:
            price = arr[3]
            chg = float(arr[32])
            temp = max(-30, min(30, round(chg * 10)))
            level = ("情绪高涨·注意过热" if temp >= 20 else
                     "情绪偏暖·谨慎做多" if temp >= 10 else
                     "情绪温和·可逢低布局" if temp >= 3 else
                     "情绪中性·观望为主" if temp > -3 else
                     "情绪偏冷·控制仓位" if temp > -10 else
                     "情绪低迷·等待企稳" if temp > -20 else
                     "情绪冰点·恐慌性抛售")
            lines.append(f"【大盘情绪·主参考】上证 {price}（{chg:+.2f}%） 情绪温度 {temp}℃ · {level}")
    except Exception as e:
        lines.append(f"- 上证指数：获取失败（{e}）")
    # —— 辅助：北向资金 ——
    try:
        nb = get_northbound_flow()
        if nb == "bull":
            lines.append("- 北向资金（辅助）：净流入，偏多")
        elif nb == "bear":
            lines.append("- 北向资金（辅助）：净流出，偏空")
        else:
            lines.append("- 北向资金（辅助）：平稳")
    except Exception as e:
        lines.append(f"- 北向资金（辅助）：获取失败（{e}）")
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
    orig = data
    now0 = cn_now()
    push_key = f"last_{kind}_push"
    period = now0.strftime("%Y-W%V") if kind == "week" else now0.strftime("%Y-%m")
    if orig.get(push_key) == period:
        print(f"  ⏭️  {kind} 总结本周期已推送，跳过（防多 cron 重复）")
        return
    # 【数据源纠正】页面每 30 秒从 ./data.json 同步（syncFromServer），data.json 才是面板真源；
    # index.html 内联的 embeddedSimData 只是本地无数据时的冷启动种子（当前为 2026-09-23 快照），
    # 用它计算收益会与面板显示严重不符 —— 不再覆盖。
    data = refresh_assets(data)
    now = cn_now()
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
    content += f"## 大盘走势过程（K线形态/当日分时/总体成交量）\n{build_sentiment_block()}\n\n"
    content += f"## 国家队资金动向（最高优先级）\n{build_national_team_block()}\n\n"
    content += f"## 底部探测（政策底/技术底）\n{build_bottom_block()}\n\n"
    content += f"## 当前策略参数\n{current_params_line()}\n"
    push_report(title, content)
    print(f"  ✅ {label}总结已推送")
    orig[push_key] = period

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

def fmt_trade_time(t):
    """交易时间 → 北京时间 'MM-DD HH:MM'。
    兼容两种格式：前端 JS 写的 UTC ISO（带 Z，如 2026-09-21T01:30:00.000Z）
    与引擎写的 naive 北京时间 ISO（如 2026-10-15T09:30:00）"""
    raw = (t.get("time") or t.get("date") or "").strip()
    if not raw:
        return "--:--"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is not None:                      # 带时区（UTC）→ 转北京时间
            dt = dt.astimezone(CN_TZ).replace(tzinfo=None)
        return dt.strftime("%m-%d %H:%M")
    except Exception:
        return raw[5:16].replace("T", " ") if len(raw) >= 16 else raw

def send_daily_summary(data, market_trend):
    """【每日收盘总结推送】—— 数据以前端页面为准"""
    # 跨运行去重：15:00/15:05/15:30 多个 cron 都落在收盘窗口内，
    # 标记写进 data.json（工作流每次跑完都会上传），同一天只推一次
    orig = data
    today = cn_now().strftime("%Y-%m-%d")
    if orig.get("last_daily_push") == today:
        print("  ⏭️  每日收盘总结今日已推送，跳过（防多 cron 重复）")
        return
    # 【数据源纠正】页面每 30 秒从 ./data.json 同步（syncFromServer），data.json 才是面板真源；
    # index.html 内联的 embeddedSimData 只是本地无数据时的冷启动种子（当前为 2026-09-23 快照），
    # 用它计算收益会与面板显示严重不符 —— 不再覆盖。
    data = refresh_assets(data)   # 用实时行情重算，与页面显示一致
    now = cn_now()
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
                # 买入行带上买入时间 + 买入原因，便于复盘归因
                tm = fmt_trade_time(t)
                rs = (t.get("reason") or "").strip() or "综合评分达标"
                content += f"- 📈 {role_id} 买入 {t['name']} {t['qty']}股 @{t['price']:.2f}｜🕐{tm}｜💡{rs}\n"
            else:
                pnl = t.get("profit", 0)
                tm = fmt_trade_time(t)
                content += f"- 📉 {role_id} 卖出 {t['name']} {t['qty']}股 @{t['price']:.2f} 盈亏{pnl:+.2f}｜🕐{tm}\n"
    
    content += "\n## 明日计划\n\n"

    # 【重大事项提前一天提醒】与前端 FIN_EVENTS 同源同步（index.html 财经大事日历）。
    # 只列 lv>=2 的事件：明天(或下一交易日)的排最前，重大(lv3)加 ⚠️ 标注。
    MAJOR_EVENTS = [
        {"d": "2026-10-14", "t": "9月CPI/PPI", "v": "国家统计局发布（预计）", "lv": 2},
        {"d": "2026-10-15", "t": "9月国民经济数据", "v": "工业/固投/社零（预计）", "lv": 2},
        {"d": "2026-10-20", "t": "10月LPR报价", "v": "贷款市场报价利率", "lv": 2},
        {"d": "2026-10-29", "t": "美联储FOMC决议", "v": "10/27-28会议 · 决议公布（北京时间02:00）", "lv": 3},
        {"d": "2026-10-31", "t": "10月官方PMI", "v": "制造业/非制造业 9:30", "lv": 2},
        {"d": "2026-11-04", "t": "美国非农就业", "v": "10月非农（北京时间21:30，预计）", "lv": 2},
        {"d": "2026-11-10", "t": "10月CPI/PPI", "v": "国家统计局发布（预计）", "lv": 2},
        {"d": "2026-11-15", "t": "10月国民经济数据", "v": "工业/固投/社零（预计）", "lv": 2},
        {"d": "2026-11-20", "t": "11月LPR报价", "v": "贷款市场报价利率", "lv": 2},
        {"d": "2026-11-27", "t": "1-10月工业企业利润", "v": "国家统计局 9:30", "lv": 1},
        {"d": "2026-11-30", "t": "11月官方PMI", "v": "制造业/非制造业 9:30", "lv": 2},
    ]
    upcoming = [e for e in MAJOR_EVENTS if e["d"] > today]
    upcoming.sort(key=lambda e: e["d"])
    if upcoming:
        content += "\n## 📅 未来重大事项（提前预警）\n\n"
        for e in upcoming[:5]:
            star = "⚠️ **重大**" if e.get("lv") == 3 else "重要" if e.get("lv") == 2 else "参考"
            gap = ""
            try:
                dd = (datetime.strptime(e["d"], "%Y-%m-%d").date() - now.date()).days
                gap = "（明天）" if dd == 1 else f"（{dd}天后）"
            except Exception:
                pass
            content += f"- {star} {e['d'][5:]} {e['t']} · {e['v']}{gap}\n"

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
    # 大盘走势过程（不只看收盘：K线形态 + 当日分时 + 总体成交量）
    content += f"\n## 大盘走势过程（K线形态/当日分时/总体成交量）\n{build_sentiment_block()}\n"
    # 国家队资金动向（最高优先级：先人一步探测资金异动）
    content += f"\n## 国家队资金动向（最高优先级）\n{build_national_team_block()}\n"
    # 底部探测：政策底（消息面）+ 技术底（量价）+ 阶段传导
    content += f"\n## 底部探测（政策底/技术底）\n{build_bottom_block()}\n"
    # 根据实际操作小幅优化策略（国家队异动作为领先指标）
    notes = adaptive_tune(data)
    ntf = national_team_factor()
    if ntf != 1.0:
        notes.append(f"国家队异动评分影响仓位系数 → {ntf:.2f}（异动确认则领先加仓，未确认则偏防御）")
    if notes:
        content += "\n## 策略微调（小幅）\n" + "\n".join(f"- {n}" for n in notes) + "\n"

    push_report(title, content)
    print("  ✅ 每日收盘总结已推送")
    orig["last_daily_push"] = today
    # 落库供前端展示：页面收盘时没打开也能看到最近一次收盘小结
    orig["last_summary"] = {"date": today, "title": title, "content": content[:6000]}

    # 周总结（周五收盘） / 月总结（月末最后交易日）
    if is_week_end(now):
        print("\n📅 周五收盘，追加每周总结...")
        send_period_summary(data, "week", market_trend)
    if is_month_end(now):
        print("\n🗓️ 月末最后交易日，追加每月总结...")
        send_period_summary(data, "month", market_trend)

# ==================== 交易逻辑 ====================
def can_sell_t1(hold):
    bt = hold.get("buyTime") or hold.get("buy_time") or hold.get("buyDate") or hold.get("time") or ""
    return bt[:10] != cn_now().strftime("%Y-%m-%d")

# ==================== D7 周线超卖反转（与前端 calcWeeklyKD/calcDailyKD 同口径） ====================
WEEKLY_KD_CACHE = {}  # code -> {t,kd,kdPrev,turnUp,ema21,ema21Up}，10分钟TTL（对齐前端 wkKDCache）

def _tx_kline(code, scale, count):
    """腾讯K线（与前端 txKline 同源）：scale=day/week，返回原始行列表（无数据返回 []）"""
    prefix = "sh" if code.startswith("6") else "sz"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{code},{scale},,{count},qfq"
    r = requests.get(url, timeout=8)
    j = r.json()
    node = j.get("data", {}).get(f"{prefix}{code}", {})
    return node.get(f"qfq{scale}") or node.get(scale) or []

def _hlc3_kd(bars):
    """HLC3=(H+L+C)/3 典型价随机指标：%K(14)−50 居中，%D=%K的5周期SMA。
    turnUp=%K由降转升 或 %K上穿%D（与前端一致）。bars 需 ≥25 根。"""
    if not bars or len(bars) < 25:
        return None
    hlcs = [(float(b[3]) + float(b[4]) + float(b[2])) / 3 for b in bars]
    k_raw = []
    for i in range(13, len(hlcs)):
        win = hlcs[i - 13:i + 1]
        lo, hi = min(win), max(win)
        k = (hlcs[i] - lo) / (hi - lo) * 100 if hi > lo else 50
        k_raw.append(k - 50)
    if len(k_raw) < 7:
        return None
    d_last = sum(k_raw[-5:]) / 5
    d_prev = sum(k_raw[-6:-1]) / 5
    kd, kd_prev, kd_prev2 = k_raw[-1], k_raw[-2], k_raw[-3]
    turn_up = (kd > kd_prev and kd_prev <= kd_prev2) or (kd > d_last and kd_prev <= d_prev)
    return {"kd": kd, "kdPrev": kd_prev, "kdPrev2": kd_prev2,
            "dLast": d_last, "dPrev": d_prev, "turnUp": turn_up}

def get_weekly_kd(code):
    """D7 周线过滤器：腾讯周K 80根（≈1.5年，够14窗口+5平滑+EMA21）。
    返回 {t,kd,kdPrev,turnUp,ema21,ema21Up}；网络/数据异常返回 None。"""
    import time as _t
    now = _t.time()
    hit = WEEKLY_KD_CACHE.get(code)
    if hit and now - hit["t"] < 600:
        return hit
    try:
        bars = _tx_kline(code, "week", 80)
        kd = _hlc3_kd(bars)
        if not kd:
            return None
        closes = [float(b[2]) for b in bars]
        ema = closes[0]
        ema_prev = ema
        for c in closes[1:]:
            ema_prev = ema
            ema = c * (2 / 22) + ema * (20 / 22)
        rec = {"t": now, "kd": round(kd["kd"], 1), "kdPrev": round(kd["kdPrev"], 1),
               "turnUp": kd["turnUp"], "ema21": round(ema, 2), "ema21Up": ema > ema_prev}
        WEEKLY_KD_CACHE[code] = rec
        return rec
    except Exception as e:
        print(f"  周线KD获取失败 {code}: {e}")
        return None

def get_daily_kd(code):
    """日线同款过滤器（HLC3 KD14/5 −50，D7 另一半止盈：读数≥+40 超买离场）"""
    try:
        kd = _hlc3_kd(_tx_kline(code, "day", 30))
        if not kd:
            return None
        return {"kd": round(kd["kd"], 1), "d": round(kd["dLast"], 1)}
    except Exception:
        return None

def check_buy_d7(player, all_codes_data, market_trend, all_players=None):
    """D7 周线超卖反转·开仓（复刻前端 D7 分支口径）：
    ① 基础过滤（持仓/ST/涨跌停/跨角色重合/大盘择时）② 全局风控闸照过
    ③ 日线粗筛超卖形态（RSI6≤45 或 贴近20日低点）④ top8 拉周线KD：
       kd≤−40 且 %K 拐头向上才开仓。打分：base85 + EMA21向上8 + 当日未深跌5。"""
    if len(player["holdings"]) >= MAX_POS:
        return None
    if market_trend == "bear" and len(player["holdings"]) >= 2:
        return None
    shortlist = []
    for code, st in all_codes_data.items():
        if any(h["code"] == code for h in player["holdings"]):
            continue
        if st["chg"] >= 9.8 or st["chg"] <= -9.8:
            continue
        if "ST" in st.get("name", ""):
            continue
        if all_players:
            overlap = sum(1 for p in all_players if any(h["code"] == code for h in p["holdings"]))
            if overlap >= MAX_OVERLAP:
                continue
        guard_ok, _why = global_guard(st)
        if not guard_ok:
            continue
        rsi = st.get("rsi6", 50)
        price = st["price"]
        lo20 = st.get("lo20") or price
        if rsi <= 45 or price <= lo20 * 1.06:
            heat = (50 - rsi) + max(0.0, (lo20 * 1.06 - price) / price * 100)
            shortlist.append((code, st, heat))
    shortlist.sort(key=lambda x: -x[2])
    best = None
    for code, st, _heat in shortlist[:8]:
        w = get_weekly_kd(code)
        if not w or w["kd"] > -40 or not w["turnUp"]:
            continue
        score = 85
        why = [f"周线超卖{w['kd']:.1f}且拐头向上"]
        if w["ema21Up"]:
            score += 8
            why.append("周线EMA21向上·趋势修复中")
        if 0 <= st["chg"] < 5:
            score += 5
            why.append("当日未深跌·反转初期")
        if best is None or score > best[2]:
            best = (code, st, score, " + ".join(why))
    if best:
        print(f"  🎯 D7 周线超卖反转候选：{best[1]['name']}({best[0]}) 评分{best[2]}（{best[3]}）")
    return best

def d7_sell_signal(hold, st):
    """D7 分批止盈（复刻前端）：未半仓时 周线EMA21触及 或 日线KD≥+40 → 减半；
    已半仓时 周线kd≥−30且拐头向下 或 日线KD≥+50 → 清仓；−10% 硬止损。
    返回 (hold, reason, ratio, 标记半仓) 或 None。"""
    bp = hold.get("buyPrice") or hold.get("cost") or 0
    price = st["price"]
    if bp <= 0:
        return None
    chg = (price - bp) / bp
    if chg <= -0.10:
        return (hold, f"周线超卖反转·跌破10%止损位·清仓（{chg*100:.1f}%）", 1.0, False)
    w = get_weekly_kd(hold["code"])
    d = get_daily_kd(hold["code"])
    if not hold.get("partialTaken"):
        ema_hit = bool(w) and price > 0 and price >= w["ema21"] * 0.995
        day_ob = bool(d) and d["kd"] >= 40
        if ema_hit or day_ob:
            why = "周线EMA21" if ema_hit else f"日线KD{d['kd']:.1f}超买"
            return (hold, f"周线超卖反转·第一止盈（{why}）减半落袋", 0.5, True)
    else:
        wk_rev = bool(w) and w["kd"] >= -30 and not w["turnUp"]
        day_deep = bool(d) and d["kd"] >= 50
        if wk_rev or day_deep:
            return (hold, "周线超卖反转·第二止盈（日线超买加深/周线反转）清仓剩余", 1.0, False)
    return None

def check_sell_signals(player, stock_data):
    signals = []
    role = ROLES[player["id"]]
    for hold in player["holdings"]:
        if hold["code"] not in stock_data or not can_sell_t1(hold): continue
        st = stock_data[hold["code"]]
        # D7 周线超卖反转：分批止盈/硬止损专属逻辑优先，未命中再走下方通用规则
        if player["id"] == "D7":
            sig = d7_sell_signal(hold, st)
            if sig:
                h, reason, ratio, mark_half = sig
                if mark_half:
                    h["partialTaken"] = True
                signals.append((h, reason, ratio))
                continue
        bp = hold.get("buyPrice") or hold.get("cost") or 0
        chg = (st["price"] - bp) / bp * 100
        if chg <= -role["sl"]: signals.append((hold, f"止损-{abs(chg):.1f}%", 1.0)); continue
        if chg >= role["tp"]: signals.append((hold, f"止盈+{chg:.1f}%", 1.0)); continue
        if st["price"] < st["ma20"] and st["ma5"] < st["ma10"]: signals.append((hold, "跌破MA20+均线走弱", 1.0)); continue
        if st["kdj_k"] < st["kdj_d"] and st["kdj_k"] > 70: signals.append((hold, "KDJ高位死叉", 1.0)); continue
        if st["vol_ratio"] > 1.5 and st["chg"] < -2: signals.append((hold, "放量下跌主力出逃", 1.0)); continue
    return signals

def role_pick_score(role_id, st):
    """【v2策略升级】各角色核心逻辑打分（0-100，与前端实时引擎对齐）→ (score, reasons)"""
    price = st["price"]
    chg = st.get("chg", 0)
    vol = st.get("vol_ratio", 1) or 1
    ma5 = st.get("ma5") or price
    ma10 = st.get("ma10") or price
    ma20 = st.get("ma20") or price
    ma60 = st.get("ma60") or ma20
    ma20_prev = st.get("ma20_prev") or ma20
    rsi = st.get("rsi6", 50)
    hi20 = st.get("hi20") or price
    trend_up = ma20 > ma60 and ma20 > ma20_prev      # 中期趋势向上且MA20翘头
    ma_align = ma5 > ma20
    score, why = 0, []
    if role_id == "D1":
        # 趋势突破：大结构 + 放量突破前高 + 贴线不追高
        if trend_up: score += 25; why.append("MA20>MA60且向上")
        if ma_align: score += 10; why.append("MA5>MA20")
        if price >= ma20 and (price - ma20) / ma20 <= 0.03: score += 15; why.append("站稳MA20不追高")
        if hi20 > 0 and price >= hi20 * 0.97: score += 20; why.append("突破/贴近前20日高点")
        if 1.2 <= vol <= 3: score += 20; why.append("放量确认·量比%.1f" % vol)
        if 50 <= rsi <= 75: score += 5
    elif role_id == "D2":
        # 多因子共识：六因子满足数（≥4条才考虑）
        n = 0
        if 0.8 <= vol <= 2.5: n += 1; why.append("量能适中")
        if 45 <= rsi <= 70: n += 1; why.append("RSI健康")
        if ma5 > ma10 > ma20: n += 1; why.append("均线多头")
        if trend_up: n += 1; why.append("MA20向上")
        if 0.5 <= chg <= 5: n += 1; why.append("涨幅适中")
        if price > ma5: n += 1; why.append("收在MA5上方")
        score = 55 + (n - 4) * 8 if n >= 4 else n * 10
    elif role_id == "D3":
        # 多空辩论：逐条计票，多方须≥空方+2；大资金（放量方向）流出一票否决；追高不辩
        bull = bear = 0
        if ma5 > ma20: bull += 1
        else: bear += 1
        if price > ma20: bull += 1
        else: bear += 1
        if chg > 0: bull += 1
        else: bear += 1
        if vol >= 1: bull += 1
        else: bear += 1
        if 50 <= rsi <= 65: bull += 1
        else: bear += 1
        if chg > 2 and vol >= 1.3: bull += 1; why.append("大资金放量流入")
        elif chg < -2 and vol >= 1.3: bear += 1; why.append("大资金放量流出")
        if chg < -2 and vol >= 1.3: return 0, ["大资金流出·一票否决"]
        if chg > 4 or vol > 2: return 0, ["追高不辩·等回调"]
        margin = bull - bear
        if margin < 2: return 0, ["多空%d:%d·多方未占优" % (bull, bear)]
        score = round(margin / 6 * 80) + 10
        why.append("多空%d比%d" % (bull, bear))
    elif role_id == "D4":
        # 量价共振：MA20>MA60 + 触前20日高点 + 量比1.2-3 + RSI45-76，全过才70起步
        if not (ma20 > ma60 and price >= ma20 and hi20 > 0 and price >= hi20 * 0.995
                and 1.2 <= vol <= 3 and 45 <= rsi <= 76):
            return 0, []
        score = 70; why.append("量价共振突破确认")
        if vol <= 2.5: score += 10; why.append("量比健康")
        if 45 <= rsi <= 70: score += 5
    elif role_id == "D5":
        # 回踩低吸：趋势向上为前提 + 缩量回踩MA20/MA10企稳 + 收阳
        if not trend_up: return 0, ["MA20未向上·不接下跌反弹"]
        score = 20; why.append("MA20>MA60趋势向上")
        if ma_align: score += 5
        if price > ma20 and abs(price - ma20) / ma20 < 0.02: score += 25; why.append("回踩MA20")
        elif ma10 > 0 and price > ma10 and abs(price - ma10) / ma10 < 0.015: score += 20; why.append("回踩MA10企稳")
        else: return 0, ["未回踩到支撑位"]
        if vol < 0.9: score += 20; why.append("缩量·量比%.1f" % vol)
        if -1 < chg < 2: score += 10
    elif role_id == "D6":
        # 涨停敢死队：反包/强势涨停 或 涨停回调洗盘（不追普通上涨）
        if chg >= 9.5 and vol >= 1:
            score = 85; why.append("反包/强势涨停")
        elif st.get("has_zt5") and price > (st.get("zt_low") or 0) and vol < 1 and chg < 5:
            zt_low = st["zt_low"]
            near_low = (price - zt_low) / zt_low * 100
            high5 = st.get("high5") or price
            pb = (high5 - price) / high5 * 100 if high5 > 0 else 0
            if near_low > 8 or pb < 5 or pb > 25 or not ma_align:
                return 0, ["涨停回调条件不符"]
            score = 75; why.append("涨停回调洗盘")
            if 40 <= rsi <= 65: score += 5
        else:
            return 0, []
    return score, why

def global_guard(st):
    """全局风控闸（与前端 liveGlobalGuard / 回测 buySignal 同口径）：
    ① 近5日涨幅>20% ② 近10日>25% ③ 较20日低点反弹>30% ④ 连涨≥3天
    ⑤ 偏离MA5>4% ⑥ 高位放量长上影 ⑦ 高位放量十字星/双长影 ⑧ 高位放量大阳
    ⑨ 下跌中继拦截（距20日高点回撤>15%或近5日跌幅>12%）：仅企稳K线/低位十字星放行。
    任一字段缺失时该项自动跳过——宁可放行，绝不让缺数据变成恒拦截。
    返回 (是否放行, 拦截原因)。"""
    if not st or not st.get("price"):
        return True, ""
    price = st["price"]
    op = st.get("open") or price
    chg = st.get("chg") or 0
    v = st.get("vol_ratio") or 1
    ma5 = st.get("ma5") or 0
    hi20 = st.get("hi20") or 0
    lo20 = st.get("lo20") or 0
    day_high = st.get("high") or 0
    day_low = st.get("low") or 0
    body = abs(price - op)
    is_doji = body > 0 and price > 0 and (body / price) < 0.008
    upper = (day_high - max(price, op)) if day_high > 0 else 0
    lower = (min(price, op) - day_low) if day_low > 0 else 0
    is_high = hi20 > 0 and price >= hi20 * 0.95
    is_low = lo20 > 0 and price <= lo20 * 1.05
    c5 = st.get("close5") or 0
    c10 = st.get("close10") or 0
    if c5 > 0 and (price / c5 - 1) * 100 > 20:
        return False, f"近5日涨幅{(price/c5-1)*100:.0f}%·追高风险"
    if c10 > 0 and (price / c10 - 1) * 100 > 25:
        return False, f"近10日涨幅{(price/c10-1)*100:.0f}%·中期涨幅过大"
    if lo20 > 0 and (price / lo20 - 1) * 100 > 30:
        return False, f"较20日低点反弹{(price/lo20-1)*100:.0f}%·等回调"
    up_streak = (st.get("up_days") or 0) + (1 if chg > 0 else 0)
    if up_streak >= 3:
        return False, f"连涨{up_streak}天·情绪高点等回调"
    if ma5 > 0 and (price - ma5) / ma5 > 0.04:
        return False, f"偏离MA5 {(price-ma5)/ma5*100:.1f}%·等回均线"
    if is_high and v >= 1.5 and body > 0 and upper / body >= 1.5:
        return False, "高位放量长上影·见顶信号"
    if is_high and v >= 1.2 and is_doji:
        return False, "高位放量十字星·变盘信号"
    if is_high and v >= 1.3 and body > 0 and upper > body * 2 and lower > body * 2:
        return False, "高位放量双长影·变盘信号"
    if is_high and v >= 1.5 and chg > 3:
        return False, "高位放量大阳·不追高"
    is_deep_drop = (hi20 > 0 and (hi20 - price) / hi20 * 100 > 15) or \
                   (c5 > 0 and (price / c5 - 1) * 100 < -12)
    if is_deep_drop and not (is_low and is_doji and v < 1.3) and day_low > 0:
        is_stabilize = v < 0.8 and (lower > body * 2 or is_doji)
        if not is_stabilize:
            return False, "下跌中继反抽·需企稳K线（缩量+长下影）才买"
    return True, ""

def check_buy_signals(player, stock_data, all_codes_data, market_trend, consecutive_losses, strong_sectors, all_players=None):
    role_id = player["id"]
    # 【连续亏损熔断】连续3笔亏损，冷却
    if consecutive_losses.get(role_id, 0) >= 3:
        print(f"  ⏸️  {role_id} 连续亏损{consecutive_losses[role_id]}笔，熔断冷却中")
        return None
    # 【大盘择时】空头市场，最多2只持仓（D6除外）
    if market_trend == "bear" and role_id != "D6" and len(player["holdings"]) >= 2:
        return None
    if len(player["holdings"]) >= MAX_POS: return None

    # 【D7 周线超卖反转】独立分支：反趋势角色，不走日线打分管线与六步门禁，
    # 由周线 KD（HLC3 随机指标 14/5 −50）≤−40 且拐头向上闸门单独裁决
    if role_id == "D7":
        return check_buy_d7(player, all_codes_data, market_trend, all_players)

    # 【多Agent共识预计算】D2的开仓依据：其他角色各自的最高分候选，汇总共识度
    consensus = {}
    if role_id == "D2":
        for rid in ["D1", "D3", "D4", "D5", "D6", "D7"]:
            if rid == "D7":
                # D7 的最优候选来自周线KD闸门（含网络，10分钟缓存兜底）
                d7p = next((p for p in (all_players or []) if p.get("id") == "D7"), None)
                if d7p:
                    best_d7 = check_buy_d7(d7p, all_codes_data, market_trend, all_players)
                    if best_d7:
                        consensus[best_d7[0]] = consensus.get(best_d7[0], 0) + 1
                continue
            best = None
            for code, st in all_codes_data.items():
                if any(h["code"] == code for h in player["holdings"]): continue
                if "ST" in st.get("name", ""): continue
                sc, _ = role_pick_score(rid, st)
                if sc >= 60 and (best is None or sc > best[1]):
                    best = (code, sc)
            if best:
                consensus[best[0]] = consensus.get(best[0], 0) + 1

    candidates = []
    for code, st in all_codes_data.items():
        if any(h["code"] == code for h in player["holdings"]): continue
        if st["chg"] >= 9.8 or st["chg"] <= -9.8: continue
        if "ST" in st["name"]: continue
        # 【同板块持仓】放开硬性同行业数量限制（用户要求），仅保留轻量提示；
        # 角色间防重复由 MAX_OVERLAP（同一只股票最多 2 个角色持有）负责
        industry = STOCK_INDUSTRY.get(code, "其他")
        same_count = sum(1 for h in player["holdings"] if STOCK_INDUSTRY.get(h["code"], "其他") == industry)
        if same_count >= 3:
            print(f"  ℹ️  {role_id} {industry}板块已持{same_count}只，注意行业集中度")
        # 【跨角色重合限制】同一只股票最多 MAX_OVERLAP 个角色持有，避免六角色持仓大幅重复
        if all_players:
            overlap = sum(1 for p in all_players if any(h["code"] == code for h in p["holdings"]))
            if overlap >= MAX_OVERLAP:
                continue

        score, reason_parts = role_pick_score(role_id, st)
        reason_parts = list(reason_parts)
        # 【全局风控闸】与回测/前端同口径：追高/连涨/高位变盘/下跌中继一律拦截
        guard_ok, guard_why = global_guard(st)
        if not guard_ok:
            print(f"  🛡️  {role_id} 风控闸拦截 {st['name']}({code})：{guard_why}")
            continue
        # 【板块轮动】强势板块加分（前5名强势板块+10分）
        if industry in strong_sectors:
            score += 10
            reason_parts.append(f"{industry}板块强势")
        # 【多Agent共识】D2专属：其他角色≥3个指向同一只 → 共识加成
        if role_id == "D2" and consensus.get(code, 0) >= 3:
            score += 30
            reason_parts.append(f"多Agent共识{consensus[code]}/6")
        elif role_id == "D2" and consensus.get(code, 0) >= 2:
            score += 10
            reason_parts.append(f"部分共识{consensus[code]}/6")
        if score >= 60:
            candidates.append((code, st, score, " + ".join(reason_parts) or "综合评分达标"))

    if not candidates: return None
    candidates.sort(key=lambda x: x[2], reverse=True)
    return candidates[0]

def do_buy(player, code, st, score, reason, ntf=1.0):
    """执行买入（含仓位动态调整：胜率高的角色加仓，胜率低的减仓；ntf=国家队仓位系数）"""
    # 【仓位动态调整】根据历史胜率调整单笔仓位比例
    trades = player.get("trades", [])
    sells = [t for t in trades if t["type"] == "sell"]
    wins = [t for t in sells if t.get("profit", 0) > 0]
    win_rate = len(wins) / len(sells) if sells else 0.5
    rid = player.get("id")
    # 【回测调参】该角色在面板回测中被判定偏弱 → 暂停开仓
    if rid in ROLES and ROLES[rid].get("enabled") is False:
        print(f"  ⏸️  {rid} 回测调参已暂停开仓，跳过 {st['name']}")
        return
    # 胜率>60%：仓位提升到25%；胜率<40%：仓位降低到15%；其他：20%
    # 【回测调参】bt_tune.json 给出单笔仓位上限时优先使用（回测结论直接约束实盘仓位）
    cap = ROLES.get(rid, {}).get("pos_cap")
    if cap:
        position_ratio = cap
    elif win_rate > 0.6:
        position_ratio = 0.25
    elif win_rate < 0.4:
        position_ratio = 0.15
    else:
        position_ratio = 0.20
    # 【国家队领先指标】明确进场适度领先加仓(×1.15)，未见动作略偏防御(×0.9)
    budget = player["cash"] * position_ratio * ntf
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
        "buyPrice": cost_per, "buyTime": cn_now().isoformat(),
        "reason": reason, "score": score
    })
    player["trades"].append({
        "type": "buy", "code": code, "name": st["name"],
        "qty": qty, "price": st["price"], "fee": fee,
        "time": cn_now().isoformat(), "reason": reason
    })
    # 【每笔买入都推送】去掉"同角色同股同日只推一次"的按日去重（.push_* 标记文件），
    # 只要真实成交就推；统一走 push_report（WxPusher 优先、Server酱兜底），消息含时间+原因
    push_report(
        f"📈 {player['id']} 建仓 {st['name']}",
        f"股票：{st['name']}({code})\n数量：{qty}股\n价格：{st['price']:.2f}元\n成本：{cost_per:.2f}元\n评分：{score}分\n原因：{reason}\n⏰ T+1：明日可卖出\n时间：{cn_now().strftime('%Y-%m-%d %H:%M:%S')}"
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
    bp = hold.get("buyPrice") or hold.get("cost") or 0
    profit = money - bp * sell_qty - total_fee
    player["trades"].append({
        "type": "sell", "code": hold["code"], "name": hold["name"],
        "qty": sell_qty, "price": st["price"], "fee": total_fee,
        "profit": profit, "time": cn_now().isoformat(), "reason": reason
    })
    # 【连续亏损熔断】更新计数
    if profit < 0:
        data["consecutive_losses"][player["id"]] = data["consecutive_losses"].get(player["id"], 0) + 1
    else:
        data["consecutive_losses"][player["id"]] = 0
    
    hold["qty"] -= sell_qty
    if hold["qty"] <= 0:
        player["holdings"] = [h for h in player["holdings"] if h["code"] != hold["code"]]
    push_key = f".push_{player['id']}_{hold['code']}_sell_{cn_now().strftime('%Y%m%d')}"
    if not os.path.exists(push_key):
        open(push_key, "w").close()
        send_wechat(
            f"📉 {player['id']} 清仓 {hold['name']} {'盈利' if profit>=0 else '亏损'}{abs(profit):.2f}元",
            f"股票：{hold['name']}({hold['code']})\n数量：{sell_qty}股\n价格：{st['price']:.2f}元\n盈亏：{profit:.2f}元\n收益率：{profit/(bp*sell_qty)*100:.2f}%\n原因：{reason}\n时间：{cn_now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
    print(f"  ✅ {player['id']} 卖出 {hold['name']} {sell_qty}股 @{st['price']:.2f} 盈亏{profit:.2f}")

# ==================== 主函数 ====================
def main():
    print(f"{'='*50}")
    print(f"  AI六角色交易引擎启动 {cn_now().strftime('%Y-%m-%d %H:%M:%S')} (北京时间 UTC+8)")
    print(f"{'='*50}")
    
    data = load_data()
    bt_notes = load_bt_tune()   # 回测调参（bt_tune.json）优先于出厂值，adaptive_tune 再在其基础上微调
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
    if is_close_time() or FORCE_SUMMARY:
        if FORCE_SUMMARY:
            print("\n🔧 FORCE_SUMMARY=1：忽略时间窗口，强制发送每日总结")
        else:
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
        # 非交易时段也刷新资产快照并落盘：data.json 必须带正确的 asset 字段。
        # 面板每 30 秒同步它（syncFromServer），asset 缺失时会退化用 cash，
        # 持仓市值被忽略，导致面板与推送口径不一致。
        data = refresh_assets(data)
        save_data(data)
        print("  ⏸️  非交易时间（已刷新并保存资产快照），退出")
        return

    # 【国家队领先指标】异动强度 → 仓位系数（先于普通情绪反应，应用到实际买入仓位）
    ntf = national_team_factor()
    print(f"  🛡️ 国家队仓位系数：{ntf:.2f}")

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
                                        market_trend, data.get("consecutive_losses", {}), strong_sectors,
                                        all_players=data["players"])
        if buy_signal:
            code, st, score, reason = buy_signal
            do_buy(player, code, st, score, reason, ntf)
    
    print(f"\n💰 更新资产...")
    for player in data["players"]:
        total_value = player["cash"]
        for hold in player["holdings"]:
            if hold["code"] in all_stock_data:
                total_value += all_stock_data[hold["code"]]["price"] * hold["qty"]
        player["asset"] = total_value
        ah = player.get("assetHistory") or player.get("asset_history")
        if ah is None:
            ah = [INIT_CASH]
        ah.append(total_value)
        if len(ah) > 252:
            ah = ah[-252:]
        player["assetHistory"] = ah
        player["asset_history"] = ah
        total_return = (total_value - INIT_CASH) / INIT_CASH * 100
        print(f"  {player['id']}: 总资产{total_value:.0f} 收益{total_return:+.2f}%")
    
    save_data(data)
    print(f"\n{'='*50}")
    print(f"  ✅ 交易引擎完成，数据已保存")
    print(f"{'='*50}")

if __name__ == "__main__":
    main()
