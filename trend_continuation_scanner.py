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
KLINE_DAYS = 120             # K 线回看天数（显示+形态识别共用）
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

# P9 2B底部反转（新低后快速收回，斯波朗迪核心）
P9_DOWNTREND_DAYS = 20        # 前期下跌趋势回看天数
P9_DOWNTREND_ZDF_MIN = -8.0   # 前期跌幅下限%（确在下跌）
P9_BREAK_LOW_PCT = 0.5       # 跌破前低的幅度% （浅破=假突破）
P9_RECOVER_DAYS_MAX = 3      # 收回创新低的天数内
P9_RECOVER_PCT = 0.5          # 收回至前低之上此%

# P10 缩量十字星（变盘前兆）
P10_DOWNTREND_DAYS = 15       # 前期下跌回看
P10_DOWNTREND_ZDF_MIN = -5.0 # 前期跌幅下限%
P10_BODY_PCT = 0.3            # 实体/振幅 ≤ 此 视为十字星
P10_VOL_RATIO_MAX = 0.7       # 量比 ≤ 此 视为缩量
P10_UPPER_SHADOW_MIN = 0.5   # 上影线/实体 ≥ 此 （有试探性买盘）

# P11 红三兵（三连阳递增）
P11_ZDF_EACH_MIN = 1.0        # 每日涨幅≥此%
P11_VOL_INC_MIN = 1.0        # 量递增（后日量/前日量≥此）
P11_CLOSE_NEAR_HIGH_PCT = 70 # 收盘在当日振幅高位%（接近最高）

# P12 早晨之星（跌+十字+大阳）
P12_DAY1_ZDF_MIN = -2.0       # 第一日跌幅≥此%
P12_DAY2_BODY_PCT = 0.4      # 第二日十字星实体小
P12_DAY3_ZDF_MIN = 3.0        # 第三日大阳线涨幅≥此%
P12_DAY3_VOL_RATIO_MIN = 1.3  # 第三日放量

# P13 上升三法（大阳+3小阴不破+大阳）
P13_DAY1_ZDF_MIN = 3.0        # 第一日大阳涨幅
P13_MID_DAYS = 3              # 中间小阴线天数
P13_MID_ZDF_MAX = -0.5        # 中间日跌幅（小阴）
P13_MID_HOLD_LOW_PCT = 0.98   # 中间不破第一日低点
P13_DAY5_ZDF_MIN = 2.0        # 第五日大阳涨幅

# P14 量价齐升（持续3天价涨量增）
P14_DAYS_MIN = 3              # 至少3天
P14_ZDF_EACH_MIN = 0.5        # 每日涨幅≥此%
P14_VOL_INC_MIN = 1.0         # 量递增
P14_TOTAL_ZDF_MIN = 5.0       # 区间总涨幅≥此%

# P15 多头排列疏散后飞吻（强势趋势收敛-贴近-再发散）
P15_MA_SHORT = 5
P15_MA_MID = 10
P15_MA_LONG = 20
P15_BULL_DAYS_MIN = 8         # 疏散多头排列最少天数
P15_SEP_PCT_MIN = 1.5         # 疏散期MA5与MA10差距≥此%（明显分开）
P15_KISS_GAP_PCT = 0.8        # 飞吻贴合时差距≤此%
P15_KISS_DAYS_MAX = 2         # 飞吻最多天数（贴合时间短=强）
P15_SEP_AGAIN_PCT = 1.0       # 飞吻后再次发散差距≥此%
P15_MA_UP = True              # 均线上行

# P16 死叉后企稳温和放量（多头排列后MA5下穿，调整企稳再放量回升）
P16_MA_SHORT = 5
P16_MA_MID = 10
P16_MA_LONG = 20
P16_BULL_DAYS_MIN = 5         # 死叉前多头排列最少天数
P16_ADJUST_DAYS_MIN = 5       # 调整（MA5<MA10）最少天数
P16_ADJUST_DAYS_MAX = 20      # 调整最多天数（放宽）
P16_SHRINK_RATIO = 0.9        # 调整期缩量（量<V_ma5×此，放宽）
P16_SHRINK_DAYS_MIN = 3       # 调整期至少N天缩量（不必全部缩量）
P16_RECOVER_VOL_RATIO = 1.1   # 企稳回升温和放量（量比≥此，且≤3.0避免突放天量）
P16_RECOVER_ZDF_MIN = 0.3     # 企稳回升区间涨幅≥此%（温和上行）
P16_HOLD_MA20_PCT = 0.95      # 调整不破MA20×此（放宽）

