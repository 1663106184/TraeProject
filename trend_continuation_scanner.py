# -*- coding: utf-8 -*-
"""
趋势中继多策略选股扫描器
======================
面向「趋势中良性调整」的选股需求，5 种形态并行扫描：

  P1 放量上涨后连续缩量回调（抄底点）
     Day_big 放量大涨 → 之后 2-5 天缩量回调（量<V_big×0.7）→ 不破 Day_big 低点
  P2 缩量横盘后放量突破（趋势延续点）
     连续 3+ 天缩量窄幅（量<V_ma5×0.7，振幅<5%）→ 放量突破（量比>1.3）
  P3 温和放量走趋势（主升浪）
     近 10 日量能阶梯递增 + 价格沿 MA20 上行 + 偏离 MA20 < 15%
  P4 缩量回踩 MA20 支撑（均线买点）
     上涨趋势中缩量回踩 MA20 ±2% → 企稳信号
  P5 放量突破后缩量回踩前高（2B 回踩确认）
     放量突破前高 → 缩量回踩前高附近（±3%）→ 支撑确认

复用 stock_full_scan 的 K线/行情/并发框架。
运行：
  py trend_continuation_scanner.py            # 命令行全市场扫描
  py trend_continuation_scanner.py --gui      # PyQt5 桌面版
  py trend_continuation_scanner.py --code 603296,300718   # 指定股票
"""

import sys
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

from stock_full_scan import (
    generate_stock_codes,
    get_stock_raw,
    get_kline_data,
    get_stock_industry,
    format_number,
)

# ---------------- 全局参数 ----------------
KLINE_DAYS = 60              # K 线回看天数
MAX_WORKERS = 30             # 并发数

# P1 放量涨后缩量回调
P1_BIG_VOL_RATIO = 1.5       # 基准日量比
P1_BIG_ZDF_MIN = 3.0         # 基准日涨幅下限 %
P1_SHRINK_RATIO = 0.7        # 回调期量 < 基准日量 × 此值
P1_PULLBACK_DAYS_MIN = 2     # 回调最少天数
P1_PULLBACK_DAYS_MAX = 5     # 回调最多天数
P1_HOLD_LOW_PCT = 0.97       # 回调不破基准日低点 × 此值
P1_TAIL_SHRINK = 0.8         # 回调末期量比下限（地量信号）

# P2 缩量横盘后放量突破
P2_SHRINK_DAYS_MIN = 3       # 缩量最少天数
P2_SHRINK_VOL_RATIO = 0.7    # 缩量期量 < V_ma5 × 此值
P2_RANGE_PCT = 5.0           # 窄幅振幅上限 %
P2_BREAK_VOL_RATIO = 1.3     # 突破日量比
P2_BREAK_ZDF_MIN = 1.0       # 突破日涨幅下限 %

# P3 温和放量走趋势
P3_TREND_DAYS = 10           # 趋势窗口
P3_VOL_STEP_MIN = 1.1        # 量能阶梯递增倍数（后日量/前日量）
P3_ALONG_MA20_MAX = 15.0     # 偏离 MA20 上限 %
P3_UPTREND_ZDF_MIN = 3.0     # 区间涨幅下限 %

# P4 缩量回踩 MA20
P4_MA_PERIOD = 20            # MA 周期
P4_TOUCH_PCT = 4.0           # 触及 MA20 ± 此 %（放宽，波动市场2%太严）
P4_SHRINK_VOL_RATIO = 0.85   # 回踩期缩量（放宽，V_ma5含近5日会被拉低）
P4_SHRINK_DAYS_MIN = 2       # 近N日中至少有这么多天缩量
P4_TREND_UP_DAYS = 20        # 上升趋势回看天数

# P5 放量突破后缩量回踩前高
P5_BREAK_VOL_RATIO = 1.5     # 突破日量比
P5_PULLBACK_DAYS_MAX = 5     # 回踩最多天数
P5_NEAR_HIGH_PCT = 3.0       # 回踩至前高 ± 此 %
P5_SHRINK_RATIO = 0.7        # 回踩期量 < 突破日量 × 此值

