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


# ============ 单股扫描 ============

PATTERN_DETECTORS = [detect_P1, detect_P2, detect_P3, detect_P4, detect_P5]


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

def scan_market(codes=None, max_workers=MAX_WORKERS, on_progress=None):
    """全市场扫描。codes=None 时扫全市场。"""
    if codes is None:
        codes = generate_stock_codes()

    results = []
    total = len(codes)
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(scan_one, c): c for c in codes}
        for fut in as_completed(futures):
            done += 1
            if on_progress:
                on_progress(done, total)
            r = fut.result()
            if r:
                results.append(r)
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