# P17 主升后调整企稳放量（第二波启动，如生益科技类型）
P17_LOOKBACK_DAYS = 120        # 回看总天数（约6个月）
P17_ZT_COUNT_MIN = 2           # 回看期内涨停（涨幅≥9.6%）最少次数
P17_MAIN_RALLY_ZDF_MIN = 30.0  # 主升段涨幅下限%（连续涨停/强势上涨）
P17_ADJUST_DAYS_MIN = 20       # 调整最少天数（数周到数月）
P17_ADJUST_HOLD_PCT = 0.50     # 调整期最低价 ≥ 主升起点 × 此（强势不破，0.5=回撤不超50%）
P17_ADJUST_RANGE_PCT = 15.0    # 调整末期窄幅区间上限%（企稳横盘）
P17_NARROW_DAYS_MIN = 10       # 企稳横盘最少天数
P17_RECOVER_VOL_RATIO = 1.5    # 启动日量比（温和放量/放倍量）
P17_RECOVER_ZDF_MIN = 1.0      # 启动日涨幅下限%


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
    # 缠论三吻：飞吻（1-2天短暂靠近）/ 唇吻（3-4天接触）/ 湿吻（5+天缠绕）
    if kiss_days <= 2:
        kiss_type = '飞吻'
    elif kiss_days <= 4:
        kiss_type = '唇吻'
    else:
        kiss_type = '湿吻'

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


def detect_P9(df):
    """P9 2B底部反转（新低后快速收回，斯波朗迪2B准则）"""
    need = P9_DOWNTREND_DAYS + 5
    if len(df) < need:
        return None
    n = len(df)
    closes = df['close'].astype(float).values
    lows = df['low'].astype(float).values
    vols = df['volume'].astype(float).values

    # 前期下跌趋势
    pre = closes[-(P9_DOWNTREND_DAYS + 5):-5]
    if len(pre) < 5:
        return None
    pre_zdf = (pre[-1] / pre[0] - 1) * 100
    if pre_zdf > P9_DOWNTREND_ZDF_MIN:   # 跌幅不够
        return None

    # 找近5日内创新低后收回
    for i in range(n - 2, max(n - 6, P9_DOWNTREND_DAYS - 1), -1):
        # 前 P9_DOWNTREND_DAYS 的最低价
        lookback_low = lows[max(0, i - P9_DOWNTREND_DAYS):i].min()
        if lookback_low <= 0:
            continue
        # 当日跌破前低（浅破）
        if lows[i] >= lookback_low * (1 - P9_BREAK_LOW_PCT / 100):
            continue
        # 后续 RECOVER_DAYS 内收回至前低之上
        for j in range(i + 1, min(i + 1 + P9_RECOVER_DAYS_MAX, n)):
            if closes[j] > lookback_low * (1 + P9_RECOVER_PCT / 100):
                # 成交量确认：收回日放量更佳
                v_ratio = vols[j] / vols[i] if vols[i] > 0 else 0
                return {
                    'pattern': 'P9',
                    'name': '2B底部反转',
                    'break_date': df.iloc[i]['date'],
                    'break_low': round(float(lows[i]), 2),
                    'recover_date': df.iloc[j]['date'],
                    'recover_close': round(float(closes[j]), 2),
                    'vol_ratio': round(float(v_ratio), 2),
                    'pre_zdf': round(float(pre_zdf), 2),
                    'score': 85 + (10 + pre_zdf) * 0.8 + v_ratio * 3,
                }
    return None


