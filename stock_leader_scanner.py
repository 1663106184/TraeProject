# -*- coding: utf-8 -*-
"""
超短线龙头/后排接力扫描器 - PyQt5 桌面版
==========================================
面向「打龙头、龙头买不到看后排」的超短线玩法，三层结构：

  1) 情绪闸门（大盘择时）：扫全市场统计涨停/跌停/连板高度，给出「可操作 / 谨慎 / 空仓」提示，
     不过滤个股，只做顶部红黄绿灯。
  2) 个股打分（20 日浮筹 + 量能 + 形态 + 封单）：
     - 筹码窗口缩短到 20 日（超短线真实浮筹），不复用 120 日；
     - 量能用「当日量 / 近5日均量」倍数，比单日量比更稳；
     - 连板身位用近 5 日 K 线判定首板/2连/3连+；
     - 封单强度看五档买一与委比。
  3) 龙头/后排分类：
     - 同板块当日涨幅第 1 且涨停 → 龙头候选；
     - 同板块第 2~4 名、放量、未涨停、获利盘高 → 后排接力；
     - 其余形态好但身位靠后 → 候选。

⚠️ 重要说明：
  日线盘后选股只能「缩小候选池」，不可能稳定抓到龙头——龙头是盘中分时+题材+资金博弈的产物。
  本工具用于盘后复盘 / 次日候选池预筛，实盘仍需结合盘口。

复用 stock_full_scan（行情/K线/行业）与 stock_chip_filter（筹码分布算法）。
运行： py stock_leader_scanner.py
"""

import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QObject, QRectF, QTimer
from PyQt5.QtGui import QColor, QFont, QPainter, QPen, QBrush
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QLabel, QTableWidget, QTableWidgetItem,
    QProgressBar, QStatusBar, QHeaderView, QComboBox, QMessageBox,
    QDoubleSpinBox, QSpinBox, QFileDialog, QGroupBox, QDialog,
    QSplitter, QCheckBox
)

from stock_full_scan import (
    generate_stock_codes,
    get_stock_raw,
    get_kline_data,
    get_stock_industry,
    format_number,
    get_freehold_ratio,
    get_freehold_ratio_cached,
    calc_real_turnover,
    calc_real_turnover_from_api,
)
import stock_chip_filter as scf


# ---------------- 超短线参数 ----------------
SHORT_CHIP_DAYS = 20         # 短期浮筹窗口（超短线真实换手）
PROFIT_RATIO_MIN = 0.55      # 获利盘下限（短期浮筹之下占比）
VOL_MA5_MIN = 1.3            # 量能下限：当日量 / 近5日均量 ≥ 1.3
ZDF_MIN = 3.0                # 当日涨幅下限（%）：超短线至少要有点动静
ZT_MIN = 9.6                 # 涨停判定（%）：考虑科创板/创业板20cm也用此粗筛，精确涨停在连板判定里再判
CONNECT_BOARD_DAYS = 5       # 连板判定回看天数
MAX_WORKERS = 30

# 情绪闸门阈值（顶部红黄绿灯用）
SENTIMENT_ZT_OK = 30         # 涨停家数 ≥ 此值视为情绪正常
SENTIMENT_ZT_BAD = 10        # 涨停家数 ≤ 此值视为情绪冰点
SENTIMENT_DT_BAD = 30        # 跌停家数 ≥ 此值警惕
SENTIMENT_HIGH_BOARD = 4     # 最高连板高度 ≥ 此值视为有赚钱效应

# 评分权重
W_PROFIT = 25.0   # 获利盘
W_VOL = 20.0      # 量能
W_ZDF = 15.0      # 涨幅
W_BOARD = 25.0    # 连板身位
W_BLOCK_RANK = 15.0  # 板块内排名


# ---------------- 工具函数 ----------------
def is_limit_up(zdf, code):
    """涨停判定：科创板/创业板 20%，主板 10%（宽松用 9.6/19.6）。"""
    if code.startswith(('300', '301', '688')):
        return zdf >= 19.6
    return zdf >= 9.6


def calc_consecutive_boards(df, code):
    """用近 N 日 K 线判断当前是第几连板（含今日）。
    返回连板数：今日未涨停返回 0，否则返回连续涨停天数。
    """
    if df is None or len(df) < 1:
        return 0
    n = len(df)
    cnt = 0
    for i in range(n - 1, -1, -1):
        c = float(df['close'].iloc[i])
        # 用前一日收盘近似涨幅（无昨收字段，用 close.shift）
        if i == 0:
            break
        prev_c = float(df['close'].iloc[i - 1])
        if prev_c <= 0:
            break
        zdf_i = (c - prev_c) / prev_c * 100
        if is_limit_up(zdf_i, code):
            cnt += 1
        else:
            break
    return cnt


def get_kline_fast(code, days=30, timeout=1.8, retries=1, backoff=0.2):
    """快速 K 线拉取：短超时 + 轻重试（默认 1 次，短退避）。
    平衡「准确性 vs 速度」：
      - 正常票 200~500ms 一次过，1.8s 超时留了 3~6 倍余量，几乎不触发超时；
      - 偶发网络抖动：重试 1 次大概率救回（不丢数据）；
      - 真·慢票/无效票：最多 1.8 + 0.2 + 1.8 = 3.8s 放弃（原 4s×2 重试 = 13.5s）。
    想更稳：调 retries=2；想更快容忍丢票：调 retries=0。
    """
    import re, json
    from stock_full_scan import session, _throttle
    api_code = f"sh{code}" if code.startswith("6") else f"sz{code}"
    url = (f"http://ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"_var=kline_dayqfq&param={api_code},day,,,{days},qfq")
    last_err = None
    for attempt in range(retries + 1):
        try:
            _throttle()
            res = session.get(url, timeout=timeout)
            res.raise_for_status()
            text = res.text
            m = re.search(r'=(\{.*\})', text)
            if not m:
                return None
            data = json.loads(m.group(1))
            if data.get('code') != 0:
                return None
            data_dict = data.get('data', {}) or {}
            key = api_code if api_code in data_dict else (list(data_dict.keys())[0] if data_dict else None)
            if not key:
                return None
            days_data = data_dict.get(key, {}).get('qfqday', []) or data_dict.get(key, {}).get('day', [])
            if not days_data:
                return None
            cleaned = []
            for row in days_data:
                if len(row) >= 6:
                    cleaned.append(row[:6])
            if not cleaned or len(cleaned[0]) < 6:
                return None
            df = pd.DataFrame(cleaned, columns=['date', 'open', 'close', 'high', 'low', 'volume'])
            df['close'] = df['close'].astype(float)
            df['high'] = df['high'].astype(float)
            df['low'] = df['low'].astype(float)
            df['volume'] = df['volume'].astype(float)
            return df
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(backoff)
    return None


def calc_short_chip(df, current_price, bins=scf.PRICE_BINS, days=SHORT_CHIP_DAYS):
    """短期浮筹：取近 N 日 K 线算筹码分布，返回 (profit_ratio, trapped_ratio, peak_price) 或 None。"""
    if df is None or len(df) < 5:
        return None
    df_short = df.tail(days)
    res = scf.build_chip_distribution(df_short, bins=bins)
    if res is None:
        return None
    centers, chip, total = res
    peak = float(centers[int(np.argmax(chip))])
    above = centers > current_price
    trapped = float(chip[above].sum()) / total
    profit = 1.0 - trapped
    return profit, trapped, peak


def compute_leader_features(code, stock=None):
    """计算单只股票的超短线特征。返回 dict 或 None。
    stock 可传入已有行情 dict，避免重复请求 get_stock_raw（两段式扫描用）。
    """
    if stock is None:
        stock = get_stock_raw(code)
    if not stock or stock['volume'] <= 0:
        return None

    # 近 5 日 K 线用于：5日均量、连板判定、短期浮筹（用快速版，短超时不重试）
    kline_df = get_kline_fast(code, days=max(SHORT_CHIP_DAYS, CONNECT_BOARD_DAYS + 2), timeout=1.8)
    if kline_df is None or len(kline_df) < 5:
        return None

    # 量能：当日量 / 近5日均量（不含今日，避免自稀释）
    vols = kline_df['volume'].astype(float)
    today_vol = float(vols.iloc[-1])
    ma5_vol = float(vols.iloc[-6:-1].mean()) if len(vols) >= 6 else float(vols.iloc[:-1].mean())
    vol_ratio5 = round(today_vol / ma5_vol, 2) if ma5_vol > 0 else 0.0

    # 旧量比（与昨日比），保留作参考
    yest_vol = float(vols.iloc[-2]) if len(vols) >= 2 else 0
    vol_ratio_yest = round(today_vol / yest_vol, 2) if yest_vol > 0 else 0.0

    # 连板身位
    boards = calc_consecutive_boards(kline_df, code)

    # 短期浮筹
    chip = calc_short_chip(kline_df, stock['now'])
    if chip is None:
        return None
    profit_f, trapped_f, peak = chip

    industry_info = get_stock_industry(code)

    # 实际换手率 = 接口换手率 / (1 - 前十流通占比)。
    # 用腾讯 data[38] 反算，避免 data[6] 成交量单位在 688 板块不一致的 bug。
    # 只查缓存（不阻塞扫描），未命中的票由后台 FreeholdFiller 异步补全。
    freehold_ratio = get_freehold_ratio_cached(code)
    real_turnover = calc_real_turnover_from_api(stock['turnover'], freehold_ratio)

    zdf = stock['zdf']
    return {
        '代码': code + ('.SH' if code.startswith('6') else '.SZ'),
        '名称': stock['name'],
        '市场板块': stock['sector'],
        '收盘价': stock['now'],
        '涨跌幅(%)': round(zdf, 2),
        '量比(5日均量)': vol_ratio5,
        '量比(昨日)': vol_ratio_yest,
        '换手率(%)': stock['turnover'],
        '换手(实)(%)': real_turnover,
        '成交额': stock['amount'],
        '流通市值': stock.get('circ_market_cap', 0.0),
        '委托比(%)': stock.get('weibi', 0.0),
        '连板数': boards,
        '是否涨停': '是' if is_limit_up(zdf, code) else '否',
        '套牢盘比例(%)': round(trapped_f * 100, 2),
        '获利盘比例(%)': round(profit_f * 100, 2),
        '筹码峰价位': round(peak, 2),
        '距筹码峰(%)': round((stock['now'] - peak) / peak * 100, 2),
        '同花顺行业': industry_info['同花顺行业'],
        '最相关概念': industry_info['最相关概念'],
        '概念列表': industry_info['概念列表'],
        '_code': code,
        '_zdf': zdf,
        '_boards': boards,
        '_vol5': vol_ratio5,
        '_profit': profit_f,
    }


def fetch_quotes_batch(codes, max_workers=MAX_WORKERS, progress_cb=None, stop_cb=None):
    """并发拉全市场行情（只调 get_stock_raw，不拉 K 线）。
    返回 list of (code, stock_dict_or_None)。progress_cb(done,total,hit) 可选。
    """
    out = []
    total = len(codes)
    hit = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(get_stock_raw, c): c for c in codes}
        for idx, fut in enumerate(as_completed(futures), 1):
            if stop_cb and stop_cb():
                for f in futures:
                    f.cancel()
                break
            c = futures[fut]
            try:
                s = fut.result()
            except Exception:
                s = None
            if s:
                out.append((c, s))
                hit += 1
            if progress_cb and (idx % 200 == 0 or idx == total):
                progress_cb(idx, total, hit)
    return out


