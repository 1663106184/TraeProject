# -*- coding: utf-8 -*-
"""
布林带 6 大策略 + 放量 选股 - PyQt5 桌面版
==========================================
基础策略：下轨 + 放量低吸买入最好（价格触及/跌破下轨，同时放量，视为超跌反弹买点）。
新增 6 大布林带策略信号识别（每只股票自动判定命中哪些）：
  S1 回踩中轨不破再上         （买点）
  S2 长期收口后放量破上轨      （买点）
  S3 下轨止跌K线（锤子/阳包阴）（买点）
  S4 跌破中轨且中轨拐头向下    （卖点/风险）
  S5 上轨外高位放量滞涨        （卖点/风险）
  S6 极度开口后突然缩口见顶    （卖点/风险）
顶部可勾选策略做过滤，表格「命中策略」列高亮（红=买点，绿=卖点，橙=混合）。

复用 stock_full_scan 中的：网络请求、K 线、行情、行业信息等函数。
K 线图支持「均线」/「布林线」两种显示模式，并在图上标记当前价格处于哪一轨。

运行： py stock_boll_volume_filter_gui.py
"""

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QObject, QRectF, QTimer
from PyQt5.QtGui import QColor, QFont, QPainter, QPen, QBrush, QFontMetrics
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QLabel, QTableWidget, QTableWidgetItem,
    QProgressBar, QStatusBar, QHeaderView, QComboBox, QMessageBox,
    QDoubleSpinBox, QSpinBox, QFileDialog, QGroupBox, QCheckBox, QDialog,
    QSplitter, QRadioButton
)

# 复用全市场扫描里的网络/K线/行情/行业函数（同一目录）
from stock_full_scan import (
    generate_stock_codes,
    get_stock_raw,
    get_kline_data,
    get_stock_industry,
    get_market_sector,
    format_number,
    get_freehold_ratio,
    get_freehold_ratio_cached,
    calc_real_turnover,
    calc_real_turnover_from_api,
)


# ---------------- 布林线参数 ----------------
BOLL_PERIOD = 20          # 布林线周期（中轨 = MA20）
BOLL_N_STD = 2.0          # 上下轨偏离倍数（标准 2 倍标准差）
VOLUME_RATIO_MIN = 1.3    # 放量阈值：量比 ≥ 1.3
KLINE_DAYS = 120          # 取近 N 个交易日用于算布林线与量比
MAX_WORKERS = 30          # 并发数


def calc_boll(df, period=BOLL_PERIOD, n_std=BOLL_N_STD):
    """计算布林线三轨。返回 (upper, middle, lower) 全序列 Series，或 None。"""
    if df is None or len(df) < period:
        return None
    close = df['close'].astype(float)
    middle = close.rolling(window=period).mean()
    std = close.rolling(window=period).std()
    upper = middle + n_std * std
    lower = middle - n_std * std
    return upper, middle, lower


def boll_position_tag(close, upper, middle, lower):
    """判定当前价处于哪一轨。返回中文标记。"""
    if close >= upper:
        return '上轨上方'
    if close <= lower:
        return '下轨下方'
    if close >= middle:
        return '中轨至上轨'
    return '下轨至中轨'


# ---------------- 布林带 6 大策略识别 ----------------
# 策略编号 → (策略名, 类型)  类型: '买'=买点信号, '卖'=卖点/风险信号
BOLL_STRATEGIES = [
    ('S1', '回踩中轨不破再上', '买'),
    ('S2', '长期收口放量破上轨', '买'),
    ('S3', '下轨止跌K线', '买'),
    ('S4', '跌破中轨且中轨拐头', '卖'),
    ('S5', '上轨外高位放量滞涨', '卖'),
    ('S6', '极度开口后缩口见顶', '卖'),
]
STRAT_CODE2NAME = {c: n for c, n, _ in BOLL_STRATEGIES}
STRAT_NAME2CODE = {n: c for c, n, _ in BOLL_STRATEGIES}
STRAT_TYPE = {c: t for c, _, t in BOLL_STRATEGIES}

# 收口/开口/极度开口判定阈值（基于轨道宽度占中轨百分比）
BOLL_SQUEEZE_PCT = 3.0     # 收口：带宽 < 3% 视为长期收口
BOLL_WIDE_PCT = 15.0       # 极度开口：带宽 > 15%
BOLL_SQUEEZE_LOOKBACK = 20 # 「长期」收口看近 20 根
BOLL_UPPER_RUN_DAYS = 3    # 连续在上轨外运行的最少天数
BOLL_HIGH_VOL_RATIO = 1.5  # 高位放量阈值（量比）
BOLL_STAGNATE_PCT = 1.0    # 滞涨：当日振幅涨幅差小，实体 < 1%


def _band_width_pct(upper, lower, middle):
    """带宽百分比 = (上轨-下轨)/中轨 * 100。返回 Series。"""
    return (upper - lower) / middle.replace(0, float('nan')) * 100


def _is_hammer(o, c, h, l):
    """锤子线：下影线长 ≥ 实体2倍，上影线短，实体在顶部。"""
    body = abs(c - o)
    lower_shadow = min(o, c) - l
    upper_shadow = h - max(o, c)
    if body <= 0:
        return lower_shadow > 0 and upper_shadow <= body
    return lower_shadow >= 2 * body and upper_shadow <= body * 0.6


def _is_bullish_engulf(prev_o, prev_c, o, c):
    """阳包阴：前阴后阳且实体包住前根。"""
    return prev_c < prev_o and c > o and c >= prev_o and o <= prev_c


def detect_boll_strategies(df, upper, middle, lower, volume_ratio):
    """识别 6 个布林带策略。返回命中策略代码列表，如 ['S1','S3']。
    df: K线DataFrame（含 open/close/high/low/volume），三轨为 Series。
    volume_ratio: 当日量比。
    """
    if df is None or len(df) < BOLL_PERIOD + 2:
        return []
    hits = []
    close = df['close'].astype(float)
    open_ = df['open'].astype(float)
    high = df['high'].astype(float)
    low = df['low'].astype(float)

    u = float(upper.iloc[-1]); m = float(middle.iloc[-1]); l = float(lower.iloc[-1])
    prev_c = float(close.iloc[-2]); prev_o = float(open_.iloc[-2])
    o = float(open_.iloc[-1]); c = float(close.iloc[-1])
    hi = float(high.iloc[-1]); lo = float(low.iloc[-1])

    # S1 回踩中轨不破再上：近几日最低触及/接近中轨后未跌破，当日收阳上攻
    # 判定：近 3 根内最低价 ≤ 中轨*1.01 且收盘均 ≥ 中轨，当日收阳且量比 > 1
    lookback = min(3, len(df) - 1)
    recent_low = float(low.iloc[-lookback:].min())
    recent_above_mid = all(float(close.iloc[i]) >= float(middle.iloc[i]) - 0.01
                           for i in range(-lookback, 0))
    if (recent_low <= m * 1.01 and recent_low >= m * 0.99
            and recent_above_mid and c > o and volume_ratio > 1.0):
        hits.append('S1')

    # S2 长期收口后放量突破上轨：近 BOLL_SQUEEZE_LOOKBACK 根带宽持续 < 阈值，当日收盘 > 上轨 且放量
    bw = _band_width_pct(upper, lower, middle)
    bw_recent = bw.iloc[-(BOLL_SQUEEZE_LOOKBACK + 1):-1]
    squeeze_long = bw_recent.notna().all() and (bw_recent < BOLL_SQUEEZE_PCT).mean() >= 0.8
    if squeeze_long and c > u and volume_ratio >= VOLUME_RATIO_MIN:
        hits.append('S2')

    # S3 下轨止跌K线：当日触及/跌破下轨，出现锤子线或阳包阴
    touch_lower = lo <= l * 1.005 or c <= l * 1.005
    stop_k = _is_hammer(o, c, hi, lo) or _is_bullish_engulf(prev_o, prev_c, o, c)
    if touch_lower and stop_k:
        hits.append('S3')

    # S4 跌破中轨且中轨拐头向下：收盘 < 中轨，且中轨近 3 根下行
    mid_recent = [float(middle.iloc[i]) for i in range(-3, 0)]
    mid_turn_down = mid_recent[0] > mid_recent[-1] and mid_recent[-1] < mid_recent[-2]
    if c < m and mid_turn_down:
        hits.append('S4')

    # S5 上轨外连续运行 + 高位放量滞涨
    run_above = 0
    for i in range(-1, -min(BOLL_UPPER_RUN_DAYS + 1, len(df)) - 1, -1):
        if float(close.iloc[i]) > float(upper.iloc[i]):
            run_above += 1
        else:
            break
    body = abs(c - o)
    stagnate = body / (o if o > 0 else 1) * 100 <= BOLL_STAGNATE_PCT
    if run_above >= BOLL_UPPER_RUN_DAYS and volume_ratio >= BOLL_HIGH_VOL_RATIO and stagnate:
        hits.append('S5')

    # S6 极度开口后突然缩口：前段带宽 > 极度开口阈值，近 2-3 根带宽明显收窄
    bw_prev = float(bw.iloc[-4]) if len(bw) >= 4 else float('nan')
    bw_now = float(bw.iloc[-1])
    if (bw_prev == bw_prev and bw_prev > BOLL_WIDE_PCT
            and bw_now < bw_prev * 0.85):
        hits.append('S6')

    return hits