def detect_P10(df):
    """P10 缩量十字星（变盘前兆）"""
    need = P10_DOWNTREND_DAYS + 3
    if len(df) < need:
        return None
    n = len(df)
    closes = df['close'].astype(float).values
    opens = df['open'].astype(float).values
    highs = df['high'].astype(float).values
    lows = df['low'].astype(float).values
    vols = df['volume'].astype(float).values
    vma5 = vols[-6:-1].mean() if n >= 6 else None
    if not vma5 or vma5 <= 0:
        return None

    # 前期下跌
    pre = closes[-(P10_DOWNTREND_DAYS + 1):-1]
    pre_zdf = (pre[-1] / pre[0] - 1) * 100
    if pre_zdf > P10_DOWNTREND_ZDF_MIN:
        return None

    # 当日（最后一根）十字星
    i = n - 1
    body = abs(closes[i] - opens[i])
    rng = highs[i] - lows[i]
    if rng <= 0:
        return None
    body_pct = body / rng
    if body_pct > P10_BODY_PCT:
        return None
    # 缩量
    if vols[i] / vma5 > P10_VOL_RATIO_MAX:
        return None
    # 上影线有试探性买盘
    upper_shadow = (highs[i] - max(closes[i], opens[i]))
    if body > 0 and (upper_shadow / body) < P10_UPPER_SHADOW_MIN:
        return None

    return {
        'pattern': 'P10',
        'name': '缩量十字星',
        'date': df.iloc[i]['date'],
        'close': round(float(closes[i]), 2),
        'body_pct': round(float(body_pct), 3),
        'vol_ratio': round(float(vols[i] / vma5), 2),
        'pre_zdf': round(float(pre_zdf), 2),
        'score': 70 + (5 + pre_zdf) * 0.8 + (P10_VOL_RATIO_MAX - vols[i] / vma5) * 20,
    }


def detect_P11(df):
    """P11 红三兵（三连阳递增+沿5日线）"""
    if len(df) < 8:
        return None
    n = len(df)
    closes = df['close'].astype(float).values
    opens = df['open'].astype(float).values
    highs = df['high'].astype(float).values
    vols = df['volume'].astype(float).values

    # 最后3日
    for k in range(3):
        i = n - 3 + k
        zdf = (closes[i] / closes[i - 1] - 1) * 100
        if zdf < P11_ZDF_EACH_MIN:
            return None
        # 阳线
        if closes[i] <= opens[i]:
            return None
        # 收盘接近当日最高
        rng = highs[i] - lows[i]
        if rng > 0:
            pos = (closes[i] - lows[i]) / rng * 100
            if pos < P11_CLOSE_NEAR_HIGH_PCT:
                return None
        # 量递增
        if k > 0:
            if vols[i] < vols[i - 1] * P11_VOL_INC_MIN:
                return None

    total_zdf = (closes[-1] / closes[-4] - 1) * 100
    return {
        'pattern': 'P11',
        'name': '红三兵',
        'end_date': df.iloc[-1]['date'],
        'total_zdf': round(float(total_zdf), 2),
        'vol_ratio': round(float(vols[-1] / vols[-3]), 2),
        'score': 75 + total_zdf * 1.5,
    }


def detect_P12(df):
    """P12 早晨之星（跌+十字+大阳）"""
    if len(df) < 5:
        return None
    n = len(df)
    closes = df['close'].astype(float).values
    opens = df['open'].astype(float).values
    highs = df['high'].astype(float).values
    lows = df['low'].astype(float).values
    vols = df['volume'].astype(float).values

    # 最后3日
    d1, d2, d3 = n - 3, n - 2, n - 1
    # 第一日跌
    zdf1 = (closes[d1] / closes[d1 - 1] - 1) * 100
    if zdf1 > P12_DAY1_ZDF_MIN:
        return None
    if closes[d1] >= opens[d1]:   # 阴线
        return None
    # 第二日十字星（小实体）
    body2 = abs(closes[d2] - opens[d2])
    rng2 = highs[d2] - lows[d2]
    if rng2 <= 0 or (body2 / rng2) > P12_DAY2_BODY_PCT:
        return None
    # 第三日大阳
    zdf3 = (closes[d3] / closes[d3 - 1] - 1) * 100
    if zdf3 < P12_DAY3_ZDF_MIN:
        return None
    if closes[d3] <= opens[d3]:
        return None
    # 第三日放量
    prev_vol = vols[d3 - 1] if vols[d3 - 1] > 0 else 1
    if vols[d3] / prev_vol < P12_DAY3_VOL_RATIO_MIN:
        return None

    return {
        'pattern': 'P12',
        'name': '早晨之星',
        'd1_zdf': round(float(zdf1), 2),
        'd3_zdf': round(float(zdf3), 2),
        'd3_vol_ratio': round(float(vols[d3] / prev_vol), 2),
        'score': 82 + zdf3 * 1.2,
    }


