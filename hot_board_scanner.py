# -*- coding: utf-8 -*-
"""
尾盘主力承接 多维量化选股扫描器
==============================
按用户给定的 5 维量化标准筛选：

  1. 板块热度  : 当日涨幅榜前 2-3 的热门板块（板块内涨停数越多赚钱效应越强）
  2. 涨幅范围  : 涨幅 ∈ [3%, 5%]（低于3%动能不足，高于5%追高风险）
  3. 流动性    : 流通市值 50-300 亿、换手率 3%-10%、量比 > 1.5
  4. 技术形态  : 站稳 5 日均线；MACD 柱由绿转红 且 DIF 上穿 DEA；
                 KDJ 超卖区（K<20）拐头向上
  5. 分时形态  : 14:40 前后创日内新高后小幅回踩不破分时均价线
                （盘中 1 分钟数据公司网络不可达，用「现价接近日内高点 + 现价≥分时均价」
                 近似代理，详见 evaluate_intraday 的注释）

每一项给出独立的通过/失败标记 + 数值依据，便于 GUI 表格展示命中依据。
复用 stock_full_scan 的 K 线 / 行情 / 行业函数与并发框架，技术指标计算复用
calculate_technical_indicators（MACD）+ 本模块新增 KDJ。

运行：
  py hot_board_scanner.py            # 命令行全市场扫描
  py hot_board_scanner.py --gui      # PyQt5 桌面版
  py hot_board_scanner.py --code 603296,300718   # 指定股票
"""

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd

from stock_full_scan import (
    generate_stock_codes,
    get_stock_raw,
    get_kline_data,
    get_stock_industry,
    get_market_sector,
    calculate_technical_indicators,
)

# ---------------- 全局参数 ----------------
KLINE_DAYS = 120             # K 线回看天数（指标计算 + 显示）
MAX_WORKERS = 30             # 并发数

# 各维度阈值（与策略原文一一对应，可调）
HOT_BOARD_TOP_N = 3          # 板块热度：取涨幅榜前 N 个板块视为热门
HOT_BOARD_ZT_WEIGHT = 3      # 板块排名 = 平均涨幅排序 + 涨停数加权（涨停越多越靠前）
ZDF_MIN = 3.0                # 涨幅范围下限 %
ZDF_MAX = 5.0                # 涨幅范围上限 %
CIRC_MCAP_MIN = 50.0         # 流通市值下限（亿）
CIRC_MCAP_MAX = 300.0        # 流通市值上限（亿）
TURNOVER_MIN = 3.0           # 换手率下限 %
TURNOVER_MAX = 10.0          # 换手率上限 %
VOL_RATIO_MIN = 1.5          # 量比下限
KDJ_OVERSOLD = 20.0          # KDJ 超卖阈值（K<20）
NEAR_HIGH_PCT = 0.3          # 分时形态：现价距日内高点 ≤ 此% 视为「接近新高」
INTRADAY_DIP_MAX = 0.0       # 分时形态：现价相对分时均价的回踩幅度上限（≥0 即不破均价）


# ---------------- 涨停判定（复用 leader_scanner 逻辑）----------------
def is_limit_up(zdf, code):
    """涨停判定：科创板/创业板 20%，主板 10%（宽松用 9.6/19.6）。"""
    if code.startswith(('300', '301', '688')):
        return zdf >= 19.6
    return zdf >= 9.6


# ---------------- KDJ 计算 ----------------
def calc_kdj(df, n=9):
    """计算 KDJ。返回 (k_now, d_now, j_now, k_prev, d_prev)。
    k_prev/d_prev 为前一日的 K/D，用于判断「拐头向上」。"""
    if df is None or len(df) < n:
        return None
    close = df['close'].astype(float)
    low = df['low'].astype(float)
    high = df['high'].astype(float)
    low_n = low.rolling(window=n, min_periods=1).min()
    high_n = high.rolling(window=n, min_periods=1).max()
    rsv = (close - low_n) / (high_n - low_n) * 100
    rsv = rsv.fillna(50)
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    j = 3 * k - 2 * d
    if len(k) < 2:
        return None
    return (float(k.iloc[-1]), float(d.iloc[-1]), float(j.iloc[-1]),
            float(k.iloc[-2]), float(d.iloc[-2]))