def compute_boll_features(code):
    """计算单只股票的布林线 + 放量特征（不过滤），供本地筛选/缓存使用。
    仅过滤无效数据（停牌/无K线/布林线算不出）。返回 dict 或 None。
    """
    stock = get_stock_raw(code)
    if not stock or stock['volume'] <= 0:
        return None

    kline_df = get_kline_data(code, days=KLINE_DAYS)
    if kline_df is None or len(kline_df) < BOLL_PERIOD:
        return None

    # 量比：今日成交量 / 昨日成交量
    yesterday_volume = int(kline_df.iloc[-2]['volume']) if len(kline_df) >= 2 else 0
    volume_ratio = round(stock['volume'] / yesterday_volume, 2) if yesterday_volume > 0 else 0.0

    # 布林线三轨（取最后一根 K 线的值）
    boll = calc_boll(kline_df)
    if boll is None:
        return None
    upper, middle, lower = boll
    u = float(upper.iloc[-1]); m = float(middle.iloc[-1]); l = float(lower.iloc[-1])
    if u != u or m != m or l != l:   # 任一 NaN
        return None

    cur = stock['now']
    pos_tag = boll_position_tag(cur, u, m, l)

    # 距下轨百分比（负=已跌破下轨，正=在下轨之上）
    dist_lower_pct = round((cur - l) / l * 100, 2) if l > 0 else 0.0
    # 距中轨百分比
    dist_mid_pct = round((cur - m) / m * 100, 2) if m > 0 else 0.0

    # 6 大布林带策略识别
    strats = detect_boll_strategies(kline_df, upper, middle, lower, volume_ratio)
    strat_names = [STRAT_CODE2NAME[s] for s in strats]
    strat_types = sorted({STRAT_TYPE[s] for s in strats})

    industry_info = get_stock_industry(code)

    # 实际换手率 = 接口换手率 / (1 - 前十流通占比)。
    # 用腾讯 data[38] 反算，避免 data[6] 成交量单位在 688 板块不一致的 bug。
    # 只查缓存（不阻塞），未命中的票由后台异步拉取补全。
    freehold_ratio = get_freehold_ratio_cached(code)
    if freehold_ratio is None:
        freehold_ratio = get_freehold_ratio(code)
    real_turnover = calc_real_turnover_from_api(stock['turnover'], freehold_ratio)

    return {
        '代码': code + ('.SH' if code.startswith('6') else '.SZ'),
        '名称': stock['name'],
        '市场板块': stock['sector'],
        '收盘价': cur,
        '涨跌幅(%)': round(stock['zdf'], 2),
        '量比': volume_ratio,
        '换手率(%)': stock['turnover'],
        '换手(实)(%)': real_turnover,
        '成交额': stock['amount'],
        '流通市值': stock.get('circ_market_cap', 0.0),
        '布林上轨': round(u, 2),
        '布林中轨': round(m, 2),
        '布林下轨': round(l, 2),
        '轨道位置': pos_tag,
        '距下轨(%)': dist_lower_pct,
        '距中轨(%)': dist_mid_pct,
        '命中策略': '、'.join(strat_names),
        '命中策略代码': strats,
        '信号类型': '、'.join(strat_types),
        '同花顺行业': industry_info['同花顺行业'],
        '最相关概念': industry_info['最相关概念'],
        '概念列表': industry_info['概念列表'],
        # 原始数据缓存（不显示，供 K 线图复用）
        '_code_raw': code,
    }


def process_one_boll(code, vol_ratio_min, zdf_min, strat_codes=None):
    """按参数过滤单只股票。下轨 + 放量为最佳买点。返回 dict 或 None。
    strat_codes 非空时，要求命中任一所选策略才保留。
    """
    feat = compute_boll_features(code)
    if feat is None:
        return None
    # 涨跌幅下限
    if feat['涨跌幅(%)'] <= zdf_min:
        return None
    # 量比下限（放量）
    if feat['量比'] < vol_ratio_min:
        return None
    # 策略过滤
    if strat_codes:
        if not any(s in feat['命中策略代码'] for s in strat_codes):
            return None
    return feat


# 结果列定义：(字段名, 显示表头, 是否右对齐数值, 是否需要格式化)
COLUMNS = [
    ('代码',             '代码',       False, False),
    ('名称',             '名称',       False, False),
    ('市场板块',         '市场板块',   False, False),
    ('收盘价',           '收盘价',     True,  False),
    ('涨跌幅(%)',        '涨跌幅',     True,  True),    # 加 %
    ('量比',             '量比',       True,  False),
    ('换手率(%)',        '换手率',     True,  True),    # 加 %
    ('换手(实)(%)',      '换手(实)',   True,  True),    # 加 %
    ('成交额',           '成交额',     True,  True),    # 格式化 万/亿
    ('流通市值',         '流通市值',   True,  True),    # 格式化 万/亿
    ('布林上轨',         '布林上轨',   True,  False),
    ('布林中轨',         '布林中轨',   True,  False),
    ('布林下轨',         '布林下轨',   True,  False),
    ('轨道位置',         '轨道位置',   False, False),
    ('命中策略',         '命中策略',   False, False),
    ('信号类型',         '信号类型',   False, False),
    ('距下轨(%)',        '距下轨',     True,  True),    # 加 %
    ('距中轨(%)',        '距中轨',     True,  True),    # 加 %
    ('同花顺行业',       '同花顺行业', False, False),
    ('最相关概念',       '概念板块',   False, False),
]


class FreeholdFiller(QObject):
    """扫描结束后台并发拉 F4（前十流通占比），拉到一只发信号，主窗口刷新对应行。
    不阻塞扫描主流程；命中缓存的票在主流程已填，这里只处理未命中的。"""
    freehold_ready = pyqtSignal(str, object)
    finished = pyqtSignal(int)

    def __init__(self, codes_to_fetch, max_workers=3):
        super().__init__()
        self.codes = codes_to_fetch
        self.max_workers = max_workers
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        from concurrent.futures import ThreadPoolExecutor, as_completed
        n = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
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


