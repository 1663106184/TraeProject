import requests
import time
import os
import pandas as pd
import re
import json
import pymysql
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

_industry_cache = {}
_industry_csv_cache = None  # 本地 CSV 行业数据缓存（懒加载，供无 MySQL 环境使用）

# ---------------- 前十流通股东持股比例（F4）缓存 ----------------
# 实际换手率 = 成交量 / (流通股本 × (1 - 前十流通占比))。前十比例是季度静态数据，
# 可缓存：进程内 dict + 落盘 snapshot/freehold_cache.csv。东财 F10 接口有风控，
# 信号量限制最多 3 个并发请求，每次间隔 ≥ 0.5s；缓存命中秒回不请求。
# 拉不到返回 None（公司网络封东财时）。
_freehold_cache = {}            # code -> ratio_pct (内存)
_freehold_semaphore = threading.Semaphore(3)  # 最多3并发
_freehold_last_call = [0.0]     # 全局节流时间戳（保证间隔≥0.5s）
_freehold_call_lock = threading.Lock()
_freehold_cache_lock = threading.Lock()
FREEHOLD_CACHE_FILE = os.path.join("snapshot", "freehold_cache.csv")
FREEHOLD_MIN_INTERVAL = 0.5     # 东财 F10 两次请求最小间隔（秒），降低到0.5配合并行
FREEHOLD_CACHE_TTL_DAYS = 30    # 落盘缓存有效期（天），超期重拉


def _load_freehold_cache():
    """懒加载落盘缓存到 _freehold_cache。"""
    global _freehold_cache
    if _freehold_cache:
        return
    try:
        if os.path.exists(FREEHOLD_CACHE_FILE):
            df = pd.read_csv(FREEHOLD_CACHE_FILE, dtype={'code': str, 'ratio': str})
            for _, row in df.iterrows():
                code = str(row.get('code') or '').strip()
                ratio = row.get('ratio')
                if code and ratio not in (None, '', 'nan'):
                    try:
                        _freehold_cache[code] = float(ratio)
                    except (TypeError, ValueError):
                        pass
    except Exception:
        pass


def _save_freehold_cache():
    """把内存缓存写回落盘 CSV。"""
    try:
        os.makedirs("snapshot", exist_ok=True)
        df = pd.DataFrame([{'code': str(k), 'ratio': v} for k, v in _freehold_cache.items()])
        df.to_csv(FREEHOLD_CACHE_FILE, index=False, encoding='utf-8-sig')
    except Exception:
        pass


def get_freehold_ratio(code):
    """获取前十流通股东合计持股比例(%)。
    优先内存缓存 -> 落盘缓存 -> 东财 F10 实时拉取（信号量 3 并发，间隔≥0.5s）。
    拉取失败/超时/被封返回 None，不抛异常。
    """
    _load_freehold_cache()
    # 命中缓存直接返回
    with _freehold_cache_lock:
        if code in _freehold_cache:
            return _freehold_cache[code]

    secucode = f"SH{code}" if code.startswith("6") else f"SZ{code}"
    ratio = None
    with _freehold_semaphore:
        with _freehold_call_lock:
            wait = FREEHOLD_MIN_INTERVAL - (time.time() - _freehold_last_call[0])
            if wait > 0:
                time.sleep(wait)
        try:
            r = session.get(
                "https://emweb.securities.eastmoney.com/PC_HSF10/ShareholderResearch/PageAjax",
                params={"code": secucode},
                headers={"Referer": "https://emweb.securities.eastmoney.com/"},
                timeout=8,
            )
            with _freehold_call_lock:
                _freehold_last_call[0] = time.time()
            j = r.json()
            gdrs = j.get("gdrs") or []
            # 取最新一期有 FREEHOLD_RATIO_TOTAL 的记录
            for g in gdrs:
                v = g.get("FREEHOLD_RATIO_TOTAL")
                if v not in (None, "", 0):
                    ratio = float(v)
                    break
        except Exception:
            with _freehold_call_lock:
                _freehold_last_call[0] = time.time()
            ratio = None

    if ratio is not None:
        with _freehold_cache_lock:
            _freehold_cache[code] = ratio
        _save_freehold_cache()   # 拉到即落盘，避免崩溃丢
    return ratio


