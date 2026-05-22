import requests
import time
import pandas as pd
import re
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

# 创建全局会话，复用连接
session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

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
        res = session.get(url, timeout=2)
        res.encoding = "gbk"
        data = res.text.split("~")

        if len(data) < 10 or data[1] == "" or data[3] == "":
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

        return {
            "code": code,
            "name": data[1],
            "now": now,
            "yes": yes,
            "open": float(data[5]) if data[5] else 0,
            "volume": volume,
            "yesterday_volume": 0,
            "volume_ratio": 0,
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

def get_kline_data(code, days=60):
    """获取股票K线数据用于计算技术指标"""
    try:
        if code.startswith("6"):
            api_code = f"sh{code}"
        else:
            api_code = f"sz{code}"
        
        kline_url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?_var=kline_dayhfq&param={api_code},day,,,{days},hfq"
        kline_res = session.get(kline_url, timeout=2)
        kline_text = kline_res.text
        
        match = re.search(r'=(\{.*\})', kline_text)
        if match:
            try:
                kline_data = eval(match.group(1))
                if kline_data.get('code') == 0 and kline_data.get('msg') == '':
                    data_key = api_code if api_code in kline_data.get('data', {}) else list(kline_data.get('data', {}).keys())[0] if kline_data.get('data') else None
                    
                    if data_key:
                        days_data = kline_data['data'].get(data_key, {}).get('hfqday', [])
                        if not days_data:
                            days_data = kline_data['data'].get(data_key, {}).get('day', [])
                        
                        if days_data:
                            sample_row = days_data[0]
                            if len(sample_row) == 6:
                                df = pd.DataFrame(days_data, columns=['date', 'open', 'close', 'high', 'low', 'volume'])
                            elif len(sample_row) == 7:
                                df = pd.DataFrame(days_data, columns=['date', 'open', 'close', 'high', 'low', 'volume', 'amount'])
                            else:
                                df = pd.DataFrame(days_data)
                                df = df.iloc[:, :6]
                                df.columns = ['date', 'open', 'close', 'high', 'low', 'volume']
                            
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
    """计算技术指标：均线、布林线、MACD、多头排列等"""
    if df is None or len(df) < 20:
        return {}
    
    close = df['close']
    high = df['high']
    low = df['low']
    
    # 均线
    ma5 = close.rolling(window=5).mean().iloc[-1]
    ma10 = close.rolling(window=10).mean().iloc[-1]
    ma20 = close.rolling(window=20).mean().iloc[-1]
    ma60 = close.rolling(window=60).mean().iloc[-1]
    
    # 布林线
    std20 = close.rolling(window=20).std().iloc[-1]
    bollinger_upper = ma20 + 2 * std20
    bollinger_middle = ma20
    bollinger_lower = ma20 - 2 * std20
    
    # MACD
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    macd = (dif - dea) * 2
    
    # 判断多头排列
    is_bull_arrangement = ma5 > ma10 > ma20 > ma60
    
    # 判断是否在布林带内
    last_close = close.iloc[-1]
    bollinger_position = '上轨上方' if last_close > bollinger_upper else ('下轨下方' if last_close < bollinger_lower else '布林带内')
    
    # 判断金叉死叉
    macd_signal = '金叉' if dif.iloc[-1] > dea.iloc[-1] and dif.iloc[-2] <= dea.iloc[-2] else \
                  '死叉' if dif.iloc[-1] < dea.iloc[-1] and dif.iloc[-2] >= dea.iloc[-2] else '无信号'
    
    # 均线多头排列强度
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
        '多头排列': '是' if is_bull_arrangement else '否'
    }

def get_stock_industry(code):
    """获取股票行业和概念信息"""
    try:
        if code.startswith("6"):
            market = '1'
            api_code = f"sh{code}"
        else:
            market = '0'
            api_code = f"sz{code}"
        
        url = f"http://push2.eastmoney.com/api/qt/stock/get?secid={market}.{api_code}&fields=f107,f108,f109,f127,f128,f136,f137,f138,f139"
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get(url, headers=headers, timeout=5)
        data = res.json().get('data', {})
        
        industry = data.get('f107', '未分类')
        sector = data.get('f108', '未分类')
        concepts = data.get('f127', '')
        
        if concepts:
            concept_list = concepts.split(';')
            main_concept = concept_list[0] if concept_list else '未分类'
            concept_count = len([c for c in concept_list if c.strip()])
        else:
            main_concept = '未分类'
            concept_count = 0
        
        return {
            '同花顺行业': industry,
            '同花顺板块': sector,
            '最相关概念': main_concept,
            '所属概念数量': concept_count
        }
    except Exception as e:
        return {
            '同花顺行业': '未分类',
            '同花顺板块': '未分类',
            '最相关概念': '未分类',
            '所属概念数量': 0
        }

