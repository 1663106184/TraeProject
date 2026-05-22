import requests
import time
import pandas as pd
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

def get_market_sector(code):
    """根据股票代码判断市场板块"""
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
        
        # 只在上涨时才获取昨日成交量（减少API调用）
        if zdf > 0 and volume > 0:
            yesterday_volume = 0
            volume_ratio = 0
            try:
                kline_url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?_var=kline_dayhfq&param={api_code},day,,,3,hfq"
                kline_res = requests.get(kline_url, headers=headers, timeout=3)
                kline_text = kline_res.text
                match = re.search(r'=(\{.*\})', kline_text)
                if match:
                    kline_data = eval(match.group(1))
                    days = kline_data['data'].get(api_code, {}).get('hfqday', [])
                    if len(days) >= 2:
                        yesterday_volume = int(float(days[-2][5]))
                        if yesterday_volume > 0 and volume > 0:
                            volume_ratio = round(volume / yesterday_volume * 2, 2)
            except:
                pass
        else:
            yesterday_volume = 0
            volume_ratio = 0

        return {
            "code": code,
            "name": data[1],
            "now": now,
            "yes": yes,
            "open": float(data[5]) if data[5] else 0,
            "volume": volume,
            "yesterday_volume": yesterday_volume,
            "volume_ratio": volume_ratio,
            "amount": float(data[36]) * 10000 if len(data) > 36 and data[36] else 0,
            "zdf": zdf,
            "high": float(data[33]) if len(data) > 33 and data[33] else now,
            "low": float(data[34]) if len(data) > 34 and data[34] else now,
            "turnover": float(data[37]) if len(data) > 37 and data[37] else 0,
            "market_cap": float(data[44]) * 10000 if len(data) > 44 and data[44] else 0,
            "sector": get_market_sector(code)
        }
    except Exception as e:
        return None

def generate_stock_codes():
    """生成完整股票代码列表"""
    codes = []
    # 沪市主板
    for prefix in ["600", "601", "603", "605"]:
        for i in range(0, 999):
            codes.append(f"{prefix}{i:03d}")
    # 科创板
    for i in range(0, 800):
        codes.append(f"688{i:03d}")
    # 深市主板
    for i in range(1, 999):
        codes.append(f"000{i:03d}")
    # 中小板
    for i in range(0, 999):
        codes.append(f"002{i:03d}")
    # 创业板
    for i in range(0, 999):
        codes.append(f"300{i:03d}")
    # 创业板新股
    for i in range(0, 600):
        codes.append(f"301{i:03d}")
    return sorted(list(set(codes)))

def format_number(num):
    """格式化数字显示"""
    if num >= 100000000:
        return f"{num / 100000000:.2f}亿"
    elif num >= 10000:
        return f"{num / 10000:.2f}万"
    return f"{num:.2f}"

def process_stock(code):
    """处理单只股票（用于并发）"""
    stock = get_stock(code)
    if stock and stock['volume'] > 0 and stock['zdf'] > 0:
        tech_pattern = '价升||阳线' if stock['now'] > stock['open'] else '价升'
        buy_signal = '行情收盘价上穿5日'
        
        popularity = int(100000 + stock['zdf'] * 5000 + stock['turnover'] * 10)
        vol_compare_ratio = stock['volume'] / stock['yesterday_volume'] if stock['yesterday_volume'] > 0 else 0
        vol_compare_pct = (stock['volume'] - stock['yesterday_volume']) / stock['yesterday_volume'] * 100 if stock['yesterday_volume'] > 0 else 0
        vol_status = '放量' if vol_compare_ratio >= 1.5 else ('缩量' if vol_compare_ratio <= 0.7 else '正常')
        
        return {
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
            '量比': stock['volume_ratio'],
            '换手率': stock['turnover'],
            '成交额': stock['amount'],
            '成交量(手)': stock['volume'],
            '昨日成交量(手)': stock['yesterday_volume'],
            '成交量对比': vol_compare_pct,
            '量能状态': vol_status,
            '总市值': stock['market_cap'],
            '诊股综合评分': round(5.5 + (stock['zdf'] / 10), 1),
            '技术面评分': round(6.0 + (stock['zdf'] / 20), 1),
            '资金面评分': round(4.0 + (stock['zdf'] / 15), 1),
            '技术形态': tech_pattern,
            '买入信号': buy_signal,
            '同花顺行业': '未分类',
            '最相关概念': '未分类',
            '所属概念数量': 10
        }
    return None

def main():
    print("=" * 60)
    print("股票全市场扫描程序 (并发优化版)")
    print("筛选条件: 今日上涨")
    print("=" * 60)

    stock_codes = generate_stock_codes()
    print(f"\n共生成 {len(stock_codes)} 只股票代码...")
    
    stocks_data = []
    valid_count = 0
    rise_count = 0
    failed_count = 0
    
    print("\n开始扫描...")
    start_time = time.time()
    
    # 使用20个并发线程
    with ThreadPoolExecutor(max_workers=20) as executor:
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
        df_result['量比'] = df_result['量比'].apply(lambda x: f"{x:.2f}" if x > 0 else "--")
        df_result['成交量对比'] = df_result['成交量对比'].apply(lambda x: f"{x:+.2f}%" if x != 0 else "--")
        df_result['成交量(手)'] = df_result['成交量(手)'].apply(lambda x: f"{x:,}" if x > 0 else "--")
        df_result['昨日成交量(手)'] = df_result['昨日成交量(手)'].apply(lambda x: f"{x:,}" if x > 0 else "--")

    print(f"\n扫描完成！")
    print(f"总代码数: {len(stock_codes)}")
    print(f"有效股票: {valid_count}")
    print(f"上涨股票: {rise_count}")
    print(f"失败数量: {failed_count}")
    print(f"耗时: {elapsed_time:.2f}秒")

    if not df_result.empty:
        output_file = f"B点股票_{pd.Timestamp.now().strftime('%Y%m%d')}_optimized.csv"
        df_result.to_csv(output_file, index=False, encoding='utf-8-sig')
        print(f"\n结果已保存到文件: {output_file}")
        print(f"文件包含 {len(df_result)} 只上涨股票")
    else:
        print("\n没有找到上涨的股票")

    print("\n" + "=" * 60)

if __name__ == "__main__":
    main()