def compute_sentiment_from_quotes(quotes, max_workers=MAX_WORKERS):
    """用已拉好的行情 list 算情绪指标，避免重复请求。
    quotes: list of (code, stock_dict)。最高连板只对涨停票抽样拉 K 线。
    """
    zt_codes, dt_codes = [], []
    up_cnt, down_cnt, total = 0, 0, 0
    board_dist = Counter()
    for c, s in quotes:
        if s is None:
            continue
        total += 1
        zdf = s['zdf']
        if zdf > 0:
            up_cnt += 1
        elif zdf < 0:
            down_cnt += 1
        if is_limit_up(zdf, c):
            zt_codes.append((c, s['name'], zdf))
        elif zdf <= -9.6:
            dt_codes.append((c, s['name'], zdf))

    # 最高连板高度：只对涨停票拉 K 线判连板（抽样限流，用快速 K 线）
    max_board = 0
    sample = zt_codes[:80]
    if sample:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(lambda c: calc_consecutive_boards(get_kline_fast(c, days=CONNECT_BOARD_DAYS + 2, timeout=1.8), c), c): c
                    for c, _, _ in sample}
            for fut in as_completed(futs):
                try:
                    b = fut.result()
                except Exception:
                    b = 0
                if b > max_board:
                    max_board = b
                board_dist[b] += 1

    return {
        'total': total,
        'up': up_cnt,
        'down': down_cnt,
        'zt_count': len(zt_codes),
        'dt_count': len(dt_codes),
        'max_board': max_board,
        'board_dist': dict(board_dist),
        'zt_sample': zt_codes[:30],
    }


def sentiment_level(sent):
    """红黄绿灯：0=空仓(红), 1=谨慎(黄), 2=可操作(绿)。"""
    if sent['zt_count'] <= SENTIMENT_ZT_BAD or sent['dt_count'] >= SENTIMENT_DT_BAD:
        return 0
    if sent['zt_count'] >= SENTIMENT_ZT_OK and sent['max_board'] >= SENTIMENT_HIGH_BOARD:
        return 2
    return 1


def classify_and_score(rows):
    """按板块分组，做龙头/后排分类 + 综合评分。原地给每个 dict 加字段。"""
    # 按概念列表里第一个概念做板块分组（兜底用行业）
    groups = defaultdict(list)
    for r in rows:
        concepts = r.get('概念列表', [])
        if isinstance(concepts, str):
            concepts = [c.strip() for c in concepts.split(';') if c.strip()]
        key = concepts[0] if concepts else r.get('同花顺行业', '未分类')
        r['_block'] = key
        groups[key].append(r)

    # 板块内按涨幅排名
    for key, lst in groups.items():
        lst.sort(key=lambda x: x['_zdf'], reverse=True)
        for rank, r in enumerate(lst, 1):
            r['_block_rank'] = rank

    # 分类 + 评分
    for r in rows:
        zdf = r['_zdf']
        boards = r['_boards']
        is_zt = is_limit_up(zdf, r['_code'])
        rank = r['_block_rank']

        # 分类
        if is_zt and rank == 1 and boards >= 1:
            cat = '龙头候选'
        elif (not is_zt) and 2 <= rank <= 4 and r['_vol5'] >= VOL_MA5_MIN:
            cat = '后排接力'
        elif is_zt and boards >= 2:
            cat = '连板'
        else:
            cat = '候选'
        r['_category'] = cat

        # 评分（归一化到 0~100）
        s_profit = min(1.0, r['_profit'])                     # 0~1
        s_vol = min(2.0, r['_vol5']) / 2.0                    # 量能上限 2 倍
        s_zdf = min(1.0, max(0.0, zdf) / 10.0)                # 涨幅上限 10%
        s_board = min(1.0, boards / 3.0)                      # 连板上限 3 板
        s_rank = 1.0 if rank == 1 else (0.6 if rank <= 3 else 0.3)

        score = (W_PROFIT * s_profit + W_VOL * s_vol + W_ZDF * s_zdf
                 + W_BOARD * s_board + W_BLOCK_RANK * s_rank)
        r['_score'] = round(score, 1)

    return rows


# ---------------- 后台 F4 填充线程 ----------------
class FreeholdFiller(QObject):
    """扫描结束后台串行拉 F4（前十流通占比），拉到一只发信号，主窗口刷新对应行。
    不阻塞扫描主流程；命中缓存的票在主流程已填，这里只处理未命中的。"""
    freehold_ready = pyqtSignal(str, object)   # code, ratio
    finished = pyqtSignal(int)                  # 已处理数

    def __init__(self, codes_to_fetch):
        super().__init__()
        self.codes = codes_to_fetch        # 需要（且未缓存）拉 F4 的 code 列表
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        from concurrent.futures import ThreadPoolExecutor, as_completed
        n = 0
        with ThreadPoolExecutor(max_workers=3) as ex:
            futures = {ex.submit(self._fetch_one, c): c for c in self.codes}
            for fut in as_completed(futures):
                if self._stop:
                    for f in futures:
                        f.cancel()
                    break
                try:
                    code, ratio = fut.result()
                except Exception:
                    code = futures[fut]
                    ratio = None
                self.freehold_ready.emit(code, ratio)
                n += 1
        self.finished.emit(n)

    def _fetch_one(self, code):
        try:
            ratio = get_freehold_ratio(code)
        except Exception:
            ratio = None
        return code, ratio