# ---------------- 分时形态评估（近似代理）----------------
def evaluate_intraday(stock):
    """分时形态近似评估。

    策略原文要求「14:40 前后创日内新高后小幅回踩不破分时均价线」。
    盘中 1 分钟数据（mootdx TCP 7709 / 腾讯分时）在公司网络均不可达，
    因此用实时行情字段做近似代理：
      - 分时均价(VWAP) = 成交额(元) / 成交量(股)
      - 「创日内新高」近似为：现价接近当日最高价（距高点 ≤ NEAR_HIGH_PCT%）
        —— 若盘中曾创新高后回踩，现价仍应接近高点；若已远离高点则视为跳水。
      - 「不破分时均价」近似为：现价 ≥ VWAP（回踩未跌破均价）
      - 「诱多跳水」识别：现价远低于日内高点（>1%）且跌破均价 → 判 False

    返回 (pass_bool, meta_str, vwap, near_high_pct, dip_vs_vwap)
    其中 dip_vs_vwap = (现价 - VWAP)/VWAP*100，正=在均价之上。
    """
    now = float(stock.get('now', 0))
    high = float(stock.get('high', now))
    volume_hand = float(stock.get('volume', 0))      # 成交量(手)
    amount_yuan = float(stock.get('amount', 0))      # 成交额(元)
    if now <= 0 or high <= 0 or volume_hand <= 0 or amount_yuan <= 0:
        return None
    # VWAP = 成交额 / 成交量(股) ；成交量(手) × 100 = 股
    vwap = amount_yuan / (volume_hand * 100)
    if vwap <= 0:
        return None
    near_high = (high - now) / high * 100   # 现价距高点的幅度%（正=在高点之下）
    dip_vs_vwap = (now - vwap) / vwap * 100  # 现价相对均价的偏移%

    # 近似判定：接近高点 + 不破均价
    ok = (near_high <= NEAR_HIGH_PCT) and (dip_vs_vwap >= INTRADAY_DIP_MAX)
    # 诱多跳水：远离高点 且 跌破均价 → 强制判 False（即便 near_high 误判）
    if near_high > 1.0 and dip_vs_vwap < -0.2:
        ok = False
    meta = f"距高{near_high:.2f}%/均价{vwap:.2f}({dip_vs_vwap:+.2f}%)"
    return (ok, meta, vwap, near_high, dip_vs_vwap)