def get_freehold_ratio_cached(code):
    """只查缓存（内存+落盘），不发网络请求。命中返回比例，未命中返回 None。
    供扫描主流程用：不阻塞，未命中的票由后台异步拉取补全。
    """
    _load_freehold_cache()
    return _freehold_cache.get(code)



def calc_real_turnover(volume_hand, price, float_mcap_yi, freehold_ratio_pct):
    """计算实际换手率(%)。
    volume_hand: 当日成交量(手)
    price: 现价
    float_mcap_yi: 流通市值(亿元)
    freehold_ratio_pct: 前十流通股东合计持股比例(%)，None 时返回 None
    返回实际换手率(%) 或 None。
    """
    if not volume_hand or not price or not float_mcap_yi or freehold_ratio_pct is None:
        return None
    try:
        float_shares = float(float_mcap_yi) * 1e8 / float(price)        # 流通股本(股)
        real_shares = float_shares * (1 - float(freehold_ratio_pct) / 100)  # 实际流通股本(股)
        if real_shares <= 0:
            return None
        vol_shares = float(volume_hand) * 100                          # 手->股
        return round(vol_shares / real_shares * 100, 3)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def calc_real_turnover_from_api(api_turnover_pct, freehold_ratio_pct):
    """用接口换手率反算实际换手率(%)。
    接口换手率 = 成交量/流通股本；实际换手率 = 成交量/实际流通股本
              = 接口换手率 / (1 - 前十流通占比)。
    供后台 F4 补全用（只需接口换手率 + F4 比例，不依赖成交量/市值）。
    """
    if not api_turnover_pct or freehold_ratio_pct is None:
        return None
    try:
        denom = 1 - float(freehold_ratio_pct) / 100
        if denom <= 0:
            return None
        return round(float(api_turnover_pct) / denom, 3)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


# API 调用参数
API_TIMEOUT = 4          # 单次请求超时（秒），原 2s 过短易误判失败
API_MAX_RETRIES = 2      # 失败重试次数
API_RETRY_BACKOFF = 0.5  # 重试退避基数（秒），每次乘 2
API_MIN_INTERVAL = 0.02  # 单线程最小请求间隔，配合并发做基础限速

# 简单的线程级请求限速：每个线程记录上次请求时间，避免空跑打爆接口
_tls = threading.local()


def _throttle():
    """限速：保证单线程两次请求间至少间隔 API_MIN_INTERVAL 秒。"""
    last = getattr(_tls, 'last_req', 0)
    now = time.monotonic()
    wait = API_MIN_INTERVAL - (now - last)
    if wait > 0:
        time.sleep(wait)
    _tls.last_req = time.monotonic()


def _request_get(url, timeout=API_TIMEOUT, encoding=None):
    """带重试 + 退避的 GET 封装，统一异常处理，复用全局 session 连接池。"""
    for attempt in range(API_MAX_RETRIES + 1):
        try:
            _throttle()
            res = session.get(url, timeout=timeout)
            if encoding:
                res.encoding = encoding
            res.raise_for_status()
            return res
        except Exception:
            if attempt < API_MAX_RETRIES:
                time.sleep(API_RETRY_BACKOFF * (2 ** attempt))
    return None

MYSQL_CONFIG = {
    'host': 'localhost',
    'port': 3306,
    'user': 'root',
    'password': 'root',
    'database': 'stock_db',
    'charset': 'utf8mb4'
}

def get_mysql_connection():
    try:
        return pymysql.connect(**MYSQL_CONFIG)
    except Exception:
        return None

# MySQL 行业数据全表缓存（一次性加载，避免逐只查询反复连接）
_mysql_industry_cache = None      # dict: code -> (industry, sector, concepts)
_mysql_tried = False              # 是否已尝试过连 MySQL（失败则不再重试，避免每只股票都等超时）
_mysql_lock = threading.Lock()


def _load_industry_mysql():
    """一次性从 MySQL 加载全部行业数据到内存。失败返回空 dict，且不再重试。"""
    global _mysql_industry_cache, _mysql_tried
    with _mysql_lock:
        if _mysql_tried:
            return _mysql_industry_cache or {}
        _mysql_tried = True
        _mysql_industry_cache = {}
        conn = get_mysql_connection()
        if not conn:
            return {}
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT code, industry, sector, concepts FROM stock_industry")
            for code, industry, sector, concepts in cursor.fetchall():
                code = (code or '').strip()
                if code:
                    _mysql_industry_cache[code] = (industry or '', sector or '', concepts or '')
        except Exception:
            _mysql_industry_cache = {}
        finally:
            conn.close()
        return _mysql_industry_cache