# ---------------- 后台扫描线程 ----------------
class LeaderScanWorker(QObject):
    progress = pyqtSignal(int, int, int)        # 已扫描, 总数, 命中
    phase_progress = pyqtSignal(str, int, int)  # 阶段名, 已处理, 总数
    result_ready = pyqtSignal(list)
    sentiment_ready = pyqtSignal(dict)
    freehold_ready = pyqtSignal(str, object)    # code, ratio( float 或 None) —— 后台F4拉到一只就发
    finished = pyqtSignal(int, int, float, int)  # 总数, 命中, 耗时, K线失败数
    error = pyqtSignal(str)

    def __init__(self, codes, params, max_workers=MAX_WORKERS, do_sentiment=True):
        super().__init__()
        self.codes = codes
        self.params = params
        self.max_workers = max_workers
        self.do_sentiment = do_sentiment
        self._stop = False
        self._batch = []
        self._batch_size = 50

    def stop(self):
        self._stop = True

    def run(self):
        try:
            start = time.time()
            total = len(self.codes)

            # ---------- 第一段：拉全市场行情（一次请求两用：情绪闸门 + 涨幅粗筛）----------
            def _prog1(done, t, hit):
                self.phase_progress.emit("拉行情", done, t)
                self.progress.emit(done, t, hit)

            quotes = fetch_quotes_batch(
                self.codes, max_workers=self.max_workers,
                progress_cb=_prog1, stop_cb=lambda: self._stop,
            )
            if self._stop:
                self.finished.emit(total, 0, time.time() - start, 0)
                return

            # 情绪闸门（复用同一批行情，不再二次请求）
            if self.do_sentiment:
                sent = compute_sentiment_from_quotes(quotes, self.max_workers)
                self.sentiment_ready.emit(sent)

            # 涨幅粗筛：只对涨幅 ≥ zdf_min 的候选拉 K 线（砍掉 80%+ 的 K 线请求）
            zdf_min = self.params['zdf_min']
            candidates = [(c, s) for c, s in quotes
                          if s and s['zdf'] >= zdf_min]

            # ---------- 第二段：对候选拉 K 线算筹码/连板/量能，打分过滤 ----------
            # K 线用快速版（短超时 + 轻重试），可安全提高并发
            hit = 0
            kline_fail = 0
            cand_total = len(candidates)
            kline_workers = max(self.max_workers, 60)

            def _compute(args):
                c, s = args
                try:
                    return compute_leader_features(c, stock=s)
                except Exception:
                    return None

            with ThreadPoolExecutor(max_workers=kline_workers) as ex:
                futures = {ex.submit(_compute, args): args for args in candidates}
                for idx, fut in enumerate(as_completed(futures), 1):
                    if self._stop:
                        for f in futures:
                            f.cancel()
                        break
                    try:
                        r = fut.result()
                    except Exception:
                        r = None
                    if r:
                        if (r['_vol5'] >= self.params['vol5_min']
                                and r['_profit'] >= self.params['profit_min']):
                            self._batch.append(r)
                            hit += 1
                            if len(self._batch) >= self._batch_size:
                                self.result_ready.emit(self._batch)
                                self._batch = []
                    else:
                        # 候选涨幅达标但 K 线拉取/解析失败 → 计入丢数据
                        kline_fail += 1
                    if idx % 100 == 0 or idx == cand_total:
                        self.phase_progress.emit("算K线", idx, cand_total)
                        self.progress.emit(idx, cand_total, hit)
            if self._batch:
                self.result_ready.emit(self._batch)
                self._batch = []
            elapsed = time.time() - start
            self.finished.emit(total, hit, elapsed, kline_fail)
        except Exception as e:
            self.error.emit(str(e))


# ---------------- 表格列 ----------------
COLUMNS = [
    ('分类',             '分类',       False, False),
    ('综合评分',         '评分',       True,  False),
    ('代码',             '代码',       False, False),
    ('名称',             '名称',       False, False),
    ('市场板块',         '市场板块',   False, False),
    ('收盘价',           '收盘价',     True,  False),
    ('涨跌幅(%)',        '涨跌幅',     True,  True),
    ('量比(5日均量)',     '量能5日',    True,  False),
    ('量比(昨日)',        '量比昨',     True,  False),
    ('连板数',           '连板',       True,  False),
    ('是否涨停',         '涨停',       False, False),
    ('换手率(%)',        '换手',       True,  True),
    ('换手(实)(%)',      '换手(实)',   True,  True),
    ('成交额',           '成交额',     True,  True),
    ('流通市值',         '流通市值',   True,  True),
    ('委托比(%)',        '委比',       True,  False),
    ('套牢盘比例(%)',    '套牢盘',     True,  False),
    ('获利盘比例(%)',    '获利盘',     True,  False),
    ('距筹码峰(%)',      '距筹码峰',   True,  True),
    ('同花顺行业',       '行业',       False, False),
    ('最相关概念',       '概念',       False, False),
]


class SortableTableWidgetItem(QTableWidgetItem):
    def __lt__(self, other):
        try:
            return float(self.data(Qt.UserRole)) < float(other.data(Qt.UserRole))
        except (TypeError, ValueError):
            return self.text() < other.text()


