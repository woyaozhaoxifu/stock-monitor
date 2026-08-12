#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实时行情看板 v2 —— 全腾讯财经数据源
- A股 / 美股 / 港股 / 日股 / 韩股：全部走腾讯 qt.gtimg.cn（国内直连）
- K线 / 分时：走腾讯 web.ifzq.gtimg.cn
- 零依赖 + pywebview 原生窗口（可选）
"""

import json
import math
import os
import shutil
import re
import time
import concurrent.futures
import threading
import webbrowser
import urllib.request
import urllib.parse
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
INDEX_PATH = os.path.join(BASE_DIR, "index.html")

# ---- 腾讯数据源 ----
TX_RT_BASE = "http://qt.gtimg.cn/q="
TX_KLINE_BASE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
SMARTBOX_URL = "http://smartbox.gtimg.cn/s3/"      # 腾讯智能搜索（中文/代码/拼音/英文）
TX_TREND_BASE = "https://web.ifzq.gtimg.cn/appstock/app/minute/query"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}

# 韩股 / 日股 中文名映射（腾讯只回英文名）
KR_NAMES = {
    "kr005930": "三星电子", "kr000660": "SK海力士", "kr005380": "现代汽车",
    "kr035420": "NAVER", "kr000270": "起亚", "kr051910": "LG化学",
    "kr005490": "浦项制铁", "kr207940": "三星生物", "kr373220": "LG新能源",
}
JP_NAMES = {
    "jp6758": "索尼", "jp9984": "软银集团", "jp7974": "任天堂",
    "jp8306": "三菱日联", "jp7203": "丰田汽车", "jp6861": "基恩士",
    "jp8035": "东京电子", "jp9432": "日本电信电话", "jp7751": "佳能",
}

_config_lock = threading.Lock()
_breadth_cache = {"ts": 0, "data": None}
_breadth_ttl = 30
_boards_cache = {"ts": 0, "data": None}   # 新浪行业板块原始数据（板块榜+涨跌家数共用）
_boards_ttl = 15
_kline_sym_cache = {}                     # 腾讯行情代码 → K线接口实际可用的代码形式
_yvol_cache = {}                           # 昨日成交量(手) 缓存：tx_sym → volume（每日仅变一次）

# 板块数据源（新浪财经行业板块，国内直连，无需代理）
VERSION = "2.10"


def _yesterday_volume(tx_sym):
    """从日K线取上一交易日成交量(手)，用于量能(较昨日)对比。结果按日缓存。"""
    if tx_sym in _yvol_cache:
        return _yvol_cache[tx_sym]
    v = None
    try:
        url = "%s?param=%s,day,,,6,qfq" % (TX_KLINE_BASE, tx_sym)
        req = urllib.request.Request(url, headers={**HEADERS, "Referer": "https://finance.qq.com/"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        root = (data.get("data") or {})
        node = root.get(tx_sym) or (list(root.values())[0] if root else None)
        if node:
            rows = node.get("qfqday") or node.get("day") or []
            if len(rows) >= 2:
                # rows[-1] 今日, rows[-2] 上一交易日；每行 [日期,开,收,高,低,量]
                v = float(rows[-2][5])
    except Exception:
        v = None
    _yvol_cache[tx_sym] = v
    return v


def get_market_volratio():
    """两市量能(较昨日)：今日沪+深成交量(手) / 昨日沪+深成交量(手)。
    腾讯免费源不提供可靠量比，用此真实比值判定缩量/放量。
    """
    try:
        q = fetch_quotes_batch(["sh000001", "sz399001"])
        t_sh = q.get("sh000001", {}).get("volume")
        t_sz = q.get("sz399001", {}).get("volume")
        if not (t_sh and t_sz):
            return {"ratio": None, "label": None}
        y_sh = _yesterday_volume("sh000001")
        y_sz = _yesterday_volume("sz399001")
        if not (y_sh and y_sz):
            return {"ratio": None, "label": None}
        ratio = (float(t_sh) + float(t_sz)) / (float(y_sh) + float(y_sz))
        if ratio < 0.9:
            label = "缩量"
        elif ratio < 0.97:
            label = "微缩"
        elif ratio <= 1.03:
            label = "平量"
        elif ratio <= 1.1:
            label = "微放"
        else:
            label = "放量"
        return {"ratio": round(ratio, 3), "label": label}
    except Exception:
        return {"ratio": None, "label": None}
SINA_BOARD_URL = "https://money.finance.sina.com.cn/q/view/newSinaHy.php"


# ================================================================
#  东方财富 secid → 腾讯 symbol 映射
# ================================================================

def em_to_tx(secid):
    """将东方财富格式的 secid 转为腾讯 qt.gtimg.cn 的 symbol。
    返回 (tx_symbol, display_name_hint) 或 None 表示不支持。
    """
    if secid.startswith("TX:"):
        return secid[3:], None  # 已经是腾讯格式

    if secid.startswith("1."):
        code = secid[2:]
        return f"sh{code}", None
    if secid.startswith("0."):
        code = secid[2:]
        return f"sz{code}", None

    # 美股指数
    us_idx_map = {
        "100.DJIA": ("usDJI", "道琼斯"),
        "100.SPX": ("us.INX", "标普500"),
        "100.NDX": ("us.NDX", "纳斯达克100"),
    }
    if secid in us_idx_map:
        return us_idx_map[secid]

    # 美股个股 105.XXXX
    if secid.startswith("105."):
        return f"us{secid[4:]}", None

    # 港股指数
    hk_idx_map = {
        "100.HSI": ("hkHSI", "恒生指数"),
    }
    if secid in hk_idx_map:
        return hk_idx_map[secid]

    # 港股个股 116.XXXXX → hkXXXXX (补0到5位)
    if secid.startswith("116."):
        code = secid[4:].zfill(5)
        return f"hk{code}", None

    # 日经225 / 韩国综合——腾讯无对应指数，返回None
    if secid in ("100.N225", "100.KS11"):
        return None

    return None


# ================================================================
#  腾讯行情批量获取
# ================================================================

def _tx_get(url, timeout=10):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("gbk", "ignore")


def fetch_quotes_batch(symbols):
    """symbols 是腾讯格式列表（如 sh000001, usAAPL, hk00700）。
    返回 {symbol: dict}。
    """
    if not symbols:
        return {}
    # 腾讯支持逗号分隔批量查询，但一次太多会截断；分批每批15个
    BATCH = 15
    result = {}
    for i in range(0, len(symbols), BATCH):
        batch = symbols[i:i + BATCH]
        try:
            raw = _tx_get(TX_RT_BASE + ",".join(batch))
            # 解析 v_xxx="..." 格式
            for m in re.finditer(r'v_([^=]+)="([^"]*)"', raw):
                sym_key = m.group(1)
                body = m.group(2)
                if not body:
                    continue
                parts = body.split("~")
                item = _parse_tx_item(sym_key, parts)
                if item:
                    result[sym_key] = item
        except Exception as e:
            for s in batch:
                if s not in result:
                    result[s] = {"error": str(e)}
    return result


def _parse_tx_item(sym_key, parts):
    """解析一条腾讯行情 ~ 分隔记录为统一字典。
    腾讯字段（已验证固定位置）：
      [1]=名称 [2]=代码 [3]=现价 [4]=昨收 [5]=今开 [6]=成交量(手)
      [30]=时间 [31]=涨跌额 [32]=涨跌幅% [33]=最高 [34]=最低
      [35]="价/量/额"字符串 [37]=成交额(万)
      [38]=换手率(%)  ← 注意：这是换手率，不是量比！腾讯免费源不提供指数/个股可靠量比([48]对指数恒为-1)
    """
    def f(i):
        try:
            return float(parts[i]) if i < len(parts) and parts[i] else None
        except (ValueError, TypeError):
            return None

    if len(parts) < 6:  # 至少需要基本字段
        return None

    name = parts[1]
    code = parts[2]
    price = f(3)
    prev_close = f(4)
    openv = f(5)
    vol_raw = f(6)

    # 涨跌额/涨跌幅/最高/最低（统一位置）
    change = f(31) if len(parts) > 31 else None
    pct = f(32) if len(parts) > 32 else None
    high = f(33) if len(parts) > 33 else None
    low = f(34) if len(parts) > 34 else None

    # 换手率（腾讯 [38]，单位 %）。注：并非量比——腾讯免费源不提供可靠的指数量比
    # （指数 [48] 恒为 -1，个股 [48] 数值错乱），故不显示量比，改用换手率 + 两市量能(较昨日)对比。
    turnover_raw = f(38) if len(parts) > 38 else None
    turnover = turnover_raw if (turnover_raw not in (None, 0)) else None
    vr = None  # 量比：免费源无可靠来源，置空

    # 成交额：腾讯不同市场字段格式不一致，按前缀分别处理
    amount = None
    is_a = sym_key.startswith(("sh", "sz"))
    is_hk = sym_key.startswith("hk")
    is_us = sym_key.startswith("us")
    if is_a:
        # A股：[35]="价/量/额"(元) 优先；fallback [37]*10000(万)
        if len(parts) > 35 and parts[35] and "/" in parts[35]:
            segs = parts[35].split("/")
            if len(segs) >= 3:
                try:
                    amount = float(segs[2])
                except (ValueError, IndexError):
                    pass
        if amount is None and len(parts) > 37:
            amt_wan = f(37)  # 成交额(万)
            if amt_wan is not None:
                amount = amt_wan * 10000
    elif is_hk:
        # 港股：[37] 单位不一致——指数(字母代码如hkHSI)为"万"，个股(数字代码如hk00700)为"元"
        amt = f(37)
        if amt is not None:
            if sym_key[2:].isalpha():
                amount = amt * 10000
            else:
                amount = amt if 1e6 < amt < 1e13 else None
    elif is_us:
        # 美股：[37]=美元成交额(已是元)；指数常返回天文数，需防御
        amt = f(37)
        if amt is not None and 1e6 < amt < 1e12:
            amount = amt
    # 日股/韩股：腾讯不提供 [37]/[38]，amount/vr 保持 None

    # 涨跌幅兜底计算
    if pct is None and price is not None and prev_close and prev_close != 0:
        try:
            pct = round((price - prev_close) / prev_close * 100, 4)
        except Exception:
            pass

    # 涨跌额兜底计算（日韩部分标的 [31] 为空）
    if change is None and price is not None and prev_close is not None:
        try:
            change = price - prev_close
        except Exception:
            pass

    # 精度归一：腾讯日韩接口返回 6~8 位小数，统一收敛到 2 位便于展示
    if pct is not None:
        pct = round(pct, 2)
    if change is not None:
        change = round(change, 3)

    # 中文名覆盖
    cn = KR_NAMES.get(sym_key) or JP_NAMES.get(sym_key)
    if cn:
        name = cn

    return {
        "secid": sym_key,
        "code": code,
        "name": name,
        "price": price,
        "prevClose": prev_close,
        "open": openv,
        "change": change,
        "pct": pct,
        "high": high,
        "low": low,
        "volume": vol_raw,
        "amount": amount,
        "turnover": turnover,
        "vr": vr,
    }


# ================================================================
#  统一入口：secid列表 → 行情列表（保持原顺序）
# ================================================================

def fetch_quotes(secids):
    """secid 用东方财富格式或 TX: 前缀。内部自动转腾讯并批量拉取。"""
    tx_map = {}  # tx_symbol → original_secid
    tx_symbols = []
    unsupported = []

    for s in secids:
        r = em_to_tx(s)
        if r is None:
            unsupported.append(s)
        else:
            tx_sym, _ = r
            tx_map[tx_sym] = s
            tx_symbols.append(tx_sym)

    batch_result = fetch_quotes_batch(tx_symbols)

    out = []
    for s in secids:
        if s in unsupported:
            out.append({"secid": s, "name": "暂不支持", "error": "腾讯无此指数数据"})
            continue
        r = em_to_tx(s)
        if r is None:
            continue
        tx_sym = r[0]
        item = batch_result.get(tx_sym)
        if item:
            it = dict(item)
            it["secid"] = s  # 还原原始secid供前端使用
            out.append(it)
        else:
            out.append({"secid": s, "error": "无数据"})
    return out


# ================================================================
#  K线（腾讯）
# ================================================================

def _kline_once(sym, period, count):
    """请求一次K线并解析为列表；拿不到就返回空列表（不抛异常，便于多候选试探）。"""
    url = f"{TX_KLINE_BASE}?param={sym},{period},,,{count},qfq"
    try:
        req = urllib.request.Request(url, headers={**HEADERS, "Referer": "https://finance.qq.com/"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []

    # 腾讯返回结构：data → {代码: {"day": [[日期,开,收,高,低,量], ...], "qt": ...}}
    root = data.get("data") or {}
    node_obj = root.get(sym)
    if node_obj is None:
        node_obj = root.get(sym.replace(".", ""))
    if node_obj is None:                     # 兜底：取第一个字典值（返回key大小写可能不同）
        for v in root.values():
            if isinstance(v, dict):
                node_obj = v
                break
    node_obj = node_obj or {}

    # 周期字段名：不复权 day/week/month，复权 qfqday/qfqweek/qfqmonth
    node = []
    if isinstance(node_obj, dict):
        for k in (period, "qfq" + period, "hfq" + period):
            v = node_obj.get(k)
            if isinstance(v, list) and v:
                node = v
                break
    elif isinstance(node_obj, list):
        node = node_obj

    out = []
    for row in node:
        try:
            if isinstance(row, list):
                out.append({
                    "date": row[0], "open": float(row[1]), "close": float(row[2]),
                    "high": float(row[3]), "low": float(row[4]),
                    "vol": float(row[5]) if len(row) > 5 else 0,
                })
            elif isinstance(row, dict):
                out.append({
                    "date": row.get("day"), "open": float(row.get("open", 0)),
                    "close": float(row.get("close", 0)), "high": float(row.get("high", 0)),
                    "low": float(row.get("low", 0)), "vol": float(row.get("volume", 0)),
                })
        except (TypeError, ValueError, KeyError):
            continue
    return out


def _kline_candidates(tx_sym):
    """美股在K线接口必须带交易所后缀，行情接口却不带，需要试探：
       纳斯达克个股 usAAPL.OQ / 纽交所个股 usJPM.N / 指数 us.DJI、us.IXIC
       （已验证：usAAPL→2根，usAAPL.OQ→320根；usDJI→1根，us.DJI→320根）
    """
    cands = [tx_sym]
    if tx_sym.startswith("us") and "." not in tx_sym:
        body = tx_sym[2:]
        cands += [f"us{body}.OQ", f"us{body}.N", f"us.{body}"]
    hit = _kline_sym_cache.get(tx_sym)
    if hit:                                   # 命中过的形式优先，省掉试探请求
        cands = [hit] + [c for c in cands if c != hit]
    return cands


def fetch_kline(secid, klt="101"):
    klt_map = {"101": "day", "102": "week", "103": "month", "5": "m5", "15": "m15", "30": "m30", "60": "m60"}
    period = klt_map.get(klt, "day")
    count = 320 if period in ("day", "week", "month") else 240

    r = em_to_tx(secid)
    if r is None:
        return []
    tx_sym = r[0]

    best = []
    for cand in _kline_candidates(tx_sym):
        rows = _kline_once(cand, period, count)
        if len(rows) > len(best):
            best = rows
        if len(rows) >= 5:                    # 够画图了，记住这个代码形式
            _kline_sym_cache[tx_sym] = cand
            return rows
    return best


# ================================================================
#  分时（腾讯）
# ================================================================

def fetch_trends(secid):
    r = em_to_tx(secid)
    if r is None:
        return []
    tx_sym = r[0]

    url = f"{TX_TREND_BASE}?code={tx_sym}"
    last_err = None
    data = None
    for _ in range(2):                       # Clash 偶发 DNS 失败，重试一次
        try:
            req = urllib.request.Request(url, headers={**HEADERS, "Referer": "https://finance.qq.com/"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except Exception as e:
            last_err = e
            time.sleep(0.5)
    if data is None:
        raise Exception(f"分时获取失败: {last_err}")

    # 结构：data → {代码: {"data": {"data": ["HHMM 价 累计量 累计额", ...], "date": "YYYYMMDD"}, "qt": ...}}
    root = data.get("data") or {}
    node_obj = root.get(tx_sym)
    if node_obj is None:
        for v in root.values():
            if isinstance(v, dict):
                node_obj = v
                break
    node_obj = node_obj or {}

    inner = node_obj.get("data") or {}
    rows = inner.get("data") if isinstance(inner, dict) else inner
    if not isinstance(rows, list):
        return []

    out = []
    for line in rows:
        if not isinstance(line, str):
            continue
        segs = line.split()
        if len(segs) < 2:
            continue
        hhmm = segs[0]
        try:
            price = float(segs[1])
        except (ValueError, TypeError):
            continue
        # 均价 = 累计成交额 / (累计成交量 × 100)；指数无意义，用合理性校验过滤
        avg = None
        if len(segs) >= 4:
            try:
                cum_vol = float(segs[2])
                cum_amt = float(segs[3])
                if cum_vol > 0:
                    a = cum_amt / (cum_vol * 100)
                    if price > 0 and 0.7 * price < a < 1.3 * price:
                        avg = round(a, 3)
            except (ValueError, TypeError, ZeroDivisionError):
                pass
        out.append({
            "time": f"{hhmm[:2]}:{hhmm[2:]}" if len(hhmm) == 4 else hhmm,
            "price": price,
            "avg": avg,
        })
    return out


# ================================================================
#  板块涨幅（用腾讯概念板块排行替代）
# ================================================================

def _sina_boards_all():
    """拉取新浪全部行业板块原始数据（带 15 秒缓存，板块榜与涨跌家数共用一次请求）。
    新浪 vals 字段：[0]代码 [1]名称 [2]成分股数 [3]均价 [4]平均涨跌幅%
                    [6]成交量 [7]成交额(元) [8]领涨股代码 [12]领涨股名
    """
    now = time.time()
    if _boards_cache["data"] and now - _boards_cache["ts"] < _boards_ttl:
        return _boards_cache["data"]

    last_err = None
    for _ in range(3):
        try:
            url = SINA_BOARD_URL + ("?" if "?" not in SINA_BOARD_URL else "&") \
                  + "t=" + str(int(time.time() * 1000))
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"})
            raw = urllib.request.urlopen(req, timeout=12).read().decode("gbk", "ignore")
            m = re.search(r'S_Finance_bankuai_sinaindustry\s*=\s*(\{.*\})\s*;?\s*$', raw, re.S)
            if not m:
                raise ValueError("板块数据格式异常")
            body = m.group(1)
            items = []
            for entry in re.finditer(r'"([^"]+)":"([^"]*)"', body):
                vals = entry.group(2).split(",")
                if len(vals) < 8:
                    continue
                try:
                    pct = round(float(vals[4]), 2)
                except (ValueError, IndexError):
                    pct = 0.0
                try:
                    amount = float(vals[7])  # 元
                except (ValueError, IndexError):
                    amount = None
                try:
                    count = int(vals[2])
                except (ValueError, IndexError):
                    count = 0
                items.append({
                    "code": vals[0], "name": vals[1], "pct": pct,
                    "amount": amount, "count": count,
                    "leader": vals[12] if len(vals) > 12 else "",
                })
            if not items:
                raise ValueError("板块解析为空")
            _boards_cache["data"] = items
            _boards_cache["ts"] = now
            return items
        except Exception as e:
            last_err = e
            time.sleep(1.0)
    if _boards_cache["data"]:      # 网络抖动时宁可用旧数据也不空着
        return _boards_cache["data"]
    raise Exception(f"新浪板块数据获取失败: {last_err}")


def fetch_boards(pz=15):
    """板块涨幅榜——新浪财经行业板块（国内直连，无需代理）。
    返回板块名 / 涨跌幅% / 成交额(元) / 领涨股。失败时降级为主要指数热度。
    """
    try:
        items = sorted(_sina_boards_all(), key=lambda x: x.get("pct") or 0, reverse=True)
        return items[:pz]
    except Exception:
        pass
    # 全部失败，降级：用主要指数涨跌模拟板块热度
    try:
        test_codes = ["sh000001","sz399001","sz399006","sh000688","sz399852","sz399905"]
        data = fetch_quotes_batch(test_codes)
        items = []
        for sym, it in data.items():
            if it.get("pct") is not None and not it.get("error"):
                items.append({"code": sym, "name": it.get("name", sym),
                              "pct": it.get("pct", 0), "amount": it.get("amount")})
        items.sort(key=lambda x: x.get("pct") or 0, reverse=True)
        return items[:pz]
    except Exception:
        return []


# ================================================================
#  涨跌家数（用主要指数成分股近似 + 标注为估算）
# ================================================================

# ================================================================
#  涨跌家数（基于新浪官方实时行情 hq.sinajs.cn 逐只统计，真实值非估算）
# ================================================================

HQ_BASE = "https://hq.sinajs.cn/list="
HQ_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}

# 全A代码区间（市场前缀 + 数字段），从源头排除可转债(11/12)/ETF(5/15/16)/B股(900/200)
_A_RANGES = [
    ("sh", [(600, 605), (688, 689)]),   # 沪主板 + 科创板
    ("sz", [(0, 3), (300, 301)]),       # 深主板 + 创业板
    ("bj", [(83, 89), (92, 92)]),       # 北交所
]
_ASTOCK_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "astocks.json")
_astock_cache = {"ts": 0, "codes": None}
_astock_ttl = 21600                       # 代码清单 6 小时刷新一次


def _fetch_hq_chunk(chunk):
    """拉取一个 hq 批次的原始文本，失败返回空串。"""
    try:
        url = HQ_BASE + ",".join(chunk)
        return urllib.request.urlopen(
            urllib.request.Request(url, headers=HQ_HEADERS), timeout=10).read().decode("gbk", "ignore")
    except Exception:
        return ""


def _fetch_hq(chunks, workers=16):
    """并发拉取多个 hq 批次，返回原始文本列表（顺序与 chunks 一致）。"""
    out = [None] * len(chunks)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_fetch_hq_chunk, c): i for i, c in enumerate(chunks)}
        for f in concurrent.futures.as_completed(futs):
            out[futs[f]] = f.result()
    return [r or "" for r in out]


def _gen_a_candidates():
    """生成全A候选代码（含部分无效代码，后续用真实行情过滤）。"""
    codes = []
    for mkt, ranges in _A_RANGES:
        for lo, hi in ranges:
            for n in range(lo * 1000, hi * 1000 + 1000):
                codes.append("%s%06d" % (mkt, n))
    return codes


def _load_valid_codes(force=False):
    """返回有效的全A代码清单（hq 返回现价>0 的），带本地文件缓存。"""
    global _astock_cache
    now = time.time()
    if (not force) and _astock_cache["codes"] and now - _astock_cache["ts"] < _astock_ttl:
        return _astock_cache["codes"]
    fp = _ASTOCK_CACHE_FILE
    if (not force) and os.path.exists(fp):
        try:
            if now - os.path.getmtime(fp) < _astock_ttl:
                with open(fp, "r", encoding="utf-8") as f:
                    codes = json.load(f)
                if codes:
                    _astock_cache["codes"] = codes
                    _astock_cache["ts"] = now
                    return codes
        except Exception:
            pass
    # 重新生成：并发拉 hq，过滤出现价>0 的有效代码（排除转债/ETF/B股/停牌）
    cands = _gen_a_candidates()
    chunks = [cands[i:i + 200] for i in range(0, len(cands), 200)]
    valid = []
    for raw in _fetch_hq(chunks):
        for line in raw.strip().split("\n"):
            if "hq_str_" not in line:
                continue
            code = line[line.find("hq_str_") + len("hq_str_"):line.find("=")]
            body = line[line.find('"') + 1:line.rfind('"')]
            segs = body.split(",")
            if len(segs) < 4 or not segs[0]:
                continue
            try:
                price = float(segs[3])
            except (ValueError, IndexError):
                continue
            if price > 0:
                valid.append(code)
    valid = sorted(set(valid))
    try:
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(valid, f)
    except Exception:
        pass
    _astock_cache["codes"] = valid
    _astock_cache["ts"] = now
    return valid


def _hq_quotes(codes):
    """并发拉 hq.sinajs.cn，返回 {code: (prev_close, price, vol)}（仅现价>0 的）。"""
    out = {}
    chunks = [codes[i:i + 200] for i in range(0, len(codes), 200)]
    for raw in _fetch_hq(chunks):
        for line in raw.strip().split("\n"):
            if "hq_str_" not in line:
                continue
            code = line[line.find("hq_str_") + len("hq_str_"):line.find("=")]
            body = line[line.find('"') + 1:line.rfind('"')]
            segs = body.split(",")
            if len(segs) < 4:
                continue
            try:
                prev = float(segs[2])
                price = float(segs[3])
                vol = float(segs[8]) if len(segs) > 8 else 0.0
            except (ValueError, IndexError):
                continue
            if price > 0:
                out[code] = (prev, price, vol)
    return out


def fetch_breadth():
    """真实涨跌家数——逐只统计全A（沪深京）实时行情，非估算。"""
    codes = _load_valid_codes()
    if not codes:
        raise Exception("无有效股票代码清单")
    quotes = _hq_quotes(codes)
    up = down = flat = 0
    for prev, price, vol in quotes.values():
        if price > prev:
            up += 1
        elif price < prev:
            down += 1
        else:
            flat += 1          # 含停牌（现价=昨收）与平盘，保证 涨+跌+平=总数
    total_amt = 0.0
    try:
        idx = fetch_quotes_batch(["sh000001", "sz399001"])
        for it in idx.values():
            if it.get("amount"):
                total_amt += float(it["amount"])
    except Exception:
        pass
    return {
        "up": up, "down": down, "flat": flat,
        "total": len(quotes), "estimate": False, "basis": "sina-hq-realtime",
        "totalAmount": total_amt,
    }


def get_breadth():
    now = time.time()
    if _breadth_cache["data"] and now - _breadth_cache["ts"] < _breadth_ttl:
        return _breadth_cache["data"]
    try:
        d = fetch_breadth()
        _breadth_cache["data"] = d
        _breadth_cache["ts"] = now
    except Exception:
        if not _breadth_cache["data"]:
            _breadth_cache["data"] = {"up": 0, "down": 0, "flat": 0, "total": 0, "error": True}
    return _breadth_cache["data"]


# ================================================================
#  个股涨跌榜（基于 hq.sinajs.cn 全A实时行情，真实排序非估算）
# ================================================================

_rank_cache = {"ts": 0, "data": None}
_rank_ttl = 30


def _rank_data():
    """全A个股涨跌榜（实时）：hq.sinajs.cn 逐只取 名称/昨收/现价，算涨跌幅排序。
    仅保留沪深（北交所腾讯行情/图表不覆盖，剔除）。返回 涨幅榜 + 跌幅榜。"""
    codes = _load_valid_codes()
    if not codes:
        return {"gainers": [], "losers": [], "total": 0}
    chunks = [codes[i:i + 200] for i in range(0, len(codes), 200)]
    rows = []
    for raw in _fetch_hq(chunks):
        for line in raw.strip().split("\n"):
            if "hq_str_" not in line:
                continue
            code = line[line.find("hq_str_") + len("hq_str_"):line.find("=")]
            if not code.startswith(("sh", "sz")):   # 剔除北交所
                continue
            body = line[line.find('"') + 1:line.rfind('"')]
            segs = body.split(",")
            if len(segs) < 4 or not segs[0]:
                continue
            try:
                prev = float(segs[2])    # 昨收
                price = float(segs[3])   # 现价
            except (ValueError, IndexError):
                continue
            if price <= 0 or prev <= 0:
                continue
            pct = round((price - prev) / prev * 100, 2)
            secid = ("1." if code.startswith("sh") else "0.") + code[2:]
            rows.append({"code": code, "secid": secid, "name": segs[0], "price": price, "pct": pct})
    gainers = sorted(rows, key=lambda x: x["pct"], reverse=True)[:15]
    losers = sorted(rows, key=lambda x: x["pct"])[:15]
    return {"gainers": gainers, "losers": losers, "total": len(rows)}


def get_rank():
    now = time.time()
    if _rank_cache["data"] and now - _rank_cache["ts"] < _rank_ttl:
        return _rank_cache["data"]
    try:
        d = _rank_data()
        _rank_cache["data"] = d
        _rank_cache["ts"] = now
    except Exception as e:
        if not _rank_cache["data"]:
            _rank_cache["data"] = {"gainers": [], "losers": [], "total": 0, "error": str(e)}
    return _rank_cache["data"]


# ================================================================
#  搜索（腾讯模糊匹配）
# ================================================================

def _unescape_u(s):
    """把 \\uXXXX 转义还原成中文（腾讯 smartbox 返回的名称是转义形式）。"""
    return re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), s)


def _guess_a_secid(code):
    """6位A股/基金代码 → 东财格式 secid（1.=沪 0.=深）。"""
    if code.startswith(("6", "9", "5", "68")):        # 沪市股票/B股/ETF
        return "1." + code
    if code.startswith(("0", "3", "1", "2")):         # 深市股票/创业板/ETF
        return "0." + code
    return None


def search_stock(q, pz=10):
    """搜索股票——腾讯 smartbox 智能搜索。
    支持中文名（需 UTF-8 提交）、代码、拼音首字母、英文名。
    返回格式：v_hint="市场~代码~名称(\\u转义)~拼音~类型^市场~..."
    """
    if not q:
        return []
    q = q.strip()
    if not q:
        return []

    results, seen = [], set()
    try:
        # 注意：查询串必须 UTF-8 编码（GBK 会返回 "N" 无结果），响应是 GBK
        url = SMARTBOX_URL + "?v=2&t=all&q=" + urllib.parse.quote(q.encode("utf-8"))
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0",
                          "Referer": "https://stockapp.finance.qq.com/"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("gbk", "ignore")
        m = re.search(r'v_hint="(.*?)"', raw, re.S)
        body = m.group(1) if m else ""
        if body and body != "N":
            for ent in body.split("^"):
                parts = ent.split("~")
                if len(parts) < 3:
                    continue
                mkt, code = parts[0].strip(), parts[1].strip()
                name = _unescape_u(parts[2])
                typ = parts[4].strip() if len(parts) > 4 else ""
                if typ in ("KJ", "QZ"):      # 过滤开放式基金、权证/牛熊证噪音
                    continue
                secid = None
                if mkt in ("sh", "sz") and re.match(r'^\d{6}$', code):
                    secid = ("1." if mkt == "sh" else "0.") + code
                elif mkt == "hk":
                    if len(code) > 5:        # 5位以上多为涡轮/牛熊证
                        continue
                    secid = "TX:hk" + code
                elif mkt == "us":
                    secid = "TX:us" + code.split(".")[0].upper()
                if not secid or secid in seen:
                    continue
                seen.add(secid)
                results.append({"secid": secid, "code": code, "name": name,
                                "market": mkt, "type": typ})
    except Exception:
        pass

    # 纯6位代码兜底（搜索接口不可用时也能加自选，且市场前缀按规则判断，不再猜错）
    if not results and re.match(r'^\d{6}$', q):
        primary = _guess_a_secid(q)
        if primary:
            results.append({"secid": primary, "code": q,
                            "name": ("沪市" if primary.startswith("1.") else "深市") + q})
        other = "0." + q if (primary or "").startswith("1.") else "1." + q
        results.append({"secid": other, "code": q,
                        "name": ("深市" if other.startswith("0.") else "沪市") + q})

    return results[:pz]


# ================================================================
#  配置读写
# ================================================================

def _ensure_config():
    """config.json 不存在时，从 config.example.json 拷贝一份默认配置，
    保证首次克隆/运行即可启动，且不会覆盖用户已有的个人配置。"""
    if os.path.exists(CONFIG_PATH):
        return
    example = os.path.join(BASE_DIR, "config.example.json")
    if os.path.exists(example):
        try:
            shutil.copy(example, CONFIG_PATH)
        except Exception:
            pass


def load_config():
    _ensure_config()
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def add_watch(secid, name=None):
    with _config_lock:
        cfg = load_config()
        wl = cfg.setdefault("watchlist", {"label": "自选股", "secids": []})
        secids = wl.setdefault("secids", [])
        if secid not in secids:
            secids.append(secid)
        save_config(cfg)
    return cfg


def remove_watch(secid):
    with _config_lock:
        cfg = load_config()
        wl = cfg.get("watchlist", {})
        secids = wl.get("secids", [])
        if secid in secids:
            secids.remove(secid)
        save_config(cfg)
    return cfg


# ================================================================
#  用户持仓批量取价（复用腾讯实时行情，供前端持仓模块计算盈亏）
# ================================================================

def fetch_user_quotes(codes_str):
    """codes_str: 逗号分隔的 6 位代码或腾讯格式符号（如 600519 / sh600519）。
    返回 [{code, name, price, prevClose, pct, secid}]，无数据的给 error 字段。
    仅做展示用取价，不依赖任何配置。
    """
    raw = [c.strip() for c in (codes_str or "").split(",") if c.strip()]
    if not raw:
        return []
    tx_map = {}      # tx_symbol -> 原始输入（用于回显 6 位代码）
    tx_symbols = []
    for c in raw:
        if re.match(r'^\d{6}$', c):
            # 6 位代码 → 腾讯符号（沪 6/9、深 0/3、北交 8/4）
            if c[0] in ("6", "9"):
                tx = "sh" + c
            elif c[0] in ("0", "3"):
                tx = "sz" + c
            else:
                tx = "bj" + c
            tx_map[tx] = c
            tx_symbols.append(tx)
        else:
            tx_map[c] = c
            tx_symbols.append(c)
    result = fetch_quotes_batch(tx_symbols)
    out = []
    for tx in tx_symbols:
        it = result.get(tx)
        if it and not it.get("error"):
            out.append({
                "code": tx_map.get(tx, tx),
                "name": it.get("name"),
                "price": it.get("price"),
                "prevClose": it.get("prevClose"),
                "pct": it.get("pct"),
                "secid": tx,
            })
        else:
            out.append({"code": tx_map.get(tx, tx), "name": None,
                        "price": None, "prevClose": None, "pct": None, "error": "无数据"})
    return out


# ================================================================
#  市场资讯（财经快讯 + 公司公告，双源互补：本机新浪快讯 / 公网东财公告）
# ================================================================

SINA_NEWS_URL = "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2509&k=&num=20&page=1"
SINA_COMPANY_URL = "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2515&k=&num=20&page=1"
EM_NOTICE_URL = "https://np-anotice-stock.eastmoney.com/api/notice/query?page_size=20&page_index=1"

_news_cache = {"sina": {"ts": 0, "data": None}, "em": {"ts": 0, "data": None}, "sina_co": {"ts": 0, "data": None}}
_news_ttl = 300


def _http_json(url, timeout=7, referer=None):
    """通用 GET JSON（容错 JSONP 包裹），失败返回 (None, '')。"""
    try:
        hd = dict(HEADERS)
        if referer:
            hd["Referer"] = referer
        req = urllib.request.Request(url, headers=hd)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "ignore")
    except Exception:
        return None, ""
    m = re.search(r'\(\s*(\{.*\})\s*\)\s*;?\s*$', raw, re.S)
    if m:
        raw = m.group(1)
    try:
        return json.loads(raw), raw
    except Exception:
        return None, raw


def get_sina_news():
    now = time.time()
    c = _news_cache["sina"]
    if c["data"] is not None and now - c["ts"] < _news_ttl:
        return c["data"]
    data, _ = _http_json(SINA_NEWS_URL, referer="https://finance.sina.com.cn/")
    items = []
    if data:
        lst = data.get("result", {}).get("data", [])
        if isinstance(lst, list):
            for it in lst:
                if not isinstance(it, dict):
                    continue
                ts = it.get("ctime")
                try:
                    ts = int(ts)
                except (TypeError, ValueError):
                    ts = 0
                items.append({
                    "title": it.get("title", ""),
                    "url": it.get("url", ""),
                    "time": ts,
                    "source": it.get("media_name") or it.get("author") or "新浪财经",
                    "intro": (it.get("intro") or it.get("summary") or "")[:80],
                })
    res = items if items else None
    _news_cache["sina"] = {"ts": now, "data": res}
    return res


def get_em_notices():
    now = time.time()
    c = _news_cache["em"]
    if c["data"] is not None and now - c["ts"] < _news_ttl:
        return c["data"]
    data, _ = _http_json(EM_NOTICE_URL, referer="https://data.eastmoney.com/")
    items = []
    if data:
        d = data.get("data", {})
        lst = d.get("list") if isinstance(d, dict) else None
        if isinstance(lst, list):
            for it in lst:
                if not isinstance(it, dict):
                    continue
                code = it.get("security_code") or it.get("code") or ""
                items.append({
                    "title": it.get("title") or it.get("notice_title") or "",
                    "url": it.get("url") or it.get("notice_url") or "",
                    "time": it.get("notice_date") or it.get("eitime") or it.get("datetime") or "",
                    "source": ("东方财富公告" + (" · " + code if code else "")),
                    "intro": (it.get("summary") or it.get("digest") or "")[:80],
                })
    res = items if items else None
    _news_cache["em"] = {"ts": now, "data": res}
    return res


def get_sina_company_news():
    """新浪公司/股票要闻（lid=2515），作为东财公告不可达时的兜底源（两端机房均可通）。"""
    now = time.time()
    c = _news_cache["sina_co"]
    if c["data"] is not None and now - c["ts"] < _news_ttl:
        return c["data"]
    data, _ = _http_json(SINA_COMPANY_URL, referer="https://finance.sina.com.cn/")
    items = []
    if data:
        d = data.get("result", {}).get("data", [])
        lst = d.get("list", []) if isinstance(d, dict) else (d if isinstance(d, list) else [])
        for it in lst:
            if not isinstance(it, dict):
                continue
            ts = it.get("ctime")
            try:
                ts = int(ts)
            except (TypeError, ValueError):
                ts = 0
            items.append({
                "title": it.get("title", ""),
                "url": it.get("url", ""),
                "time": ts,
                "source": it.get("media_name") or it.get("author") or "新浪财经",
                "intro": (it.get("intro") or it.get("summary") or it.get("brief", ""))[:80],
            })
    res = items if items else None
    _news_cache["sina_co"] = {"ts": now, "data": res}
    return res


def get_news(src="sina"):
    if src == "em":
        items = get_em_notices()
        real = "em"
        if not items:                      # 东财公告不可达时，回退新浪公司要闻
            items = get_sina_company_news()
            real = "sina_co"
    else:
        items = get_sina_news()
        real = "sina"
    return {
        "src": real,
        "items": items or [],
        "updated": int(time.time() * 1000),
        "error": None if items else "暂无可用的资讯源（可能受网络限制）",
    }


LHB_URL = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
           "?reportName=RPT_DAILYBILLBOARD_GENERAL"
           "&columns=SECURITYCODE,SECURITYNAME,EXPLANATION,CLOSEPRICE,CHANGERATE,TURNOVER,TOTALBUY,TOTALSELL,NETBUY"
           "&pageSize=15&p=1&sortColumns=NETBUY&sortTypes=-1&source=WEB&client=WEB")
_billboard_cache = {"ts": 0, "data": None}
_billboard_ttl = 60


def get_billboard():
    now = time.time()
    c = _billboard_cache
    if c["data"] is not None and now - c["ts"] < _billboard_ttl:
        return c["data"]
    data, _ = _http_json(LHB_URL, referer="https://data.eastmoney.com/")
    items = []
    if data:
        d = data.get("data") if isinstance(data, dict) else None
        lst = []
        if isinstance(d, dict):
            v = d.get("list")
            if isinstance(v, list):
                lst = v
        elif isinstance(d, list):
            lst = d
        for it in lst:
            if not isinstance(it, dict):
                continue
            code = it.get("SECURITYCODE") or it.get("STOCKCODE") or it.get("CODE") or ""
            name = it.get("SECURITYNAME") or it.get("STOCKNAME") or it.get("NAME") or ""
            reason = it.get("EXPLANATION") or it.get("REASON") or ""
            pct = it.get("CHANGERATE")
            net = it.get("NETBUY") or it.get("BILLBOARD_NET_BUY")
            buy = it.get("TOTALBUY")
            sell = it.get("TOTALSELL")
            close = it.get("CLOSEPRICE")
            items.append({
                "code": code, "name": name, "reason": reason,
                "pct": pct, "net": net, "buy": buy, "sell": sell, "close": close,
            })
    res = items if items else None
    _billboard_cache["ts"] = now
    _billboard_cache["data"] = res
    return res


# ================================================================
#  状态聚合
# ================================================================

def build_state():
    cfg = load_config()
    market_ids = cfg.get("market", {}).get("secids", [])
    watch_ids = cfg.get("watchlist", {}).get("secids", [])
    global_groups = cfg.get("global", [])

    market_items = []
    watch_items = []

    try:
        if market_ids:
            market_items = fetch_quotes(market_ids)
    except Exception as e:
        market_items = [{"error": str(e)}]

    try:
        if watch_ids:
            watch_items = fetch_quotes(watch_ids)
    except Exception as e:
        watch_items = [{"error": str(e)}]

    global_out = []
    for g in global_groups:
        gids = g.get("secids", [])
        try:
            gitems = fetch_quotes(gids) if gids else []
        except Exception as e:
            gitems = [{"error": str(e)}]
        global_out.append({"label": g.get("label", "全球"), "items": gitems})

    # 两市成交额：仅上证 + 深证 两家主板（指数本身是全市场汇总，不能再相加，否则严重重复计算）
    total_amount = 0.0
    for it in market_items:
        if not isinstance(it, dict):
            continue
        if str(it.get("secid", "")) in ("1.000001", "0.399001", "sh000001", "sz399001"):
            if it.get("amount"):
                try:
                    total_amount += float(it["amount"])
                except (TypeError, ValueError):
                    pass
    # 量能(较昨日)：用真实成交量比值判定缩量/放量（腾讯免费源无可靠量比）
    volratio = get_market_volratio()
    avg_vr = None  # 量比不可靠，不再展示

    return {
        "ts": int(time.time() * 1000),
        "version": VERSION,
        "title": cfg.get("title", "实时行情看板"),
        "refreshSeconds": cfg.get("refreshSeconds", 3),
        "market": {"label": cfg.get("market", {}).get("label", "大盘指数"), "items": market_items},
        "watchlist": {"label": cfg.get("watchlist", {}).get("label", "自选股"), "items": watch_items},
        "global": global_out,
        "summary": {"totalAmount": total_amount, "avgVr": avg_vr,
                    "volRatio": volratio.get("ratio"), "volLabel": volratio.get("label")},
    }


# ================================================================
#  HTTP 服务
# ================================================================

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        self._send(200, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            try:
                with open(INDEX_PATH, "r", encoding="utf-8") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, "index.html not found")
            return
        if path == "/config.json":
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    self._send(200, f.read(), "application/json; charset=utf-8")
            except FileNotFoundError:
                self._send(404, "config.json not found")
            return
        if path == "/echarts.min.js":
            try:
                with open(os.path.join(BASE_DIR, "echarts.min.js"), "rb") as f:
                    self._send(200, f.read(), "application/javascript; charset=utf-8")
            except FileNotFoundError:
                self._send(404, "echarts.min.js not found")
            return
        if path == "/api/state":
            try:
                self._json(build_state())
            except Exception as e:
                self._json({"error": str(e)})
            return
        if path == "/api/boards":
            try:
                self._json({"items": fetch_boards(15)})
            except Exception as e:
                self._json({"error": str(e)})
            return
        if path == "/api/breadth":
            try:
                self._json(get_breadth())
            except Exception as e:
                self._json({"error": str(e)})
            return
        if path == "/api/rank":
            try:
                self._json(get_rank())
            except Exception as e:
                self._json({"error": str(e)})
            return
        if path == "/api/kline":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            secid = (qs.get("secid") or [""])[0]
            klt = (qs.get("klt") or ["101"])[0]
            try:
                self._json({"secid": secid, "klt": klt, "data": fetch_kline(secid, klt)})
            except Exception as e:
                self._json({"error": str(e)})
            return
        if path == "/api/trends":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            secid = (qs.get("secid") or [""])[0]
            try:
                self._json({"secid": secid, "data": fetch_trends(secid)})
            except Exception as e:
                self._json({"error": str(e)})
            return
        if path == "/api/search":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            q = (qs.get("q") or [""])[0]
            try:
                self._json({"q": q, "items": search_stock(q) if q else []})
            except Exception as e:
                self._json({"error": str(e)})
            return
        if path == "/api/quotes":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            codes = (qs.get("codes") or [""])[0]
            try:
                self._json({"items": fetch_user_quotes(codes)})
            except Exception as e:
                self._json({"error": str(e)})
            return
        if path == "/api/news":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            src = (qs.get("src") or ["sina"])[0]
            try:
                self._json(get_news(src))
            except Exception as e:
                self._json({"error": str(e)})
            return
        if path == "/api/billboard":
            try:
                items = get_billboard()
                self._json({"items": items or [], "error": None if items else "龙虎榜暂不可用（数据源受限）"})
            except Exception as e:
                self._json({"error": str(e)})
            return
        self._send(404, "not found")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            payload = {}
        if path == "/api/add_watch":
            secid = payload.get("secid")
            if secid:
                cfg = add_watch(secid, payload.get("name"))
                self._json({"ok": True, "secids": cfg.get("watchlist", {}).get("secids", [])})
            else:
                self._json({"ok": False, "msg": "missing secid"})
            return
        if path == "/api/remove_watch":
            secid = payload.get("secid")
            if secid:
                cfg = remove_watch(secid)
                self._json({"ok": True, "secids": cfg.get("watchlist", {}).get("secids", [])})
            else:
                self._json({"ok": False, "msg": "missing secid"})
            return
        if path == "/api/reorder_watch":
            new_order = payload.get("secids")
            if new_order and isinstance(new_order, list):
                with _config_lock:
                    cfg = load_config()
                    wl = cfg.setdefault("watchlist", {"label": "自选股", "secids": []})
                    wl["secids"] = new_order
                    save_config(cfg)
                self._json({"ok": True, "secids": new_order})
            else:
                self._json({"ok": False, "msg": "missing secids"})
            return
        self._send(404, "not found")

    def log_message(self, *args):
        pass


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ================================================================
#  启动
# ================================================================

def main():
    import socket, subprocess

    def pick_port(pref=8787):
        """选空闲端口；若 pref 被旧进程占用，先杀掉旧进程再绑定，避免连到旧版服务。"""
        for attempt in range(2):
            for port in range(pref, pref + 10):
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.3)
                try:
                    s.bind(("127.0.0.1", port))
                    s.close()
                    return port
                except OSError:
                    s.close()
                    if attempt == 0:
                        try:
                            out = subprocess.run(["netstat", "-ano"],
                                                 capture_output=True,
                                                 encoding="gbk", errors="ignore",
                                                 timeout=5).stdout
                            for line in out.splitlines():
                                if f":{port}" in line and "LISTENING" in line:
                                    pid = line.split()[-1]
                                    if pid.isdigit():
                                        subprocess.run(["taskkill", "/f", "/pid", pid],
                                                       capture_output=True, timeout=5)
                        except Exception:
                            pass
                        time.sleep(0.4)
        return pref

    port = pick_port(int(os.environ.get("PORT", "8787")))
    # 本地默认只监听回环；部署到云平台（Render 等，HOST 或 RENDER 环境变量）时监听 0.0.0.0 才能接收外部请求
    host = os.environ.get("HOST") or ("0.0.0.0" if os.environ.get("RENDER") else "127.0.0.1")
    server = ThreadingHTTPServer((host, port), Handler)
    shown = "127.0.0.1" if host == "0.0.0.0" else host
    url = f"http://{shown}:{port}/"
    try:
        app_title = load_config().get("title", "实时行情看板")
    except Exception:
        app_title = "实时行情看板"
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    try:
        import webview  # noqa
        has_wv = True
    except Exception:
        has_wv = False

    force_browser = os.environ.get("NO_WINDOW") == "1"
    print(f"[看板] 行情看板已启动（v{VERSION}，腾讯/新浪数据源）：{url}")
    if has_wv and not force_browser:
        try:
            print("[看板] 已打开原生窗口（可最大化/最小化/拖拽缩放）。")
            webview.create_window(
                app_title, url,
                width=1280, height=820, resizable=True, min_size=(720, 520),
            )
            webview.start()
        except Exception as e:
            print(f"[看板] 原生窗口打开失败（{e}），改用浏览器打开。")
            try:
                webbrowser.open(url)
            except Exception:
                pass
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
    else:
        if not has_wv:
            print("[看板] 未安装 pywebview，改用浏览器打开。")
            print("[看板] 想要原生窗口可运行：python -m pip install pywebview")
        try:
            webbrowser.open(url)
        except Exception:
            pass
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    print("\n[看板] 已关闭。")
    server.shutdown()


if __name__ == "__main__":
    main()