class BollScanWorker(QObject):
    """后台扫描线程：并发拉取，通过信号回传进度与结果。"""
    progress = pyqtSignal(int, int, int)        # 已扫描, 总数, 命中
    result_ready = pyqtSignal(list)             # 一批结果(dict 列表)
    finished = pyqtSignal(int, int, int, float)  # 总数, 命中, 失败, 耗时
    error = pyqtSignal(str)

    def __init__(self, params, max_workers=MAX_WORKERS, mode="filter"):
        super().__init__()
        self.params = params            # dict: vol_ratio_min / zdf_min
        self.max_workers = max_workers
        self.mode = mode                # "filter"=过滤显示 ; "snapshot"=存全量快照
        self._stop = False
        self._batch = []
        self._batch_size = 50

    def stop(self):
        self._stop = True

    def run(self):
        try:
            vol_min = self.params['vol_ratio_min']
            zdf_min = self.params['zdf_min']
            strat_codes = self.params.get('strat_codes') or []

            codes = generate_stock_codes()
            total = len(codes)
            hit = failed = 0
            start = time.time()

            with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
                futures = {ex.submit(self._task, c, vol_min, zdf_min, strat_codes): c for c in codes}
                for idx, fut in enumerate(as_completed(futures), 1):
                    if self._stop:
                        for f in futures:
                            f.cancel()
                        break
                    try:
                        res = fut.result()
                    except Exception:
                        res = None
                    if res:
                        self._batch.append(res)
                        hit += 1
                        if len(self._batch) >= self._batch_size:
                            self.result_ready.emit(self._batch)
                            self._batch = []

                    if idx % 200 == 0 or idx == total:
                        self.progress.emit(idx, total, hit)

            if self._batch:
                self.result_ready.emit(self._batch)
                self._batch = []

            elapsed = time.time() - start
            self.finished.emit(total, hit, total - hit, elapsed)
        except Exception as e:
            self.error.emit(str(e))

    def _task(self, code, vol_min, zdf_min, strat_codes):
        # snapshot 模式算全量指标（不过滤）；filter 模式按参数过滤
        if self.mode == "snapshot":
            return compute_boll_features(code)
        return process_one_boll(code, vol_min, zdf_min, strat_codes)


class SortableTableWidgetItem(QTableWidgetItem):
    """让文本列按数值排序。"""
    def __lt__(self, other):
        try:
            return float(self.data(Qt.UserRole)) < float(other.data(Qt.UserRole))
        except (TypeError, ValueError):
            return self.text() < other.text()


class BollKLineWidget(QWidget):
    """自绘 K 线图（蜡烛 + 成交量）+ 均线 / 布林线（可切换）+ 可选副图指标。
    在图上标记当前价格处于哪一轨。不依赖第三方库。"""

    def __init__(self, kline_df, show_mode="boll", show_flags=None, parent=None):
        super().__init__(parent)
        self.df = kline_df
        self.show_mode = show_mode     # "boll"=布林线 ; "ma"=均线 ; "both"=两者都画
        # 副图指标开关：macd / kdj / rsi
        self.show_flags = show_flags or {}
        self.setMinimumHeight(220)
        self.setStyleSheet("background:#1e2433;")
        # 预算布林线（整段，供绘图与位置判定）
        self._boll = calc_boll(kline_df) if kline_df is not None and len(kline_df) >= BOLL_PERIOD else None

    def _y_of(self, val, pmin, pmax, price_h):
        return int((pmax - val) / (pmax - pmin) * price_h)

    def _draw_sub_line(self, p, series, sub_pmin, sub_pmax, sub_h, sub_top,
                        kline_w, n, cw, color, width=1.5):
        if series is None:
            return
        pen = QPen(color, width)
        p.setPen(pen)
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
        w = self.width()
        h = self.height()

        df = self.df
        n = len(df)
        # 副图数量
        sub_indicators = [k for k in ('macd', 'kdj', 'rsi') if self.show_flags.get(k, False)]
        n_sub = len(sub_indicators)

        # 价格范围：把布林线/均线一并纳入纵轴
        highs = df['high'].astype(float)
        lows = df['low'].astype(float)
        pmin, pmax = float(lows.min()), float(highs.max())
        if self._boll is not None and self.show_mode in ("boll", "both"):
            up, mid, lo = self._boll
            valid = up.dropna()
            if len(valid):
                pmin = min(pmin, float(valid.min()))
                pmax = max(pmax, float(valid.max()))
        if pmax <= pmin:
            pmax = pmin + 1
        pad = (pmax - pmin) * 0.05
        pmin -= pad; pmax += pad

        kline_w = w
        # 垂直布局：主图 + 量图 + 副图
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

        # 网格线 + 价格刻度
        p.setPen(QPen(QColor('#2a3142'), 1, Qt.DashLine))
        for i in range(5):
            y = int(price_h * i / 4)
            p.drawLine(0, y, kline_w, y)
            price = pmax - (pmax - pmin) * i / 4
            p.setPen(QColor('#7a8499'))
            p.drawText(2, y + 12, f"{price:.2f}")
            p.setPen(QPen(QColor('#2a3142'), 1, Qt.DashLine))

        # 蜡烛图
        cw = max(3, kline_w / n - 1)
        vols = df['volume'].astype(float)
        vmax = float(vols.max()) if len(vols) else 1
        if vmax <= 0: vmax = 1

        for i, (_, row) in enumerate(df.iterrows()):
            o = float(row['open']); c = float(row['close'])
            hi = float(row['high']); lo = float(row['low'])
            x = int(i * kline_w / n + cw / 2)
            up_candle = c >= o
            color = QColor('#e23b3b') if up_candle else QColor('#1faa52')
            p.setPen(QPen(color, 1))
            y_hi = int((pmax - hi) / (pmax - pmin) * price_h)
            y_lo = int((pmax - lo) / (pmax - pmin) * price_h)
            p.drawLine(x, y_hi, x, y_lo)
            y_o = int((pmax - o) / (pmax - pmin) * price_h)
            y_c = int((pmax - c) / (pmax - pmin) * price_h)
            body_h = max(1, abs(y_o - y_c))
            p.setBrush(QBrush(color))
            p.drawRect(QRectF(x - cw / 2, min(y_o, y_c), cw, body_h))
            # 成交量柱
            v = float(row['volume'])
            vh = int(v / vmax * vol_h)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(color))
            p.drawRect(QRectF(x - cw / 2, vol_top + vol_h - vh, cw, vh))

        close = df['close'].astype(float)

        # ---------- 布林线 ----------
        if self._boll is not None and self.show_mode in ("boll", "both"):
            up, mid, lo = self._boll
            self._draw_band_line(p, up, kline_w, price_h, pmin, pmax, n, cw, QColor('#36c5f0'), "上轨")
            self._draw_band_line(p, mid, kline_w, price_h, pmin, pmax, n, cw, QColor('#ffcc00'), "中轨")
            self._draw_band_line(p, lo, kline_w, price_h, pmin, pmax, n, cw, QColor('#e23b3b'), "下轨")
            # 上下轨之间淡色填充
            self._fill_boll_band(p, up, lo, kline_w, price_h, pmin, pmax, n, cw)

        # ---------- 均线 ----------
        if self.show_mode in ("ma", "both"):
            ma_defs = [
                (5,  QColor('#ffcc00')),   # MA5 黄
                (10, QColor('#ff6a00')),   # MA10 橙
                (20, QColor('#a974ff')),   # MA20 紫
                (60, QColor('#36c5f0')),   # MA60 青
            ]
            for period, col in ma_defs:
                if n < period:
                    continue
                ma = close.rolling(window=period).mean()
                pen = QPen(col, 1.5)
                p.setPen(pen)
                prev_y = None
                for i in range(period - 1, n):
                    val = ma.iloc[i]
                    if val != val:
                        prev_y = None
                        continue
                    x = int(i * kline_w / n + cw / 2)
                    y = int((pmax - float(val)) / (pmax - pmin) * price_h)
                    if prev_y is not None:
                        p.drawLine(x - int(kline_w / n), prev_y, x, y)
                    prev_y = y

        # ---------- 当前价标线 + 轨道位置标记 ----------
        cur = float(close.iloc[-1])
        cur_y = int((pmax - cur) / (pmax - pmin) * price_h)
        p.setPen(QPen(QColor('#ffffff'), 1, Qt.DashLine))
        p.drawLine(0, cur_y, kline_w, cur_y)
        p.setPen(QColor('#ffffff'))
        p.drawText(4, cur_y - 4, f"现价 {cur:.2f}")

        if self._boll is not None and self.show_mode in ("boll", "both"):
            up, mid, lo = self._boll
            u = float(up.iloc[-1]); m = float(mid.iloc[-1]); l = float(lo.iloc[-1])
            if u == u and m == m and l == l:
                tag = boll_position_tag(cur, u, m, l)
                # 位置标记颜色：下轨下方=最佳买点(红加粗)，中轨下=橙，其他=灰
                if tag == '下轨下方':
                    tag_col = QColor('#ff4d4f'); bold = True
                elif tag == '下轨至中轨':
                    tag_col = QColor('#ff8c1a'); bold = False
                elif tag == '中轨至上轨':
                    tag_col = QColor('#cfd6e4'); bold = False
                else:
                    tag_col = QColor('#1faa52'); bold = False
                p.setPen(tag_col)
                f = QFont(); f.setBold(True); f.setPointSize(10)
                p.setFont(f)
                p.drawText(kline_w - 160, cur_y - 6, f"【{tag}】")
                # 下轨价位标注
                lo_y = int((pmax - l) / (pmax - pmin) * price_h)
                p.setPen(QPen(QColor('#e23b3b'), 1, Qt.DashLine))
                p.drawLine(0, lo_y, kline_w, lo_y)
                p.setPen(QColor('#e23b3b'))
                p.drawText(4, lo_y + 12, f"下轨 {l:.2f}")
                # 上轨价位标注
                up_y = int((pmax - u) / (pmax - pmin) * price_h)
                p.setPen(QPen(QColor('#36c5f0'), 1, Qt.DashLine))
                p.drawLine(0, up_y, kline_w, up_y)
                p.setPen(QColor('#36c5f0'))
                p.drawText(4, up_y - 4, f"上轨 {u:.2f}")

        # 图例（右上角）
        legend_x = kline_w - 260
        legend_y = 14
        if self.show_mode in ("boll", "both"):
            for label, col in (("上轨", QColor('#36c5f0')), ("中轨", QColor('#ffcc00')), ("下轨", QColor('#e23b3b'))):
                p.setPen(col)
                p.drawLine(legend_x, legend_y, legend_x + 16, legend_y)
                p.setPen(QColor('#cfd6e4'))
                p.drawText(legend_x + 20, legend_y + 4, label)
                legend_x += 60
        elif self.show_mode == "ma":
            for period, col in [(5, QColor('#ffcc00')), (10, QColor('#ff6a00')), (20, QColor('#a974ff')), (60, QColor('#36c5f0'))]:
                if n < period:
                    continue
                p.setPen(col)
                p.drawLine(legend_x, legend_y, legend_x + 16, legend_y)
                p.setPen(QColor('#cfd6e4'))
                p.drawText(legend_x + 20, legend_y + 4, f"MA{period}")
                legend_x += 60

        # 成交量区标题
        p.setPen(QColor('#7a8499'))
        p.drawText(2, vol_top + 12, "成交量")

        # ---------- 副图指标 ----------
        close = df['close'].astype(float)
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

        p.end()

    def _draw_band_line(self, p, series, kline_w, price_h, pmin, pmax, n, cw, col, label):
        pen = QPen(col, 1.5)
        p.setPen(pen)
        prev_y = None
        for i in range(len(series)):
            val = series.iloc[i]
            if val != val:
                prev_y = None
                continue
            x = int(i * kline_w / n + cw / 2)
            y = int((pmax - float(val)) / (pmax - pmin) * price_h)
            if prev_y is not None:
                p.drawLine(x - int(kline_w / n), prev_y, x, y)
            prev_y = y

    def _fill_boll_band(self, p, up, lo, kline_w, price_h, pmin, pmax, n, cw):
        """上下轨之间淡色填充（半透明）。"""
        from PyQt5.QtGui import QPolygonF
        from PyQt5.QtCore import QPointF
        pts_top = []
        pts_bottom = []
        for i in range(len(up)):
            uv = up.iloc[i]; lv = lo.iloc[i]
            if uv != uv or lv != lv:
                continue
            x = int(i * kline_w / n + cw / 2)
            yu = (pmax - float(uv)) / (pmax - pmin) * price_h
            yl = (pmax - float(lv)) / (pmax - pmin) * price_h
            pts_top.append(QPointF(x, yu))
            pts_bottom.append(QPointF(x, yl))
        if len(pts_top) < 2:
            return
        poly = QPolygonF(pts_top + list(reversed(pts_bottom)))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(54, 197, 240, 30)))
        p.drawPolygon(poly)