# ---------------- 单股特征计算 ----------------
def compute_features(code):
    """计算单只股票的全部维度特征。返回 dict 或 None（数据不足）。
    注意：板块热度需全市场扫描后聚合，此处先填该股所属概念，
    由 scan_market 在汇总阶段回填板块热度字段（hot_board / board_rank / board_zt_n）。
    """
    stock = get_stock_raw(code)
    if not stock or stock.get('volume', 0) <= 0:
        return None

    df = get_kline_data(code, days=KLINE_DAYS)
    if df is None or len(df) < 35:    # MACD 需要 26+9，KDJ 需要 9
        return None

    # 基础字段
    name = stock.get('name', '')
    sector = stock.get('sector', get_market_sector(code))
    now = float(stock['now'])
    zdf = float(stock['zdf'])
    turnover = float(stock.get('turnover', 0))         # 换手率 %
    circ_mc_yi = float(stock.get('circ_market_cap', 0)) / 1e8   # 流通市值(亿)
    amount = float(stock.get('amount', 0))

    closes = df['close'].astype(float).values
    vols = df['volume'].astype(float).values
    n = len(df)
    ma5 = float(closes[-5:].mean()) if n >= 5 else now
    # 量比：今日成交量 / 昨日成交量（stock_full_scan 的 volume_ratio 字段恒为 0，
    # 用 K 线最近两根成交量自行计算，与 process_stock 一致）
    if n >= 2 and vols[-2] > 0:
        vol_ratio = float(vols[-1]) / float(vols[-2])
    else:
        vol_ratio = 0.0

    # 技术指标（MACD 复用 stock_full_scan）
    tech = calculate_technical_indicators(df) or {}
    dif_now = float(tech.get('MACD_DIF', 0))
    dea_now = float(tech.get('MACD_DEA', 0))
    macd_bar_now = float(tech.get('MACD柱', 0))
    macd_bar_prev = float(macd_bar_prev_calc(df))   # 见下方函数
    dif_prev = float(dif_prev_calc(df))

    # KDJ
    kdj = calc_kdj(df)
    if kdj is None:
        return None
    k_now, d_now, j_now, k_prev, d_prev = kdj

    # 分时形态
    intraday = evaluate_intraday(stock)
    if intraday is None:
        intraday_ok, intraday_meta, vwap, near_high, dip_vs_vwap = False, '数据不足', 0, 0, 0
    else:
        intraday_ok, intraday_meta, vwap, near_high, dip_vs_vwap = intraday

    # 行业 / 概念
    ind = get_stock_industry(code) or {}
    industry = ind.get('同花顺行业', '') or ind.get('行业', '')
    concept_list = ind.get('概念列表', []) or []
    concept = ind.get('最相关概念', '') or (concept_list[0] if concept_list else '')
    concept_str = ','.join(concept_list) if concept_list else concept

    # ---- 逐项条件 ----
    conds = {}
    meta = {}

    # 涨幅范围
    conds['涨幅范围'] = (ZDF_MIN <= zdf <= ZDF_MAX)
    meta['涨幅范围'] = f"{zdf:+.2f}%"

    # 流动性：流通市值 + 换手率 + 量比（三项同时满足）
    liq_mc = CIRC_MCAP_MIN <= circ_mc_yi <= CIRC_MCAP_MAX
    liq_turn = TURNOVER_MIN <= turnover <= TURNOVER_MAX
    liq_vr = vol_ratio > VOL_RATIO_MIN
    conds['流动性'] = liq_mc and liq_turn and liq_vr
    meta['流动性'] = f"市值{circ_mc_yi:.0f}亿/换手{turnover:.2f}%/量比{vol_ratio:.2f}"

    # 技术形态：站稳5日均线 + MACD柱绿转红且DIF上穿DEA + KDJ超卖区K<20拐头向上
    # 站稳5日均线
    stand_ma5 = now > ma5
    # MACD柱由绿转红：前日柱<=0 且 当日柱>0
    macd_red_turn = (macd_bar_prev <= 0) and (macd_bar_now > 0)
    # DIF上穿DEA：前日 DIF<=前日 DEA 且 当日 DIF>当日 DEA（金叉）
    dea_prev = _dea_prev_calc(df)
    dif_cross_dea = (dif_prev <= dea_prev) and (dif_now > dea_now)
    # KDJ超卖区K<20 且 拐头向上（K今 > K昨）
    kdj_oversold_turn = (k_now < KDJ_OVERSOLD) and (k_now > k_prev)
    conds['技术形态'] = stand_ma5 and macd_red_turn and dif_cross_dea and kdj_oversold_turn
    meta['技术形态'] = f"MA5{ma5:.2f}/柱{macd_bar_prev:.2f}->{macd_bar_now:.2f}/K{k_now:.1f}"

    # 分时形态（近似代理）
    conds['分时形态'] = intraday_ok
    meta['分时形态'] = intraday_meta

    # 板块热度：占位，scan_market 聚合后回填
    conds['板块热度'] = False
    meta['板块热度'] = '待聚合'

    pass_all = all(conds.values())
    fail_n = sum(1 for v in conds.values() if not v)

    return {
        'code': code,
        'name': name,
        'sector': sector,
        'close': now,
        'zdf': zdf,
        'amount': amount,
        'turnover': turnover,
        'circ_mc_yi': round(circ_mc_yi, 2),
        'vol_ratio': round(vol_ratio, 2),
        'vwap': round(vwap, 2) if vwap else 0,
        'industry': industry,
        'concept': concept_str,
        'concept_list': concept_list,
        'is_zt': is_limit_up(zdf, code),
        'conds': conds,
        'meta': meta,
        'pass_all': pass_all,
        'fail_n': fail_n,
        # 板块聚合字段（待回填）
        'hot_board': '',
        'board_rank': 0,
        'board_zt_n': 0,
        'board_avg_zdf': 0.0,
    }