# P6 老鸭头形态（MA5下穿MA10不破MA20后重新上穿）
P6_MA_SHORT = 5              # 短期均线
P6_MA_MID = 10               # 中期均线
P6_MA_LONG = 20              # 长期均线
P6_NECK_DAYS_MIN = 5         # 鸭颈（多头排列）最少天数
P6_HEAD_DAYS_MAX = 8         # 鸭头（MA5<MA10）最多天数
P6_MA20_UP = True            # 要求MA20上行

# P7 沿5日线缓慢上升趋势
P7_TREND_DAYS = 10           # 趋势回看天数
P7_MA_SHORT = 5              # 短期均线
P7_HOLD_MA_DAYS_MIN = 7      # 沿5日线最少天数（收盘≥MA5）
P7_DAILY_ZDF_MAX = 5.0       # 单日涨幅上限%（温和不是暴涨）
P7_TOTAL_ZDF_MIN = 3.0       # 区间涨幅下限%
P7_TOTAL_ZDF_MAX = 25.0      # 区间涨幅上限%（缓慢不是主升浪）
P7_MA5_UP = True             # MA5上行

# P8 均线吻形态（飞吻/舌吻，MA5贴近MA10后远离）
P8_MA_SHORT = 5
P8_MA_MID = 10
P8_KISS_DAYS_MAX = 5         # 吻（贴合）最多天数
P8_KISS_GAP_PCT = 1.0        # 吻时MA5与MA10差距≤此%（视作贴合）
P8_SEP_GAP_PCT = 1.5         # 远离时MA5与MA10差距≥此%
P8_MA_MID_UP = True           # MA10需上行（大趋势向上）


# ============ 形态识别算法 ============

def _vol_ma5(df):
    """近5日均量"""
    if len(df) < 5:
        return None
    return df['volume'].iloc[-5:].mean()


def _ma(df, period):
    if len(df) < period:
        return None
    return df['close'].iloc[-period:].mean()


def _ma_series(df, period):
    """返回整条 MA 均线（numpy 数组，前面不足部分为 NaN）"""
    return df['close'].astype(float).rolling(period).mean().values


def _ma_at(ma_arr, i):
    """取 MA 数组第 i 项，NaN 安全"""
    if i < 0 or i >= len(ma_arr):
        return None
    v = ma_arr[i]
    return None if (v is None or np.isnan(v)) else float(v)


def detect_P1(df):
    """P1 放量上涨后连续缩量回调"""
    if len(df) < 15:
        return None
    vols = df['volume'].values
    closes = df['close'].values
    lows = df['low'].values
    zdf = (closes[-1] / closes[-2] - 1) * 100 if closes[-2] > 0 else 0

    # 找近 10 日内放量上涨日作为基准日（取最近一个）
    big_idx = None
    for i in range(len(df) - 2, max(len(df) - 11, 1), -1):
        if i < 1:
            break
        vol_ratio = vols[i] / vols[i - 1] if vols[i - 1] > 0 else 0
        day_zdf = (closes[i] / closes[i - 1] - 1) * 100
        if vol_ratio >= P1_BIG_VOL_RATIO and day_zdf >= P1_BIG_ZDF_MIN:
            big_idx = i
            break
    if big_idx is None:
        return None

    big_vol = vols[big_idx]
    big_low = lows[big_idx]
    big_close = closes[big_idx]

    # 基准日之后的回调期
    pullback = df.iloc[big_idx + 1:]
    pb_days = len(pullback)
    if pb_days < P1_PULLBACK_DAYS_MIN or pb_days > P1_PULLBACK_DAYS_MAX:
        return None

    # 条件1: 回调期每日量 < 基准日量 × 0.7
    if not all(pullback['volume'].values < big_vol * P1_SHRINK_RATIO):
        return None
    # 条件2: 回调不破基准日低点
    if pullback['low'].min() < big_low * P1_HOLD_LOW_PCT:
        return None
    # 条件3: 回调期收盘 < 基准日收盘（确实在回调）
    if not all(pullback['close'].values < big_close):
        return None
    # 条件4: 末期量能萎缩（地量信号）
    vma5 = _vol_ma5(df)
    tail_vol = pullback['volume'].iloc[-1]
    tail_ratio = tail_vol / vma5 if vma5 and vma5 > 0 else 1
    if tail_ratio > P1_TAIL_SHRINK:
        return None

    return {
        'pattern': 'P1',
        'name': '放量涨后缩量回调',
        'big_date': df.iloc[big_idx]['date'],
        'big_close': round(big_close, 2),
        'pullback_days': pb_days,
        'pullback_pct': round((pullback['close'].iloc[-1] / big_close - 1) * 100, 2),
        'tail_vol_ratio': round(tail_ratio, 2),
        'score': 80 + pb_days * 3 + (1 - tail_ratio) * 20,
    }