def _load_industry_csv():
    """懒加载本地 stock_industry.csv（随 exe 打包分发，对方无 MySQL 时用）。
    返回 dict: code -> (industry, sector, concepts)。加载失败返回 None。
    """
    global _industry_csv_cache
    if _industry_csv_cache is not None:
        return _industry_csv_cache
    _industry_csv_cache = {}  # 标记已尝试，避免反复读盘
    try:
        import csv, os
        # 1) PyInstaller 打包后资源在 sys._MEIPASS；2) 源码运行用当前目录
        path = None
        try:
            from sys import _MEIPASS  # 打包后才存在
            cand = os.path.join(_MEIPASS, 'stock_industry.csv')
            if os.path.exists(cand):
                path = cand
        except Exception:
            pass
        if not path:
            # 依次尝试当前工作目录、本文件所在目录
            for d in (os.getcwd(), os.path.dirname(os.path.abspath(__file__))):
                cand = os.path.join(d, 'stock_industry.csv')
                if os.path.exists(cand):
                    path = cand
                    break
        if not path:
            return None
        with open(path, encoding='utf-8-sig') as f:
            r = csv.DictReader(f)
            for row in r:
                code = (row.get('code') or '').strip()
                if not code:
                    continue
                _industry_csv_cache[code] = (
                    row.get('industry') or '',
                    row.get('sector') or '',
                    row.get('concepts') or '',
                )
    except Exception:
        _industry_csv_cache = {}
    return _industry_csv_cache


def get_stock_industry(code):
    global _industry_cache

    if code in _industry_cache:
        return _industry_cache[code]

    # 市场板块作为兜底归属（至少能区分主板/创业板/科创板）
    market_sector = get_market_sector(code)

    result = {
        '同花顺行业': '未分类',
        '同花顺板块': market_sector,
        '最相关概念': '未分类',
        '所属概念数量': 0,
        '概念列表': []
    }

    # 优先读本地 CSV（随 exe 分发，对方无 MySQL 也能用）；读不到再查 MySQL 全表缓存
    row = None
    csv_data = _load_industry_csv()
    if csv_data and code in csv_data:
        row = csv_data[code]  # (industry, sector, concepts)
    else:
        # MySQL 全表已一次性载入内存；未命中则该票无行业数据，用市场板块兜底
        mysql_data = _load_industry_mysql()
        if code in mysql_data:
            row = mysql_data[code]

    if row:
        industry = row[0] or ''
        # 行业有效则采用；空或「未分类」则保留市场板块兜底
        if industry and industry != '未分类':
            result['同花顺行业'] = industry
        else:
            result['同花顺行业'] = f'未分类({market_sector})'
        # 板块：优先用表里的 sector，否则用市场板块
        result['同花顺板块'] = row[1] or market_sector
        concepts = row[2] or ''
        if concepts:
            # 概念用 ';' 分隔存储，清洗空值
            concept_list = [c.strip() for c in concepts.split(';') if c.strip()]
            result['概念列表'] = concept_list
            result['最相关概念'] = concept_list[0] if concept_list else '未分类'
            result['所属概念数量'] = len(concept_list)

    _industry_cache[code] = result
    return result

def get_market_sector(code):
    if code.startswith('688'):
        return '科创板'
    elif code.startswith(('300', '301')):
        return '创业板'
    elif code.startswith('6'):
        return '沪市主板'
    elif code.startswith('0'):
        return '深市主板'
    else:
        return '其他'