# MACD 前日柱 / 前日 DIF / 前日 DEA 辅助计算
def macd_bar_prev_calc(df):
    """前一日 MACD 柱。"""
    if df is None or len(df) < 35:
        return 0.0
    close = df['close'].astype(float)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    macd = (dif - dea) * 2
    return float(macd.iloc[-2]) if len(macd) >= 2 else 0.0


def dif_prev_calc(df):
    """前一日 DIF。"""
    if df is None or len(df) < 35:
        return 0.0
    close = df['close'].astype(float)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    return float(dif.iloc[-2]) if len(dif) >= 2 else 0.0


def _dea_prev_calc(df):
    """前一日 DEA。"""
    if df is None or len(df) < 35:
        return 0.0
    close = df['close'].astype(float)
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    return float(dea.iloc[-2]) if len(dea) >= 2 else 0.0


# 条件中文名（表格展示顺序）
COND_NAMES = ['板块热度', '涨幅范围', '流动性', '技术形态', '分时形态']


# ---------------- 板块热度聚合 ----------------
def aggregate_hot_boards(results, top_n=HOT_BOARD_TOP_N):
    """全市场扫描后聚合板块热度，回填到每只股票。

    做法：
      1. 按概念列表的第一个概念做板块分组（兜底用行业）；
      2. 每个板块算：平均涨幅 + 涨停个股数；
      3. 板块排名 = 平均涨幅降序，涨停数作加权参考（涨停越多越靠前）；
      4. 取前 top_n 个板块视为「热门板块」；
      5. 属于热门板块的股票，其 conds['板块热度'] 置 True，回填 meta。
    """
    if not results:
        return

    # 1. 分组
    board_members = defaultdict(list)
    for r in results:
        concepts = r.get('concept_list', []) or []
        if isinstance(concepts, str):
            concepts = [c.strip() for c in concepts.split(';') if c.strip()]
        board = concepts[0] if concepts else r.get('industry', '')
        if not board:
            board = '未分类'
        board_members[board].append(r)

    # 2. 每板块统计
    board_stat = {}
    for board, members in board_members.items():
        avg_zdf = sum(m['zdf'] for m in members) / len(members)
        zt_n = sum(1 for m in members if m.get('is_zt'))
        board_stat[board] = {
            'avg_zdf': avg_zdf,
            'zt_n': zt_n,
            'count': len(members),
        }

    # 3. 排名：平均涨幅降序 + 涨停数加权（涨停数×0.5 作为加分，避免纯靠涨幅）
    ranked = sorted(board_stat.items(),
                    key=lambda kv: (kv[1]['avg_zdf'] + kv[1]['zt_n'] * 0.5),
                    reverse=True)
    hot_boards = set(b for b, _ in ranked[:top_n])

    # 4. 回填
    for r in results:
        concepts = r.get('concept_list', []) or []
        if isinstance(concepts, str):
            concepts = [c.strip() for c in concepts.split(';') if c.strip()]
        board = concepts[0] if concepts else r.get('industry', '')
        if not board:
            board = '未分类'
        st = board_stat.get(board, {'avg_zdf': 0, 'zt_n': 0})
        r['hot_board'] = board
        r['board_rank'] = next((i + 1 for i, (b, _) in enumerate(ranked) if b == board), 0)
        r['board_zt_n'] = st['zt_n']
        r['board_avg_zdf'] = round(st['avg_zdf'], 2)
        is_hot = board in hot_boards
        r['conds']['板块热度'] = is_hot
        r['meta']['板块热度'] = f"#{r['board_rank']} {board}(涨停{st['zt_n']})"
        # 重算 pass_all / fail_n
        r['pass_all'] = all(r['conds'].values())
        r['fail_n'] = sum(1 for v in r['conds'].values() if not v)

    return hot_boards


# ============ 单股扫描 ============

def scan_one(code, require_all=True):
    """扫描单只股票。require_all=True 时只返回全部条件成立的股票。"""
    try:
        r = compute_features(code)
        if r is None:
            return None
        if require_all:
            # 板块热度需聚合，单股模式无法判定 → 退化为只看其余 4 项
            other_ok = all(v for k, v in r['conds'].items() if k != '板块热度')
            if not other_ok:
                return None
        return r
    except Exception:
        return None