def detect_P2(df):
    """P2 缩量横盘后放量突破"""
    if len(df) < 15:
        return None
    vols = df['volume'].values
    closes = df['close'].values
    vma5 = _vol_ma5(df)
    if not vma5:
        return None

    # 找连续缩量区间（从最近往前数）
    shrink_end = -1
    shrink_len = 0
    for i in range(len(df) - 2, max(len(df) - 8, -1), -1):
        if vols[i] < vma5 * P2_SHRINK_VOL_RATIO:
            shrink_len += 1
            shrink_end = i
        else:
            if shrink_len >= P2_SHRINK_DAYS_MIN:
                break
            shrink_len = 0
    if shrink_len < P2_SHRINK_DAYS_MIN:
        return None

    shrink_start = shrink_end - shrink_len + 1
    shrink_df = df.iloc[shrink_start:shrink_end + 1]
    # 窄幅条件
    shrink_high = shrink_df['high'].max()
    shrink_low = shrink_df['low'].min()
    if shrink_low <= 0:
        return None
    range_pct = (shrink_high - shrink_low) / shrink_low * 100
    if range_pct > P2_RANGE_PCT:
        return None

    # 突破日 = 缩量区间后一日
    break_idx = shrink_end + 1
    if break_idx >= len(df):
        return None
    break_vol = vols[break_idx]
    break_close = closes[break_idx]
    break_zdf = (break_close / closes[break_idx - 1] - 1) * 100
    break_vol_ratio = break_vol / vols[break_idx - 1] if vols[break_idx - 1] > 0 else 0

    if break_vol_ratio < P2_BREAK_VOL_RATIO or break_zdf < P2_BREAK_ZDF_MIN:
        return None
    # 突破需超过缩量区间高点
    if break_close <= shrink_high:
        return None

    return {
        'pattern': 'P2',
        'name': '缩量横盘后放量突破',
        'shrink_days': shrink_len,
        'range_pct': round(range_pct, 2),
        'break_vol_ratio': round(break_vol_ratio, 2),
        'break_zdf': round(break_zdf, 2),
        'score': 75 + shrink_len * 3 + break_vol_ratio * 5,
    }


def detect_P3(df):
    """P3 温和放量走趋势"""
    if len(df) < P3_TREND_DAYS + 5:
        return None
    recent = df.iloc[-P3_TREND_DAYS:]
    vols = recent['volume'].values
    closes = recent['close'].values

    # 量能阶梯递增：后日量/前日量 >= 1.1 的天数过半
    vol_steps = sum(1 for i in range(1, len(vols)) if vols[i] >= vols[i - 1] * P3_VOL_STEP_MIN)
    if vol_steps < P3_TREND_DAYS * 0.5:
        return None

    # 价格沿 MA20 上行
    ma20 = _ma(df, 20)
    if not ma20 or ma20 <= 0:
        return None
    cur = closes[-1]
    dev_pct = abs(cur - ma20) / ma20 * 100
    if dev_pct > P3_ALONG_MA20_MAX:
        return None

    # 区间涨幅
    zdf = (closes[-1] / closes[0] - 1) * 100
    if zdf < P3_UPTREND_ZDF_MIN:
        return None

    # MA20 斜率向上
    if len(df) >= 25:
        ma20_prev = df['close'].iloc[-25:-5].mean()
        if ma20_prev and ma20 <= ma20_prev:
            return None

    return {
        'pattern': 'P3',
        'name': '温和放量走趋势',
        'trend_days': P3_TREND_DAYS,
        'vol_steps': vol_steps,
        'zdf': round(zdf, 2),
        'dev_ma20': round(dev_pct, 2),
        'score': 70 + vol_steps * 2 + zdf * 0.5,
    }