def detect_P13(df):
    """P13 上升三法（大阳+3小阴不破+大阳）"""
    if len(df) < 7:
        return None
    n = len(df)
    closes = df['close'].astype(float).values
    opens = df['open'].astype(float).values
    lows = df['low'].astype(float).values

    # 取最后5日：大阳+3小阴+大阳
    d1 = n - 5
    # 第一日大阳
    zdf1 = (closes[d1] / closes[d1 - 1] - 1) * 100
    if zdf1 < P13_DAY1_ZDF_MIN or closes[d1] <= opens[d1]:
        return None
    d1_low = lows[d1]
    d1_close = closes[d1]
    # 中间3日小阴，不破第一日低点
    for k in range(1, 1 + P13_MID_DAYS):
        i = d1 + k
        if closes[i] > opens[i]:   # 不是阴线
            return None
        zdf = (closes[i] / closes[i - 1] - 1) * 100
        if zdf > P13_MID_ZDF_MAX:   # 跌幅不够（或上涨）视为非小阴
            return None
        if lows[i] < d1_low * P13_MID_HOLD_LOW_PCT:
            return None
    # 第五日大阳突破
    d5 = d1 + 1 + P13_MID_DAYS
    if d5 >= n:
        return None
    zdf5 = (closes[d5] / closes[d5 - 1] - 1) * 100
    if zdf5 < P13_DAY5_ZDF_MIN or closes[d5] <= opens[d5]:
        return None
    if closes[d5] <= d1_close:   # 需突破第一日收盘
        return None

    return {
        'pattern': 'P13',
        'name': '上升三法',
        'd1_zdf': round(float(zdf1), 2),
        'd5_zdf': round(float(zdf5), 2),
        'hold_low': round(float(d1_low), 2),
        'score': 84 + zdf5 * 1.0,
    }


def detect_P14(df):
    """P14 量价齐升（持续3天价涨量增）"""
    if len(df) < P14_DAYS_MIN + 2:
        return None
    n = len(df)
    closes = df['close'].astype(float).values
    vols = df['volume'].astype(float).values

    days = P14_DAYS_MIN
    # 最后 days 天
    for k in range(days):
        i = n - days + k
        zdf = (closes[i] / closes[i - 1] - 1) * 100
        if zdf < P14_ZDF_EACH_MIN:
            return None
        if k > 0 and vols[i] < vols[i - 1] * P14_VOL_INC_MIN:
            return None
    total_zdf = (closes[-1] / closes[-days - 1] - 1) * 100
    if total_zdf < P14_TOTAL_ZDF_MIN:
        return None

    return {
        'pattern': 'P14',
        'name': '量价齐升',
        'days': days,
        'total_zdf': round(float(total_zdf), 2),
        'vol_ratio': round(float(vols[-1] / vols[-days]), 2),
        'score': 76 + total_zdf * 1.2,
    }


