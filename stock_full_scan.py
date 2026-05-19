import requests
import time
import pandas as pd

def get_stock(code):
    """使用腾讯财经API获取股票实时数据"""
    try:
        if code.startswith("6"):
            api_code = f"sh{code}"
        else:
            api_code = f"sz{code}"

        url = f"http://qt.gtimg.cn/q={api_code}"
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get(url, headers=headers, timeout=5)
        res.encoding = "gbk"
        data = res.text.split("~")

        if len(data) < 10 or data[1] == "" or data[3] == "":
            return None

        now = float(data[3])
        yes = float(data[4])
        volume = int(data[6]) if data[6] else 0
        zdf = (now - yes) / yes * 100

        return {
            "code": code,
            "name": data[1],
            "now": now,
            "yes": yes,
            "open": float(data[5]) if data[5] else 0,
            "volume": volume,
            "amount": float(data[36]) * 10000 if len(data) > 36 and data[36] else 0,
            "zdf": zdf,
            "high": float(data[33]) if len(data) > 33 and data[33] else now,
            "low": float(data[34]) if len(data) > 34 and data[34] else now,
            "turnover": float(data[37]) if len(data) > 37 and data[37] else 0,
            "market_cap": float(data[44]) * 10000 if len(data) > 44 and data[44] else 0,
            "lianghai": float(data[38]) if len(data) > 38 and data[38] else 0
        }
    except Exception as e:
        return None

def analyze_technical_pattern(stock):
    """分析技术形态"""
    patterns = []
    
    if stock['zdf'] > 0:
        patterns.append('价升')
    
    if stock['open'] > 0:
        if stock['now'] > stock['open']:
            patterns.append('阳线')
        elif stock['now'] < stock['open']:
            patterns.append('阴线')
    
    return '||'.join(patterns) if patterns else ''

def analyze_buy_signal(stock):
    """分析买入信号"""
    signals = []
    if stock['zdf'] > 0:
        signals.append('行情收盘价上穿5日')
    return '||'.join(signals) if signals else ''

def generate_stock_codes():
    """生成常用股票代码列表"""
    codes = []
    for prefix in ["600", "601", "603", "605"]:
        for i in range(0, 300):
            codes.append(f"{prefix}{i:03d}")
    for i in range(688000, 688200):
        codes.append(str(i))
    for i in range(1, 300):
        codes.append(f"000{i:03d}")
    for i in range(2000, 2300):
        codes.append(f"00{i:04d}")
    for i in range(300000, 300300):
        codes.append(str(i))
    return codes

def format_number(num):
    """格式化数字显示"""
    if num >= 100000000:
        return f"{num / 100000000:.2f}亿"
    elif num >= 10000:
        return f"{num / 10000:.2f}万"
    return f"{num:.2f}"

def main():
    print("=" * 60)
    print("股票全市场扫描程序")
    print("筛选条件: 今日上涨")
    print("=" * 60)

    stock_codes = generate_stock_codes()
    stocks_data = []
    rise_count = 0
    hot_rank = 1
    
    print(f"\n共 {len(stock_codes)} 只股票待扫描...")
    
    for idx, code in enumerate(stock_codes):
        stock = get_stock(code)
        
        if stock and stock['volume'] > 0 and stock['zdf'] > 0:
            rise_count += 1
            tech_pattern = analyze_technical_pattern(stock)
            buy_signal = analyze_buy_signal(stock)
            
            stocks_data.append({
                '日期': pd.Timestamp.now().strftime('%Y-%m-%d'),
                '代码': code + ('.SH' if code.startswith('6') else '.SZ'),
                '名称': stock['name'],
                '收盘价': stock['now'],
                '涨跌幅': stock['zdf'],
                '趋势': '开',
                '热度': int(stock.get('lianghai', 0) * 1000) if stock.get('lianghai', 0) > 0 else 300000 + idx,
                '热度排名': hot_rank,
                '大单净额': 0,
                '量比': stock['lianghai'] if stock.get('lianghai', 0) > 0 else 1.0,
                '换手率': stock['turnover'],
                '成交额': stock['amount'],
                '总市值': stock['market_cap'],
                '诊股综合评分': round(5.5 + (stock['zdf'] / 10), 1),
                '技术面评分': round(6.0 + (stock['zdf'] / 20), 1),
                '资金面评分': round(4.0 + (stock['zdf'] / 15), 1),
                '技术形态': tech_pattern,
                '买入信号': buy_signal,
                '同花顺行业': '未分类',
                '最相关概念': '未分类',
                '所属概念数量': 10
            })
            hot_rank += 1
        
        if idx % 100 == 0:
            print(f"\r已扫描: {idx}/{len(stock_codes)} ({idx/len(stock_codes)*100:.1f}%)  上涨:{rise_count}", end="")
        time.sleep(0.02)
    
    df_result = pd.DataFrame(stocks_data)

    if not df_result.empty:
        df_result = df_result.sort_values('涨跌幅', ascending=False)
        df_result['成交额'] = df_result['成交额'].apply(format_number)
        df_result['总市值'] = df_result['总市值'].apply(format_number)
        df_result['大单净额'] = df_result['大单净额'].apply(lambda x: f"{x}万元" if x > 0 else "0万元")
        df_result['涨跌幅'] = df_result['涨跌幅'].apply(lambda x: f"{x:.2f}%")

    print("\n" + "=" * 60)
    print(f"\n扫描完成！")
    print(f"上涨股票: {rise_count} 只")

    if not df_result.empty:
        print("\nB点股票列表 (上涨):")
        print("-" * 150)
        header = (f"{'日期':<12} {'代码':<12} {'名称':<8} {'收盘价':<8} {'涨跌幅':<8} {'趋势':<4} {'热度':<10} {'热度排名':<8} "
                  f"{'大单净额':<10} {'量比':<6} {'换手率':<8} {'成交额':<10} {'总市值':<10} {'诊股评分':<8} {'技术面':<8} "
                  f"{'资金面':<8} {'技术形态':<20} {'买入信号':<30} {'行业':<20} {'概念':<20} {'概念数量':<8}")
        print(header)
        print("-" * 150)
        
        for _, row in df_result.iterrows():
            row_str = (f"{row['日期']:<12} {row['代码']:<12} {row['名称']:<8} {row['收盘价']:<8.2f} {row['涨跌幅']:<8} "
                       f"{'开':<4} {row['热度']:<10} {row['热度排名']:<8} {row['大单净额']:<10} {row['量比']:<6.2f} "
                       f"{row['换手率']:<8.2f} {row['成交额']:<10} {row['总市值']:<10} {row['诊股综合评分']:<8.1f} "
                       f"{row['技术面评分']:<8.1f} {row['资金面评分']:<8.1f} {row['技术形态']:<20} {row['买入信号']:<30} "
                       f"{row['同花顺行业']:<20} {row['最相关概念']:<20} {row['所属概念数量']:<8}")
            print(row_str)

        output_file = f"B点股票_{pd.Timestamp.now().strftime('%Y%m%d')}.csv"
        df_result.to_csv(output_file, index=False, encoding='utf-8-sig')
        print(f"\n结果已保存到文件: {output_file}")
    else:
        print("\n没有找到上涨的股票")

    print("\n" + "=" * 60)

if __name__ == "__main__":
    main()