def detect_P4(df):
    """P4 缩量回踩 MA20 支撑"""
    if len(df) < P4_TREND_UP_DAYS + P4_MA_PERIOD:
        return None
    closes = df['close'].values
    vols = df['volume'].values

    # 上升趋势：近20日 MA20 上行
    ma20_now = _ma(df, P4_MA_PERIOD)
    ma20_prev = df['close'].iloc[-(P4_MA_PERIOD + 5):-5].mean()
    if not ma20_now or not ma20_prev or ma20_now <= ma20_prev:
        return None

    # 当前价格触及 MA20 ±2%
    cur = closes[-1]
    if ma20_now <= 0:
        return None
    dist = abs(cur - ma20_now) / ma20_now * 100
    if dist > P4_TOUCH_PCT:
        return None

    # 回踩期缩量（近3日中至少2天量 < V_ma5 × 0.85）
    vma5 = _vol_ma5(df)
    if not vma5:
        return None
    recent_vols = vols[-3:]
    shrink_days = sum(1 for v in recent_vols if v < vma5 * P4_SHRINK_VOL_RATIO)
    if shrink_days < P4_SHRINK_DAYS_MIN:
        return None

    # 近20日涨幅为正（确认是回踩而非破位）
    zdf20 = (closes[-1] / closes[-20] - 1) * 100
    if zdf20 < 0:
        return None

    return {
        'pattern': 'P4',
        'name': '缩量回踩MA20支撑',
        'ma20': round(ma20_now, 2),
        'dist_ma20': round(dist, 2),
        'zdf20': round(zdf20, 2),
        'score': 72 + (P4_TOUCH_PCT - dist) * 5 + zdf20 * 0.3,
    }


def detect_P5(df):
    """P5 放量突破前高后缩量回踩"""
    if len(df) < 20:
        return None
    vols = df['volume'].values
    closes = df['close'].values
    highs = df['high'].values

    # 找近 15 日内放量突破前高日
    lookback = min(15, len(df) - 1)
    break_idx = None
    prev_high = None
    for i in range(len(df) - 2, max(len(df) - lookback - 1, 5), -1):
        window_high = highs[max(0, i - 20):i].max()
        vol_ratio = vols[i] / vols[i - 1] if vols[i - 1] > 0 else 0
        day_zdf = (closes[i] / closes[i - 1] - 1) * 100
        if closes[i] > window_high and vol_ratio >= P5_BREAK_VOL_RATIO and day_zdf > 0:
            break_idx = i
            prev_high = window_high
            break
    if break_idx is None:
        return None

    break_vol = vols[break_idx]
    pullback = df.iloc[break_idx + 1:]
    pb_days = len(pullback)
    if pb_days < 1 or pb_days > P5_PULLBACK_DAYS_MAX:
        return None

    # 回踩期缩量
    if not all(pullback['volume'].values < break_vol * P5_SHRINK_RATIO):
        return None

    # 回踩至前高附近（±3%）
    pb_close = pullback['close'].iloc[-1]
    if prev_high <= 0:
        return None
    near_pct = abs(pb_close - prev_high) / prev_high * 100
    if near_pct > P5_NEAR_HIGH_PCT:
        return None
    # 回踩不破前高太多（在前高下方 ±3% 内）
    if pb_close < prev_high * 0.97:
        return None

    return {
        'pattern': 'P5',
        'name': '放量破前高后缩量回踩',
        'break_date': df.iloc[break_idx]['date'],
        'prev_high': round(prev_high, 2),
        'pullback_days': pb_days,
        'near_high_pct': round(near_pct, 2),
        'score': 78 + pb_days * 2 + (P5_NEAR_HIGH_PCT - near_pct) * 3,
    }


