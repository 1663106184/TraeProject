"""
筹码获利盘筛选器
=====================
挑选「获利盘 ≥ 阈值」且「放量」的股票，上涨下跌都收。

核心思路：
1. 通过腾讯 K 线接口拉取近 N 日后复权日 K 数据；
2. 用日成交量近似估算每个价位的筹码分布（带时间衰减权重）；
3. 获利盘 = 当前价之下的筹码占比；
4. 获利盘 ≥ 阈值 且 量比 ≥ 阈值 即入选（涨跌幅下限默认 -99，即下跌也收）。

复用 stock_full_scan 中的：网络请求、K 线、行情、行业信息等函数。
"""

import time
import numpy as np
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

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

# ---------------- 筛选参数 ----------------
PROFIT_RATIO_MIN = 0.60      # 获利盘比例下限（60%）：当前价之下筹码占比 ≥ 60%
VOLUME_RATIO_MIN = 1.3       # 量比下限：放量阈值 ≥ 1.3
VOLUME_RATIO_MAX = 999.0     # 量比上限：缩量阈值 ≤ 0.7（放量填大值如999）
ZDF_MIN = -99.0              # 涨跌幅下限（%）：默认 -99，即上涨下跌都收；填 0 则只要上涨
ZDF_MAX = 99.0               # 涨跌幅上限（%）：缩量下跌填 0 则只要下跌
KLINE_DAYS = 120             # 取近 120 个交易日构建筹码分布
PRICE_BINS = 200             # 价位切片数量，越大越精细
MAX_WORKERS = 30             # 并发数


def build_chip_distribution(df, bins=PRICE_BINS):
    """
    基于日成交量近似构造筹码(成本)分布。

    把每个交易日的成交量均匀摊到当日 [low, high] 价格区间，加时间衰减权重
    （老筹码 0.3 → 新筹码 1.0），让近期换手更显著。

    返回 (centers, chip, total) 或 None。
    """
    if df is None or len(df) < 5:
        return None

    low_all = float(df['low'].min())
    high_all = float(df['high'].max())
    if high_all <= low_all:
        return None

    edges = np.linspace(low_all, high_all, bins + 1)
    centers = (edges[:-1] + edges[1:]) / 2.0
    chip = np.zeros(bins, dtype=float)

    n = len(df)
    decay = np.linspace(0.3, 1.0, n)

    for i, (_, row) in enumerate(df.iterrows()):
        lo = float(row['low'])
        hi = float(row['high'])
        vol = float(row['volume'])
        if vol <= 0 or hi <= lo:
            continue
        idx_lo = int(np.searchsorted(edges, lo, side='left') - 1)
        idx_hi = int(np.searchsorted(edges, hi, side='right') - 1)
        idx_lo = max(0, idx_lo)
        idx_hi = min(bins - 1, idx_hi)
        if idx_hi < idx_lo:
            continue
        span = idx_hi - idx_lo + 1
        chip[idx_lo:idx_hi + 1] += vol / span * decay[i]

    total = chip.sum()
    if total <= 0:
        return None

    return centers, chip, total


def calc_chip_ratio(df, current_price, bins=PRICE_BINS):
    """
    计算获利盘/套牢盘比例。

    获利盘 = 当前价之下的筹码量占比；套牢盘 = 当前价之上占比。
    返回 (profit_ratio, trapped_ratio, peak_price) 或 None。
    """
    res = build_chip_distribution(df, bins=bins)
    if res is None:
        return None
    centers, chip, total = res

    peak_idx = int(np.argmax(chip))
    peak_price = float(centers[peak_idx])

    above_mask = centers > current_price
    trapped = float(chip[above_mask].sum()) / total
    profit = 1.0 - trapped
    return profit, trapped, peak_price


def compute_features(code):
    """计算单只股票的全部特征指标（不过滤），供本地筛选/缓存使用。
    仅过滤无效数据（停牌/无K线）。返回 dict 或 None。
    """
    stock = get_stock_raw(code)
    if not stock or stock['volume'] <= 0:
        return None

    kline_df = get_kline_data(code, days=KLINE_DAYS)
    if kline_df is None or len(kline_df) < 20:
        return None

    # 量比
    yesterday_volume = int(kline_df.iloc[-2]['volume']) if len(kline_df) >= 2 else 0
    volume_ratio = round(stock['volume'] / yesterday_volume, 2) if yesterday_volume > 0 else 0.0

    # 筹码分布 & 获利盘/套牢盘
    res = calc_chip_ratio(kline_df, stock['now'])
    if res is None:
        return None
    profit_ratio_f, trapped_ratio_f, peak_price = res

    industry_info = get_stock_industry(code)

    # 实际换手率 = 接口换手率 / (1 - 前十流通占比)。
    # 用腾讯 data[38] 换手率反算，而不是用成交量手动算，
    # 避免 data[6]（成交量）在不同板块间单位不一致的问题：
    #  - main/SME/ChiNext: data[6] 是"手"
    #  - STAR/科创板(688):  data[6] 是"股"
    # 只查缓存（不阻塞），未命中的票由后台 FreeholdFiller 异步补全。
    freehold_ratio = get_freehold_ratio_cached(code)
    real_turnover = calc_real_turnover_from_api(stock['turnover'], freehold_ratio)

    return {
        '代码': code + ('.SH' if code.startswith('6') else '.SZ'),
        '名称': stock['name'],
        '市场板块': stock['sector'],
        '收盘价': stock['now'],
        '涨跌幅(%)': round(stock['zdf'], 2),
        '量比': volume_ratio,
        '换手率(%)': stock['turnover'],
        '换手(实)(%)': real_turnover,
        '成交额': stock['amount'],
        '流通市值': stock.get('circ_market_cap', 0.0),
        '套牢盘比例(%)': round(trapped_ratio_f * 100, 2),
        '获利盘比例(%)': round(profit_ratio_f * 100, 2),
        '筹码峰价位': round(peak_price, 2),
        '距筹码峰(%)': round((stock['now'] - peak_price) / peak_price * 100, 2),
        '同花顺行业': industry_info['同花顺行业'],
        '最相关概念': industry_info['最相关概念'],
        '概念列表': industry_info['概念列表'],
    }