def get_stock_raw(code):
    """与 get_stock 相同，但不过滤涨跌方向（下跌/平盘也返回）。
    供筹码筛选等需要全方向股票的逻辑使用；主扫描流程仍用 get_stock（只上涨）。
    """
    try:
        if code.startswith("6"):
            api_code = f"sh{code}"
        else:
            api_code = f"sz{code}"

        url = f"http://qt.gtimg.cn/q={api_code}"
        res = _request_get(url, encoding="gbk")
        if res is None:
            return None

        data = res.text.split("~")

        if len(data) < 45 or data[1] == "" or data[3] == "":
            return None

        now = float(data[3])
        yes = float(data[4])
        volume = 0
        if len(data) > 6 and data[6] and data[6] != '0' and data[6] != '':
            try:
                volume = int(data[6])
            except:
                volume = 0

        zdf = (now - yes) / yes * 100

        # 只过滤停牌/无效成交量，不过滤涨跌方向
        if volume <= 0 or volume > 10**12:
            return None

        bid_vol = 0.0
        ask_vol = 0.0
        try:
            for i in (10, 12, 14, 16, 18):
                if len(data) > i and data[i] and data[i] != '':
                    bid_vol += float(data[i])
            for i in (20, 22, 24, 26, 28):
                if len(data) > i and data[i] and data[i] != '':
                    ask_vol += float(data[i])
        except Exception:
            bid_vol = ask_vol = 0.0
        total = bid_vol + ask_vol
        weibi = round((bid_vol - ask_vol) / total * 100, 2) if total > 0 else 0.0

        # 流通市值(data[44],亿元) + 总市值(data[45],亿元)；实际换手率按流通股本算
        circ_mc = float(data[44]) * 10**8 if len(data) > 44 and data[44] else 0.0
        total_mc = float(data[45]) * 10**8 if len(data) > 45 and data[45] else 0.0
        # 实际换手率 = 当日成交量(股) / 流通股本(股) * 100
        turnover_real = 0.0
        if circ_mc > 0 and now > 0 and volume > 0:
            circ_shares = circ_mc / now
            turnover_real = (volume * 100) / circ_shares * 100  # volume 是手，×100 转股

        return {
            "code": code,
            "name": data[1],
            "now": now,
            "yes": yes,
            "open": float(data[5]) if data[5] else 0,
            "volume": volume,
            "yesterday_volume": 0,
            "volume_ratio": 0,
            "weibi": weibi,
            "bid_vol": int(bid_vol),
            "ask_vol": int(ask_vol),
            "amount": float(data[37]) * 10000 if len(data) > 37 and data[37] else 0,
            "zdf": zdf,
            "high": float(data[33]) if len(data) > 33 and data[33] else now,
            "low": float(data[34]) if len(data) > 34 and data[34] else now,
            "turnover": float(data[38]) if len(data) > 38 and data[38] else 0,
            "turnover_real": round(turnover_real, 3),
            "market_cap": total_mc,
            "circ_market_cap": circ_mc,
            "sector": get_market_sector(code)
        }
    except Exception as e:
        return None


def get_stock(code):
    try:
        if code.startswith("6"):
            api_code = f"sh{code}"
        else:
            api_code = f"sz{code}"

        url = f"http://qt.gtimg.cn/q={api_code}"
        res = _request_get(url, encoding="gbk")
        if res is None:
            return None

        data = res.text.split("~")

        if len(data) < 45 or data[1] == "" or data[3] == "":
            return None

        now = float(data[3])
        yes = float(data[4])
        volume = 0
        if len(data) > 6 and data[6] and data[6] != '0' and data[6] != '':
            try:
                volume = int(data[6])
            except:
                volume = 0

        zdf = (now - yes) / yes * 100

        if zdf <= 0 or volume <= 0 or volume > 10**12:
            return None

        # 委比(五档量) = (Σ买五档量 - Σ卖五档量) / (Σ买五档量 + Σ卖五档量) * 100
        # 买五档量索引: 10,12,14,16,18；卖五档量索引: 20,22,24,26,28
        bid_vol = 0.0
        ask_vol = 0.0
        try:
            for i in (10, 12, 14, 16, 18):
                if len(data) > i and data[i] and data[i] != '':
                    bid_vol += float(data[i])
            for i in (20, 22, 24, 26, 28):
                if len(data) > i and data[i] and data[i] != '':
                    ask_vol += float(data[i])
        except Exception:
            bid_vol = ask_vol = 0.0
        total = bid_vol + ask_vol
        weibi = round((bid_vol - ask_vol) / total * 100, 2) if total > 0 else 0.0

        # 流通市值(data[44],亿元) + 总市值(data[45],亿元)；实际换手率按流通股本算
        circ_mc = float(data[44]) * 10**8 if len(data) > 44 and data[44] else 0.0
        total_mc = float(data[45]) * 10**8 if len(data) > 45 and data[45] else 0.0
        turnover_real = 0.0
        if circ_mc > 0 and now > 0 and volume > 0:
            circ_shares = circ_mc / now
            turnover_real = (volume * 100) / circ_shares * 100  # volume 是手，×100 转股

        return {
            "code": code,
            "name": data[1],
            "now": now,
            "yes": yes,
            "open": float(data[5]) if data[5] else 0,
            "volume": volume,
            "yesterday_volume": 0,
            "volume_ratio": 0,
            "weibi": weibi,
            "bid_vol": int(bid_vol),
            "ask_vol": int(ask_vol),
            # 腾讯 qt 接口字段：data[36]=成交量(手)，data[37]=成交额(万元)，data[38]=换手率(%)
            "amount": float(data[37]) * 10000 if len(data) > 37 and data[37] else 0,
            "zdf": zdf,
            "high": float(data[33]) if len(data) > 33 and data[33] else now,
            "low": float(data[34]) if len(data) > 34 and data[34] else now,
            "turnover": float(data[38]) if len(data) > 38 and data[38] else 0,
            "turnover_real": round(turnover_real, 3),
            # data[44] 流通市值、data[45] 总市值单位均为"亿元"，转元需 *10**8
            "market_cap": total_mc,
            "circ_market_cap": circ_mc,
            "sector": get_market_sector(code)
        }
    except Exception as e:
        return None