def detect_P6(df):
    """P6 老鸭头形态（MA5下穿MA10不破MA20后重新上穿）"""
    need = P6_MA_LONG + P6_NECK_DAYS_MIN + P6_HEAD_DAYS_MAX + 2
    if len(df) < need:
        return None
    closes = df['close'].astype(float).values
    ma5 = _ma_series(df, P6_MA_SHORT)
    ma10 = _ma_series(df, P6_MA_MID)
    ma20 = _ma_series(df, P6_MA_LONG)

    # 从后往前找：最近的 MA5 上穿 MA10（鸭嘴）
    n = len(df)
    mouth_idx = None
    for i in range(n - 1, max(n - P6_HEAD_DAYS_MAX - P6_NECK_DAYS_MIN - 2, P6_MA_LONG - 1), -1):
        s_prev = _ma_at(ma5, i - 1); s_now = _ma_at(ma5, i)
        m_prev = _ma_at(ma10, i - 1); m_now = _ma_at(ma10, i)
        if None in (s_prev, s_now, m_prev, m_now):
            continue
        if s_prev <= m_prev and s_now > m_now:   # 金叉
            mouth_idx = i
            break
    if mouth_idx is None:
        return None

    # 鸭头：mouth 之前，MA5 < MA10 的连续天数
    head_days = 0
    for i in range(mouth_idx - 1, max(mouth_idx - P6_HEAD_DAYS_MAX - 1, P6_MA_LONG - 1), -1):
        s = _ma_at(ma5, i); m = _ma_at(ma10, i); l = _ma_at(ma20, i)
        if None in (s, m, l):
            break
        if s < m:                 # MA5 在 MA10 下方
            if l > 0 and m >= l:  # 但 MA10 不破 MA20（鸭头不破长线）
                head_days += 1
            else:
                return None       # 破了 MA20，不是老鸭头
        else:
            break
    if head_days < 1:
        return None

    # 鸭颈：鸭头之前，MA5>MA10>MA20 多头排列
    neck_days = 0
    neck_start = mouth_idx - 1 - head_days
    for i in range(neck_start, max(neck_start - P6_NECK_DAYS_MIN - 2, P6_MA_LONG - 1), -1):
        s = _ma_at(ma5, i); m = _ma_at(ma10, i); l = _ma_at(ma20, i)
        if None in (s, m, l):
            break
        if s > m > l:
            neck_days += 1
        else:
            break
    if neck_days < P6_NECK_DAYS_MIN:
        return None

    # MA20 上行
    ma20_now = _ma_at(ma20, mouth_idx)
    ma20_prev = _ma_at(ma20, mouth_idx - 5)
    if P6_MA20_UP and (not ma20_now or not ma20_prev or ma20_now <= ma20_prev):
        return None

    return {
        'pattern': 'P6',
        'name': '老鸭头形态',
        'mouth_date': df.iloc[mouth_idx]['date'],
        'head_days': head_days,
        'neck_days': neck_days,
        'ma5': round(_ma_at(ma5, mouth_idx) or 0, 2),
        'ma10': round(_ma_at(ma10, mouth_idx) or 0, 2),
        'ma20': round(ma20_now or 0, 2),
        'score': 82 + neck_days * 2 + head_days,
    }


def detect_P7(df):
    """P7 沿5日线缓慢上升趋势（不破5日线温和上行）"""
    need = P7_TREND_DAYS + P7_MA_SHORT + 2
    if len(df) < need:
        return None
    closes = df['close'].astype(float).values
    n = len(df)
    ma5 = _ma_series(df, P7_MA_SHORT)

    # 检查近 P7_TREND_DAYS 内：收盘价始终≥MA5（不破5日线）
    start = n - P7_TREND_DAYS
    hold_days = 0
    for i in range(start, n):
        s = _ma_at(ma5, i)
        if s is None:
            return None
        if closes[i] >= s:
            hold_days += 1
        # 允许盘中破但收盘收回，这里用收盘价判断
    if hold_days < P7_HOLD_MA_DAYS_MIN:
        return None

    # MA5 上行
    ma5_now = _ma_at(ma5, n - 1)
    ma5_prev = _ma_at(ma5, n - 1 - P7_MA_SHORT)
    if P7_MA5_UP and (not ma5_now or not ma5_prev or ma5_now <= ma5_prev):
        return None

    # 区间涨幅温和（不是暴涨也不是横盘）
    zdf = (closes[-1] / closes[start] - 1) * 100
    if zdf < P7_TOTAL_ZDF_MIN or zdf > P7_TOTAL_ZDF_MAX:
        return None

    # 单日涨幅不超阈值（温和）
    max_daily = 0
    for i in range(start + 1, n):
        d = (closes[i] / closes[i - 1] - 1) * 100
        if d > max_daily:
            max_daily = d
    if max_daily > P7_DAILY_ZDF_MAX:
        return None

    return {
        'pattern': 'P7',
        'name': '沿5日线缓慢上升',
        'hold_days': hold_days,
        'trend_days': P7_TREND_DAYS,
        'zdf': round(zdf, 2),
        'max_daily': round(max_daily, 2),
        'ma5': round(ma5_now, 2),
        'score': 76 + hold_days * 1.5 + zdf * 0.3,
    }