# ---------------- K 线 widget（复用 chip 风格） ----------------
class KLineWidget(QWidget):
    """自绘 K 线图（蜡烛 + 成交量）+ 筹码分布 + 可选技术指标叠加。"""

    def __init__(self, kline_df, chip_info=None, show_flags=None, parent=None):
        super().__init__(parent)
        self.df = kline_df
        self.chip_info = chip_info
        self.show_flags = show_flags or {}
        self.setMinimumHeight(220)
        self.setStyleSheet("background:#1e2433;")

    def _y_of(self, val, pmin, pmax, price_h):
        return int((pmax - val) / (pmax - pmin) * price_h)

    def _draw_line(self, p, series, pmin, pmax, price_h, kline_w, n, cw, color, width=1.5):
        if series is None:
            return
        p.setPen(QPen(color, width))
        prev_x = prev_y = None
        step = kline_w / n
        for i in range(n):
            val = series.iloc[i]
            if val != val:
                prev_x = prev_y = None
                continue
            x = int(i * step + cw / 2)
            y = self._y_of(float(val), pmin, pmax, price_h)
            if prev_y is not None:
                p.drawLine(prev_x, prev_y, x, y)
            prev_x, prev_y = x, y

    def _draw_sub_line(self, p, series, sub_pmin, sub_pmax, sub_h, sub_top,
                        kline_w, n, cw, color, width=1.5):
        if series is None:
            return
        p.setPen(QPen(color, width))
        prev_x = prev_y = None
        step = kline_w / n
        for i in range(n):
            val = series.iloc[i]
            if val != val:
                prev_x = prev_y = None
                continue
            x = int(i * step + cw / 2)
            y = int((sub_pmax - float(val)) / (sub_pmax - sub_pmin) * sub_h) + sub_top
            if prev_y is not None:
                p.drawLine(prev_x, prev_y, x, y)
            prev_x, prev_y = x, y

    def paintEvent(self, event):
        if self.df is None or len(self.df) == 0:
            return
        import numpy as np
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w = self.width(); h = self.height()
        kline_w = int(w * 0.65) if self.chip_info else w
        chip_x = kline_w + 10
        chip_w = w - kline_w - 20

        df = self.df
        n = len(df)
        sub_indicators = [k for k in ('macd', 'kdj', 'rsi') if self.show_flags.get(k, False)]
        n_sub = len(sub_indicators)

        highs = df['high'].astype(float); lows = df['low'].astype(float)
        pmin, pmax = float(lows.min()), float(highs.max())
        if self.show_flags.get('boll', False) and n >= 20:
            close_b = df['close'].astype(float)
            ma20 = close_b.rolling(window=20).mean()
            std20 = close_b.rolling(window=20).std()
            boll_up = ma20 + 2 * std20
            boll_lo = ma20 - 2 * std20
            bmin = float(np.nanmin(boll_lo.values))
            bmax = float(np.nanmax(boll_up.values))
            pmin = min(pmin, bmin); pmax = max(pmax, bmax)
        if pmax <= pmin: pmax = pmin + 1
        pad = (pmax - pmin) * 0.05
        pmin -= pad; pmax += pad

        if n_sub == 0:
            price_ratio, vol_ratio = 0.70, 0.30
        else:
            price_ratio = max(0.35, 0.55 - n_sub * 0.05)
            vol_ratio = 0.15
        price_h = int(h * price_ratio)
        vol_h = int(h * vol_ratio)
        vol_top = price_h + 8
        sub_regions = []
        cur_top = vol_top + vol_h + 8
        sub_ratio = (1.0 - price_ratio - vol_ratio) / n_sub if n_sub else 0
        for name in sub_indicators:
            sh = int(h * sub_ratio)
            sub_regions.append((name, cur_top, sh))
            cur_top += sh + 8

        p.setPen(QPen(QColor('#2a3142'), 1, Qt.DashLine))
        for i in range(5):
            y = int(price_h * i / 4)
            p.drawLine(0, y, kline_w, y)
            price = pmax - (pmax - pmin) * i / 4
            p.setPen(QColor('#7a8499'))
            p.drawText(2, y + 12, f"{price:.2f}")
            p.setPen(QPen(QColor('#2a3142'), 1, Qt.DashLine))

        cw = max(3, kline_w / n - 1)
        vols = df['volume'].astype(float)
        vmax = float(vols.max()) if len(vols) else 1
        if vmax <= 0: vmax = 1

        for i, (_, row) in enumerate(df.iterrows()):
            o = float(row['open']); c = float(row['close'])
            hi = float(row['high']); lo = float(row['low'])
            x = int(i * kline_w / n + cw / 2)
            up = c >= o
            color = QColor('#e23b3b') if up else QColor('#1faa52')
            p.setPen(QPen(color, 1))
            y_hi = self._y_of(hi, pmin, pmax, price_h)
            y_lo = self._y_of(lo, pmin, pmax, price_h)
            p.drawLine(x, y_hi, x, y_lo)
            y_o = self._y_of(o, pmin, pmax, price_h)
            y_c = self._y_of(c, pmin, pmax, price_h)
            body_h = max(1, abs(y_o - y_c))
            p.setBrush(QBrush(color))
            p.drawRect(QRectF(x - cw / 2, min(y_o, y_c), cw, body_h))
            v = float(row['volume'])
            vh = int(v / vmax * vol_h)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(color))
            p.drawRect(QRectF(x - cw / 2, vol_top + vol_h - vh, cw, vh))

        close = df['close'].astype(float)
        # 均线
        if self.show_flags.get('ma', True):
            ma_defs = [(5, QColor('#ffcc00')), (10, QColor('#ff6a00')),
                       (20, QColor('#a974ff')), (60, QColor('#36c5f0'))]
            for period, col in ma_defs:
                if n < period: continue
                ma = close.rolling(window=period).mean()
                self._draw_line(p, ma, pmin, pmax, price_h, kline_w, n, cw, col)
            # 图例
            p.setPen(QColor('#cfd6e4'))
            legend_x = kline_w - 280
            for period, col in ma_defs:
                if n < period: continue
                p.setPen(col)
                p.drawLine(legend_x, 14, legend_x + 16, 14)
                p.setPen(QColor('#cfd6e4'))
                p.drawText(legend_x + 20, 18, f"MA{period}")
                legend_x += 70

        # 布林带
        if self.show_flags.get('boll', False) and n >= 20:
            ma20 = close.rolling(window=20).mean()
            std20 = close.rolling(window=20).std()
            self._draw_line(p, ma20 + 2 * std20, pmin, pmax, price_h, kline_w, n, cw, QColor('#8a8f9c'), 1)
            self._draw_line(p, ma20 - 2 * std20, pmin, pmax, price_h, kline_w, n, cw, QColor('#8a8f9c'), 1)
            self._draw_line(p, ma20, pmin, pmax, price_h, kline_w, n, cw, QColor('#ffcc00'), 1)
            p.setPen(QColor('#8a8f9c'))
            p.drawLine(kline_w - 90, 14, kline_w - 74, 14)
            p.setPen(QColor('#cfd6e4'))
            p.drawText(kline_w - 70, 18, "BOLL")

        p.setPen(QColor('#7a8499'))
        p.drawText(2, vol_top + 12, "成交量")

        # 副图指标
        if 'macd' in [r[0] for r in sub_regions]:
            region = next(r for r in sub_regions if r[0] == 'macd')
            _, sub_top, sub_h = region
            ema12 = close.ewm(span=12, adjust=False).mean()
            ema26 = close.ewm(span=26, adjust=False).mean()
            dif = ema12 - ema26
            dea = dif.ewm(span=9, adjust=False).mean()
            macd = (dif - dea) * 2
            allvals = list(dif.dropna()) + list(dea.dropna()) + list(macd.dropna())
            if allvals:
                sub_pmin, sub_pmax = min(allvals), max(allvals)
                if sub_pmax <= sub_pmin: sub_pmax = sub_pmin + 1
                pad2 = (sub_pmax - sub_pmin) * 0.1
                sub_pmin -= pad2; sub_pmax += pad2
                zero_y = int((sub_pmax - 0) / (sub_pmax - sub_pmin) * sub_h) + sub_top
                p.setPen(QPen(QColor('#2a3142'), 1, Qt.DashLine))
                p.drawLine(0, zero_y, kline_w, zero_y)
                p.setPen(Qt.NoPen)
                for i in range(n):
                    val = macd.iloc[i]
                    if val != val: continue
                    x = int(i * kline_w / n + cw / 2)
                    y = int((sub_pmax - float(val)) / (sub_pmax - sub_pmin) * sub_h) + sub_top
                    col = QColor('#e23b3b') if float(val) >= 0 else QColor('#1faa52')
                    p.setBrush(QBrush(col))
                    p.drawRect(QRectF(x - cw / 2, min(y, zero_y), max(1, cw), abs(y - zero_y)))
                self._draw_sub_line(p, dif, sub_pmin, sub_pmax, sub_h, sub_top, kline_w, n, cw, QColor('#ffcc00'))
                self._draw_sub_line(p, dea, sub_pmin, sub_pmax, sub_h, sub_top, kline_w, n, cw, QColor('#ff6a00'))
                p.setPen(QColor('#7a8499'))
                p.drawText(2, sub_top + 12, f"MACD  DIF={float(dif.iloc[-1]):.3f}  DEA={float(dea.iloc[-1]):.3f}")

        if 'kdj' in [r[0] for r in sub_regions]:
            region = next(r for r in sub_regions if r[0] == 'kdj')
            _, sub_top, sub_h = region
            low_n = df['low'].astype(float).rolling(window=9, min_periods=1).min()
            high_n = df['high'].astype(float).rolling(window=9, min_periods=1).max()
            rsv = (close - low_n) / (high_n - low_n) * 100
            rsv = rsv.fillna(50)
            k = rsv.ewm(com=2, adjust=False).mean()
            d = k.ewm(com=2, adjust=False).mean()
            j = 3 * k - 2 * d
            sub_pmin, sub_pmax = float(j.min()), float(j.max())
            if sub_pmax <= sub_pmin: sub_pmax = sub_pmin + 1
            pad2 = (sub_pmax - sub_pmin) * 0.1
            sub_pmin -= pad2; sub_pmax += pad2
            for ref in (20, 50, 80):
                ry = int((sub_pmax - ref) / (sub_pmax - sub_pmin) * sub_h) + sub_top
                p.setPen(QPen(QColor('#2a3142'), 1, Qt.DashLine))
                p.drawLine(0, ry, kline_w, ry)
            self._draw_sub_line(p, k, sub_pmin, sub_pmax, sub_h, sub_top, kline_w, n, cw, QColor('#ffcc00'))
            self._draw_sub_line(p, d, sub_pmin, sub_pmax, sub_h, sub_top, kline_w, n, cw, QColor('#ff6a00'))
            self._draw_sub_line(p, j, sub_pmin, sub_pmax, sub_h, sub_top, kline_w, n, cw, QColor('#a974ff'))
            p.setPen(QColor('#7a8499'))
            p.drawText(2, sub_top + 12, f"KDJ  K={float(k.iloc[-1]):.2f}  D={float(d.iloc[-1]):.2f}  J={float(j.iloc[-1]):.2f}")

        if 'rsi' in [r[0] for r in sub_regions]:
            region = next(r for r in sub_regions if r[0] == 'rsi')
            _, sub_top, sub_h = region
            delta = close.diff()
            gain = delta.clip(lower=0)
            loss = -delta.clip(upper=0)
            avg_gain = gain.ewm(alpha=1/6, adjust=False).mean()
            avg_loss = loss.ewm(alpha=1/6, adjust=False).mean()
            rs = avg_gain / avg_loss.replace(0, np.nan)
            rsi = 100 - 100 / (1 + rs)
            rsi = rsi.fillna(50)
            sub_pmin, sub_pmax = 0, 100
            for ref in (20, 50, 80):
                ry = int((sub_pmax - ref) / (sub_pmax - sub_pmin) * sub_h) + sub_top
                p.setPen(QPen(QColor('#2a3142'), 1, Qt.DashLine))
                p.drawLine(0, ry, kline_w, ry)
            self._draw_sub_line(p, rsi, sub_pmin, sub_pmax, sub_h, sub_top, kline_w, n, cw, QColor('#36c5f0'))
            p.setPen(QColor('#7a8499'))
            p.drawText(2, sub_top + 12, f"RSI(6)  {float(rsi.iloc[-1]):.2f}")

        if self.chip_info and chip_w > 30:
            profit, trapped, peak, centers, chip, total = self.chip_info
            p.setPen(QColor('#cfd6e4'))
            p.drawText(chip_x, 16, "筹码分布(20日)")
            cmax = float(chip.max()) if len(chip) else 1
            if cmax <= 0: cmax = 1
            bar_h = max(1, price_h / len(chip))
            p.setPen(Qt.NoPen)
            for i, cv in enumerate(chip):
                if cv <= 0: continue
                y = int(price_h - (i + 1) * price_h / len(chip))
                bw = int(cv / cmax * (chip_w - 10))
                price_at = float(centers[i])
                col = QColor('#1faa52') if price_at > float(self.df['close'].iloc[-1]) else QColor('#e23b3b')
                p.setBrush(QBrush(col))
                p.drawRect(QRectF(chip_x, y, bw, max(1, bar_h - 1)))
            p.setPen(QPen(QColor('#ffcc00'), 1, Qt.DashLine))
            peak_y = self._y_of(peak, pmin, pmax, price_h)
            p.drawLine(chip_x - 5, peak_y, chip_x + chip_w, peak_y)
            p.setPen(QColor('#ffcc00'))
            p.drawText(chip_x, peak_y - 4, f"峰 {peak:.2f}")
            cur = float(self.df['close'].iloc[-1])
            cur_y = self._y_of(cur, pmin, pmax, price_h)
            p.setPen(QPen(QColor('#ffffff'), 1, Qt.DashLine))
            p.drawLine(0, cur_y, kline_w, cur_y)
            p.setPen(QColor('#cfd6e4'))
            p.drawText(chip_x, price_h + 20, f"获利 {profit*100:.1f}%")
            p.drawText(chip_x, price_h + 38, f"套牢 {trapped*100:.1f}%")
        p.end()