def detect_P15(df):
    """P15 多头排列疏散后飞吻（强势趋势：疏散多头->收敛贴近->再发散）"""
    need = P15_MA_LONG + P15_BULL_DAYS_MIN + 5
    if len(df) < need:
        return None
    n = len(df)
    ma5 = _ma_series(df, P15_MA_SHORT)
    ma10 = _ma_series(df, P15_MA_MID)
    ma20 = _ma_series(df, P15_MA_LONG)

    # 找最近的飞吻点（MA5与MA10差距≤KISS_GAP，且≤2天）
    # 从后往前找，放宽搜索范围到近20天
    kiss_idx = None
    for i in range(n - 1, max(n - 20, P15_MA_LONG - 1), -1):
        s = _ma_at(ma5, i); m = _ma_at(ma10, i)
        if None in (s, m) or m <= 0:
            continue
        gap = abs(s - m) / m * 100
        if gap <= P15_KISS_GAP_PCT:
            # 往前看连续几天小（飞吻≤2天）
            kiss_days = 1
            for j in range(i - 1, max(i - 5, P15_MA_LONG - 1), -1):
                sj = _ma_at(ma5, j); mj = _ma_at(ma10, j)
                if None in (sj, mj) or mj <= 0:
                    break
                if abs(sj - mj) / mj * 100 <= P15_KISS_GAP_PCT:
                    kiss_days += 1
                else:
                    break
            if kiss_days <= P15_KISS_DAYS_MAX:
                kiss_idx = i - kiss_days + 1  # 飞吻开始日
                break
    if kiss_idx is None:
        return None

    # 飞吻前需有疏散多头排列（MA5>MA10>MA20，且MA5与MA10差距≥SEP_PCT）
    # 吻点往前可能先经过收敛区（gap<SEP_PCT），再进入疏散区，需跳过收敛区
    bull_days = 0
    max_sep = 0
    in_bull = False   # 是否已进入疏散多头区
    for i in range(kiss_idx - 1, max(kiss_idx - 30, P15_MA_LONG - 1), -1):
        s = _ma_at(ma5, i); m = _ma_at(ma10, i); l = _ma_at(ma20, i)
        if None in (s, m, l):
            break
        if not (s > m > l):   # 不是多头排列，停
            break
        gap = (s - m) / m * 100
        if gap >= P15_SEP_PCT_MIN:
            # 进入疏散多头区
            bull_days += 1
            max_sep = max(max_sep, gap)
            in_bull = True
        else:
            # 收敛区：如果还没进入疏散区，继续往前找；已进入则停
            if not in_bull:
                continue
            else:
                break
    if bull_days < P15_BULL_DAYS_MIN:
        return None

    # 飞吻后再次发散（当前MA5>MA10，差距≥SEP_AGAIN）
    s_now = _ma_at(ma5, n - 1); m_now = _ma_at(ma10, n - 1)
    if None in (s_now, m_now) or m_now <= 0:
        return None
    sep_now = (s_now - m_now) / m_now * 100
    if sep_now < P15_SEP_AGAIN_PCT:
        return None

    # 均线上行
    ma20_now = _ma_at(ma20, n - 1); ma20_prev = _ma_at(ma20, n - 6)
    if P15_MA_UP and (not ma20_now or not ma20_prev or ma20_now <= ma20_prev):
        return None

    return {
        'pattern': 'P15',
        'name': '多头疏散后飞吻',
        'bull_days': bull_days,
        'max_sep': round(float(max_sep), 2),
        'kiss_date': df.iloc[kiss_idx]['date'],
        'sep_now': round(float(sep_now), 2),
        'ma5': round(float(s_now), 2),
        'ma10': round(float(m_now), 2),
        'ma20': round(float(ma20_now), 2),
        'score': 86 + bull_days * 1.5 + sep_now * 2,
    }