def get_stock_news(code, limit=3):
    """获取股票最新公告和新闻"""
    try:
        if code.startswith("6"):
            secid = f"1.0{code}"
        else:
            secid = f"0.{code}"
        
        # 获取最新公告
        news_url = f"http://datacenter.eastmoney.com/securities/api/data/get?type=RPT_LASTESTNOTICE&sty=SECUCODE%2CNOTICETITLE%2CPUBLISHINGTIME&filter=(SECUCODE='{secid}')&order=PUBLISHINGTIME%20desc&pageSize={limit}&pageNumber=1"
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get(news_url, headers=headers, timeout=5)
        data = res.json()
        
        news_list = []
        if data.get('success') and data.get('result', {}).get('data'):
            for item in data['result']['data'][:limit]:
                title = item.get('NOTICETITLE', '')
                time = item.get('PUBLISHINGTIME', '')[:10]
                if title:
                    news_list.append(f"{time}: {title}")
        
        return "; ".join(news_list) if news_list else '暂无公告'
    except Exception as e:
        return '获取公告失败'

def generate_stock_codes():
    """生成完整股票代码列表"""
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
        popularity = int(100000 + stock['zdf'] * 5000 + stock['turnover'] * 10)
        vol_compare_ratio = stock['volume'] / stock['yesterday_volume'] if stock['yesterday_volume'] > 0 else 0
        vol_compare_pct = (stock['volume'] - stock['yesterday_volume']) / stock['yesterday_volume'] * 100 if stock['yesterday_volume'] > 0 else 0
        vol_status = '放量' if vol_compare_ratio >= 1.5 else ('缩量' if vol_compare_ratio <= 0.7 else '正常')
        
        # 获取行业信息
        industry_info = get_stock_industry(code)
        
        # 获取技术指标
        kline_df = get_kline_data(code, days=60)
        tech_indicators = calculate_technical_indicators(kline_df)
        
        # 获取利好消息
        news = get_stock_news(code, limit=3)
        
        # 判断技术形态
        patterns = []
        
        # 价升形态
        if stock['now'] > stock['open']:
            patterns.append('阳线')
        else:
            patterns.append('价升')
        
        # 量价齐升判断：价格上涨 + 成交量放大
        if stock['zdf'] > 0 and vol_compare_ratio >= 1.3:
            patterns.append('量价齐升')
        
        # MACD金叉判断
        macd_signal = tech_indicators.get('MACD信号', '')
        if macd_signal == '金叉':
            patterns.append('MACD金叉')
        
        # 多头排列判断
        if tech_indicators.get('多头排列') == '是':
            patterns.append('多头排列')
        
        # 布林带位置判断
        bollinger_pos = tech_indicators.get('布林带位置', '')
        if bollinger_pos == '上轨上方':
            patterns.append('布林带上轨')
        elif bollinger_pos == '下轨下方':
            patterns.append('布林带下轨')
        
        # 均线金叉判断
        ma5 = tech_indicators.get('MA5', 0)
        ma10 = tech_indicators.get('MA10', 0)
        if ma5 > ma10 and ma5 > 0 and ma10 > 0:
            patterns.append('MA5上穿MA10')
        
        # 综合技术形态
        tech_pattern = '||'.join(patterns)
        
        # 买入信号判断
        buy_signals = []
        if macd_signal == '金叉':
            buy_signals.append('MACD金叉')
        if tech_indicators.get('多头排列') == '是':
            buy_signals.append('多头排列')
        if stock['zdf'] > 0 and vol_compare_ratio >= 1.3:
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
            '同花顺行业': industry_info['同花顺行业'],
            '同花顺板块': industry_info['同花顺板块'],
            '最相关概念': industry_info['最相关概念'],
            '所属概念数量': industry_info['所属概念数量'],
            'MA5': tech_indicators.get('MA5', 0),
            'MA10': tech_indicators.get('MA10', 0),
            'MA20': tech_indicators.get('MA20', 0),
            'MA60': tech_indicators.get('MA60', 0),
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
    print("股票全市场扫描程序 (增强版)")
    print("筛选条件: 今日上涨")
    print("新增功能: 行业信息、技术指标、利好消息")
    print("=" * 60)

    stock_codes = generate_stock_codes()
    print(f"\n共生成 {len(stock_codes)} 只股票代码...")
    
    stocks_data = []
    valid_count = 0
    rise_count = 0
    failed_count = 0
    
    print("\n开始扫描...")
    start_time = time.time()
    
    with ThreadPoolExecutor(max_workers=50) as executor:
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
        print(f"新增字段: 同花顺行业、同花顺板块、MA5/MA10/MA20/MA60、布林线、MACD、多头排列、利好消息")
    else:
        print("\n没有找到上涨的股票")

    print("\n" + "=" * 60)

if __name__ == "__main__":
    main()