def detect_P8(df):
    """P8 均线吻形态（飞吻/舌吻：MA5贴近MA10后远离，贴近点=买点）"""
    need = P8_MA_MID + P8_KISS_DAYS_MAX + 5
    if len(df) < need:
        return None
    n = len(df)
    ma5 = _ma_series(df, P8_MA_SHORT)
    ma10 = _ma_series(df, P8_MA_MID)

    # 找最近的"吻"区间：MA5 与 MA10 差距 ≤ P8_KISS_GAP_PCT%
    # 然后当前 MA5 远离 MA10 向上（差距 ≥ P8_SEP_GAP_PCT%，且 MA5>MA10）
    # 先看当前是否已分离
    s_now = _ma_at(ma5, n - 1); m_now = _ma_at(ma10, n - 1)
    if not s_now or not m_now or m_now <= 0:
        return None
    sep_gap = (s_now - m_now) / m_now * 100
    if sep_gap < P8_SEP_GAP_PCT:
        return None   # 当前还没远离，不构成"吻后分离"

    # MA10 上行（大趋势向上）
    m_prev = _ma_at(ma10, n - 1 - 5)
    if P8_MA_MID_UP and (not m_prev or m_now <= m_prev):
        return None

    # 往前找吻区间：连续几天 gap ≤ P8_KISS_GAP_PCT%
    kiss_end = n - 1
    kiss_days = 0
    for i in range(n - 2, max(n - 2 - P8_KISS_DAYS_MAX - 3, P8_MA_MID - 1), -1):
        s = _ma_at(ma5, i); m = _ma_at(ma10, i)
        if None in (s, m) or m <= 0:
            break
        gap = abs(s - m) / m * 100
        if gap <= P8_KISS_GAP_PCT:
            kiss_days += 1
            kiss_end = i
        else:
            break
    if kiss_days < 1:
        return None

    # 吻之前应该是 MA5>MA10（从上方下来吻），或 MA5<MA10（从下方上来吻后上穿）
    # 这里只要求吻之后 MA5 在 MA10 上方（向上分离）
    kiss_type = '飞吻' if kiss_days <= 2 else '舌吻'

    return {
        'pattern': 'P8',
        'name': f'均线{kiss_type}（MA5贴近MA10后远离）',
        'kiss_type': kiss_type,
        'kiss_days': kiss_days,
        'kiss_end_date': df.iloc[kiss_end]['date'],
        'sep_gap': round(sep_gap, 2),
        'ma5': round(s_now, 2),
        'ma10': round(m_now, 2),
        'score': 80 + kiss_days * 2 + sep_gap * 1.5,
    }


# ============ 单股扫描 ============

PATTERN_DETECTORS = [detect_P1, detect_P2, detect_P3, detect_P4, detect_P5,
                     detect_P6, detect_P7, detect_P8]


def scan_one(code):
    """扫描单只股票，返回所有命中的形态。返回 dict 或 None。"""
    try:
        stock = get_stock_raw(code)
        if not stock or stock.get('volume', 0) <= 0:
            return None
        df = get_kline_data(code, days=KLINE_DAYS)
        if df is None or len(df) < 15:
            return None

        hits = []
        for detector in PATTERN_DETECTORS:
            r = detector(df)
            if r:
                hits.append(r)
        if not hits:
            return None

        ind = get_stock_industry(code)
        return {
            'code': code,
            'name': stock.get('name', ''),
            'close': stock.get('now', 0),
            'zdf': (stock.get('now', 0) / stock.get('open', 1) - 1) * 100 if stock.get('open') else 0,
            'industry': ind.get('industry', '') if ind else '',
            'concept': ind.get('concept', '') if ind else '',
            'hits': hits,
            'best_score': max(h['score'] for h in hits),
            'patterns': ','.join(sorted({h['pattern'] for h in hits})),
        }
    except Exception:
        return None