# ---------------- 主窗口 ----------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("超短线龙头/后排接力扫描 - 桌面版")
        self.resize(1320, 800)

        self.all_rows = []
        self.worker = None
        self.thread = None
        self.scanning = False
        self._pending_row = None
        self._kline_timer = QTimer(self)
        self._kline_timer.setSingleShot(True)
        self._kline_timer.timeout.connect(self._flush_pending_kline)
        self._sector_box_ready = False
        self._phase = "拉行情"
        # F4 后台填充线程
        self.filler = None
        self.filler_thread = None

        self._build_ui()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # ---------- 情绪灯 ----------
        emo = QGroupBox("情绪闸门（大盘择时）")
        el = QHBoxLayout(emo)
        el.setContentsMargins(10, 8, 10, 8)
        self.lbl_sent = QLabel("未扫描")
        self.lbl_sent.setStyleSheet("font-size:14px; padding:4px;")
        self.lbl_sent_light = QLabel("⚪")
        self.lbl_sent_light.setStyleSheet("font-size:24px;")
        el.addWidget(self.lbl_sent_light)
        el.addWidget(self.lbl_sent, 1)
        layout.addWidget(emo)

        # ---------- 参数区 ----------
        param_group = QGroupBox("筛选参数")
        pl = QHBoxLayout(param_group)
        pl.setContentsMargins(10, 10, 10, 10)
        pl.setSpacing(10)

        def make_double(label, val, step=0.05, lo=0.0, hi=1.0, suffix=""):
            box = QDoubleSpinBox(); box.setRange(lo, hi); box.setSingleStep(step)
            box.setDecimals(2); box.setValue(val); box.setSuffix(suffix); box.setFixedHeight(28)
            pl.addWidget(QLabel(label)); pl.addWidget(box); return box

        def make_int(label, val, lo=10, hi=300, step=5):
            box = QSpinBox(); box.setRange(lo, hi); box.setSingleStep(step)
            box.setValue(val); box.setFixedHeight(28)
            pl.addWidget(QLabel(label)); pl.addWidget(box); return box

        self.spin_profit = make_double("获利盘下限:", PROFIT_RATIO_MIN, 0.05, 0.0, 1.0, "")
        self.spin_vol = make_double("量能下限(5日均量倍数):", VOL_MA5_MIN, 0.1, 0.0, 10.0, "")
        self.spin_zdf = make_double("涨幅下限(%):", ZDF_MIN, 0.5, -5.0, 20.0, "")
        self.spin_days = make_int("浮筹天数:", SHORT_CHIP_DAYS, 5, 60, 5)
        pl.addStretch()
        layout.addWidget(param_group)

        # ---------- 工具栏 ----------
        bar = QHBoxLayout()
        self.btn_scan = QPushButton("开始扫描")
        self.btn_scan.setFixedHeight(30)
        self.btn_scan.setStyleSheet(
            "QPushButton{background:#2d8cf0;color:white;font-weight:bold;border-radius:4px;padding:0 18px}"
            "QPushButton:hover{background:#5cadff}"
            "QPushButton:disabled{background:#a0c4f0}")
        self.btn_scan.clicked.connect(self.toggle_scan)

        self.btn_export = QPushButton("导出 CSV")
        self.btn_export.setFixedHeight(30)
        self.btn_export.clicked.connect(self.export_csv)

        self.btn_clear = QPushButton("清空")
        self.btn_clear.setFixedHeight(30)
        self.btn_clear.clicked.connect(self.clear_table)

        self.cat_filter = QComboBox(); self.cat_filter.setFixedHeight(30)
        self.cat_filter.addItem("全部分类", "")
        for c in ["龙头候选", "后排接力", "连板", "候选"]:
            self.cat_filter.addItem(c, c)
        self.cat_filter.currentIndexChanged.connect(self.apply_filter)

        self.filter_box = QLineEdit()
        self.filter_box.setPlaceholderText("输入 代码 / 名称 / 行业 / 概念 实时筛选…")
        self.filter_box.setFixedHeight(30)
        self.filter_box.textChanged.connect(self.apply_filter)

        self.sector_filter = QComboBox(); self.sector_filter.setFixedHeight(30)
        self.sector_filter.addItem("全部板块", "")
        self.sector_filter.currentIndexChanged.connect(self.apply_filter)

        self.count_label = QLabel("共 0 只")
        self.count_label.setStyleSheet("color:#666")

        bar.addWidget(self.btn_scan)
        bar.addWidget(self.btn_export)
        bar.addWidget(self.btn_clear)
        bar.addWidget(QLabel("分类:")); bar.addWidget(self.cat_filter, 1)
        bar.addWidget(QLabel("板块:")); bar.addWidget(self.sector_filter, 2)
        bar.addWidget(self.filter_box, 3)
        bar.addWidget(self.count_label)
        layout.addLayout(bar)

        # ---------- 表格 ----------
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels([h for _, h, _, _ in COLUMNS])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionsMovable(True)
        self.table.horizontalHeader().setStretchLastSection(True)
        self._sort_col = None; self._sort_order = 0
        self._orig_headers = [h for _, h, _, _ in COLUMNS]
        self.table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)

        for i, (field, header, is_num, _) in enumerate(COLUMNS):
            if field in ('代码', '名称'): self.table.setColumnWidth(i, 95)
            elif field == '最相关概念': self.table.setColumnWidth(i, 180)
            elif field == '同花顺行业': self.table.setColumnWidth(i, 110)
            elif field == '分类': self.table.setColumnWidth(i, 90)
            elif field == '流通市值': self.table.setColumnWidth(i, 95)
            elif is_num: self.table.setColumnWidth(i, 85)
            else: self.table.setColumnWidth(i, 85)
        self.table.itemDoubleClicked.connect(self.show_detail)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)

        # ---------- K 线区 ----------
        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.addWidget(self.table)
        self.kline_container = QWidget()
        self.kline_container.setMinimumHeight(200)
        kl = QVBoxLayout(self.kline_container); kl.setContentsMargins(2, 2, 2, 2)
        top_bar = QHBoxLayout(); top_bar.setContentsMargins(0, 0, 0, 0)
        self.kline_info_label = QLabel("（点击表格行查看 K 线）")
        self.kline_info_label.setStyleSheet("color:#cfd6e4; padding:2px;")
        top_bar.addWidget(self.kline_info_label, 1)
        top_bar.addWidget(QLabel("指标:"))
        self.cb_ind_ma = QCheckBox("均线")
        self.cb_ind_ma.setChecked(True)
        self.cb_ind_boll = QCheckBox("BOLL")
        self.cb_ind_macd = QCheckBox("MACD")
        self.cb_ind_kdj = QCheckBox("KDJ")
        self.cb_ind_rsi = QCheckBox("RSI")
        for cb in (self.cb_ind_ma, self.cb_ind_boll, self.cb_ind_macd,
                    self.cb_ind_kdj, self.cb_ind_rsi):
            cb.setStyleSheet("QCheckBox{color:#cfd6e4; padding:0 4px}")
            cb.setFixedHeight(24)
            cb.toggled.connect(self._on_indicator_toggled)
            top_bar.addWidget(cb)
        self.btn_close_kline = QPushButton("关闭 K 线")
        self.btn_close_kline.setFixedHeight(24)
        self.btn_close_kline.setStyleSheet(
            "QPushButton{background:#3a4252;color:#cfd6e4;border-radius:3px;padding:0 10px}"
            "QPushButton:hover{background:#4a5266}")
        self.btn_close_kline.clicked.connect(self.hide_kline)
        top_bar.addWidget(self.btn_close_kline)
        kl.addLayout(top_bar)
        self.kline_widget = None
        self.splitter.addWidget(self.kline_container)
        self.splitter.setStretchFactor(0, 3); self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes([520, 300])
        self.kline_container.setVisible(False)
        layout.addWidget(self.splitter, 1)

        # ---------- 进度条 + 状态栏 ----------
        self.progress = QProgressBar(); self.progress.setFixedHeight(18)
        self.progress.setTextVisible(True); self.progress.setValue(0)
        layout.addWidget(self.progress)
        self.status = QStatusBar(); self.setStatusBar(self.status)
        self.status.showMessage("就绪。设置参数后点击「开始扫描」")

    # ---------- 扫描 ----------
    def toggle_scan(self):
        if self.scanning: self.stop_scan()
        else: self.start_scan()

    def _collect_params(self):
        return {
            'profit_min': float(self.spin_profit.value()),
            'vol5_min': float(self.spin_vol.value()),
            'zdf_min': float(self.spin_zdf.value()),
            'days': int(self.spin_days.value()),
        }

    def start_scan(self):
        self.clear_table()
        self.scanning = True
        self.btn_scan.setText("停止扫描")
        self.btn_export.setEnabled(False); self.btn_clear.setEnabled(False)
        for w in (self.spin_profit, self.spin_vol, self.spin_zdf, self.spin_days):
            w.setEnabled(False)
        self.progress.setValue(0)
        self.status.showMessage("正在扫描… 先算情绪闸门，再扫个股")
        # 全局短期浮筹天数
        global SHORT_CHIP_DAYS
        SHORT_CHIP_DAYS = int(self.spin_days.value())

        codes = generate_stock_codes()
        params = self._collect_params()
        self.thread = QThread()
        self.worker = LeaderScanWorker(codes, params, max_workers=MAX_WORKERS, do_sentiment=True)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.on_progress)
        self.worker.phase_progress.connect(self.on_phase_progress)
        self.worker.result_ready.connect(self.on_result_batch)
        self.worker.sentiment_ready.connect(self.on_sentiment)
        self.worker.finished.connect(self.on_finished)
        self.worker.error.connect(self.on_error)
        self.worker.finished.connect(self.thread.quit)
        self.thread.finished.connect(self._cleanup_thread)
        self.thread.start()

    def stop_scan(self):
        if self.worker: self.worker.stop()
        self.status.showMessage("正在停止…")

    def _cleanup_thread(self):
        self.thread = None; self.worker = None

    def on_phase_progress(self, phase, done, total):
        self._phase = phase
        if phase == "拉行情":
            self.status.showMessage(f"[1/2] 拉行情  {done}/{total}")
        else:
            self.status.showMessage(f"[2/2] 算K线打分  {done}/{total}")

    def on_progress(self, done, total, hit):
        pct = int(done / total * 100) if total else 0
        self.progress.setValue(pct)
        self.progress.setFormat(f"{pct}%  {done}/{total}")
        phase = getattr(self, '_phase', '拉行情')
        tag = "[1/2]拉行情" if phase == "拉行情" else "[2/2]算K线"
        self.status.showMessage(f"{tag}  {done}/{total}  命中 {hit}")

    def on_sentiment(self, sent):
        """情绪闸门结果：红黄绿灯 + 文字。"""
        lv = sentiment_level(sent)
        light = {0: ("🔴", "建议空仓", "#e23b3b"),
                 1: ("🟡", "谨慎操作", "#d4882f"),
                 2: ("🟢", "可操作", "#1faa52")}[lv]
        icon, word, color = light
        self.lbl_sent_light.setText(icon)
        zt = sent['zt_count']; dt = sent['dt_count']; mb = sent['max_board']
        up = sent['up']; down = sent['down']; total = sent['total']
        txt = (f"【{word}】 涨停 {zt} 家  跌停 {dt} 家  最高连板 {mb} 板  "
               f"上涨 {up}  下跌 {down}  共 {total}  | "
               f"涨停≥{SENTIMENT_ZT_OK}且连板≥{SENTIMENT_HIGH_BOARD}=可操作，"
               f"涨停≤{SENTIMENT_ZT_BAD}或跌停≥{SENTIMENT_DT_BAD}=空仓")
        self.lbl_sent.setText(txt)
        self.lbl_sent.setStyleSheet(f"color:{color}; font-size:13px; padding:4px;")

    def on_result_batch(self, batch):
        # 还没分类，攒着，结束时统一分类（需要板块内排名，必须等全部到位）
        self.all_rows.extend(batch)
        self.count_label.setText(f"已采集 {len(self.all_rows)} 只")

    def on_finished(self, total, hit, elapsed, kline_fail=0):
        self.scanning = False
        self.btn_scan.setText("开始扫描")
        self.btn_export.setEnabled(True); self.btn_clear.setEnabled(True)
        for w in (self.spin_profit, self.spin_vol, self.spin_zdf, self.spin_days):
            w.setEnabled(True)
        self.progress.setValue(100)
        # 分类 + 评分 + 排序
        rows = classify_and_score(self.all_rows)
        rows.sort(key=lambda r: (r['_category'] != '龙头候选',
                                 r['_category'] != '连板',
                                 r['_category'] != '后排接力',
                                 -r['_score']))
        self.all_rows = rows
        self.table.setRowCount(0)
        self._append_rows(rows)
        self.populate_sector_filter()
        self.count_label.setText(f"共 {len(rows)} 只")
        fail_tag = f"  K线丢弃 {kline_fail} 只" if kline_fail else ""
        self.status.showMessage(
            f"扫描完成  总数 {total}  命中 {hit}  耗时 {elapsed:.1f}s{fail_tag}  正在后台补全换手(实)…", 8000)
        # 启动后台 F4 填充（只拉未缓存的 code）
        self._start_freehold_filler(rows)

    def _start_freehold_filler(self, rows):
        """扫描后启动后台线程串行拉 F4，拉到一只刷新表格对应行的换手(实)列。"""
        # 停掉上一次未完成的 filler
        self._stop_freehold_filler()
        # 筛出换手(实)还是 None 的票（即主流程没命中缓存的）
        codes = []
        for r in rows:
            if r.get('换手(实)(%)') is None:
                codes.append(r.get('_code') or r.get('代码', '').split('.')[0])
        if not codes:
            return
        self.filler_thread = QThread()
        self.filler = FreeholdFiller(codes)
        self.filler.moveToThread(self.filler_thread)
        self.filler_thread.started.connect(self.filler.run)
        self.filler.freehold_ready.connect(self._on_freehold_ready)
        self.filler.finished.connect(self._on_freehold_finished)
        self.filler.finished.connect(self.filler_thread.quit)
        self.filler_thread.start()

    def _stop_freehold_filler(self):
        if self.filler is not None:
            try:
                self.filler.stop()
            except Exception:
                pass
        if self.filler_thread is not None:
            try:
                self.filler_thread.quit(); self.filler_thread.wait(2000)
            except Exception:
                pass
        self.filler = None
        self.filler_thread = None

    def _on_freehold_ready(self, code, ratio):
        """后台拉到一只 F4，刷新表格对应行的换手(实)列。"""
        # 找到这行在 all_rows 里的记录，重算换手(实)
        target = None
        for r in self.all_rows:
            rc = r.get('_code') or r.get('代码', '').split('.')[0]
            if rc == code:
                target = r
                break
        if target is None:
            return
        # 用接口换手率 + F4 比例反算实际换手率（数学等价于 成交量/实际流通股本）
        api_turn = target.get('换手率(%)')
        real_turn = calc_real_turnover_from_api(api_turn, ratio)
        target['换手(实)(%)'] = real_turn
        # 刷新表格显示
        self._refresh_row_cell(target)

    def _on_freehold_finished(self, n):
        self.status.showMessage(f"换手(实)后台补全完成（{n} 只）", 5000)

    def _refresh_row_cell(self, target):
        """刷新某一行在表格里的换手(实)单元格。"""
        code = target.get('_code') or target.get('代码', '').split('.')[0]
        # 找列索引
        col = next((i for i, (f, *_) in enumerate(COLUMNS) if f == '换手(实)(%)'), None)
        if col is None:
            return
        # 找行索引：遍历表格找匹配 _code 的行
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if not item:
                continue
            rd = item.data(Qt.UserRole + 1)
            if not rd:
                continue
            rc = rd.get('_code') or rd.get('代码', '').split('.')[0]
            if rc == code:
                val = target.get('换手(实)(%)')
                text = '-' if (val is None or val != val) else f"{float(val):.2f}%"
                cell = self.table.item(row, col)
                if cell:
                    cell.setText(text)
                    cell.setData(Qt.UserRole, val if val is not None else 0)
                return

    def on_error(self, msg):
        self.scanning = False
        self.btn_scan.setText("开始扫描")
        self.btn_export.setEnabled(True); self.btn_clear.setEnabled(True)
        for w in (self.spin_profit, self.spin_vol, self.spin_zdf, self.spin_days):
            w.setEnabled(True)
        QMessageBox.critical(self, "扫描出错", msg)
        self.status.showMessage("扫描出错: " + msg)

    # ---------- 排序 ----------
    def _on_header_clicked(self, col):
        if self._sort_col == col:
            self._sort_order = 2 if self._sort_order == 1 else 1
        else:
            self._sort_col = col; self._sort_order = 2
        self._refresh_sort_marks()
        order = Qt.AscendingOrder if self._sort_order == 1 else Qt.DescendingOrder
        self.table.sortItems(col, order)

    def _refresh_sort_marks(self):
        for i, header in enumerate(self._orig_headers):
            mark = ''
            if i == self._sort_col and self._sort_order == 1: mark = ' ▲'
            elif i == self._sort_col and self._sort_order == 2: mark = ' ▼'
            self.table.horizontalHeaderItem(i).setText(header + mark)

    # ---------- 表格填充 ----------
    def _append_rows(self, rows):
        start_row = self.table.rowCount()
        self.table.setRowCount(start_row + len(rows))
        for r, item in enumerate(rows):
            self._fill_row(start_row + r, item)

    def _fill_row(self, row, item):
        for col, (field, header, is_num, need_fmt) in enumerate(COLUMNS):
            val = item.get(field, '')
            if need_fmt:
                text = self._format_value(field, val)
            else:
                text = '' if val is None else str(val)

            if field == '最相关概念':
                concepts = item.get('概念列表', []) or []
                if isinstance(concepts, str):
                    concepts = [c.strip() for c in concepts.split(';') if c.strip()]
                text = '、'.join(concepts[:4]) if concepts else '未分类'
                if len(concepts) > 4: text += f' 等{len(concepts)}个'

            cell = SortableTableWidgetItem(text)
            cell.setData(Qt.UserRole + 1, item)
            sort_val = val
            if is_num and isinstance(val, (int, float)): sort_val = float(val)
            cell.setData(Qt.UserRole, sort_val if sort_val != '' else 0)
            cell.setTextAlignment((Qt.AlignRight | Qt.AlignVCenter) if is_num
                                  else (Qt.AlignLeft | Qt.AlignVCenter))

            try:
                if field == '分类':
                    cat = item.get('_category', '')
                    color = {'龙头候选': '#e23b3b', '连板': '#ff6a00',
                             '后排接力': '#d4882f', '候选': '#aab'}.get(cat, '#aab')
                    cell.setForeground(QColor(color)); cell.setFont(QFont('', -1, QFont.Bold))
                elif field == '综合评分':
                    f = float(item.get('_score', 0))
                    cell.setForeground(QColor('#ffcc00')); cell.setFont(QFont('', -1, QFont.Bold))
                    cell.setText(f"{f:.1f}")
                    cell.setData(Qt.UserRole, f)
                elif field == '涨跌幅(%)':
                    f = float(val)
                    cell.setForeground(QColor('#e23b3b') if f > 0 else (QColor('#1faa52') if f < 0 else QColor('#cfd6e4')))
                elif field == '获利盘比例(%)':
                    f = float(val)
                    if f >= 80: cell.setForeground(QColor('#e23b3b')); cell.setFont(QFont('', -1, QFont.Bold))
                    elif f >= 60: cell.setForeground(QColor('#d4882f'))
                    else: cell.setForeground(QColor('#1faa52'))
                elif field == '连板数':
                    f = float(val)
                    if f >= 2: cell.setForeground(QColor('#ff6a00')); cell.setFont(QFont('', -1, QFont.Bold))
                    elif f == 1: cell.setForeground(QColor('#e23b3b'))
            except (TypeError, ValueError):
                pass
            self.table.setItem(row, col, cell)

    def _format_value(self, field, val):
        try:
            if field == '成交额': return format_number(float(val)) if val else '0'
            if field == '流通市值': return format_number(float(val)) if val else '0'
            if field == '涨跌幅(%)': return f"{float(val):+.2f}%" if val != '' else '0.00%'
            if field == '换手率(%)': return f"{float(val):.2f}%" if val != '' else '0.00%'
            if field == '换手(实)(%)':
                if val is None or val == '' or val != val: return '-'
                return f"{float(val):.2f}%"
            if field == '委托比(%)': return f"{float(val):+.2f}%" if val != '' else '0.00%'
            if field == '距筹码峰(%)':
                f = float(val); return f"{f:+.2f}%" if abs(f) > 0.001 else "0.00%"
        except (TypeError, ValueError):
            pass
        return '' if val is None else str(val)

    # ---------- 筛选 ----------
    def populate_sector_filter(self):
        cnt = Counter()
        for r in self.all_rows:
            concepts = r.get('概念列表', [])
            if isinstance(concepts, str):
                concepts = [c.strip() for c in concepts.split(';') if c.strip()]
            for c in (concepts or []): cnt[c] += 1
        cur = self.sector_filter.currentData()
        self.sector_filter.blockSignals(True)
        self.sector_filter.clear()
        self.sector_filter.addItem(f"全部板块 ({len(self.all_rows)})", "")
        for name, n in cnt.most_common():
            self.sector_filter.addItem(f"{name} ({n})", name)
        if cur:
            idx = self.sector_filter.findData(cur)
            if idx >= 0: self.sector_filter.setCurrentIndex(idx)
        self.sector_filter.blockSignals(False)
        self._sector_box_ready = True

    def apply_filter(self, *_):
        kw = self.filter_box.text().strip().lower()
        cat = self.cat_filter.currentData()
        sector = self.sector_filter.currentData() if self._sector_box_ready else ""
        shown = 0
        for r in range(self.table.rowCount()):
            match = True
            row_item = self.table.item(r, 0)
            row_data = row_item.data(Qt.UserRole + 1) if row_item else None
            if cat and row_data:
                if row_data.get('_category') != cat: match = False
            if match and sector and row_data:
                concepts = row_data.get('概念列表', [])
                if isinstance(concepts, str):
                    concepts = [c.strip() for c in concepts.split(';') if c.strip()]
                if sector not in (concepts or []): match = False
            if match and kw:
                match = any(kw in (self.table.item(r, c).text().lower() if self.table.item(r, c) else '')
                            for c in range(self.table.columnCount()))
            self.table.setRowHidden(r, not match)
            if match: shown += 1
        self.count_label.setText(f"显示 {shown} / 共 {len(self.all_rows)} 只")

    def clear_table(self):
        self._stop_freehold_filler()   # 清空时停掉后台 F4 填充
        self.table.setRowCount(0)
        self.all_rows = []
        self.count_label.setText("共 0 只")
        self.progress.setValue(0)
        self._sort_col = None; self._sort_order = 0
        for i, header in enumerate(self._orig_headers):
            self.table.horizontalHeaderItem(i).setText(header)
        self.sector_filter.blockSignals(True)
        self.sector_filter.clear(); self.sector_filter.addItem("全部板块", "")
        self._sector_box_ready = False
        self.sector_filter.blockSignals(False)
        self.status.showMessage("已清空")

    # ---------- K 线 ----------
    def show_detail(self, item):
        row_data = item.data(Qt.UserRole + 1)
        if not row_data:
            row = item.row()
            if row >= len(self.all_rows): return
            row_data = self.all_rows[row]
        code = row_data.get('_code', row_data.get('代码', '').split('.')[0])
        name = row_data.get('名称', '')
        dlg = QDialog(self); dlg.setWindowTitle(f"{code} {name} - K线详情"); dlg.resize(900, 560)
        lay = QVBoxLayout(dlg); lay.setContentsMargins(8, 8, 8, 8)
        kline_df = get_kline_data(code, days=max(SHORT_CHIP_DAYS, 60))
        chip_info = None
        if kline_df is not None and len(kline_df) >= 5:
            res = scf.build_chip_distribution(kline_df.tail(SHORT_CHIP_DAYS), bins=scf.PRICE_BINS)
            if res is not None:
                centers, chip, total = res
                cur = float(kline_df['close'].iloc[-1])
                above = centers > cur
                trapped = float(chip[above].sum()) / total
                profit = 1.0 - trapped
                peak = float(centers[int(np.argmax(chip))])
                chip_info = (profit, trapped, peak, centers, chip, total)
        if kline_df is None or len(kline_df) < 5:
            lay.addWidget(QLabel("K线数据获取失败"))
        else:
            lay.addWidget(KLineWidget(kline_df, chip_info, show_flags={'ma': True}), 1)
        dlg.exec_()

    def _on_selection_changed(self):
        items = self.table.selectedItems()
        if not items: return
        item = items[0]
        row_data = item.data(Qt.UserRole + 1)
        if not row_data: return
        if not self.kline_container.isVisible(): self.show_kline()
        self._pending_row = row_data
        self._kline_timer.start(150)

    def _flush_pending_kline(self):
        if self._pending_row is None: return
        self._refresh_kline(self._pending_row)
        self._pending_row = None

    def show_kline(self): self.kline_container.setVisible(True)

    def hide_kline(self):
        self.kline_container.setVisible(False)
        self.table.clearSelection()
        self.kline_info_label.setText("（点击表格行查看 K 线）")
        if self.kline_widget is not None:
            kl = self.kline_container.layout()
            kl.removeWidget(self.kline_widget); self.kline_widget.deleteLater(); self.kline_widget = None

    def _refresh_kline(self, row_data):
        code = row_data.get('_code', row_data.get('代码', '').split('.')[0])
        name = row_data.get('名称', '')
        self.kline_info_label.setText(
            f"{name} ({code})   收盘 {row_data.get('收盘价','')}   "
            f"涨跌幅 {row_data.get('涨跌幅(%)','')}%   量能5日 {row_data.get('量比(5日均量)','')}   "
            f"连板 {row_data.get('连板数','')}   获利盘 {row_data.get('获利盘比例(%)','')}%   "
            f"分类 {row_data.get('_category','')}")
        kline_df = get_kline_data(code, days=max(SHORT_CHIP_DAYS, 60))
        chip_info = None
        if kline_df is not None and len(kline_df) >= 5:
            res = scf.build_chip_distribution(kline_df.tail(SHORT_CHIP_DAYS), bins=scf.PRICE_BINS)
            if res is not None:
                centers, chip, total = res
                cur = float(kline_df['close'].iloc[-1])
                above = centers > cur
                trapped = float(chip[above].sum()) / total
                profit = 1.0 - trapped
                peak = float(centers[int(np.argmax(chip))])
                chip_info = (profit, trapped, peak, centers, chip, total)
        kl = self.kline_container.layout()
        if self.kline_widget is not None:
            kl.removeWidget(self.kline_widget); self.kline_widget.deleteLater()
        if kline_df is None or len(kline_df) < 5:
            self.kline_widget = QLabel("K线数据获取失败")
            self.kline_widget.setStyleSheet("color:#aaa; padding:20px;")
        else:
            self.kline_widget = KLineWidget(kline_df, chip_info, show_flags=self._indicator_flags())
        kl.addWidget(self.kline_widget, 1)
        self.kline_container.update()

    def _indicator_flags(self):
        return {
            'ma': self.cb_ind_ma.isChecked(),
            'boll': self.cb_ind_boll.isChecked(),
            'macd': self.cb_ind_macd.isChecked(),
            'kdj': self.cb_ind_kdj.isChecked(),
            'rsi': self.cb_ind_rsi.isChecked(),
        }

    def _on_indicator_toggled(self):
        if self.kline_widget is not None and isinstance(self.kline_widget, KLineWidget):
            self.kline_widget.show_flags = self._indicator_flags()
            self.kline_widget.update()

    def export_csv(self):
        if not self.all_rows:
            QMessageBox.information(self, "提示", "当前没有数据可导出"); return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 CSV",
            f"超短线龙头_{pd.Timestamp.now().strftime('%Y%m%d_%H%M')}.csv", "CSV 文件 (*.csv)")
        if not path: return
        try:
            df = pd.DataFrame([{k: v for k, v in r.items() if not k.startswith('_')} for r in self.all_rows])
            df.to_csv(path, index=False, encoding='utf-8-sig')
            QMessageBox.information(self, "导出成功", f"已保存到:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    def closeEvent(self, event):
        if self.scanning and self.worker:
            self.worker.stop()
            if self.thread:
                self.thread.quit(); self.thread.wait(3000)
        self._stop_freehold_filler()
        event.accept()


def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