def get_kline_data(code, days=60):
    try:
        if code.startswith("6"):
            api_code = f"sh{code}"
        else:
            api_code = f"sz{code}"

        # 前复权(qfq)：最新 K 线 close 即真实现价，与 get_stock 的 now 同价格空间，
        # 筹码分布才能正确比较。hfq(后复权)会把历史价放大，导致现价永远在筹码之下。
        kline_url = f"http://ifzq.gtimg.cn/appstock/app/fqkline/get?_var=kline_dayqfq&param={api_code},day,,,{days},qfq"
        kline_res = _request_get(kline_url)
        if kline_res is None:
            return None
        kline_text = kline_res.text

        match = re.search(r'=(\{.*\})', kline_text)
        if match:
            try:
                # 用 json.loads 替代 eval，避免安全风险与解析歧义
                kline_data = json.loads(match.group(1))
                if kline_data.get('code') == 0 and kline_data.get('msg') == '':
                    data_key = api_code if api_code in kline_data.get('data', {}) else list(kline_data.get('data', {}).keys())[0] if kline_data.get('data') else None

                    if data_key:
                        # 前复权优先 qfqday，回退到 day（不复权）
                        days_data = kline_data['data'].get(data_key, {}).get('qfqday', [])
                        if not days_data:
                            days_data = kline_data['data'].get(data_key, {}).get('day', [])

                        if days_data:
                            cleaned_data = []
                            for row in days_data:
                                if len(row) == 7 and isinstance(row[-1], dict):
                                    cleaned_data.append(row[:6])
                                elif len(row) >= 6:
                                    cleaned_data.append(row[:6])
                                else:
                                    cleaned_data.append(row)

                            if len(cleaned_data[0]) == 6:
                                df = pd.DataFrame(cleaned_data, columns=['date', 'open', 'close', 'high', 'low', 'volume'])
                            else:
                                df = pd.DataFrame(cleaned_data)

                            df['close'] = df['close'].astype(float)
                            df['high'] = df['high'].astype(float)
                            df['low'] = df['low'].astype(float)
                            df['volume'] = df['volume'].astype(float)
                            return df
            except:
                pass
    except Exception as e:
        pass

    return None