def process_one(code):
    """处理单只股票，按当前参数过滤，返回筛选结果 dict 或 None。"""
    feat = compute_features(code)
    if feat is None:
        return None
    zdf = feat['涨跌幅(%)']
    vol = feat['量比']
    profit = feat['获利盘比例(%)']
    # 涨跌幅区间
    if zdf <= ZDF_MIN or zdf >= ZDF_MAX:
        return None
    # 量比区间
    if vol < VOLUME_RATIO_MIN or vol > VOLUME_RATIO_MAX:
        return None
    # 获利盘下限
    if profit < PROFIT_RATIO_MIN * 100:
        return None
    return feat


# ---------------- 快照缓存（指标版） ----------------
import os

SNAPSHOT_DIR = "snapshot"
SNAPSHOT_FILE = "snapshot/stock_features.csv"


def save_snapshot(rows, meta=None):
    """把全市场特征指标（算好的 dict 列表）存到本地 CSV。

    rows: list of dict（每只股票一行，含获利盘/量比/涨跌幅等已算好的指标）
    meta: 可选 dict，写入 CSV 第一行注释（saved_at / kline_days / bins）
    """
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    df = pd.DataFrame(rows)
    # 把 meta 信息存成单独文件，避免污染 CSV
    if meta:
        import json
        with open(os.path.join(SNAPSHOT_DIR, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    df.to_csv(SNAPSHOT_FILE, index=False, encoding='utf-8-sig')
    return SNAPSHOT_FILE


def load_snapshot():
    """读取本地快照，返回 (rows, meta)。无快照返回 (None, None)。"""
    if not os.path.exists(SNAPSHOT_FILE):
        return None, None
    try:
        df = pd.read_csv(SNAPSHOT_FILE, encoding='utf-8-sig')
    except Exception:
        return None, None
    rows = df.to_dict('records')
    # 读 meta
    meta = {}
    meta_path = os.path.join(SNAPSHOT_DIR, "meta.json")
    if os.path.exists(meta_path):
        try:
            import json
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            meta = {}
    return rows, meta


def filter_local(rows, profit_min, vol_ratio_min, vol_ratio_max, zdf_min, zdf_max):
    """在本地指标快照上按参数筛选（纯数值比较，毫秒级）。返回符合条件的 rows。"""
    if not rows:
        return []
    profit_pct = profit_min * 100
    res = []
    for r in rows:
        try:
            zdf = float(r.get('涨跌幅(%)', 0))
            vol = float(r.get('量比', 0))
            profit = float(r.get('获利盘比例(%)', 0))
            if zdf <= zdf_min or zdf >= zdf_max:
                continue
            if vol < vol_ratio_min or vol > vol_ratio_max:
                continue
            if profit < profit_pct:
                continue
            res.append(r)
        except (TypeError, ValueError):
            continue
    return res


def main():
    print("=" * 60)
    print("筹码获利盘筛选器")
    print(f"筛选条件: 获利盘 ≥ {PROFIT_RATIO_MIN*100:.0f}%  +  放量(量比≥{VOLUME_RATIO_MIN})  +  涨跌幅>{ZDF_MIN}%")
    print("=" * 60)

    codes = generate_stock_codes()
    print(f"共生成 {len(codes)} 只股票代码...")

    results = []
    valid = 0
    failed = 0
    start = time.time()

    print("\n开始扫描...")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(process_one, c): c for c in codes}
        for idx, fut in enumerate(as_completed(futures), 1):
            r = fut.result()
            if r:
                results.append(r)
                valid += 1
            else:
                failed += 1
            if idx % 200 == 0:
                el = time.time() - start
                prog = idx / len(codes) * 100
                print(f"\r已扫描: {idx}/{len(codes)} ({prog:.1f}%)  命中:{valid}  耗时:{el:.1f}s", end="")

    el = time.time() - start
    print("\n" + "=" * 60)
    print(f"扫描完成！命中 {valid} 只，耗时 {el:.1f}s")

    if not results:
        print("没有找到符合条件的股票。")
        return

    df = pd.DataFrame(results)
    # 排序：获利盘越大越靠前，其次量比越大越靠前
    df = df.sort_values(['获利盘比例(%)', '量比'], ascending=[False, False])
    df['成交额'] = df['成交额'].apply(format_number)

    out = f"筹码获利盘_{pd.Timestamp.now().strftime('%Y%m%d')}.csv"
    df.to_csv(out, index=False, encoding='utf-8-sig')
    print(f"结果已保存: {out}  (共 {len(df)} 只)")
    print("\n前 20 名预览:")
    pd.set_option('display.unicode.east_asian_width', True)
    pd.set_option('display.max_columns', None)
    pd.set_option('display.width', 200)
    print(df.head(20).to_string(index=False))


if __name__ == "__main__":
    main()