def detect_P16(df):
    """P16 死叉后企稳温和放量（多头排列后MA5下穿，调整企稳再放量回升）"""
    need = P16_MA_LONG + P16_BULL_DAYS_MIN + P16_ADJUST_DAYS_MAX + 5
    if len(df) < need:
        return None
    n = len(df)
    closes = df['close'].astype(float).values
    vols = df['volume'].astype(float).values
    ma5 = _ma_series(df, P16_MA_SHORT)
    ma10 = _ma_series(df, P16_MA_MID)
    ma20 = _ma_series(df, P16_MA_LONG)

    # 找最近的死叉点（MA5从上方下穿MA10）
    death_idx = None
    for i in range(n - 1, max(n - 25, P16_MA_LONG - 1), -1):
        s_prev = _ma_at(ma5, i - 1); s_now = _ma_at(ma5, i)
        m_prev = _ma_at(ma10, i - 1); m_now = _ma_at(ma10, i)
        if None in (s_prev, s_now, m_prev, m_now):
            continue
        if s_prev >= m_prev and s_now < m_now:   # 死叉
            death_idx = i
            break
    if death_idx is None:
        return None

    # 死叉前有多头排列（MA5>MA10>MA20）
    bull_days = 0
    for i in range(death_idx - 1, max(death_idx - P16_BULL_DAYS_MIN - 2, P16_MA_LONG - 1), -1):
        s = _ma_at(ma5, i); m = _ma_at(ma10, i); l = _ma_at(ma20, i)
        if None in (s, m, l):
            break
        if s > m > l:
            bull_days += 1
        else:
            break
    if bull_days < P16_BULL_DAYS_MIN:
        return None

    # 调整期：MA5<MA10 持续 ADJUST_DAYS_MIN~MAX 天，不破MA20
    adj_end = n - 1
    adj_days = 0
    for i in range(death_idx, n):
        s = _ma_at(ma5, i); m = _ma_at(ma10, i); l = _ma_at(ma20, i)
        if None in (s, m, l):
            break
        if s < m:   # 仍死叉
            if l > 0 and closes[i] < l * P16_HOLD_MA20_PCT:   # 破MA20
                return None
            adj_days += 1
            adj_end = i
        else:
            break   # 重新金叉，调整结束
    if adj_days < P16_ADJUST_DAYS_MIN or adj_days > P16_ADJUST_DAYS_MAX:
        return None

    # 调整期缩量（至少N天缩量，不必全部）
    vma5 = vols[-(adj_days + 5):-adj_days].mean() if n >= adj_days + 5 else None
    if not vma5 or vma5 <= 0:
        return None
    adj_vols = vols[death_idx:death_idx + adj_days]
    shrink_days = sum(1 for v in adj_vols if v < vma5 * P16_SHRINK_RATIO)
    if shrink_days < P16_SHRINK_DAYS_MIN:
        return None

    # 企稳回升：近几天温和放量（量递增，不是单日突放天量）
    # 取调整结束后的最近几天，检查量能是否温和递增 + 价格上行
    recover_start = adj_end + 1
    if recover_start >= n:
        return None
    # 看近 MIN(5, 剩余) 天的量能是否温和递增
    recover_days = min(5, n - recover_start)
    if recover_days < 2:
        return None
    recover_vols = vols[recover_start:recover_start + recover_days]
    recover_closes = closes[recover_start:recover_start + recover_days]
    # 温和递增：后日量 > 前日量 的天数过半（不是单根天量）
    inc_days = sum(1 for i in range(1, len(recover_vols)) if recover_vols[i] > recover_vols[i - 1])
    if inc_days < recover_days * 0.5:
        return None
    # 量能从地量温和放大：最近量 / 调整期均量 在 1.1~3.0 之间（温和，不是突放天量）
    rec_vol_ratio = float(recover_vols[-1]) / vma5
    if rec_vol_ratio < P16_RECOVER_VOL_RATIO or rec_vol_ratio > 3.0:
        return None
    # 价格上行
    rec_zdf = (recover_closes[-1] / recover_closes[0] - 1) * 100
    if rec_zdf < P16_RECOVER_ZDF_MIN:
        return None

    return {
        'pattern': 'P16',
        'name': '死叉后企稳温和放量',
        'death_date': df.iloc[death_idx]['date'],
        'bull_days': bull_days,
        'adjust_days': adj_days,
        'shrink_days': shrink_days,
        'recover_days': recover_days,
        'recover_vol_ratio': round(float(rec_vol_ratio), 2),
        'recover_zdf': round(float(rec_zdf), 2),
        'recover_inc_days': inc_days,
        'score': 83 + bull_days + adj_days + inc_days * 2 + rec_vol_ratio,
    }