def calculate_technical_indicators(df):
    if df is None or len(df) < 20:
        return {}

    close = df['close']
    high = df['high']
    low = df['low']

    ma5 = close.rolling(window=5).mean().iloc[-1]
    ma10 = close.rolling(window=10).mean().iloc[-1]
    ma20 = close.rolling(window=20).mean().iloc[-1]
    ma60 = close.rolling(window=60).mean().iloc[-1]

    std20 = close.rolling(window=20).std().iloc[-1]
    bollinger_upper = ma20 + 2 * std20
    bollinger_middle = ma20
    bollinger_lower = ma20 - 2 * std20

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    macd = (dif - dea) * 2

    is_bull_arrangement = ma5 > ma10 > ma20 > ma60

    last_close = close.iloc[-1]

    # BBI(多空指标) = (MA3 + MA6 + MA12 + MA24) / 4
    ma3 = close.rolling(window=3).mean().iloc[-1]
    ma6 = close.rolling(window=6).mean().iloc[-1]
    ma12 = close.rolling(window=12).mean().iloc[-1]
    ma24 = close.rolling(window=24).mean().iloc[-1]
    bbi = (ma3 + ma6 + ma12 + ma24) / 4
    # 股价在 BBI 之上视为多头，之下视为空头
    bbi_position = 'BBI之上(多)' if last_close > bbi else 'BBI之下(空)'

    bollinger_position = '上轨上方' if last_close > bollinger_upper else ('下轨下方' if last_close < bollinger_lower else '布林带内')

    macd_signal = '金叉' if dif.iloc[-1] > dea.iloc[-1] and dif.iloc[-2] <= dea.iloc[-2] else \
                  '死叉' if dif.iloc[-1] < dea.iloc[-1] and dif.iloc[-2] >= dea.iloc[-2] else '无信号'

    ma_strength = f"多头排列" if is_bull_arrangement else f"震荡整理"

    return {
        'MA5': round(ma5, 2),
        'MA10': round(ma10, 2),
        'MA20': round(ma20, 2),
        'MA60': round(ma60, 2),
        '布林线上轨': round(bollinger_upper, 2),
        '布林线中轨': round(bollinger_middle, 2),
        '布林线下轨': round(bollinger_lower, 2),
        '布林带位置': bollinger_position,
        'MACD_DIF': round(dif.iloc[-1], 2),
        'MACD_DEA': round(dea.iloc[-1], 2),
        'MACD柱': round(macd.iloc[-1], 2),
        'MACD信号': macd_signal,
        '均线状态': ma_strength,
        '多头排列': '是' if is_bull_arrangement else '否',
        'BBI': round(bbi, 2),
        'BBI位置': bbi_position
    }

def get_stock_news(code, limit=3):
    try:
        # secid 格式：市场前缀.代码。沪市为 1，深市为 0（原 1.0{code} 拼接错误）
        if code.startswith("6"):
            secid = f"1.{code}"
        else:
            secid = f"0.{code}"

        news_url = f"http://datacenter.eastmoney.com/securities/api/data/get?type=RPT_LASTESTNOTICE&sty=SECUCODE%2CNOTICETITLE%2CPUBLISHINGTIME&filter=(SECUCODE='{secid}')&order=PUBLISHINGTIME%20desc&pageSize={limit}&pageNumber=1"
        # 复用全局 session（连接池 + 统一 UA），通过 _request_get 限速重试
        res = _request_get(news_url, timeout=5)
        if res is None:
            return '暂无公告'

        # 接口可能返回 HTML 拦截页（URL过滤），此时 text 不以 { 开头，直接降级
        text = res.text.strip()
        if not text or text[0] != '{':
            return '暂无公告'

        data = json.loads(text)

        news_list = []
        if data.get('success') and data.get('result', {}).get('data'):
            for item in data['result']['data'][:limit]:
                title = item.get('NOTICETITLE', '')
                pub_time = item.get('PUBLISHINGTIME', '')[:10]
                if title:
                    news_list.append(f"{pub_time}: {title}")

        return "; ".join(news_list) if news_list else '暂无公告'
    except Exception as e:
        return '暂无公告'

def generate_stock_codes():
    codes = []
    for prefix in ["600", "601", "603", "605"]:
        for i in range(0, 999):
            codes.append(f"{prefix}{i:03d}")
    for i in range(0, 800):
        codes.append(f"688{i:03d}")
    for i in range(1, 999):
        codes.append(f"000{i:03d}")
    for i in range(0, 999):
        codes.append(f"002{i:03d}")
    for i in range(0, 999):
        codes.append(f"300{i:03d}")
    for i in range(0, 600):
        codes.append(f"301{i:03d}")
    return sorted(list(set(codes)))

def format_number(num):
    if num >= 100000000:
        return f"{num / 100000000:.2f}亿"
    elif num >= 10000:
        return f"{num / 10000:.2f}万"
    return f"{num:.2f}"