# ============ 全市场扫描 ============

def scan_market(codes=None, max_workers=MAX_WORKERS, on_progress=None,
                stop_check=None, require_all=True, days=None):
    """全市场扫描。codes=None 时扫全市场。
    流程：先并发拉取所有股票特征 → 聚合板块热度 → 按 require_all 过滤排序。
    """
    if codes is None:
        codes = generate_stock_codes()

    results = []
    total = len(codes)
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(compute_features, c): c for c in codes}
        try:
            for fut in as_completed(futures):
                if stop_check and stop_check():
                    break
                done += 1
                if on_progress:
                    on_progress(done, total)
                try:
                    r = fut.result()
                except Exception:
                    r = None
                if r:
                    results.append(r)
        finally:
            for f in futures:
                f.cancel()
            ex.shutdown(wait=False, cancel_futures=True)

    # 聚合板块热度（回填到 results）
    aggregate_hot_boards(results)

    # 过滤
    if require_all:
        results = [r for r in results if r['pass_all']]

    # 排序：板块排名升序（越热越前）+ 涨幅降序
    results.sort(key=lambda x: (x.get('board_rank', 999), -x['zdf']))
    return results


# ============ 命令行入口 ============

def print_results(results, top=50):
    print(f"\n{'=' * 110}")
    print(f"尾盘主力承接多维量化扫描结果 | 命中 {len(results)} 只 | 显示前 {min(top, len(results))} 只")
    print(f"{'=' * 110}")
    print(f"{'代码':<8}{'名称':<10}{'现价':>8}{'涨跌%':>8}{'流通市值':>10}{'热门板块':<14}概念")
    print('-' * 110)
    for r in results[:top]:
        mc = f"{r['circ_mc_yi']:.0f}亿"
        print(f"{r['code']:<8}{r['name']:<10}{r['close']:>8.2f}{r['zdf']:>8.2f}  "
              f"{mc:>10}  {r.get('hot_board',''):<14}{r['concept'][:30]}")
    print('-' * 110)


def main():
    parser = argparse.ArgumentParser(description='尾盘主力承接 多维量化选股扫描器')
    parser.add_argument('--code', help='指定股票代码（逗号分隔），不指定则全市场扫描')
    parser.add_argument('--top', type=int, default=50, help='显示前N只（默认50）')
    parser.add_argument('--gui', action='store_true', help='启动 PyQt5 桌面版')
    args = parser.parse_args()

    if args.gui:
        try:
            from hot_board_gui import run_gui
            run_gui()
        except ImportError:
            print("GUI 模块未安装，使用命令行模式")
            return
        return

    codes = args.code.split(',') if args.code else None
    print(f"开始扫描... {'指定股票' if codes else '全市场'}")

    def progress(d, t):
        if d % 200 == 0:
            print(f"  进度: {d}/{t} ({d * 100 // t}%)", end='\r')

    results = scan_market(codes=codes, on_progress=progress, require_all=not args.code)
    print_results(results, top=args.top)

    if not codes and results:
        out = 'hot_board_results.csv'
        rows = []
        for r in results:
            row = {
                'code': r['code'], 'name': r['name'], 'sector': r['sector'],
                'close': r['close'], 'zdf': round(r['zdf'], 2),
                'circ_mc_yi': r['circ_mc_yi'], 'turnover': r['turnover'],
                'vol_ratio': r['vol_ratio'], 'vwap': r['vwap'],
                'industry': r['industry'], 'concept': r['concept'],
                'hot_board': r['hot_board'], 'board_rank': r['board_rank'],
                'board_zt_n': r['board_zt_n'], 'board_avg_zdf': r['board_avg_zdf'],
                'fail_n': r['fail_n'], 'pass_all': r['pass_all'],
            }
            for cn in COND_NAMES:
                row[f'条件_{cn}'] = int(r['conds'].get(cn, False))
                row[f'依据_{cn}'] = r['meta'].get(cn, '')
            rows.append(row)
        pd.DataFrame(rows).to_csv(out, index=False, encoding='utf-8-sig')
        print(f"\n结果已导出: {out}")


if __name__ == '__main__':
    main()