def detect_P17(df):
    """P17 主升后调整企稳放量（第二波启动，如生益科技类型）
    历史有涨停基因/主升 -> 长期调整保持强势 -> 末期企稳横盘 -> 当前放量启动
    """
    if len(df) < P17_LOOKBACK_DAYS:
        return None
    n = len(df)
    closes = df['close'].astype(float).values
    highs = df['high'].astype(float).values
    lows = df['low'].astype(float).values
    vols = df['volume'].astype(float).values

    # 1. 回看期内找涨停日（涨幅≥9.6%，用前收对比）
    zt_days = []
    for i in range(1, n):
        zdf = (closes[i] / closes[i - 1] - 1) * 100
        if zdf >= 9.6:
            zt_days.append(i)
    if len(zt_days) < P17_ZT_COUNT_MIN:
        return None

    # 2. 找主升段：连续涨停或强势上涨的高点
    # 取涨停最密集的区间，高点 = 该区间最高价
    # 简化：取回看期内最高点及其附近
    high_idx = int(np.argmax(highs))
    high_price = float(highs[high_idx])
    # 主升起点：高点往前找，涨幅≥30%的起点
    main_start = None
    for i in range(high_idx, 0, -1):
        if closes[i] <= 0:
            continue
        rally_zdf = (high_price / closes[i] - 1) * 100
        if rally_zdf >= P17_MAIN_RALLY_ZDF_MIN:
            main_start = i
            break
    if main_start is None:
        return None
    main_start_price = float(closes[main_start])

    # 3. 调整期：从高点到当前
    adjust_df = df.iloc[high_idx:]
    adj_days = len(adjust_df)
    if adj_days < P17_ADJUST_DAYS_MIN:
        return None   # 调整时间不够

    # 调整保持强势：最低价 ≥ 主升起点 × HOLD_PCT
    adj_low = float(lows[high_idx:].min())
    if adj_low < main_start_price * P17_ADJUST_HOLD_PCT:
        return None

    # 4. 调整末期企稳横盘：近 NARROW_DAYS 天振幅 < RANGE_PCT
    narrow_df = df.iloc[-P17_NARROW_DAYS_MIN:]
    narrow_high = float(narrow_df['high'].astype(float).max())
    narrow_low = float(narrow_df['low'].astype(float).min())
    if narrow_low <= 0:
        return None
    narrow_range = (narrow_high - narrow_low) / narrow_low * 100
    if narrow_range > P17_ADJUST_RANGE_PCT:
        return None

    # 5. 当前放量启动：最后1-2日量比≥1.5 + 涨幅≥1%
    vma5 = vols[-6:-1].mean() if n >= 6 else None
    if not vma5 or vma5 <= 0:
        return None
    rec_vol_ratio = vols[-1] / vma5
    rec_zdf = (closes[-1] / closes[-2] - 1) * 100
    if rec_vol_ratio < P17_RECOVER_VOL_RATIO or rec_zdf < P17_RECOVER_ZDF_MIN:
        return None

    # 评分：涨停次数 + 调整天数 + 放量程度
    pullback_pct = (high_price - closes[-1]) / high_price * 100
    return {
        'pattern': 'P17',
        'name': '主升后调整企稳放量',
        'zt_count': len(zt_days),
        'high_date': df.iloc[high_idx]['date'],
        'high_price': round(high_price, 2),
        'main_start_price': round(main_start_price, 2),
        'adjust_days': adj_days,
        'pullback_pct': round(float(pullback_pct), 2),
        'narrow_range': round(float(narrow_range), 2),
        'recover_vol_ratio': round(float(rec_vol_ratio), 2),
        'recover_zdf': round(float(rec_zdf), 2),
        'score': 88 + len(zt_days) * 2 + adj_days * 0.2 + rec_vol_ratio * 3,
    }


# ============ 单股扫描 ============

PATTERN_DETECTORS = [detect_P1, detect_P2, detect_P3, detect_P4, detect_P5,
                     detect_P6, detect_P7, detect_P8, detect_P9, detect_P10,
                     detect_P11, detect_P12, detect_P13, detect_P14,
                     detect_P15, detect_P16, detect_P17]


def scan_one(code, days=None):
    """扫描单只股票，返回所有命中的形态。返回 dict 或 None。"""
    try:
        stock = get_stock_raw(code)
        if not stock or stock.get('volume', 0) <= 0:
            return None
        df = get_kline_data(code, days=days or KLINE_DAYS)
        if df is None or len(df) < 15:
            return None

        hits = []
        for detector in PATTERN_DETECTORS:
            r = detector(df)
            if r:
                hits.append(r)
        if not hits:
            return None

        ind = get_stock_industry(code) or {}
        # 字段名与 stock_boll_volume_filter_gui 一致
        industry = ind.get('同花顺行业', '') or ind.get('行业', '')
        concept_list = ind.get('概念列表', []) or []
        concept = ind.get('最相关概念', '') or (concept_list[0] if concept_list else '')
        concept_str = ','.join(concept_list) if concept_list else concept
        return {
            'code': code,
            'name': stock.get('name', ''),
            'close': stock.get('now', 0),
            'zdf': (stock.get('now', 0) / stock.get('open', 1) - 1) * 100 if stock.get('open') else 0,
            'industry': industry,
            'concept': concept_str,
            'concept_list': concept_list,
            'hits': hits,
            'best_score': max(h['score'] for h in hits),
            'patterns': ','.join(sorted({h['pattern'] for h in hits})),
        }
    except Exception:
        return None


# ============ 全市场扫描 ============

def scan_market(codes=None, max_workers=MAX_WORKERS, on_progress=None, stop_check=None, days=None):
    """全市场扫描。codes=None 时扫全市场。
    stop_check: 无参回调，返回 True 时立即停止扫描（取消未完成任务）。
    days: K线回看天数，None 用 KLINE_DAYS 默认值。
    """
    if codes is None:
        codes = generate_stock_codes()

    _days = days or KLINE_DAYS
    results = []
    total = len(codes)
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(scan_one, c, _days): c for c in codes}
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