class StockBollDetailDialog(QDialog):
    """双击行弹出的详情窗：K线 + 布林线 + 文字详情。"""

    def __init__(self, row_data, show_mode="boll", parent=None):
        super().__init__(parent)
        code_raw = row_data.get('代码', '')
        code = row_data.get('_code_raw') or code_raw.split('.')[0]
        name = row_data.get('名称', '')
        self.setWindowTitle(f"{code} {name} - 布林线详情")
        self.resize(900, 560)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)

        # 顶部信息条
        info = (
            f"{name} ({code})   收盘 {row_data.get('收盘价','')}   "
            f"涨跌幅 {row_data.get('涨跌幅(%)','')}%   量比 {row_data.get('量比','')}   "
            f"轨道位置 {row_data.get('轨道位置','')}   下轨 {row_data.get('布林下轨','')}"
        )
        lbl = QLabel(info)
        lbl.setStyleSheet("color:#cfd6e4; padding:4px;")
        lay.addWidget(lbl)

        kline_df = get_kline_data(code, days=KLINE_DAYS)
        if kline_df is None or len(kline_df) < 5:
            lay.addWidget(QLabel("K线数据获取失败"))
        else:
            self.kw = BollKLineWidget(kline_df, show_mode=show_mode)
            lay.addWidget(self.kw, 1)

        # 底部文字详情
        detail_lines = []
        for field, header, _, _ in COLUMNS:
            v = row_data.get(field, '')
            detail_lines.append(f"{header}: {v}")
        detail = QLabel("\n".join(detail_lines))
        detail.setStyleSheet("color:#aab; font-size:12px; padding:6px;")
        detail.setWordWrap(True)
        lay.addWidget(detail)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("布林带 6 大策略 + 放量 选股 - 桌面版")
        self.resize(1360, 800)

        self.all_rows = []
        self.worker = None
        self.thread = None
        self.scanning = False
        self._pending_row = None
        self._kline_timer = QTimer(self)
        self._kline_timer.setSingleShot(True)
        self._kline_timer.timeout.connect(self._flush_pending_kline)
        # 本地快照：存全市场特征（不过滤），供离线筛选
        self.local_snapshot = None      # list of dict（指标）
        self.local_meta = {}            # 快照元信息
        self.scan_mode = "filter"
        # K 线图显示模式：boll / ma / both
        self.show_mode = "boll"
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

        # ---------- 参数区 ----------
        param_group = QGroupBox("筛选参数（下轨+放量低吸 + 6大布林带策略信号）")
        pl = QHBoxLayout(param_group)
        pl.setContentsMargins(10, 10, 10, 10)
        pl.setSpacing(10)

        def make_double(label, val, step=0.1, lo=0.0, hi=20.0, suffix=""):
            box = QDoubleSpinBox()
            box.setRange(lo, hi)
            box.setSingleStep(step)
            box.setDecimals(2)
            box.setValue(val)
            box.setSuffix(suffix)
            box.setFixedHeight(28)
            pl.addWidget(QLabel(label))
            pl.addWidget(box)
            return box

        self.spin_vol = make_double("量比下限:", VOLUME_RATIO_MIN, 0.1, 0.0, 20.0, "")
        self.spin_vol.setToolTip("放量阈值：量比 ≥ 该值才算放量。下轨+放量为低吸买点")
        self.spin_zdf = make_double("涨跌幅下限(%):", -99.0, 0.5, -99.0, 20.0, "")
        self.spin_zdf.setToolTip("默认 -99，即上涨下跌都收；填 0 则只要上涨")
        # 筛选参数变化 → 本地重筛
        for box in (self.spin_vol, self.spin_zdf):
            box.valueChanged.connect(self._on_param_changed_for_local)

        # 轨道位置过滤（仅本地快照筛选时生效）
        pl.addWidget(QLabel("轨道位置:"))
        self.pos_filter = QComboBox()
        self.pos_filter.setFixedHeight(28)
        self.pos_filter.addItem("全部位置", "")
        self.pos_filter.addItem("下轨下方(最佳买点)", "下轨下方")
        self.pos_filter.addItem("下轨至中轨", "下轨至中轨")
        self.pos_filter.addItem("中轨至上轨", "中轨至上轨")
        self.pos_filter.addItem("上轨上方", "上轨上方")
        self.pos_filter.currentIndexChanged.connect(self._on_param_changed_for_local)
        pl.addWidget(self.pos_filter)

        # 策略多选：勾选后扫描/本地筛选只保留命中任一所选策略的股票
        pl.addWidget(QLabel("命中策略:"))
        self.strat_checks = []  # [(code, name, QCheckBox)]
        for scode, sname, stype in BOLL_STRATEGIES:
            cb = QCheckBox(sname)
            cb.setToolTip(f"{scode} · {'买点' if stype == '买' else '卖点/风险'}")
            cb.setStyleSheet("color:#cfd6e4; spacing:4px;")
            cb.stateChanged.connect(self._on_param_changed_for_local)
            pl.addWidget(cb)
            self.strat_checks.append((scode, sname, cb))
        # 快捷：全选 / 清空 / 仅买点
        self.btn_strat_all = QPushButton("全选")
        self.btn_strat_all.setFixedHeight(24)
        self.btn_strat_all.clicked.connect(lambda: self._set_strat_checks(True))
        self.btn_strat_buy = QPushButton("仅买点")
        self.btn_strat_buy.setFixedHeight(24)
        self.btn_strat_buy.clicked.connect(
            lambda: self._set_strat_checks(True, only_type='买'))
        self.btn_strat_none = QPushButton("清空")
        self.btn_strat_none.setFixedHeight(24)
        self.btn_strat_none.clicked.connect(lambda: self._set_strat_checks(False))
        for b in (self.btn_strat_all, self.btn_strat_buy, self.btn_strat_none):
            b.setStyleSheet(
                "QPushButton{background:#3a4252;color:#cfd6e4;border-radius:3px;padding:0 8px}"
                "QPushButton:hover{background:#4a5266}"
            )
            pl.addWidget(b)
        pl.addWidget(QLabel("(不勾=不按策略过滤)"))

        pl.addStretch()
        layout.addWidget(param_group)

        # ---------- 工具栏 ----------
        bar = QHBoxLayout()
        self.btn_scan = QPushButton("开始扫描")
        self.btn_scan.setFixedHeight(30)
        self.btn_scan.setStyleSheet(
            "QPushButton{background:#2d8cf0;color:white;font-weight:bold;border-radius:4px;padding:0 18px}"
            "QPushButton:hover{background:#5cadff}"
            "QPushButton:disabled{background:#a0c4f0}"
        )
        self.btn_scan.clicked.connect(self.toggle_scan)

        self.btn_export = QPushButton("导出 CSV")
        self.btn_export.setFixedHeight(30)
        self.btn_export.clicked.connect(self.export_csv)

        self.btn_scan_save = QPushButton("扫描并存快照")
        self.btn_scan_save.setFixedHeight(30)
        self.btn_scan_save.setToolTip("扫描全市场并把所有股票布林线特征存到本地，之后可离线筛选")
        self.btn_scan_save.clicked.connect(self.toggle_scan_save)

        self.btn_load_local = QPushButton("加载本地快照")
        self.btn_load_local.setFixedHeight(30)
        self.btn_load_local.setToolTip("从本地快照加载，按当前参数筛选（不联网）")
        self.btn_load_local.clicked.connect(self.load_local_snapshot)

        self.btn_clear = QPushButton("清空")
        self.btn_clear.setFixedHeight(30)
        self.btn_clear.clicked.connect(self.clear_table)

        self.filter_box = QLineEdit()
        self.filter_box.setPlaceholderText("输入 代码 / 名称 / 行业 / 概念 实时筛选…")
        self.filter_box.setFixedHeight(30)
        self.filter_box.textChanged.connect(self.apply_filter)

        self.col_filter = QComboBox()
        self.col_filter.setFixedHeight(30)
        self.col_filter.addItem("全部列", "")
        for field, header, _, _ in COLUMNS:
            self.col_filter.addItem(header, field)
        self.col_filter.currentIndexChanged.connect(self.apply_filter)

        # 板块筛选
        self.sector_filter = QComboBox()
        self.sector_filter.setFixedHeight(30)
        self.sector_filter.addItem("全部板块", "")
        self.sector_filter.currentIndexChanged.connect(self.apply_filter)
        self._sector_box_ready = False

        self.count_label = QLabel("共 0 只")
        self.count_label.setStyleSheet("color:#666")

        bar.addWidget(self.btn_scan)
        bar.addWidget(self.btn_scan_save)
        bar.addWidget(self.btn_load_local)
        bar.addWidget(self.btn_export)
        bar.addWidget(self.btn_clear)
        bar.addWidget(QLabel("列:"))
        bar.addWidget(self.col_filter, 1)
        bar.addWidget(QLabel("板块:"))
        bar.addWidget(self.sector_filter, 2)
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

        self._sort_col = None
        self._sort_order = 0
        self._orig_headers = [h for _, h, _, _ in COLUMNS]
        self.table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)

        for i, (field, header, is_num, _) in enumerate(COLUMNS):
            if field in ('代码', '名称'):
                self.table.setColumnWidth(i, 95)
            elif field == '最相关概念':
                self.table.setColumnWidth(i, 200)
            elif field == '同花顺行业':
                self.table.setColumnWidth(i, 130)
            elif field == '市场板块':
                self.table.setColumnWidth(i, 90)
            elif field == '轨道位置':
                self.table.setColumnWidth(i, 110)
            elif field == '命中策略':
                self.table.setColumnWidth(i, 220)
            elif field == '信号类型':
                self.table.setColumnWidth(i, 90)
            elif field == '流通市值':
                self.table.setColumnWidth(i, 95)
            elif is_num:
                self.table.setColumnWidth(i, 90)
            else:
                self.table.setColumnWidth(i, 90)
        self.table.itemDoubleClicked.connect(self.show_detail)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)

        # ---------- 表格 + K线区 用 splitter 垂直分隔 ----------
        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.addWidget(self.table)
        # K 线容器
        self.kline_container = QWidget()
        self.kline_container.setMinimumHeight(200)
        kl = QVBoxLayout(self.kline_container)
        kl.setContentsMargins(2, 2, 2, 2)
        # 顶部信息条 + 显示模式切换 + 关闭按钮
        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(0, 0, 0, 0)
        self.kline_info_label = QLabel("（点击表格行查看 K 线）")
        self.kline_info_label.setStyleSheet("color:#cfd6e4; padding:2px;")
        top_bar.addWidget(self.kline_info_label, 1)

        top_bar.addWidget(QLabel("显示:"))
        self.rb_boll = QRadioButton("布林线")
        self.rb_ma = QRadioButton("均线")
        self.rb_both = QRadioButton("两者")
        self.rb_boll.setChecked(True)
        for rb in (self.rb_boll, self.rb_ma, self.rb_both):
            rb.setStyleSheet("color:#cfd6e4;")
            rb.toggled.connect(self._on_show_mode_changed)
            top_bar.addWidget(rb)

        top_bar.addWidget(QLabel("副图:"))
        self.cb_ind_macd = QCheckBox("MACD")
        self.cb_ind_kdj = QCheckBox("KDJ")
        self.cb_ind_rsi = QCheckBox("RSI")
        for cb in (self.cb_ind_macd, self.cb_ind_kdj, self.cb_ind_rsi):
            cb.setStyleSheet("QCheckBox{color:#cfd6e4; padding:0 4px}")
            cb.setFixedHeight(24)
            cb.toggled.connect(self._on_indicator_toggled)
            top_bar.addWidget(cb)

        self.btn_close_kline = QPushButton("关闭 K 线")
        self.btn_close_kline.setFixedHeight(24)
        self.btn_close_kline.setStyleSheet(
            "QPushButton{background:#3a4252;color:#cfd6e4;border-radius:3px;padding:0 10px}"
            "QPushButton:hover{background:#4a5266}"
        )
        self.btn_close_kline.clicked.connect(self.hide_kline)
        top_bar.addWidget(self.btn_close_kline)
        kl.addLayout(top_bar)
        self.kline_widget = None
        self.splitter.addWidget(self.kline_container)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes([500, 300])
        self.kline_container.setVisible(False)
        layout.addWidget(self.splitter, 1)

        # ---------- 进度条 ----------
        self.progress = QProgressBar()
        self.progress.setFixedHeight(18)
        self.progress.setTextVisible(True)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        # ---------- 状态栏 ----------
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("就绪。勾选「命中策略」可按 6 大布林带信号过滤。设置参数后点击「开始扫描」")

    # ---------- 显示模式切换 ----------
    def _on_show_mode_changed(self):
        if self.rb_boll.isChecked():
            self.show_mode = "boll"
        elif self.rb_ma.isChecked():
            self.show_mode = "ma"
        else:
            self.show_mode = "both"
        # 若已有 K 线，立即重绘
        if self.kline_container.isVisible() and self._pending_row is None:
            items = self.table.selectedItems()
            if items:
                row_data = items[0].data(Qt.UserRole + 1)
                if row_data:
                    self._refresh_kline(row_data)

    # ---------- 扫描控制 ----------
    def toggle_scan(self):
        if self.scanning:
            self.stop_scan()
        else:
            self.start_scan()

    def _collect_params(self):
        return {
            'vol_ratio_min': float(self.spin_vol.value()),
            'zdf_min': float(self.spin_zdf.value()),
            'strat_codes': self._selected_strat_codes(),
        }

    def start_scan(self):
        self._start_scan_internal(mode="filter")

    def toggle_scan_save(self):
        if self.scanning:
            self.stop_scan()
        else:
            self._start_scan_internal(mode="snapshot")

    def _start_scan_internal(self, mode):
        self.clear_table()
        self.scanning = True
        self.scan_mode = mode
        self.btn_scan.setText("停止扫描" if mode == "filter" else "开始扫描")
        self.btn_scan_save.setText("停止扫描" if mode == "snapshot" else "扫描并存快照")
        self.btn_export.setEnabled(False)
        self.btn_clear.setEnabled(False)
        for w in (self.spin_vol, self.spin_zdf, self.pos_filter):
            w.setEnabled(False)
        self.progress.setValue(0)
        self.status.showMessage("正在扫描（存快照模式）…" if mode == "snapshot" else "正在扫描…")

        params = self._collect_params()
        self.thread = QThread()
        self.worker = BollScanWorker(params=params, max_workers=MAX_WORKERS, mode=mode)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.on_progress)
        self.worker.result_ready.connect(self.on_result_batch)
        self.worker.finished.connect(self.on_finished)
        self.worker.error.connect(self.on_error)
        self.worker.finished.connect(self.thread.quit)
        self.thread.finished.connect(self._cleanup_thread)
        self.thread.start()

    def stop_scan(self):
        if self.worker:
            self.worker.stop()
        self.status.showMessage("正在停止…")

    def _cleanup_thread(self):
        self.thread = None
        self.worker = None

    def on_progress(self, done, total, hit):
        pct = int(done / total * 100) if total else 0
        self.progress.setValue(pct)
        self.progress.setFormat(f"{pct}%  {done}/{total}")
        self.status.showMessage(f"扫描中  已扫描 {done}/{total}  命中 {hit}")

    def on_result_batch(self, batch):
        if self.scan_mode == "snapshot":
            self.all_rows.extend(batch)
            self.count_label.setText(f"已采集 {len(self.all_rows)} 只")
            return
        self.all_rows.extend(batch)
        self._append_rows(batch)
        self.count_label.setText(f"共 {len(self.all_rows)} 只")

    def on_finished(self, total, hit, failed, elapsed):
        self.scanning = False
        self.btn_scan.setText("开始扫描")
        self.btn_scan_save.setText("扫描并存快照")
        self.btn_export.setEnabled(True)
        self.btn_clear.setEnabled(True)
        for w in (self.spin_vol, self.spin_zdf, self.pos_filter):
            w.setEnabled(True)
        self.progress.setValue(100)

        # 快照模式：把指标存 CSV + meta
        if self.scan_mode == "snapshot":
            try:
                import datetime, os, json
                os.makedirs("snapshot", exist_ok=True)
                meta = {
                    'saved_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
                    'kline_days': KLINE_DAYS,
                    'boll_period': BOLL_PERIOD,
                    'boll_n_std': BOLL_N_STD,
                    'type': 'boll',
                    'count': len(self.all_rows),
                }
                df = pd.DataFrame([{k: v for k, v in r.items()
                                    if not k.startswith('_') and k != '命中策略代码'} for r in self.all_rows])
                df.to_csv("snapshot/stock_boll_features.csv", index=False, encoding='utf-8-sig')
                with open("snapshot/boll_meta.json", "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)
                self.local_snapshot = self.all_rows[:]
                self.local_meta = meta
                self.status.showMessage(
                    f"快照已保存: snapshot/stock_boll_features.csv  共 {len(self.all_rows)} 只  耗时 {elapsed:.1f}s  现在可调参数离线筛选", 10000
                )
                self._refilter_local()
            except Exception as e:
                QMessageBox.critical(self, "保存快照失败", str(e))
                self.status.showMessage("保存快照失败: " + str(e))
            return

        self.status.showMessage(
            f"扫描完成  总数 {total}  命中 {hit}  耗时 {elapsed:.1f}s  正在后台补全换手(实)…", 8000
        )
        self.populate_sector_filter()
        self._start_freehold_filler(self.all_rows)

    # ---------- F4 后台填充 ----------
    def _start_freehold_filler(self, rows):
        self._stop_freehold_filler()
        codes = []
        for r in rows:
            if r.get('换手(实)(%)') is None:
                codes.append(r.get('代码', '').split('.')[0])
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
            try: self.filler.stop()
            except Exception: pass
        if self.filler_thread is not None:
            try: self.filler_thread.quit(); self.filler_thread.wait(2000)
            except Exception: pass
        self.filler = None
        self.filler_thread = None

    def _on_freehold_ready(self, code, ratio):
        target = None
        for r in self.all_rows:
            if r.get('代码', '').split('.')[0] == code:
                target = r; break
        if target is None:
            return
        api_turn = target.get('换手率(%)')
        target['换手(实)(%)'] = calc_real_turnover_from_api(api_turn, ratio)
        col = next((i for i, (f, *_) in enumerate(COLUMNS) if f == '换手(实)(%)'), None)
        if col is None: return
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if not item: continue
            rd = item.data(Qt.UserRole + 1)
            if not rd: continue
            if rd.get('代码', '').split('.')[0] == code:
                val = target.get('换手(实)(%)')
                text = '-' if (val is None or val != val) else f"{float(val):.2f}%"
                cell = self.table.item(row, col)
                if cell:
                    cell.setText(text)
                    cell.setData(Qt.UserRole, val if val is not None else 0)
                return

    def _on_freehold_finished(self, n):
        self.status.showMessage(f"换手(实)后台补全完成（{n} 只）", 5000)

    def on_error(self, msg):
        self.scanning = False
        self.btn_scan.setText("开始扫描")
        self.btn_export.setEnabled(True)
        self.btn_clear.setEnabled(True)
        for w in (self.spin_vol, self.spin_zdf, self.pos_filter):
            w.setEnabled(True)
        QMessageBox.critical(self, "扫描出错", msg)
        self.status.showMessage("扫描出错: " + msg)

    # ---------- 排序 ----------
    def _on_header_clicked(self, col):
        if self._sort_col == col:
            self._sort_order = 2 if self._sort_order == 1 else 1
        else:
            self._sort_col = col
            self._sort_order = 2
        self._refresh_sort_marks()
        order = Qt.AscendingOrder if self._sort_order == 1 else Qt.DescendingOrder
        self.table.sortItems(col, order)

    def _refresh_sort_marks(self):
        for i, header in enumerate(self._orig_headers):
            mark = ''
            if i == self._sort_col and self._sort_order == 1:
                mark = ' ▲'
            elif i == self._sort_col and self._sort_order == 2:
                mark = ' ▼'
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

            # 概念板块列
            if field == '最相关概念':
                concepts = item.get('概念列表', []) or []
                if isinstance(concepts, str):
                    concepts = [c.strip() for c in concepts.split(';') if c.strip()]
                if concepts:
                    shown = '、'.join(concepts[:4])
                    if len(concepts) > 4:
                        shown += f' 等{len(concepts)}个'
                    text = shown
                else:
                    text = '未分类'

            cell = SortableTableWidgetItem(text)
            cell.setData(Qt.UserRole + 1, item)
            sort_val = val
            if is_num and isinstance(val, (int, float)):
                sort_val = float(val)
            cell.setData(Qt.UserRole, sort_val if sort_val != '' else 0)

            if is_num:
                cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            else:
                cell.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)

            # 颜色标记
            try:
                if field == '轨道位置':
                    pos = str(val)
                    if pos == '下轨下方':
                        cell.setForeground(QColor('#ff4d4f')); cell.setFont(QFont('', -1, QFont.Bold))
                    elif pos == '下轨至中轨':
                        cell.setForeground(QColor('#ff8c1a'))
                    elif pos == '中轨至上轨':
                        cell.setForeground(QColor('#cfd6e4'))
                    elif pos == '上轨上方':
                        cell.setForeground(QColor('#1faa52'))
                elif field == '命中策略':
                    if val:
                        types = item.get('信号类型', '')
                        # 含买点→红加粗，纯卖点→绿，混合→橙
                        if '买' in str(types) and '卖' in str(types):
                            cell.setForeground(QColor('#ff8c1a')); cell.setFont(QFont('', -1, QFont.Bold))
                        elif '买' in str(types):
                            cell.setForeground(QColor('#ff4d4f')); cell.setFont(QFont('', -1, QFont.Bold))
                        elif '卖' in str(types):
                            cell.setForeground(QColor('#1faa52'))
                    else:
                        cell.setForeground(QColor('#555'))
                elif field == '信号类型':
                    s = str(val)
                    if s == '买':
                        cell.setForeground(QColor('#ff4d4f'))
                    elif s == '卖':
                        cell.setForeground(QColor('#1faa52'))
                    elif '、' in s:
                        cell.setForeground(QColor('#ff8c1a'))
                elif field == '距下轨(%)':
                    f = float(val)
                    if f <= 0:   # 已跌破/触及下轨
                        cell.setForeground(QColor('#ff4d4f')); cell.setFont(QFont('', -1, QFont.Bold))
                    elif f <= 3:
                        cell.setForeground(QColor('#ff8c1a'))
                    else:
                        cell.setForeground(QColor('#7a8499'))
                elif field == '量比':
                    f = float(val)
                    if f >= 1.3:
                        cell.setForeground(QColor('#e23b3b'))
                    elif f >= 1.0:
                        cell.setForeground(QColor('#d4882f'))
                    else:
                        cell.setForeground(QColor('#7a8499'))
                elif field == '涨跌幅(%)':
                    f = float(val)
                    if f > 0:
                        cell.setForeground(QColor('#e23b3b'))
                    elif f < 0:
                        cell.setForeground(QColor('#1faa52'))
            except (TypeError, ValueError):
                pass

            self.table.setItem(row, col, cell)

    def _format_value(self, field, val):
        try:
            if field == '成交额':
                return format_number(float(val)) if val else '0'
            if field == '流通市值':
                return format_number(float(val)) if val else '0'
            if field == '涨跌幅(%)':
                return f"{float(val):.2f}%" if val != '' else '0.00%'
            if field == '换手率(%)':
                return f"{float(val):.2f}%" if val != '' else '0.00%'
            if field == '换手(实)(%)':
                if val is None or val == '' or val != val: return '-'
                return f"{float(val):.2f}%"
            if field in ('距下轨(%)', '距中轨(%)'):
                f = float(val)
                return f"{f:+.2f}%" if abs(f) > 0.001 else "0.00%"
        except (TypeError, ValueError):
            pass
        return '' if val is None else str(val)

    # ---------- 筛选 ----------
    def populate_sector_filter(self):
        from collections import Counter
        cnt = Counter()
        for r in self.all_rows:
            concepts = r.get('概念列表', [])
            if isinstance(concepts, str):
                concepts = [c.strip() for c in concepts.split(';') if c.strip()]
            for c in (concepts or []):
                cnt[c] += 1
        cur = self.sector_filter.currentData()
        self.sector_filter.blockSignals(True)
        self.sector_filter.clear()
        self.sector_filter.addItem(f"全部板块 ({len(self.all_rows)})", "")
        for name, n in cnt.most_common():
            self.sector_filter.addItem(f"{name} ({n})", name)
        if cur:
            idx = self.sector_filter.findData(cur)
            if idx >= 0:
                self.sector_filter.setCurrentIndex(idx)
        self.sector_filter.blockSignals(False)
        self._sector_box_ready = True

    def apply_filter(self, *_):
        kw = self.filter_box.text().strip().lower()
        field = self.col_filter.currentData()
        sector = self.sector_filter.currentData() if self._sector_box_ready else ""
        shown = 0
        for row in range(self.table.rowCount()):
            match = True
            if sector:
                row_data = self.table.item(row, 0).data(Qt.UserRole + 1) if self.table.item(row, 0) else None
                concepts = (row_data or {}).get('概念列表', []) if row_data else []
                if isinstance(concepts, str):
                    concepts = [c.strip() for c in concepts.split(';') if c.strip()]
                if sector not in (concepts or []):
                    match = False
            if match and kw:
                if field:
                    col = next((i for i, (f, *_) in enumerate(COLUMNS) if f == field), None)
                    if col is not None:
                        c_item = self.table.item(row, col)
                        txt = c_item.text().lower() if c_item else ''
                        match = kw in txt
                    else:
                        match = False
                else:
                    match = any(
                        kw in (self.table.item(row, c).text().lower() if self.table.item(row, c) else '')
                        for c in range(self.table.columnCount())
                    )
            self.table.setRowHidden(row, not match)
            if match:
                shown += 1
        self.count_label.setText(f"显示 {shown} / 共 {len(self.all_rows)} 只")

    # ---------- 其他 ----------
    def clear_table(self):
        self._stop_freehold_filler()
        self.table.setRowCount(0)
        self.all_rows = []
        self.count_label.setText("共 0 只")
        self.progress.setValue(0)
        self._sort_col = None
        self._sort_order = 0
        for i, header in enumerate(self._orig_headers):
            self.table.horizontalHeaderItem(i).setText(header)
        self.local_snapshot = None
        self.local_meta = {}
        self.sector_filter.blockSignals(True)
        self.sector_filter.clear()
        self.sector_filter.addItem("全部板块", "")
        self._sector_box_ready = False
        self.sector_filter.blockSignals(False)
        self.status.showMessage("已清空")

    # ---------- 本地快照筛选 ----------
    def load_local_snapshot(self):
        import os, json
        path = "snapshot/stock_boll_features.csv"
        meta_path = "snapshot/boll_meta.json"
        if not os.path.exists(path):
            QMessageBox.information(self, "提示", "未找到本地布林线快照,请先点「扫描并存快照」。")
            return
        try:
            df = pd.read_csv(path, encoding='utf-8-sig')
        except Exception as e:
            QMessageBox.critical(self, "加载失败", str(e))
            return
        rows = df.to_dict('records')
        # 概念列表字符串还原
        for r in rows:
            c = r.get('概念列表')
            if isinstance(c, str):
                r['概念列表'] = [x.strip() for x in c.split(';') if x.strip()]
            else:
                r['概念列表'] = []
        meta = {}
        if os.path.exists(meta_path):
            try:
                with open(meta_path, encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                meta = {}
        self.local_snapshot = rows
        self.local_meta = meta or {}
        self.status.showMessage(
            f"已加载本地布林线快照: {len(rows)} 只  采集于 {self.local_meta.get('saved_at','?')}  按当前参数筛选…"
        )
        self._refilter_local()

    def _on_param_changed_for_local(self):
        if self.local_snapshot:
            self._refilter_local()

    def _set_strat_checks(self, checked, only_type=None):
        """批量勾选/取消策略复选框。only_type 给定时只动该类型。"""
        for scode, sname, cb in self.strat_checks:
            if only_type and STRAT_TYPE[scode] != only_type:
                cb.setChecked(False)
                continue
            cb.setChecked(checked)

    def _selected_strat_codes(self):
        """返回当前勾选的策略代码列表；空表示不按策略过滤。"""
        return [scode for scode, _, cb in self.strat_checks if cb.isChecked()]

    def _refilter_local(self):
        """用当前界面参数在本地布林线快照上筛选（纯数值比较，毫秒级）。"""
        if not self.local_snapshot:
            return
        vol_min = float(self.spin_vol.value())
        zdf_min = float(self.spin_zdf.value())
        pos_sel = self.pos_filter.currentData()
        strat_sel = self._selected_strat_codes()
        filtered = []
        for r in self.local_snapshot:
            try:
                if float(r.get('涨跌幅(%)', 0)) <= zdf_min:
                    continue
                if float(r.get('量比', 0)) < vol_min:
                    continue
                if pos_sel and str(r.get('轨道位置', '')) != pos_sel:
                    continue
                # 策略过滤：命中任一所选策略即保留
                if strat_sel:
                    codes = r.get('命中策略代码', [])
                    if isinstance(codes, str):
                        codes = [c.strip() for c in codes.strip("[]").replace("'", "").split(',') if c.strip()]
                    elif not isinstance(codes, list):
                        codes = []
                    # 快照 CSV 无代码列时，从「命中策略」文本反查
                    if not codes:
                        names = str(r.get('命中策略', ''))
                        if names:
                            codes = [c for c, nm in STRAT_NAME2CODE.items() if nm in names]
                    if not any(s in codes for s in strat_sel):
                        continue
                filtered.append(r)
            except (TypeError, ValueError):
                continue
        # 排序：距下轨升序（越接近/跌破下轨越靠前），量比降序
        filtered.sort(key=lambda r: (float(r.get('距下轨(%)', 999)), -float(r.get('量比', 0))))
        self.table.setRowCount(0)
        self.all_rows = filtered[:]
        self._append_rows(filtered)
        self.populate_sector_filter()
        n_total = len(self.local_snapshot)
        self.count_label.setText(f"显示 {len(filtered)} / 快照 {n_total} 只")
        self.status.showMessage(
            f"本地筛选: {len(filtered)} 只符合  采集于 {self.local_meta.get('saved_at','?')}  快照共 {n_total} 只"
        )

    def show_detail(self, item):
        row_data = item.data(Qt.UserRole + 1)
        if not row_data:
            row = item.row()
            if row >= len(self.all_rows):
                return
            row_data = self.all_rows[row]
        dlg = StockBollDetailDialog(row_data, show_mode=self.show_mode, parent=self)
        dlg.exec_()

    def _on_selection_changed(self):
        items = self.table.selectedItems()
        if not items:
            return
        item = items[0]
        row_data = item.data(Qt.UserRole + 1)
        if not row_data:
            return
        if not self.kline_container.isVisible():
            self.show_kline()
        self._pending_row = row_data
        self._kline_timer.start(150)

    def _flush_pending_kline(self):
        if self._pending_row is None:
            return
        self._refresh_kline(self._pending_row)
        self._pending_row = None

    def show_kline(self):
        self.kline_container.setVisible(True)

    def hide_kline(self):
        self.kline_container.setVisible(False)
        self.table.clearSelection()
        self.kline_info_label.setText("（点击表格行查看 K 线）")
        if self.kline_widget is not None:
            kl = self.kline_container.layout()
            kl.removeWidget(self.kline_widget)
            self.kline_widget.deleteLater()
            self.kline_widget = None

    def _refresh_kline(self, row_data):
        """刷新下方 K 线区。"""
        code_raw = row_data.get('代码', '')
        code = row_data.get('_code_raw') or code_raw.split('.')[0]
        name = row_data.get('名称', '')

        self.kline_info_label.setText(
            f"{name} ({code})   收盘 {row_data.get('收盘价','')}   "
            f"涨跌幅 {row_data.get('涨跌幅(%)','')}%   量比 {row_data.get('量比','')}   "
            f"轨道位置 {row_data.get('轨道位置','')}   下轨 {row_data.get('布林下轨','')}"
        )

        kline_df = get_kline_data(code, days=KLINE_DAYS)

        kl = self.kline_container.layout()
        if self.kline_widget is not None:
            kl.removeWidget(self.kline_widget)
            self.kline_widget.deleteLater()
        if kline_df is None or len(kline_df) < 5:
            self.kline_widget = QLabel("K线数据获取失败")
            self.kline_widget.setStyleSheet("color:#aaa; padding:20px;")
        else:
            self.kline_widget = BollKLineWidget(kline_df, show_mode=self.show_mode,
                                                  show_flags=self._indicator_flags())
        kl.addWidget(self.kline_widget, 1)
        self.kline_container.update()

    def _indicator_flags(self):
        return {
            'macd': self.cb_ind_macd.isChecked(),
            'kdj': self.cb_ind_kdj.isChecked(),
            'rsi': self.cb_ind_rsi.isChecked(),
        }

    def _on_indicator_toggled(self):
        if self.kline_widget is not None and isinstance(self.kline_widget, BollKLineWidget):
            self.kline_widget.show_flags = self._indicator_flags()
            self.kline_widget.update()

    def export_csv(self):
        if not self.all_rows:
            QMessageBox.information(self, "提示", "当前没有数据可导出")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 CSV", f"布林线放量_{pd.Timestamp.now().strftime('%Y%m%d')}.csv",
            "CSV 文件 (*.csv)"
        )
        if not path:
            return
        try:
            df = pd.DataFrame([{k: v for k, v in r.items()
                                if not k.startswith('_') and k != '命中策略代码'} for r in self.all_rows])
            df.to_csv(path, index=False, encoding='utf-8-sig')
            QMessageBox.information(self, "导出成功", f"已保存到:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    def closeEvent(self, event):
        if self.scanning and self.worker:
            self.worker.stop()
            if self.thread:
                self.thread.quit()
                self.thread.wait(3000)
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