# ============ 全市场扫描 ============

def scan_market(codes=None, max_workers=MAX_WORKERS, on_progress=None, stop_check=None):
    """全市场扫描。codes=None 时扫全市场。
    stop_check: 无参回调，返回 True 时立即停止扫描（取消未完成任务）。
    """
    if codes is None:
        codes = generate_stock_codes()

    results = []
    total = len(codes)
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(scan_one, c): c for c in codes}
        try:
            for fut in as_completed(futures):
                # 检查停止标志
                if stop_check and stop_check():
                    break
                done += 1
                if on_progress:
                    on_progress(done, total)
                r = fut.result()
                if r:
                    results.append(r)
        finally:
            # 停止时取消所有未开始的任务
            for f in futures:
                f.cancel()
            # 不等剩余任务，强制关闭线程池
            ex.shutdown(wait=False, cancel_futures=True)
    # 按最佳得分排序
    results.sort(key=lambda x: x['best_score'], reverse=True)
    return results


# ============ 命令行入口 ============

def print_results(results, top=50):
    print(f"\n{'='*100}")
    print(f"趋势中继多策略扫描结果 | 命中 {len(results)} 只 | 显示前 {min(top, len(results))} 只")
    print(f"{'='*100}")
    print(f"{'代码':<8}{'名称':<10}{'现价':>8}{'涨跌%':>8}{'形态':<12}{'评分':>6}  {'行业':<12}概念")
    print('-' * 100)
    for r in results[:top]:
        print(f"{r['code']:<8}{r['name']:<10}{r['close']:>8.2f}{r['zdf']:>8.2f}  "
              f"{r['patterns']:<12}{r['best_score']:>6.1f}  {r['industry']:<12}{r['concept'][:30]}")
    print(f"{'-'*100}")
    # 按形态统计
    from collections import Counter
    pc = Counter()
    for r in results:
        for h in r['hits']:
            pc[h['pattern']] += 1
    print("形态命中统计:", dict(pc))


def main():
    parser = argparse.ArgumentParser(description='趋势中继多策略选股扫描器')
    parser.add_argument('--code', help='指定股票代码（逗号分隔），不指定则全市场扫描')
    parser.add_argument('--top', type=int, default=50, help='显示前N只（默认50）')
    parser.add_argument('--gui', action='store_true', help='启动 PyQt5 桌面版')
    args = parser.parse_args()

    if args.gui:
        try:
            from trend_continuation_gui import run_gui
            run_gui()
        except ImportError:
            print("GUI 模块未安装，使用命令行模式")
            args.gui = False

    if args.gui:
        return

    codes = args.code.split(',') if args.code else None
    print(f"开始扫描... {'指定股票' if codes else '全市场'}")

    def progress(d, t):
        if d % 200 == 0:
            print(f"  进度: {d}/{t} ({d*100//t}%)", end='\r')

    results = scan_market(codes=codes, on_progress=progress)
    print_results(results, top=args.top)

    # 可选：导出 CSV
    import os
    if not codes and results:
        out = 'trend_continuation_results.csv'
        rows = []
        for r in results:
            for h in r['hits']:
                rows.append({
                    'code': r['code'], 'name': r['name'], 'close': r['close'],
                    'zdf': round(r['zdf'], 2), 'industry': r['industry'],
                    'pattern': h['pattern'], 'pattern_name': h['name'],
                    'score': round(h['score'], 1), 'concept': r['concept'],
                    **{k: v for k, v in h.items() if k not in ('pattern', 'name', 'score')}
                })
        pd.DataFrame(rows).to_csv(out, index=False, encoding='utf-8-sig')
        print(f"\n结果已导出: {out}")


if __name__ == '__main__':
    main()