def process_stock(code):
    stock = get_stock(code)
    if stock and stock['volume'] > 0 and stock['zdf'] > 0:
        popularity = int(100000 + stock['zdf'] * 5000 + stock['turnover'] * 10)

        kline_df = get_kline_data(code, days=60)
        if kline_df is not None and len(kline_df) >= 2:
            yesterday_volume = int(kline_df.iloc[-2]['volume'])
            volume_ratio = round(stock['volume'] / yesterday_volume, 2) if yesterday_volume > 0 else 0
        else:
            yesterday_volume = 0
            volume_ratio = 0

        # 实际换手率 = 接口换手率 / (1 - 前十流通占比)。
        # 用腾讯 data[38] 换手率反算，避免 data[6] 成交量单位在不同板块不一致的问题。
        freehold_ratio = get_freehold_ratio(code)
        real_turnover = calc_real_turnover_from_api(stock['turnover'], freehold_ratio)

        vol_compare_pct = (stock['volume'] - yesterday_volume) / yesterday_volume * 100 if yesterday_volume > 0 else 0
        vol_status = '放量' if volume_ratio >= 1.5 else ('缩量' if volume_ratio <= 0.7 else '正常')

        tech_indicators = calculate_technical_indicators(kline_df)

        industry_info = get_stock_industry(code)

        news = get_stock_news(code, limit=3)

        patterns = []

        if stock['now'] > stock['open']:
            patterns.append('阳线')
        else:
            patterns.append('价升')

        if stock['zdf'] > 0 and volume_ratio >= 1.3:
            patterns.append('量价齐升')

        macd_signal = tech_indicators.get('MACD信号', '')
        if macd_signal == '金叉':
            patterns.append('MACD金叉')

        if tech_indicators.get('多头排列') == '是':
            patterns.append('多头排列')

        bollinger_pos = tech_indicators.get('布林带位置', '')
        if bollinger_pos == '上轨上方':
            patterns.append('布林带上轨')
        elif bollinger_pos == '下轨下方':
            patterns.append('布林带下轨')

        ma5 = tech_indicators.get('MA5', 0)
        ma10 = tech_indicators.get('MA10', 0)
        if ma5 > ma10 and ma5 > 0 and ma10 > 0:
            patterns.append('MA5上穿MA10')

        tech_pattern = '||'.join(patterns)

        buy_signals = []
        if macd_signal == '金叉':
            buy_signals.append('MACD金叉')
        if tech_indicators.get('多头排列') == '是':
            buy_signals.append('多头排列')
        if stock['zdf'] > 0 and volume_ratio >= 1.3:
            buy_signals.append('量价齐升')
        if bollinger_pos == '下轨下方':
            buy_signals.append('布林带下轨支撑')

        buy_signal = ';'.join(buy_signals) if buy_signals else '观望'

        result = {
            '日期': pd.Timestamp.now().strftime('%Y-%m-%d'),
            '代码': code + ('.SH' if code.startswith('6') else '.SZ'),
            '名称': stock['name'],
            '市场板块': stock['sector'],
            '收盘价': stock['now'],
            '涨跌幅': stock['zdf'],
            '趋势': '开',
            '热度': popularity,
            '热度排名': 0,
            '大单净额': 0,
            '量比': volume_ratio,
            '委托比': stock['weibi'],
            '换手率': stock['turnover'],
            '换手(实)': real_turnover,
            '成交额': stock['amount'],
            '成交量(手)': stock['volume'],
            '昨日成交量(手)': yesterday_volume,
            '成交量对比': vol_compare_pct,
            '量能状态': vol_status,
            '总市值': stock['market_cap'],
            '流通市值': stock.get('circ_market_cap', 0.0),
            '诊股综合评分': round(5.5 + (stock['zdf'] / 10), 1),
            '技术面评分': round(6.0 + (stock['zdf'] / 20), 1),
            '资金面评分': round(4.0 + (stock['zdf'] / 15), 1),
            '技术形态': tech_pattern,
            '买入信号': buy_signal,
            '同花顺行业': industry_info['同花顺行业'],
            '同花顺板块': industry_info['同花顺板块'],
            '最相关概念': industry_info['最相关概念'],
            '所属概念数量': industry_info['所属概念数量'],
            '概念列表': industry_info['概念列表'],
            'MA5': tech_indicators.get('MA5', 0),
            'MA10': tech_indicators.get('MA10', 0),
            'MA20': tech_indicators.get('MA20', 0),
            'MA60': tech_indicators.get('MA60', 0),
            'BBI': tech_indicators.get('BBI', 0),
            'BBI位置': tech_indicators.get('BBI位置', '未知'),
            '布林线上轨': tech_indicators.get('布林线上轨', 0),
            '布林线中轨': tech_indicators.get('布林线中轨', 0),
            '布林线下轨': tech_indicators.get('布林线下轨', 0),
            '布林带位置': tech_indicators.get('布林带位置', '未知'),
            'MACD_DIF': tech_indicators.get('MACD_DIF', 0),
            'MACD_DEA': tech_indicators.get('MACD_DEA', 0),
            'MACD柱': tech_indicators.get('MACD柱', 0),
            'MACD信号': macd_signal,
            '均线状态': tech_indicators.get('均线状态', '未知'),
            '多头排列': tech_indicators.get('多头排列', '否'),
            '利好消息': news
        }
        return result
    return None

