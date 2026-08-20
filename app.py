#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实时行情看板 v3.0 · 詹姆斯是goat —— 全腾讯财经数据源
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
import datetime
import collections
import concurrent.futures
import threading
import webbrowser
import sys
import atexit
import tempfile
import urllib.request
import urllib.parse
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
HOLDINGS_PATH = os.path.join(BASE_DIR, "holdings.json")
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
_em_boards_cache = {"ts": 0, "data": None}   # 东财行业+概念板块（具体题材板块）
_kline_sym_cache = {}                     # 腾讯行情代码 → K线接口实际可用的代码形式
_preclose_cache = {}                        # 代码 → 昨收价（用于分时 / K线百分比轴）
_kline_cache = {}                          # (tx_sym, period, count) → {"ts":.., "rows":..} 短缓存，降低腾讯并发压力
_kline_ttl = 60
_yvol_cache = {}                           # 昨日成交量(手) 缓存：tx_sym → volume（每日仅变一次）

# 板块数据源（新浪财经行业板块，国内直连，无需代理）
VERSION = "3.0"


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

    # 东财格式：1.XXXXX(沪) / 0.XXXXX(深)
    if secid.startswith("1."):
        code = secid[2:]
        return f"sh{code}", None
    if secid.startswith("0."):
        code = secid[2:]
        return f"sz{code}", None

    # 腾讯直传格式：shXXXXX / szXXXXX（前端点击股票时直接传此格式）
    if secid.startswith("sh") or secid.startswith("sz"):
        return secid, None

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
    """请求一次K线并解析为列表；拿不到就返回空列表（不抛异常，便于多候选试探）。
    腾讯对并发请求会限流/偶发超时，这里做最多 3 次重试 + 退避，显著提升取数成功率。"""
    url = f"{TX_KLINE_BASE}?param={sym},{period},,,{count},qfq"
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={**HEADERS, "Referer": "https://finance.qq.com/"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except Exception as e:
            last_err = e
            if attempt < 2:
                time.sleep(0.4 * (attempt + 1))   # 0.4s / 0.8s 退避
    else:
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

    # 提取昨收（用于K线百分比轴）：腾讯 qt 实时块 [4]=昨收
    pre_close = None
    try:
        _qt = node_obj.get("qt") or {}
        _qarr = _qt.get(sym) if isinstance(_qt, dict) else None
        if isinstance(_qarr, list) and len(_qarr) > 4:
            pre_close = float(_qarr[4])
    except (ValueError, TypeError, AttributeError):
        pre_close = None

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
    # 兜底：qt 实时块常缺昨收（尤其非交易时段/历史复权数据），从日K数据本身推算：
    #   日K/周K/月K → 倒数第二根收盘价 ≈ 上一周期收盘（最接近"昨收"）
    #   分钟级 → 最后一根的前一根 close 也比没有强
    if pre_close is None and len(out) >= 2:
        pre_close = out[-2].get("close")
    return out, pre_close


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

    # 短缓存：同一标的同周期 60s 内直接返回，既加速又压低腾讯并发（避免被限流导致 K线取不到）
    cache_key = (tx_sym, period, count)
    now = time.time()
    c = _kline_cache.get(cache_key)
    if c and now - c["ts"] < _kline_ttl and c["rows"]:
        return c["rows"]

    best = []
    for cand in _kline_candidates(tx_sym):
        rows, pc = _kline_once(cand, period, count)
        if pc is not None:
            _preclose_cache[secid] = pc
            _preclose_cache[tx_sym] = pc
        if len(rows) > len(best):
            best = rows
        if len(rows) >= 5:                    # 够画图了，记住这个代码形式
            _kline_sym_cache[tx_sym] = cand
            _kline_cache[cache_key] = {"ts": now, "rows": rows}
            return rows
    if len(best) >= 5:                         # 仅缓存「够画图」的结果，避免把残缺数据缓存住
        _kline_cache[cache_key] = {"ts": now, "rows": best}
    return best


# ================================================================
#  分时（腾讯）
# ================================================================

def _em_minute(secid):
    """东财分时（美股指数/个股 fallback）。
    腾讯 minute/query 对美股指数只返回 1 点最新快照、无分时序列，
    故美股改用东财 push2delay 分时接口。返回 [{time,price,avg}]。"""
    url = ("https://push2delay.eastmoney.com/api/qt/stock/trends2/get?secid=%s"
           "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&iscr=0&ndays=1" % secid)
    try:
        req = urllib.request.Request(url, headers={**HEADERS, "Referer": "https://quote.eastmoney.com/"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []
    data = d.get("data")
    if not isinstance(data, dict):
        return []
    ts = data.get("trends") or []
    out = []
    for line in ts:
        if not isinstance(line, str):
            continue
        parts = line.split(",")
        if len(parts) < 8:
            continue
        t = parts[0]
        hhmm = t.split(" ")[1] if " " in t else t
        try:
            price = float(parts[2])
        except (ValueError, TypeError):
            continue
        avg = None
        try:
            a = float(parts[7])
            if a > 0:
                avg = round(a, 3)
        except (ValueError, TypeError, ZeroDivisionError):
            pass
        out.append({"time": hhmm, "price": price, "avg": avg})
    return out


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

    # 提取昨收（用于分时图百分比轴）：腾讯 qt 实时块 [4]=昨收
    try:
        _qt = node_obj.get("qt") or {}
        _qarr = _qt.get(tx_sym) if isinstance(_qt, dict) else None
        if isinstance(_qarr, list) and len(_qarr) > 4:
            _pc = float(_qarr[4])
            _preclose_cache[secid] = _pc
            _preclose_cache[tx_sym] = _pc
    except (ValueError, TypeError, AttributeError):
        pass
    # qt 实时块经常缺失昨收 → 回退日K昨收兜底（fetch_kline 内部会把 pc 写入缓存），
    # 确保分时图百分比轴一定有基准，避免前端 %轴因 preClose=null 被跳过
    if _preclose_cache.get(secid) is None:
        try:
            fetch_kline(secid, "101")
        except Exception:
            pass

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
    # 腾讯分时对美股指数仅返回 1 点最新快照（无分时序列），改走东财分时
    if len(out) < 3 and (secid.startswith("100.") or secid.startswith("105.")):
        er = _em_minute(secid)
        if er:
            return er
    return out


# ================================================================
#  板块涨幅（用腾讯概念板块排行替代）
# ================================================================

def _sina_boards_all():
    """拉取新浪全部行业板块原始数据（带 15 秒缓存，板块榜与涨跌家数共用一次请求）。
    新浪 vals 字段：[0]代码 [1]名称 [2]成分股数 [3]均价 [4]平均涨跌额(元) [5]平均涨跌幅%
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
                    pct = round(float(vals[5]), 2)  # [5]=平均涨跌幅%; 注意[4]是涨跌额(元)而非涨跌幅
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
                    "leaderCode": vals[8] if len(vals) > 8 else "",
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


# 东财板块列表接口：push2.eastmoney.com 近期不稳定（偶发空返回 → 触发降级成指数近似，
# 面板显示上证指数/深证成指等"非具体板块"）。改用 push2delay.eastmoney.com（与分时接口同主机，
# 实测稳定返回真实行业/概念板块）。保留 push2 作为兜底 host。
_EM_BOARD_HOSTS = [
    "https://push2delay.eastmoney.com/api/qt/clist/get",
    "https://push2.eastmoney.com/api/qt/clist/get",
]
_EM_BOARD_URL = _EM_BOARD_HOSTS[0]
_EM_BOARD_FIELDS = "f12,f14,f3,f62,f104,f105"
_EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/center/boardlist.html",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Connection": "keep-alive",
}


def _em_boards_all():
    """东方财富板块：行业板块(t:2) + 概念板块(t:3) 合并，返回具体行业/题材板块。
    字段：f12代码 f14名称 f3涨跌幅% f62主力净流入(元) f104涨家数 f105跌家数。
    合并时按名称归一去重（去掉末尾「概念」），行业干净名优先，避免「煤炭」与「煤炭概念」并存。
    """
    now = time.time()
    if _em_boards_cache["data"] and now - _em_boards_cache["ts"] < _boards_ttl:
        return _em_boards_cache["data"]
    cats = ["m:90+t:2", "m:90+t:3"]
    last_err = None
    for host in _EM_BOARD_HOSTS:
        merged, seen_base = [], set()
        try:
            for fs in cats:
                url = (host + "?" + "pn=1&pz=500&po=1&np=1&fltt=2&invt=2&fields="
                       + _EM_BOARD_FIELDS + "&fs=" + fs + "&_=" + str(int(time.time() * 1000)))
                req = urllib.request.Request(
                    url, headers=_EM_HEADERS)
                raw = urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "ignore")
                obj = json.loads(raw)
                diff = (obj.get("data") or {}).get("diff") or []
                for d in diff:
                    name = (d.get("f14") or "").strip()
                    if not name:
                        continue
                    base = re.sub(r"概念$", "", name)
                    if base in seen_base:
                        continue
                    seen_base.add(name)
                    try:
                        pct = round(float(d.get("f3") or 0), 2)
                    except (ValueError, TypeError):
                        pct = 0.0
                    try:
                        inflow = float(d.get("f62") or 0)
                    except (ValueError, TypeError):
                        inflow = 0.0
                    merged.append({
                        "code": d.get("f12"), "name": name, "pct": pct,
                        "inflow": inflow,
                        "up": d.get("f104"), "down": d.get("f105"),
                        "source": "em",
                    })
            if not merged:
                raise ValueError("东财板块为空")
            _em_boards_cache["data"] = merged
            _em_boards_cache["ts"] = now
            return merged
        except Exception as e:
            last_err = e
            continue
    if _em_boards_cache["data"]:
        return _em_boards_cache["data"]
    raise Exception(f"东财板块获取失败: {last_err}")


def _em_board_leader(code):
    """东财板块成分股领涨（涨幅最高的一只）。code=BKxxxx。"""
    try:
        url = (_EM_BOARD_URL + "?" + "pn=1&pz=5&po=1&np=1&fltt=2&invt=2&fields=f12,f14,f3"
               + "&fs=b:" + str(code) + "&_=" + str(int(time.time() * 1000)))
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"})
        raw = urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "ignore")
        obj = json.loads(raw)
        diff = (obj.get("data") or {}).get("diff") or []
        rows = []
        for d in diff:
            try:
                rows.append({"code": (d.get("f12") or "").strip(),
                             "name": (d.get("f14") or "").strip(),
                             "pct": round(float(d.get("f3") or 0), 2)})
            except (ValueError, TypeError):
                pass
        if rows:
            rows.sort(key=lambda x: x["pct"], reverse=True)
            return rows[0]
    except Exception:
        pass
    return None


def _em_board_stocks(code, pz=30):
    """东财板块成分股列表（前 pz 只，按涨幅降序）。code=BKxxxx。"""
    try:
        url = (_EM_BOARD_URL + "?" + "pn=1&pz=%d&po=1&np=1&fltt=2&invt=2&fields=f12,f14,f3,f2,f62"
               % pz + "&fs=b:" + str(code) + "&_=" + str(int(time.time() * 1000)))
        req = urllib.request.Request(url, headers=_EM_HEADERS)
        raw = urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "ignore")
        obj = json.loads(raw)
        diff = (obj.get("data") or {}).get("diff") or []
        rows = []
        for d in diff:
            code_ = (d.get("f12") or "").strip()
            name = (d.get("f14") or "").strip()
            if not code_ or not name:
                continue
            try:
                pct = round(float(d.get("f3") or 0), 2)
            except (ValueError, TypeError):
                pct = 0.0
            try:
                price = round(float(d.get("f2") or 0), 2)
            except (ValueError, TypeError):
                price = None
            try:
                inflow = float(d.get("f62") or 0)
            except (ValueError, TypeError):
                inflow = 0.0
            secid = ("1." if re.match(r"^[69]", code_) else "0.") + code_
            rows.append({"code": code_, "name": name, "pct": pct,
                         "price": price, "inflow": inflow, "secid": secid})
        rows.sort(key=lambda x: x["pct"], reverse=True)
        return rows
    except Exception:
        return []


def fetch_boards(pz=None):
    """板块数据——东方财富行业+概念板块（具体题材：半导体/CPO/PCB/煤炭/电力/创新药…）。
    主源东财；东财不可用时降级新浪行业板块；再不可用降级为指数近似（标注 degraded）。
    pz=None 返回「全部板块」（降序），确保领跌也能覆盖到真正的下跌板块。
    """
    try:
        items = sorted(_em_boards_all(), key=lambda x: x.get("pct") or 0, reverse=True)
        for it in items:
            it.setdefault("degraded", False)
        return items if pz is None else items[:pz]
    except Exception:
        pass
    # 东财失败，降级：新浪行业板块（标注 degraded，前端提示非东财口径）
    try:
        items = sorted(_sina_boards_all(), key=lambda x: x.get("pct") or 0, reverse=True)
        for it in items:
            it["degraded"] = True
            it["source"] = "新浪行业板块(东财不可用)"
        return items if pz is None else items[:pz]
    except Exception:
        pass
    # 再降级：用主要指数涨跌模拟板块热度（标注 degraded=True，不伪装成真实板块）
    try:
        test_codes = ["sh000001","sz399001","sz399006","sh000688","sz399852","sz399905"]
        data = fetch_quotes_batch(test_codes)
        items = []
        for sym, it in data.items():
            if it.get("pct") is not None and not it.get("error"):
                items.append({"code": sym, "name": it.get("name", sym),
                              "pct": it.get("pct", 0), "amount": it.get("amount"),
                              "leader": None, "degraded": True, "source": "指数近似(东财/新浪不可用)"})
        items.sort(key=lambda x: x.get("pct") or 0, reverse=True)
        return items if pz is None else items[:pz]
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
    # 重新生成：用腾讯批量取价探测有效代码（替代 hq.sinajs，避免被网络代理拦截）
    cands = _gen_a_candidates()
    chunks = [cands[i:i + 15] for i in range(0, len(cands), 15)]
    valid = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as ex:
        for res in ex.map(fetch_quotes_batch, chunks):
            for sym, it in res.items():
                if isinstance(it, dict) and it.get("price"):
                    try:
                        if float(it["price"]) > 0:
                            valid.append(sym)
                    except (ValueError, TypeError):
                        pass
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


# ================================================================
#  全A 腾讯实时行情（共享缓存）
#  替代 hq.sinajs.cn：腾讯源在本地(Clash)与公网均可用，保证两端都能看
# ================================================================

_tx_all_cache = {"ts": 0, "data": None}
_tx_all_ttl = 15


def _tx_scan(codes, workers=24):
    """并发用腾讯 qt.gtimg.cn 批量取价，返回 {code: dict}。"""
    if not codes:
        return {}
    chunks = [codes[i:i + 15] for i in range(0, len(codes), 15)]
    out = {}

    def _w(ch):
        try:
            return fetch_quotes_batch(ch)
        except Exception:
            return {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(_w, chunks):
            out.update(r)
    return out


def _tx_all_quotes(force=False):
    """全A 腾讯实时行情（{code: {name,price,prevClose,pct,high,low,turnover,amount}}），15s 缓存供多面板复用。"""
    global _tx_all_cache
    now = time.time()
    if (not force) and _tx_all_cache["data"] and now - _tx_all_cache["ts"] < _tx_all_ttl:
        return _tx_all_cache["data"]
    codes = _load_valid_codes()
    d = _tx_scan(codes) if codes else {}
    _tx_all_cache["data"] = d
    _tx_all_cache["ts"] = now
    return d


# ================================================================
#  异动监控（后端滚动采样：基于全A实时行情历史计算涨跌速度/量能异动/板块异动）
#  说明：免费腾讯源无可靠量比，放量/缩量用换手率代理；急拉急跌用价格历史窗口的速度。
# ================================================================
ANOMALY_HIST = {}        # code -> deque([(t, price)], maxlen=8)
BOARD_HIST = {}          # name -> deque([(t, pct)], maxlen=8)
_ANOM_LOCK = threading.Lock()


def _code_to_em(code):
    c = re.sub(r"[^0-9]", "", str(code or ""))
    if not c:
        return str(code or "")
    if c[0] in ("6", "9"):
        return "1." + c
    if c[0] in ("0", "3"):
        return "0." + c
    return "TX:bj" + c


def get_anomaly():
    quotes = _tx_all_quotes()
    now = time.time()
    groups = {k: [] for k in ("急拉", "急跌", "涨停", "跌停", "大涨", "大跌", "放量", "缩量")}
    with _ANOM_LOCK:
        for code, it in quotes.items():
            if not isinstance(it, dict) or it.get("error"):
                continue
            price = it.get("price"); pct = it.get("pct"); turnover = it.get("turnover")
            name = it.get("name") or code
            secid = _code_to_em(code)
            hq = ANOMALY_HIST.setdefault(code, collections.deque(maxlen=8))
            if price is not None:
                hq.append((now, price))
            velocity = None
            if len(hq) >= 2 and (now - hq[0][0]) >= 6:
                old = hq[0][1]
                if old:
                    velocity = (price - old) / old * 100
            types = []
            if pct is not None:
                if pct >= 9.5: types.append("涨停")
                elif pct <= -9.5: types.append("跌停")
                elif pct >= 5: types.append("大涨")
                elif pct <= -5: types.append("大跌")
            if velocity is not None:
                if velocity >= 1.0 and (pct or 0) > 0: types.append("急拉")
                elif velocity <= -1.0 and (pct or 0) < 0: types.append("急跌")
            if turnover is not None:
                if turnover >= 8: types.append("放量")
                elif turnover <= 0.3: types.append("缩量")
            if types:
                rec = {"code": code, "name": name, "secid": secid, "pct": pct,
                       "turnover": turnover,
                       "velocity": round(velocity, 2) if velocity is not None else None,
                       "types": types}
                for t in types:
                    groups[t].append(rec)
    boards = []
    try:
        blist = fetch_boards()
        with _ANOM_LOCK:
            for b in blist:
                nm = b.get("name"); pct = b.get("pct")
                if nm is None:
                    continue
                hb = BOARD_HIST.setdefault(nm, collections.deque(maxlen=8))
                if pct is not None:
                    hb.append((now, pct))
                delta = None
                if len(hb) >= 2 and (now - hb[0][0]) >= 6:
                    old = hb[0][1]
                    if old is not None:
                        delta = pct - old
                btypes = []
                if pct is not None:
                    if pct >= 2: btypes.append("板块拉升")
                    elif pct <= -2: btypes.append("板块跳水")
                if delta is not None and abs(delta) >= 0.8:
                    btypes.append("板块异动")
                if btypes:
                    boards.append({"name": nm, "pct": pct,
                                   "delta": round(delta, 2) if delta is not None else None,
                                   "types": btypes})
    except Exception:
        pass
    for k in groups:
        groups[k] = groups[k][:12]
    boards = boards[:12]
    return {"groups": groups, "boards": boards, "updated": now}


# ================================================================
#  大盘异动（分时图 + 板块异动事件流）
# ================================================================

_ANOM_EVENTS = collections.deque(maxlen=60)   # 滚动事件缓存：最近 60 条
_ANOM_EVENT_LOCK = threading.Lock()
_LAST_BOARD_SNAP = {}                        # 上次板块快照 {name: pct} 用于计算速度


def _classify_anomaly_event(pct, delta=None):
    """根据涨跌幅和速度返回事件类型与中文描述模板。"""
    if pct is not None and pct >= 2.5:
        return "拉升", f"{pct:.2f}%"
    if pct is not None and pct <= -2.5:
        return "下挫", f"{pct:.2f}%"
    if pct is not None and pct >= 1.0:
        return "快速拉升", f"{pct:.2f}%"
    if pct is not None and pct <= -1.0:
        return "快速下挫", f"{pct:.2f}%"
    if delta is not None and delta >= 0.8:
        return "急速拉升高", f"+{delta:.2f}%"
    if delta is not None and delta <= -0.8:
        return "急速下跌", f"{delta:.2f}%"
    return "异动", ""


def get_market_anomaly():
    """大盘异动：返回上证分时数据 + 板块/个股异动事件流（带时间戳）。"""
    global _LAST_BOARD_SNAP
    now = time.time()
    now_str = time.strftime("%H:%M", time.localtime(now))

    # ---- 1) 分时数据（上证指数）----
    # 注意：fetch_trends 内部走 em_to_tx，需传东方财富格式 secid（1.000001），
    # 直接传 "sh000001" 会被 em_to_tx 判为不支持而返回空，导致异动面板分时图空白。
    trends = []
    try:
        trends = fetch_trends("1.000001")
    except Exception:
        pass

    # ---- 2) 当前领涨/领跌板块：直接作为分时图上的异动标注 ----
    # 设计：不再等板块「突然」突破阈值才记录（那样盘中大部分时间 events 为空 → 图上看不到异动），
    # 而是每轮把当前最值得关注的板块（涨跌幅大 或 较上次快照突变）算作"异动"，标注到分时图。
    new_events = []
    movers = []
    try:
        blist = fetch_boards()
        current_snap = {}
        for b in blist:
            nm = b.get("name")
            pct = b.get("pct")
            if nm is not None and pct is not None:
                current_snap[nm] = {"pct": pct, "amount": b.get("amount", 0),
                                    "leader": b.get("leader", ""),
                                    "code": b.get("code")}  # 板块代码(BKxxxx)，用于拉成分股

        # 与上次快照对比，得到"突变"幅度，用于捕捉"突然"拉升/跳水
        for nm, cur in current_snap.items():
            old = _LAST_BOARD_SNAP.get(nm)
            cur["delta"] = (cur["pct"] - old["pct"]) if old is not None else 0.0

        with _ANOM_EVENT_LOCK:
            # 选取"值得标注"的板块：涨跌幅够大 或 变化够突然；综合排序取前 8
            cands = []
            for nm, c in current_snap.items():
                pct = c["pct"]; delta = c.get("delta", 0.0)
                if abs(pct) < 0.4 and abs(delta) < 0.25:
                    continue
                score = abs(pct) + abs(delta) * 2.0
                cands.append((score, nm, c))
            cands.sort(key=lambda x: x[0], reverse=True)
            for _, nm, c in cands[:8]:
                pct = c["pct"]; delta = c.get("delta", 0.0)
                etype, edesc = _classify_anomaly_event(pct, delta)
                # 按排名将标注均匀分散到分时时间轴上（避免全部挤在当前时刻→全堆图右侧）
                # 交易时段 09:30~15:00，按排名插值分配时间
                rank = len(movers)
                total = min(8, max(len(cands), 1))   # 预估总数，用于均匀分布
                # 解析当前时间（格式 HH:MM）
                parts = now_str.split(":")
                cur_h = int(parts[0]) if parts else 14
                cur_m = int(parts[1]) if len(parts) > 1 else 0
                cur_total_min = cur_h * 60 + cur_m
                start_min = 9 * 60 + 30                 # 09:30
                end_min = min(15 * 60, max(start_min + 10, cur_total_min))  # 不超过 15:00
                span = max(10, end_min - start_min)
                alloc_min = start_min + int(span * rank / max(total - 1, 1))
                alloc_h = alloc_min // 60
                alloc_m = alloc_min % 60
                mv_time = f"{alloc_h:02d}:{alloc_m:02d}"
                mv = {
                    "time": mv_time,
                    "title": f"{nm}板块{etype}",
                    "type": etype,
                    "dir": "up" if (pct or 0) > 0 else "down",
                    "sector": nm,
                    "pct": round(pct, 2) if pct is not None else None,
                    "delta": round(delta, 2) if delta is not None else None,
                    "desc": edesc,
                    "stocks": [],
                }
                # 拉取该板块成分股（前4只：2领涨+2领跌），用于异动列表展示
                bkcode = c.get("code")
                if bkcode:
                    try:
                        stk_list = _em_board_stocks(bkcode, pz=6)
                        if stk_list:
                            # 取涨幅最高的2只 + 最低的2只（或按实际数量灵活取）
                            up_stks = [s for s in stk_list if s.get("pct", 0) > 0][:2]
                            dn_stks = [s for s in sorted(stk_list, key=lambda x: x.get("pct", 0)) if s.get("pct", 0) <= 0][:2]
                            mv["stocks"] = (up_stks + dn_stks)[:4]
                    except Exception:
                        pass
                movers.append(mv)
                new_events.append(dict(mv, time_ts=now))   # 同时进入滚动时间线

            _LAST_BOARD_SNAP = current_snap
    except Exception:
        pass

    # ---- 3) 个股级别异动（涨停/急拉/急跌等）也注入事件流 ----
    try:
        anom = get_anomaly()
        stock_groups = anom.get("groups", {})
        for gtype in ("急拉", "急跌", "涨停", "跌停"):
            items = stock_groups.get(gtype, [])[:3]  # 每类最多取3条避免刷屏
            for it in items:
                name = it.get("name", "")
                pct = it.get("pct")
                event = {
                    "time": now_str,
                    "time_ts": now,
                    "title": f"{name}{gtype}" + (f" {fmt_pct(pct)}" if pct is not None else ""),
                    "type": gtype,
                    "dir": "up" if gtype in ("急拉", "涨停", "大涨") else "down",
                    "sector": None,
                    "pct": round(pct, 2) if pct is not None else None,
                    "delta": None,
                    "desc": "",
                    "code": it.get("code"),
                    "secid": it.get("secid"),
                    "stocks": [],
                }
                new_events.append(event)
    except Exception:
        pass

    # ---- 4) 去重后写入滚动缓存 ----
    with _ANOM_EVENT_LOCK:
        for ev in new_events:
            # 简单去重：同一板块/个股在 120 秒内不重复触发同类型
            dup = False
            for old_ev in _ANOM_EVENTS:
                if (old_ev.get("sector") == ev.get("sector") or
                        old_ev.get("code") == ev.get("code")):
                    if old_ev.get("type") == ev.get("type"):
                        if now - old_ev.get("time_ts", 0) < 120:
                            dup = True
                            break
            if not dup:
                _ANOM_EVENTS.append(ev)

    events = sorted(_ANOM_EVENTS, key=lambda e: e.get("time_ts", 0), reverse=True)[:30]

    # ---- 5) 上证日K（用于大盘异动面板的"日K"标签）----
    kline_data = None
    try:
        kline_data = fetch_kline("1.000001", "101")
    except Exception:
        pass

    return {
        "trends": trends,
        "events": events,
        "movers": movers,
        "preClose": _preclose_cache.get("1.000001"),
        "kline": kline_data,
        "updated": now,
    }


def fmt_pct(v):
    """格式化百分比（兼容 None）。"""
    if v is None: return "--"
    return f"{v:+.2f}%"


def fetch_breadth():
    """真实涨跌家数——逐只统计全A（沪深京）腾讯实时行情，非估算。"""
    quotes = _tx_all_quotes()
    if not quotes:
        raise Exception("无有效行情数据")
    up = down = flat = 0
    valid = 0
    total_amt = 0.0
    for it in quotes.values():
        if not isinstance(it, dict) or it.get("error"):
            continue
        valid += 1
        pct = it.get("pct")
        price = it.get("price"); prev = it.get("prevClose")
        if pct is not None:
            if pct > 0: up += 1
            elif pct < 0: down += 1
            else: flat += 1
        elif price is not None and prev:
            if price > prev: up += 1
            elif price < prev: down += 1
            else: flat += 1
        amt = it.get("amount")
        if amt:
            try:
                total_amt += float(amt)
            except (TypeError, ValueError):
                pass
    return {
        "up": up, "down": down, "flat": flat,
        "total": valid, "estimate": False, "basis": "tencent-realtime",
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
    """全A个股涨跌榜（腾讯实时）：取 名称/昨收/现价 算涨跌幅排序。
    仅保留沪深（北交所腾讯行情覆盖弱，剔除）。返回 涨幅榜 + 跌幅榜。"""
    quotes = _tx_all_quotes()
    if not quotes:
        return {"gainers": [], "losers": [], "total": 0}
    rows = []
    for code, it in quotes.items():
        if not isinstance(it, dict) or it.get("error"):
            continue
        if not code.startswith(("sh", "sz")):   # 剔除北交所
            continue
        price = it.get("price"); prev = it.get("prevClose"); pct = it.get("pct")
        if price is None or prev is None or price <= 0 or prev <= 0:
            continue
        if pct is None:
            pct = round((price - prev) / prev * 100, 2)
        secid = ("1." if code.startswith("sh") else "0.") + code[2:]
        rows.append({"code": code, "secid": secid, "name": it.get("name", code), "price": price, "pct": pct})
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

    # 板块搜索（内置行业+概念板块名表，离线可用）
    q_lower = q.lower()
    sectors = []
    try:
        for s in get_sectors():
            nm = s.get("name", "")
            if not nm:
                continue
            if q in nm or q_lower in nm.lower() or nm.startswith(q):
                sectors.append({"kind": "sector", "name": nm, "code": s.get("code", "")})
            if len(sectors) >= 8:
                break
    except Exception:
        sectors = []

    return {"stocks": results[:pz], "sectors": sectors}


# ================================================================
#  板块名表（行业+概念，离线搜索用）
# ================================================================

_SECTORS_CACHE = None

def get_sectors():
    """读取内置板块名表 sectors.json（行业+Sina代码 + 概念）；首次读取后缓存。"""
    global _SECTORS_CACHE
    if _SECTORS_CACHE is not None:
        return _SECTORS_CACHE
    fp = os.path.join(BASE_DIR, "sectors.json")
    try:
        with open(fp, "r", encoding="utf-8") as f:
            _SECTORS_CACHE = json.load(f)
    except Exception:
        _SECTORS_CACHE = []
    return _SECTORS_CACHE


# 常见细分行业/概念词 → 新浪行业板块名（新浪仅 49 个一级行业，银行/券商等归在 金融行业下）
_SECTOR_ALIAS = {
    "银行": "金融行业", "券商": "金融行业", "证券": "金融行业", "保险": "金融行业",
    "信托": "金融行业", "金融": "金融行业", "基金": "金融行业",
    "半导体": "电子器件", "芯片": "电子器件", "集成电路": "电子器件",
    "电子": "电子器件", "元器件": "电子器件", "消费电子": "电子器件",
    "白酒": "酿酒行业", "啤酒": "酿酒行业", "酿酒": "酿酒行业", "红酒": "酿酒行业",
    "地产": "房地产", "房地产": "房地产", "楼市": "房地产",
    "钢铁": "钢铁行业", "钢": "钢铁行业",
    "煤炭": "煤炭行业", "煤": "煤炭行业",
    "汽车": "汽车制造",
    "医药": "生物制药", "制药": "生物制药", "生物": "生物制药", "医疗": "生物制药",
    "化工": "化工行业",
    "有色": "有色金属", "金属": "有色金属", "稀土": "有色金属",
    "电力": "电力行业", "发电": "电力行业", "新能源": "电力行业",
    "水泥": "水泥行业",
    "食品": "食品行业",
    "家电": "家电行业",
    "机械": "机械行业",
    "纺织": "纺织行业",
    "造纸": "造纸行业",
    "石油": "石油行业", "石化": "石油行业",
    "传媒": "传媒娱乐", "影视": "传媒娱乐", "娱乐": "传媒娱乐",
    "旅游": "酒店旅游", "酒店": "酒店旅游",
    "军工": "飞机制造", "飞机": "飞机制造", "无人机": "飞机制造",
    "船舶": "船舶制造",
    "环保": "环保行业",
    "建材": "建筑建材",
    "农林": "农林牧渔", "农业": "农林牧渔",
    "化肥": "农药化肥",
    "塑料": "塑料制品",
    "家具": "家具行业",
    "百货": "商业百货", "零售": "商业百货", "商业": "商业百货",
    "外贸": "物资外贸",
    "仪表": "仪器仪表",
    "印刷": "印刷包装",
    "陶瓷": "陶瓷行业",
    "公路": "公路桥梁", "桥梁": "公路桥梁", "高速": "公路桥梁",
    "供水": "供水供气", "供气": "供水供气",
    "发电设备": "发电设备",
    "化纤": "化纤行业",
    "服装": "服装鞋类", "鞋": "服装鞋类",
    "摩托": "摩托车",
    "玻璃": "玻璃行业",
    "综合": "综合行业",
    "信息": "电子信息",
}

def get_sector_info(name):
    """板块详情：优先东财行业/概念板块（题材覆盖全，含半导体/CPO/PCB/创新药等）；
    找不到再回落新浪行业板块。东财返回涨跌幅/涨跌家数/主力净流入/领涨成分股。"""
    # ---- 东财优先：按名称在板块列表匹配，并拉领涨成分股 ----
    try:
        for b in _em_boards_all():
            if (b.get("name") or "").strip() == (name or "").strip():
                code = b.get("code")
                leader = _em_board_leader(code) if code else None
                stocks = _em_board_stocks(code, 30) if code else []
                return {
                    "name": name, "found": True, "source": "em",
                    "pct": b.get("pct"), "up": b.get("up"), "down": b.get("down"),
                    "inflow": b.get("inflow"),
                    "leaderCode": leader.get("code") if leader else "",
                    "leaderName": leader.get("name") if leader else "",
                    "leaderPct": leader.get("pct") if leader else None,
                    "stocks": stocks,
                }
    except Exception:
        pass
    # ---- 回落新浪行业板块 ----
    try:
        boards = _sina_boards_all()
    except Exception:
        return {"name": name, "found": False}
    def _norm(n):
        return re.sub(r"(行业|板块|概念)$", "", (n or "").strip())
    tgt = _norm(name)
    for b in boards:
        if _norm(b.get("name")) == tgt:
            return {
                "name": name, "found": True,
                "count": b.get("count"), "pct": b.get("pct"),
                "amount": b.get("amount"),
                "leaderCode": b.get("leaderCode", ""),
                "leaderName": b.get("leader", ""),
            }
    # 别名映射：常见细分词 → 新浪一级行业
    for key, sinaname in _SECTOR_ALIAS.items():
        if key in name or name in key or _norm(name) == _norm(key):
            for b in boards:
                if _norm(b.get("name")) == _norm(sinaname):
                    return {
                        "name": name, "found": True,
                        "count": b.get("count"), "pct": b.get("pct"),
                        "amount": b.get("amount"),
                        "leaderCode": b.get("leaderCode", ""),
                        "leaderName": b.get("leader", ""),
                    }
    return {"name": name, "found": False}


# ================================================================
#  个股基本资料（点击股票弹窗）
# ================================================================

def fetch_f10(tx_sym):
    """东方财富 F10 公司概况；本地常被代理拦截，失败返回 None（前端降级展示）。"""
    if tx_sym.startswith("sh"):
        em = "SH" + tx_sym[2:]
    elif tx_sym.startswith("sz"):
        em = "SZ" + tx_sym[2:]
    else:
        return None
    url = "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax?code=" + em
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://emweb.securities.eastmoney.com/"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            d = json.loads(resp.read().decode("utf-8", "ignore"))
        data = (d.get("data") or {}) if isinstance(d, dict) else {}
        cs = data.get("CompanySurvey") or {}
        industry = main = listdate = None
        # 宽松提取：不同版本键名不一，做关键字匹配
        pool = {}
        def walk(o, pre=""):
            if isinstance(o, dict):
                for k, v in o.items():
                    walk(v, pre + "/" + str(k))
            elif isinstance(o, list):
                for i, v in enumerate(o):
                    walk(v, pre + "/" + str(i))
            else:
                pool[pre] = o
        walk(cs)
        for k, v in pool.items():
            kl = k.lower()
            if industry is None and ("hy" in kl or "industry" in kl or "sshymc" in kl):
                industry = v
            if main is None and ("zy" in kl and ("fw" in kl or "business" in kl or "yw" in kl or "jyfw" in kl)):
                main = v
            if listdate is None and ("ssrq" in kl or "listdate" in kl or "ssr" in kl or "listingdate" in kl):
                listdate = v
        return {"industry": industry, "mainBusiness": main, "listingDate": listdate}
    except Exception:
        return None


def get_stock_info(secid):
    """个股基本资料：聚合腾讯实时行情字段（名称/价/涨跌/市值/市盈率/52周高低等）
    + 东方财富F10（行业/主营/上市日期，失败降级）。
    secid 支持东财格式(1.600519/0.000001/TX:hk00700/TX:usAAPL)与腾讯格式(sh600519)，
    内部统一转腾讯符号，与 fetch_kline/fetch_trends 行为一致。"""
    info = {"secid": secid, "name": "", "code": secid,
            "price": None, "prevClose": None, "open": None, "volume": None,
            "change": None, "pct": None, "high": None, "low": None,
            "amount": None, "turnover": None, "amplitude": None,
            "pe": None, "volratio": None, "floatMv": None, "totalMv": None,
            "week52High": None, "week52Low": None, "weibi": None, "avg": None,
            "f10": None}
    # 东财 secid → 腾讯符号（与 kline/trends 一致）；不支持的市场直接降级返回空资料
    try:
        _r = em_to_tx(secid)
        tx_sym = _r[0] if _r else None
    except Exception:
        tx_sym = None
    if not tx_sym:
        info["f10"] = None
        return info
    try:
        raw = _tx_get(TX_RT_BASE + tx_sym)
        m = re.search(r'v_%s="([^"]*)"' % re.escape(tx_sym), raw)
        if not m:
            return info
        p = m.group(1).split("~")

        def f(i):
            try:
                return float(p[i]) if i < len(p) and p[i] else None
            except (ValueError, TypeError):
                return None

        if len(p) < 6:
            return info
        info.update({
            "name": p[1], "code": p[2], "secid": tx_sym,
            "price": f(3), "prevClose": f(4), "open": f(5), "volume": f(6),
            "change": f(31), "pct": f(32), "high": f(33), "low": f(34),
            "amount": (f(37) * 10000) if f(37) is not None else None,  # 腾讯[37]单位万→元
            "turnover": f(38),
            "amplitude": f(43),
            "pe": f(39),
            # 腾讯[44]/[45] 为市值（单位：亿元）→ 统一换算成「元」，与 amount 一致
            "floatMv": (f(44) * 1e8) if f(44) is not None else None,
            "totalMv": (f(45) * 1e8) if f(45) is not None else None,
            "volratio": f(46),
            "week52High": f(47), "week52Low": f(48),
            "weibi": f(49),
            "avg": f(51),
        })
        # 振幅兜底计算
        if info["amplitude"] is None and info["high"] and info["low"] and info["prevClose"]:
            try:
                info["amplitude"] = round((info["high"] - info["low"]) / info["prevClose"] * 100, 2)
            except Exception:
                pass
    except Exception:
        pass
    # F10 公司资料（东方财富，失败降级为 None）
    info["f10"] = fetch_f10(tx_sym)
    return info


# ================================================================
#  个股所属板块 + 题材权重（东方财富 push2；本机/公网直连，沙箱可能被代理拦截）
#  说明：push2 免费接口返回 行业(f136)/概念(f138)/地域(f139)；
#        概念板的成分权重(f184) 通过逐板 clist 取该股权重，归一化即"题材构成%"。
# ================================================================

_BOARD_CACHE = {}
_BOARD_TTL = 300


def _to_em_secid_for_board(secid):
    """转东方财富 push2 格式 secid（1.600667 / 0.000001）。非 A 股返回 None。"""
    if secid.startswith(("1.", "0.")):
        return secid
    if secid.startswith("TX:"):
        c = secid[3:]
        if c.startswith("sh"):
            return "1." + c[2:]
        if c.startswith("sz"):
            return "0." + c[2:]
    return None


def _parse_board_str(s):
    """f138/f139 形如 'BK0735,半导体;BK1033,PCB概念' 或 '半导体;PCB'。返回 [(code,name)]。"""
    out = []
    if not s:
        return out
    for part in str(s).split(";"):
        part = part.strip()
        if not part:
            continue
        segs = part.split(",")
        if len(segs) >= 2:
            code = segs[0].strip()
            name = segs[-1].strip()
        else:
            code, name = "", part
        if name:
            out.append((code, name))
    return out


def get_stock_boards(secid):
    """个股所属板块 + 题材构成%（归一化）。返回 {found, blocked, industry, region,
    concepts:[{name,code,weight}], weights:{name:pct}}。push2 不可达时 found=False/blocked=True。"""
    em = _to_em_secid_for_board(secid)
    if not em:
        return {"found": False, "blocked": False, "reason": "仅支持 A 股"}
    now = time.time()
    cached = _BOARD_CACHE.get(secid)
    if cached and now - cached["ts"] < _BOARD_TTL:
        return cached["data"]
    try:
        url = ("https://push2.eastmoney.com/api/qt/stock/get?secid=%s"
               "&fields=f57,f58,f136,f138,f139,f140&invt=2&fltt=2" % em)
        req = urllib.request.Request(url, headers={**HEADERS, "Referer": "https://quote.eastmoney.com/"})
        with urllib.request.urlopen(req, timeout=6) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        data = (d.get("data") or {})
        if not data:
            res = {"found": False, "blocked": False, "reason": "无板块数据"}
            _BOARD_CACHE[secid] = {"ts": now, "data": res}
            return res
        industry = (data.get("f136") or "").strip()
        concepts = _parse_board_str(data.get("f138"))
        region = (data.get("f139") or "").strip()
        stk_code = (data.get("f57") or em.split(".")[-1])

        # 题材权重：逐概念板取该股成分权重 f184
        weights = {}

        def _board_weight(cc):
            code, name = cc
            try:
                u = ("https://push2.eastmoney.com/api/qt/clist/get?fs=b:%s"
                     "&fields=f12,f14,f184&pn=1&pz=500&invt=2&fltt=2" % code)
                rq = urllib.request.Request(u, headers={**HEADERS, "Referer": "https://quote.eastmoney.com/"})
                with urllib.request.urlopen(rq, timeout=5) as rr:
                    j = json.loads(rr.read().decode("utf-8", "ignore"))
                items = (j.get("data") or {}).get("diff") or []
                for it in items:
                    if str(it.get("f12")) == stk_code:
                        w = it.get("f184")
                        try:
                            weights[name] = float(w)
                        except (TypeError, ValueError):
                            pass
                        break
            except Exception:
                pass

        coded = [(c, n) for c, n in concepts if c]
        if coded:
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
                list(ex.map(_board_weight, coded[:12]))
        total = sum(weights.values())
        if total:
            for k in list(weights):
                weights[k] = round(weights[k] / total * 100, 1)
        else:
            weights.clear()
        res = {
            "found": True, "blocked": False,
            "industry": industry, "region": region,
            "concepts": [{"name": n, "code": c, "weight": weights.get(n)} for c, n in concepts],
            "weights": weights,
        }
        _BOARD_CACHE[secid] = {"ts": now, "data": res}
        return res
    except Exception as e:
        res = {"found": False, "blocked": True, "reason": str(e)[:80]}
        _BOARD_CACHE[secid] = {"ts": now, "data": res}
        return res


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
#  用户持仓（后端持久化，避免依赖浏览器 localStorage 在动态端口下丢失）
# ================================================================

def load_holdings():
    try:
        with open(HOLDINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception:
        return []


def save_holdings(lst):
    """整体覆盖保存持仓列表（前端负责去重/合并），原子写入防止写坏。"""
    if not isinstance(lst, list):
        lst = []
    tmp = HOLDINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(lst, f, ensure_ascii=False, indent=2)
    os.replace(tmp, HOLDINGS_PATH)
    return lst


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


# 龙虎榜（真实·东方财富数据中心）：近交易日上榜股票 + 买卖席位 + 净买入额
_LHB_COLS = ("SECURITY_CODE,SECURITY_NAME_ABBR,SECUCODE,TRADE_DATE,CLOSE_PRICE,CHANGE_RATE,"
             "TURNOVERRATE,BILLBOARD_BUY_AMT,BILLBOARD_SELL_AMT,BILLBOARD_NET_AMT,EXPLAIN,"
             "BUY_SEAT,SELL_SEAT,ACCUM_AMOUNT,TRADE_MARKET,"
             "D1_CLOSE_ADJCHRATE,D5_CLOSE_ADJCHRATE,D10_CLOSE_ADJCHRATE,D20_CLOSE_ADJCHRATE")
LHB_URL = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
           "?reportName=RPT_DAILYBILLBOARD_DETAILS&columns=%s" % _LHB_COLS)
_billboard_cache = {"ts": 0, "data": None}
_billboard_ttl = 600


def _secucode_to_secid(sc):
    """SECUCODE 如 002379.SZ / 600721.SH / 8xxxxx.BJ -> 东财 secid 0.002379 / 1.600721"""
    try:
        code, mkt = sc.split(".")
    except Exception:
        return None
    if mkt in ("SH", "BJ"):
        return "1." + code
    if mkt == "SZ":
        return "0." + code
    return None


def _fetch_org_net_map(date):
    """取指定交易日全市场龙虎榜「机构专用席位」净买卖汇总。
    返回 {code: {buy(元), sell(元), net(元), buy_cnt, sell_cnt}}。
    机构专用席位判定：OPERATEDEPT_CODE == '0'（operate dept = 机构专用）。
    席位明细来自 RPT_BILLBOARD_DAILYDETAILSBUY / RPT_BILLBOARD_DAILYDETAILSSELL。
    失败返回空 dict（不影响主榜单）。"""
    out = {}
    try:
        hd = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
        f = "(TRADE_DATE%%3D'%s')" % date
        base = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
                "?reportName=%s&columns=ALL&filter=%s&pageSize=500&source=WEB&client=WEB")
        for rep, side in (("RPT_BILLBOARD_DAILYDETAILSBUY", "buy"),
                          ("RPT_BILLBOARD_DAILYDETAILSSELL", "sell")):
            url = base % (rep, f)
            req = urllib.request.Request(url, headers=hd)
            with urllib.request.urlopen(req, timeout=10) as r:
                js = json.loads(r.read().decode("utf-8", "ignore"))
            rows = (js.get("result") or {}).get("data") or []
            for row in rows:
                if str(row.get("OPERATEDEPT_CODE", "")) != "0":
                    continue
                code = row.get("SECURITY_CODE") or ""
                if not code:
                    continue
                e = out.setdefault(code, {"buy": 0.0, "sell": 0.0, "buy_cnt": 0, "sell_cnt": 0})
                if side == "buy":
                    e["buy"] += float(row.get("BUY") or 0)
                    e["buy_cnt"] += 1
                else:
                    e["sell"] += float(row.get("SELL") or 0)
                    e["sell_cnt"] += 1
        for v in out.values():
            v["net"] = v["buy"] - v["sell"]
    except Exception:
        return {}
    return out


def _fetch_real_billboard(date=None):
    """取真实龙虎榜。date 为指定交易日(YYYY-MM-DD)时只取该日；否则倒序试最近 12 个自然日。
    返回 (date_str, [items]) 或 (None, [])。
    items 字段：secid/code/name/pct/net(元)/buy(元)/sell(元)/turnover/reason/
               org_net(元)/org_buy/org_sell/org_buy_cnt/org_sell_cnt/d1/d5/d10/d20/market。"""
    hd = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
    if date:
        dates = [date]
    else:
        today = datetime.date.today()
        dates = [ (today - datetime.timedelta(days=off)).strftime("%Y-%m-%d") for off in range(0, 12) ]
    for d in dates:
        url = (LHB_URL + "&filter=(TRADE_DATE%%3D'%s')&pageSize=20"
               "&sortColumns=BILLBOARD_NET_AMT&sortTypes=-1&source=WEB&client=WEB" % d)
        try:
            req = urllib.request.Request(url, headers=hd)
            with urllib.request.urlopen(req, timeout=8) as r:
                js = json.loads(r.read().decode("utf-8", "ignore"))
            res = js.get("result")
            if not res or not res.get("data"):
                if date:
                    return d, []          # 指定日期无数据，直接返回空
                continue
            org = _fetch_org_net_map(d)
            items = []
            for it in res["data"]:
                sc = it.get("SECUCODE") or ""
                code = it.get("SECURITY_CODE") or ""
                o = org.get(code) or {}
                items.append({
                    "secid": _secucode_to_secid(sc),
                    "code": code,
                    "name": it.get("SECURITY_NAME_ABBR") or code or "",
                    "pct": it.get("CHANGE_RATE"),
                    "net": it.get("BILLBOARD_NET_AMT"),        # 元
                    "buy": it.get("BILLBOARD_BUY_AMT"),         # 元
                    "sell": it.get("BILLBOARD_SELL_AMT"),       # 元
                    "turnover": it.get("TURNOVERRATE"),
                    "reason": it.get("EXPLAIN") or "",
                    "buySeat": it.get("BUY_SEAT"),
                    "sellSeat": it.get("SELL_SEAT"),
                    "market": it.get("TRADE_MARKET") or "",
                    "org_net": o.get("net"),
                    "org_buy": o.get("buy"),
                    "org_sell": o.get("sell"),
                    "org_buy_cnt": o.get("buy_cnt"),
                    "org_sell_cnt": o.get("sell_cnt"),
                    "d1": it.get("D1_CLOSE_ADJCHRATE"),
                    "d5": it.get("D5_CLOSE_ADJCHRATE"),
                    "d10": it.get("D10_CLOSE_ADJCHRATE"),
                    "d20": it.get("D20_CLOSE_ADJCHRATE"),
                })
            return d, items
        except Exception:
            if date:
                return d, []
            continue
    return None, []


def _tx_anomaly_billboard():
    """降级：腾讯全A实时行情按异动条件（涨停/高换手/高振幅）筛选。"""
    quotes = _tx_all_quotes()
    items = []
    if not quotes:
        return items
    for code, it in quotes.items():
        if not isinstance(it, dict) or it.get("error"):
            continue
        if not code.startswith(("sh", "sz")):   # 剔除北交所
            continue
        price = it.get("price"); prev = it.get("prevClose"); pct = it.get("pct")
        if pct is None and price and prev:
            try:
                pct = (price - prev) / prev * 100
            except Exception:
                pct = None
        high = it.get("high"); low = it.get("low"); turnover = it.get("turnover")
        reasons = []
        if pct is not None and pct >= 9.5:
            reasons.append("涨停")
        if turnover is not None and turnover >= 15:
            reasons.append("高换手%d%%" % round(turnover))
        amp = None
        if high and low and prev:
            try:
                amp = (high - low) / prev * 100
            except Exception:
                amp = None
        if amp is not None and amp >= 15:
            reasons.append("高振幅%d%%" % round(amp))
        if not reasons:
            continue
        items.append({
            "secid": None, "code": code, "name": it.get("name", code),
            "pct": round(pct, 2) if pct is not None else None,
            "net": None, "buy": None, "sell": None,
            "turnover": turnover, "reason": " · ".join(reasons),
            "buySeat": None, "sellSeat": None, "market": "",
        })
    items.sort(key=lambda x: (0 if "涨停" in x["reason"] else 1, -(x["pct"] or 0)))
    return items[:15]


def get_billboard(date=None):
    """龙虎榜：优先真实东方财富（含席位净买入），失败/盘中无数据降级为腾讯异动榜。
    指定 date(YYYY-MM-DD) 时只取该交易日（不走缓存）。
    返回 {type:'real'|'proxy', date, items}。"""
    if date:
        try:
            d, items = _fetch_real_billboard(date)
            if items:
                return {"type": "real", "date": d, "items": items}
        except Exception:
            pass
        return {"type": "real", "date": date, "items": []}
    now = time.time()
    c = _billboard_cache
    if c["data"] is not None and now - c["ts"] < _billboard_ttl:
        return c["data"]
    try:
        d, items = _fetch_real_billboard()
        if items:
            res = {"type": "real", "date": d, "items": items}
            _billboard_cache.update(ts=now, data=res)
            return res
    except Exception:
        pass
    items = _tx_anomaly_billboard()
    res = {"type": "proxy", "date": None, "items": items}
    _billboard_cache.update(ts=now, data=res)
    return res


def get_stock_lhb_history(code, look_back=45):
    """个股近 N 日龙虎榜记录（带上榜后 N 日涨跌率）。
    返回 {code, count, records:[{date, pct, net(元), reason, d1,d5,d10,d20}]}，按日期倒序。
    用于弹窗「点开看上榜后 N 日涨跌」。失败返回 {code, count:0, records:[]}。"""
    try:
        code = str(code).strip()
        if not code:
            return {"code": code, "count": 0, "records": []}
        end = datetime.date.today()
        start = end - datetime.timedelta(days=look_back)
        today_str = end.strftime("%Y-%m-%d")
        start_str = start.strftime("%Y-%m-%d")
        cols = ("SECURITY_CODE,TRADE_DATE,CHANGE_RATE,BILLBOARD_NET_AMT,EXPLANATION,"
                "D1_CLOSE_ADJCHRATE,D5_CLOSE_ADJCHRATE,D10_CLOSE_ADJCHRATE,D20_CLOSE_ADJCHRATE")
        url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_DAILYBILLBOARD_DETAILS"
               "&columns=%s"
               "&filter=(TRADE_DATE%%3E%%3D'%s')(TRADE_DATE%%3C%%3D'%s')(SECURITY_CODE%%3D%%22%s%%22)"
               "&pageSize=30&sortColumns=TRADE_DATE&sortTypes=-1&source=WEB&client=WEB"
               % (cols, start_str, today_str, code))
        hd = {"User-Agent": "Mozilla/5.0", "Referer": "https://data.eastmoney.com/"}
        req = urllib.request.Request(url, headers=hd)
        with urllib.request.urlopen(req, timeout=10) as r:
            js = json.loads(r.read().decode("utf-8", "ignore"))
        rows = (js.get("result") or {}).get("data") or []
        recs = []
        for it in rows:
            td = str(it.get("TRADE_DATE") or "")[:10]
            recs.append({
                "date": td,
                "pct": it.get("CHANGE_RATE"),
                "net": it.get("BILLBOARD_NET_AMT"),
                "reason": it.get("EXPLANATION") or "",
                "d1": it.get("D1_CLOSE_ADJCHRATE"),
                "d5": it.get("D5_CLOSE_ADJCHRATE"),
                "d10": it.get("D10_CLOSE_ADJCHRATE"),
                "d20": it.get("D20_CLOSE_ADJCHRATE"),
            })
        return {"code": code, "count": len(recs), "records": recs}
    except Exception as e:
        return {"code": str(code), "count": 0, "records": [], "error": str(e)}


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
        "title": cfg.get("title", "实时行情看板 · 詹姆斯是goat"),
        "refreshSeconds": cfg.get("refreshSeconds", 3),
        "market": {"label": cfg.get("market", {}).get("label", "大盘指数"), "items": market_items},
        "watchlist": {"label": cfg.get("watchlist", {}).get("label", "自选股"), "items": watch_items},
        "global": global_out,
        "summary": {"totalAmount": total_amount, "avgVr": avg_vr,
                    "volRatio": volratio.get("ratio"), "volLabel": volratio.get("label")},
    }


def _safe(v, d=None):
    return v if v not in (None, "", 0) else d


def get_daily_report():
    """每日市场调研报告：聚合盘口/涨跌家数/涨跌榜/板块/异动/龙虎榜/资讯，
    产出结构化 JSON（前端排版为自包含报告）。各子模块自带缓存，整体仅比一次 /api/state 略慢。"""
    now = datetime.datetime.now()
    date_str = now.strftime("%Y年%m月%d日")
    weekday = "周" + "一二三四五六日"[now.weekday()]
    report = {
        "date": date_str, "weekday": weekday,
        "ts": int(time.time() * 1000),
        "indices": [], "global": [], "breadth": None,
        "gainers": [], "losers": [], "boards": {"up": [], "down": []},
        "anomaly": [], "billboard": [], "news": [],
        "summary": "", "sentiment": "中性",
    }
    # ---- 1) 指数 + 全球市场 ----
    try:
        state = build_state()
        report["indices"] = [
            {"name": it.get("name"), "price": _safe(it.get("price")),
             "pct": _safe(it.get("pct")), "secid": it.get("secid")}
            for it in state.get("market", {}).get("items", []) if isinstance(it, dict)
        ]
        for g in state.get("global", []):
            items = [
                {"name": it.get("name"), "price": _safe(it.get("price")),
                 "pct": _safe(it.get("pct")), "secid": it.get("secid")}
                for it in g.get("items", []) if isinstance(it, dict)
            ]
            if items:
                report["global"].append({"label": g.get("label"), "items": items})
    except Exception:
        pass

    # ---- 2) 涨跌家数 ----
    try:
        b = get_breadth()
        up, down, flat, total = b.get("up", 0), b.get("down", 0), b.get("flat", 0), b.get("total", 0)
        report["breadth"] = {
            "up": up, "down": down, "flat": flat, "total": total,
            "ratio": round(up / down, 2) if down else (None if not up else 99),
            "upPct": round(up / total * 100, 1) if total else None,
            "totalAmount": b.get("totalAmount"),
        }
    except Exception:
        pass

    # ---- 3) 个股涨跌榜 ----
    try:
        rk = get_rank()
        def _trim(lst):
            out = []
            for it in lst[:10]:
                out.append({"name": it.get("name"), "code": it.get("code"),
                            "price": _safe(it.get("price")), "pct": _safe(it.get("pct")),
                            "secid": it.get("secid")})
            return out
        report["gainers"] = _trim(rk.get("gainers", []))
        report["losers"] = _trim(rk.get("losers", []))
    except Exception:
        pass

    # ---- 4) 板块（领涨 / 领跌）---- 用全量板块，避免漏掉真正的下跌板块
    try:
        bs = [x for x in fetch_boards() if x.get("pct") is not None]
        if bs and bs[0].get("degraded"):
            # 新浪板块源降级（指数模拟填充），不展示为真实板块榜
            report["boards"]["degraded"] = True
        else:
            bs.sort(key=lambda x: x.get("pct", 0), reverse=True)
            # 领涨：仅取真正上涨的板块（按涨幅降序）
            ups = [x for x in bs if x.get("pct", 0) > 0][:8]
            report["boards"]["up"] = [
                {"name": x.get("name"), "pct": round(x.get("pct", 0), 2),
                 "amount": x.get("amount"), "leader": x.get("leader")}
                for x in ups
            ]
            # 领跌：取真正下跌的板块，跌幅最大的排最前
            downs = [x for x in bs if x.get("pct", 0) < 0]
            downs.sort(key=lambda x: x.get("pct", 0))
            report["boards"]["down"] = [
                {"name": x.get("name"), "pct": round(x.get("pct", 0), 2),
                 "amount": x.get("amount"), "leader": x.get("leader")}
                for x in downs[:5]
            ]
    except Exception:
        pass

    # ---- 5) 异动（涨停/急拉/大跌/急跌 取前几条）----
    try:
        an = get_anomaly()
        picks = []
        for gt in ("涨停", "急拉", "大涨", "跌停", "急跌", "大跌"):
            for it in an.get("groups", {}).get(gt, [])[:3]:
                picks.append({"type": gt, "name": it.get("name"), "code": it.get("code"),
                              "pct": _safe(it.get("pct")), "secid": it.get("secid")})
        report["anomaly"] = picks[:14]
    except Exception:
        pass

    # ---- 6) 龙虎榜 ----
    try:
        bb = get_billboard()
        items = bb.get("items", []) or []
        top = sorted([x for x in items if x.get("net") is not None],
                     key=lambda x: x.get("net", 0), reverse=True)[:10]
        report["billboard"] = [
            {"name": x.get("name"), "code": x.get("code"), "pct": _safe(x.get("pct")),
             "net": x.get("net"), "orgNet": x.get("org_net"),
             "reason": x.get("reason"), "secid": x.get("secid")}
            for x in top
        ]
        report["billboardDate"] = bb.get("date")
        report["billboardType"] = bb.get("type")
    except Exception:
        pass

    # ---- 7) 资讯要闻 ----
    try:
        nw = get_news("sina") or {}
        items = nw.get("items") or []
        report["news"] = [
            {"title": it.get("title"), "url": it.get("url"),
             "source": it.get("source"), "time": it.get("time")}
            for it in items[:10] if it.get("title")
        ]
    except Exception:
        pass

    # ---- 8) 自动撰写盘面综述 ----
    report["summary"], report["sentiment"] = _compose_summary(report)
    return report


def _compose_summary(r):
    """依据聚合数据生成一段盘面综述文本 + 情绪标签。"""
    b = r.get("breadth") or {}
    up, down, total = b.get("up", 0), b.get("down", 0), b.get("total", 0)
    up_pct = b.get("upPct")
    # 主板主要指数涨跌
    main_up = [x for x in r.get("indices", []) if (x.get("pct") or 0) > 0]
    main_down = [x for x in r.get("indices", []) if (x.get("pct") or 0) < 0]
    idx_lines = "、".join("%s%s%%" % (x["name"], ("+" if x["pct"] > 0 else "") + str(x["pct"]))
                          for x in r.get("indices", [])[:6] if x.get("pct") is not None)
    bu = r.get("boards", {}).get("up", [])
    bd = r.get("boards", {}).get("down", [])
    top_board = bu[0] if bu else None
    top_down_board = bd[0] if bd else None
    g = r.get("gainers", [])[:3]
    l = r.get("losers", [])[:3]

    # 情绪判定
    if total and up_pct is not None:
        if up_pct >= 70:
            sentiment = "强势普涨"
        elif up_pct >= 55:
            sentiment = "偏多"
        elif up_pct >= 45:
            sentiment = "多空均衡"
        elif up_pct >= 30:
            sentiment = "偏弱"
        else:
            sentiment = "普跌"
    else:
        sentiment = "中性"

    parts = []
    if idx_lines:
        parts.append("今日主要指数%s。" % idx_lines)
    if total and up_pct is not None:
        parts.append("两市涨跌家数 %d 涨 / %d 跌 / %d 平，上涨占比 %.1f%%。" % (up, down, b.get("flat", 0), up_pct))
        amt = b.get("totalAmount")
        if amt:
            parts.append("全市场成交额约 %.2f 亿元。" % (amt / 1e8))
    if top_board:
        parts.append("领涨板块为%s(%s%%)。" % (top_board["name"], ("+" if top_board["pct"] > 0 else "") + str(top_board["pct"])))
    if top_down_board:
        parts.append("领跌板块为%s(%s%%)。" % (top_down_board["name"], str(top_down_board["pct"])))
    if g:
        parts.append("个股方面，涨幅居前：%s。" % "、".join("%s%s%%" % (x["name"], ("+" if x["pct"] > 0 else "") + str(x["pct"])) for x in g))
    if l:
        parts.append("跌幅居前：%s。" % "、".join("%s%s%%" % (x["name"], str(x["pct"])) for x in l))
    an = r.get("anomaly", [])
    if an:
        hot = "、".join("%s%s" % (x["name"], x["type"]) for x in an[:4])
        parts.append("盘面异动：%s。" % hot)
    bb = r.get("billboard", [])
    if bb:
        parts.append("龙虎榜净买入居首：%s（净买%.2f万元）。" % (bb[0]["name"], (bb[0]["net"] or 0) / 1e4))
    if not parts:
        parts.append("暂无足够行情数据生成综述，请稍后重试。")
    return "".join(parts), sentiment


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
        if path == "/api/daily_report":
            try:
                # 首次生成需扫描全市场行情；用线程+超时避免请求挂死。
                # 注意：不能用 `with ThreadPoolExecutor` 上下文管理器——退出 with 块时
                # shutdown(wait=True) 会阻塞等后台线程跑完，使 30s 超时保护完全失效。
                _ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                _fut = _ex.submit(get_daily_report)
                try:
                    d = _fut.result(timeout=30)
                except concurrent.futures.TimeoutError:
                    _ex.shutdown(wait=False)   # 立即返回，不等待后台线程
                    self._json({"error": "报告生成超时（首次生成需扫描全市场行情，约 10–20 秒），请稍候再点一次"})
                    return
                _ex.shutdown(wait=False)
                self._json(d)
            except Exception as e:
                self._json({"error": str(e)})
            return
        if path == "/api/boards":
            try:
                items = fetch_boards()
                # 返回全部行业板块；degraded=True 表示新浪源不可用、已降级为指数模拟
                self._json({"items": items, "degraded": any(x.get("degraded") for x in items)})
            except Exception as e:
                self._json({"error": str(e)})
            return
        if path == "/api/breadth":
            try:
                self._json(get_breadth())
            except Exception as e:
                self._json({"error": str(e)})
            return
        if path == "/api/holdings":
            try:
                self._json({"items": load_holdings()})
            except Exception as e:
                self._json({"error": str(e)})
            return
        if path == "/api/anomaly":
            try:
                self._json(get_anomaly())
            except Exception as e:
                self._json({"error": str(e)})
            return
        if path == "/api/market-anomaly":
            try:
                self._json(get_market_anomaly())
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
                kdata = fetch_kline(secid, klt)
                pc = _preclose_cache.get(secid)
                if pc is None:
                    # 腾讯日K历史接口（fqkline）的 qt 实时块常缺昨收，preClose 会返回 null，
                    # 导致前端 renderKline 的百分比轴被 `pre != null` 守卫掉而不显示。
                    # 回退用分时实时 qt 补一次昨收（fetch_trends 会写入 _preclose_cache）。
                    try:
                        fetch_trends(secid)
                        pc = _preclose_cache.get(secid)
                    except Exception:
                        pc = None
                self._json({"secid": secid, "klt": klt, "data": kdata, "preClose": pc})
            except Exception as e:
                self._json({"error": str(e)})
            return
        if path == "/api/trends":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            secid = (qs.get("secid") or [""])[0]
            try:
                self._json({"secid": secid, "data": fetch_trends(secid), "preClose": _preclose_cache.get(secid)})
            except Exception as e:
                self._json({"error": str(e)})
            return
        if path == "/api/search":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            q = (qs.get("q") or [""])[0]
            try:
                res = search_stock(q) if q else {"stocks": [], "sectors": []}
                self._json({"q": q, "items": res.get("stocks", []),
                            "sectors": res.get("sectors", []) if q else []})
            except Exception as e:
                self._json({"error": str(e)})
            return
        if path == "/api/stock_info":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code = (qs.get("code") or [""])[0]
            try:
                self._json({"info": get_stock_info(code)})
            except Exception as e:
                self._json({"error": str(e)})
            return
        if path == "/api/stock_boards":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code = (qs.get("code") or [""])[0]
            try:
                self._json(get_stock_boards(code))
            except Exception as e:
                self._json({"found": False, "blocked": True, "reason": str(e)[:80]})
            return
        if path == "/api/sector":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            name = (qs.get("name") or [""])[0]
            try:
                self._json(get_sector_info(name))
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
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            bdate = (qs.get("date") or [None])[0]
            try:
                d = get_billboard(bdate)
                self._json(d or {"type": "proxy", "date": None, "items": []})
            except Exception as e:
                self._json({"type": "proxy", "date": None, "items": [], "error": str(e)})
            return
        if path == "/api/stock_lhb":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code = (qs.get("code") or [""])[0]
            try:
                self._json(get_stock_lhb_history(code))
            except Exception as e:
                self._json({"code": code, "count": 0, "records": [], "error": str(e)})
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
        if path == "/api/holdings":
            items = payload.get("items")
            if isinstance(items, list):
                # 清洗字段，防止前端误传脏数据写坏文件
                clean = []
                for it in items:
                    if not isinstance(it, dict) or not it.get("code"):
                        continue
                    clean.append({
                        "code": str(it.get("code")),
                        "name": it.get("name") or "",
                        "cost": float(it.get("cost") or 0),
                        "qty": float(it.get("qty") or 0),
                        "note": it.get("note") or "",
                        "secid": it.get("secid") or "",
                    })
                save_holdings(clean)
                self._json({"ok": True, "items": clean})
            else:
                self._json({"ok": True, "items": load_holdings()})
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

    # ---- 单实例锁：跨平台可靠互斥，防止多实例抢 WebView2 窗口类闪退 ----
    # Windows: 用内核命名互斥量(CreateMutex)。
    #   关键改进 v2 —— 彻底不用 GetLastError（Python 内部 API 会在两次调用间将其重置导致竞态），
    #   改用「创建 + WaitForSingleObject(0ms)」双步骤判断：
    #     a) WAIT_OBJECT_0   → 我们是唯一创建者（或前进程已崩溃 abandoned），安全持有继续运行
    #     b) WAIT_TIMEOUT   → 别的实例正持有锁 → 二次验证：找活着的 Python 进程，找不到=僵尸锁→强夺
    #     c) WAIT_ABANDONED → 前进程崩溃遗留，自动回收继续运行
    # Linux/Mac(如 Render): 用脚本目录下的固定锁文件 + fcntl 排他锁。
    try:
        if os.name == "nt":
            import ctypes, subprocess
            _mux_name = "Global\\StockBoard_Singleton_9f3a"
            _k32 = ctypes.windll.kernel32
            _h_mux = _k32.CreateMutexW(None, False, _mux_name)
            _WAIT_TIMEOUT = 0x00000102
            _WAIT_ABANDONED = 0x00000080
            _rc = _k32.WaitForSingleObject(_h_mux, 0)
            if _rc == _WAIT_TIMEOUT:
                # 检测到锁被占用：找出其它正在运行的 stock-monitor 实例（排除自己）
                _others = []
                try:
                    _r = subprocess.run(
                        ["wmic", "process", "where", "name='python.exe'", "get", "ProcessId,CommandLine", "/FORMAT:CSV"],
                        capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
                    _my_pid = os.getpid()
                    for _wl in _r.stdout.decode("gbk", errors="replace").splitlines():
                        if ("app.py" in _wl or "stock-monitor" in _wl) and str(_my_pid) not in _wl:
                            # CSV 列顺序: Node,ProcessId,CommandLine（CommandLine 可能含逗号，取第 2 列即 PID）
                            _pid = _wl.split(",")[1].strip() if "," in _wl else ""
                            if _pid.isdigit():
                                _others.append(int(_pid))
                except Exception:
                    _others = []
                if _others:
                    # 已有实例在跑：非阻塞——本进程改用独立锁名并照常打开自己的窗口。
                    # 关键修复：绝不强杀旧窗口！强杀活动的 WebView2 进程会把运行时搞坏，
                    # 导致后续新窗口也开不出来（表现为「打不开」）。多开时各实例端口由
                    # pick_port 自动避让，互不干扰，用户关掉多余窗口即可。
                    print("[看板] 检测到已有实例在运行，本次将另外打开一个独立窗口（旧的那个可直接关闭）。")
                    _k32.CloseHandle(_h_mux)
                    _mux_name = "Global\\StockBoard_Singleton_9f3a_" + str(os.getpid())
                    _h_mux = _k32.CreateMutexW(None, False, _mux_name)
                    _rc = _k32.WaitForSingleObject(_h_mux, 0)
                else:
                    # 无活跃进程但 mutex 仍被持有 → 僵尸锁，强制夺取
                    print("[看板] 检测到僵尸锁（无活跃进程但互斥量未释放），强制清理并继续启动。")
                    _k32.CloseHandle(_h_mux)
                    _mux_name = "Global\\StockBoard_Singleton_9f3a_v2"
                    _h_mux = _k32.CreateMutexW(None, False, _mux_name)
                    _rc = _k32.WaitForSingleObject(_h_mux, 0)
            if _rc == _WAIT_ABANDONED:
                # 前进程崩溃遗留的 abandoned mutex —— 安全回收
                print("[看板] 检测到残留锁（旧进程已异常退出），自动清理并继续启动。")
                _k32.ReleaseMutex(_h_mux)
            # _rc == WAIT_OBJECT_0(0)：正常拿到锁，持有句柄继续运行
            # 进程退出/崩溃时操作系统自动 CloseHandle 并释放互斥量
        else:
            _lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".stock-monitor.lock")
            _lock_fh = open(_lock_path, "w")
            try:
                import fcntl
                fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                _lock_fh.close()
                print("[看板] 已有实例在运行。请直接使用已打开的窗口。")
                sys.exit(1)
    except Exception:
        # 锁机制异常时不阻断启动（宁可多开也不至于起不来）
        pass

    port = pick_port(int(os.environ.get("PORT", "8787")))
    # 本地默认只监听回环；部署到云平台（Render 等，HOST 或 RENDER 环境变量）时监听 0.0.0.0 才能接收外部请求
    host = os.environ.get("HOST") or ("0.0.0.0" if os.environ.get("RENDER") else "127.0.0.1")
    server = ThreadingHTTPServer((host, port), Handler)
    shown = "127.0.0.1" if host == "0.0.0.0" else host
    url = f"http://{shown}:{port}/"
    try:
        app_title = load_config().get("title", "实时行情看板 · 詹姆斯是goat")
    except Exception:
        app_title = "实时行情看板 · 詹姆斯是goat"
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    try:
        import webview  # noqa
        has_wv = True
    except Exception:
        has_wv = False

    force_browser = os.environ.get("NO_WINDOW") == "1"
    # 禁用 WebView2 GPU 硬件加速：规避远程桌面/集显/虚拟机下的渲染进程崩溃闪退
    os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGS", "--disable-gpu")
    print(f"[看板] 行情看板已启动（v{VERSION}，腾讯/新浪数据源）：{url}")
    if has_wv and not force_browser:
        try:
            print("[看板] 已打开原生窗口（可最大化/最小化/拖拽缩放）。")
            webview.create_window(
                app_title, url,
                width=1280, height=820, resizable=True, min_size=(720, 520),
            )
            webview.start(gui="edgechromium")
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