def main():
    print("=" * 60)
    print("股票全市场扫描程序 (MySQL版)")
    print("筛选条件: 今日上涨")
    print("新增功能: 行业信息、技术指标、利好消息")
    print("=" * 60)

    conn = get_mysql_connection()
    if conn:
        print("\nMySQL连接成功！")
        conn.close()
    else:
        print("\n警告: MySQL连接失败，行业信息可能无法获取！")

    stock_codes = generate_stock_codes()
    print(f"\n共生成 {len(stock_codes)} 只股票代码...")

    stocks_data = []
    valid_count = 0
    rise_count = 0
    failed_count = 0

    print("\n开始扫描...")
    start_time = time.time()

    # 并发数从 50 降至 30：配合限速降低被接口封禁的概率，失败率更低
    with ThreadPoolExecutor(max_workers=30) as executor:
        futures = {executor.submit(process_stock, code): code for code in stock_codes}

        for idx, future in enumerate(as_completed(futures), 1):
            result = future.result()
            if result:
                stocks_data.append(result)
                rise_count += 1
                valid_count += 1
            else:
                failed_count += 1

            if idx % 200 == 0:
                elapsed = time.time() - start_time
                progress = (idx / len(stock_codes)) * 100
                print(f"\r已扫描: {idx}/{len(stock_codes)} ({progress:.1f}%)  有效:{valid_count}  上涨:{rise_count}  耗时:{elapsed:.1f}s", end="")

    elapsed_time = time.time() - start_time
    print("\n" + "=" * 60)

    df_result = pd.DataFrame(stocks_data)

    if not df_result.empty:
        df_result = df_result.sort_values('热度', ascending=False)
        df_result['热度排名'] = df_result['热度'].rank(ascending=False, method='dense').astype(int)
        df_result['成交额'] = df_result['成交额'].apply(format_number)
        df_result['总市值'] = df_result['总市值'].apply(format_number)
        df_result['大单净额'] = df_result['大单净额'].apply(lambda x: f"{x}万元" if x > 0 else "0万元")
        df_result['涨跌幅'] = df_result['涨跌幅'].apply(lambda x: f"{x:.2f}%")
        df_result['量比'] = df_result['量比'].apply(lambda x: round(x, 2) if x > 0 else 0)
        df_result['委托比'] = df_result['委托比'].apply(lambda x: f"{x:+.2f}%" if x != 0 else "0%")
        df_result['成交量对比'] = df_result['成交量对比'].apply(lambda x: f"{float(x):+.2f}%" if abs(x) > 0.001 else "0%")
        df_result['成交量(手)'] = df_result['成交量(手)'].astype(int)
        df_result['昨日成交量(手)'] = df_result['昨日成交量(手)'].astype(int)

    print(f"\n扫描完成！")
    print(f"总代码数: {len(stock_codes)}")
    print(f"有效股票: {valid_count}")
    print(f"上涨股票: {rise_count}")
    print(f"失败数量: {failed_count}")
    print(f"耗时: {elapsed_time:.2f}秒")

    if not df_result.empty:
        output_file = f"B点股票_{pd.Timestamp.now().strftime('%Y%m%d')}.csv"
        df_result.to_csv(output_file, index=False, encoding='utf-8-sig')
        print(f"\n结果已保存到文件: {output_file}")
        print(f"文件包含 {len(df_result)} 只上涨股票")
        print(f"新增字段: 同花顺行业、同花顺板块、MA5/MA10/MA20/MA60、BBI、布林线、MACD、多头排列、利好消息")
    else:
        print("\n没有找到上涨的股票")

    print("\n" + "=" * 60)

if __name__ == "__main__":
    main()