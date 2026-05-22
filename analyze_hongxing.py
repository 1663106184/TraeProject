import requests
import pandas as pd
import re
import json

# 获取60天K线数据
url = 'http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?_var=kline_dayhfq&param=sh600367,day,,,60,hfq'
headers = {'User-Agent': 'Mozilla/5.0'}
res = requests.get(url, headers=headers, timeout=10)

text = res.text
match = re.search(r'=(\{.*\})', text)
data = json.loads(match.group(1))
days = data['data']['sh600367']['hfqday']

df = pd.DataFrame(days, columns=['date', 'open', 'close', 'high', 'low', 'volume'])
for col in ['open', 'close', 'high', 'low', 'volume']:
    df[col] = pd.to_numeric(df[col])

df['MA5'] = df['close'].rolling(5).mean()
df['MA10'] = df['close'].rolling(10).mean()
df['MA20'] = df['close'].rolling(20).mean()
df['MA60'] = df['close'].rolling(60).mean()

df['EMA12'] = df['close'].ewm(span=12, adjust=False).mean()
df['EMA26'] = df['close'].ewm(span=26, adjust=False).mean()
df['MACD_DIF'] = df['EMA12'] - df['EMA26']
df['MACD_DEA'] = df['MACD_DIF'].ewm(span=9, adjust=False).mean()
df['MACD_HIST'] = (df['MACD_DIF'] - df['MACD_DEA']) * 2

last = df.iloc[-1]
prev = df.iloc[-2]

# 实时价格
real_time = requests.get('http://qt.gtimg.cn/q=sh600367', headers=headers, timeout=5)
real_time.encoding = 'gbk'
rt_data = real_time.text.split('~')

print('=' * 60)
print('红星发展 (600367) MACD趋势共振分析')
print('=' * 60)

print()
print('【一、实时行情】')
print('  名称:', rt_data[1])
print('  现价:', rt_data[3], '元')
print('  昨收:', rt_data[4], '元')
print('  涨跌:', '{:+.2f}'.format(float(rt_data[3])-float(rt_data[4])), '元')
print('  涨幅:', '{:+.2f}'.format((float(rt_data[3])-float(rt_data[4]))/float(rt_data[4])*100), '%')
print('  成交量:', int(rt_data[6]), '手')

print()
print('【二、趋势方向】')
print('  MA5:', round(last['MA5'], 2))
print('  MA10:', round(last['MA10'], 2))
print('  MA20:', round(last['MA20'], 2))
print('  MA60:', round(last['MA60'], 2))

ma60_dir = '向上' if last['MA60'] > prev['MA60'] else '向下'
print('  60日线方向:', ma60_dir)

price_vs_ma60 = (float(last['close']) / float(last['MA60']) - 1) * 100 if last['MA60'] > 0 else 0
print('  股价相对60日线:', '{:+.2f}%'.format(price_vs_ma60))

if pd.notna(last['MA5']) and pd.notna(last['MA10']) and pd.notna(last['MA20']):
    if last['MA5'] > last['MA10'] > last['MA20']:
        print('  均线结构: 多头排列')
    else:
        print('  均线结构: 非多头')

print()
print('【三、MACD分析】')
dif = last['MACD_DIF']
dea = last['MACD_DEA']
dif_prev = prev['MACD_DIF']
dea_prev = prev['MACD_DEA']
print('  DIF:', '{:.4f}'.format(dif))
print('  DEA:', '{:.4f}'.format(dea))
print('  MACD柱状:', '{:.4f}'.format(last['MACD_HIST']))

if dif > dea:
    print('  MACD状态: 金叉区域')
else:
    print('  MACD状态: 死叉区域')

if dif > 0:
    print('  MACD位置: 0轴上方')
else:
    print('  MACD位置: 0轴下方')

if dif_prev < dea_prev and dif > dea:
    print('  近期发生金叉: 是')
elif dif_prev > dea_prev and dif < dea:
    print('  近期发生死叉: 是')

print()
print('【四、综合评级】')
trend_score = 0
macd_score = 0

if ma60_dir == '向上': trend_score += 20
if price_vs_ma60 > 0: trend_score += 10
if pd.notna(last['MA5']) and pd.notna(last['MA10']) and pd.notna(last['MA20']):
    if last['MA5'] > last['MA10'] > last['MA20']:
        trend_score += 10

if dif > 0: macd_score += 15
if dif > dea: macd_score += 10
if dif_prev < dea_prev and dif > dea: macd_score += 10

total = trend_score + macd_score + 25
print('  趋势得分:', trend_score, '/40')
print('  MACD得分:', macd_score, '/35')
print('  总分:', total, '/100')

if total >= 85:
    rating = 'EXECUTE (强烈关注)'
elif total >= 70:
    rating = 'LIGHT (轻仓试错)'
elif total >= 55:
    rating = 'OBSERVE (观察)'
else:
    rating = 'AVOID (回避)'
print('  评级:', rating)

print()
print('【五、买卖建议】')
print('  入场触发: 回调至MA20({:.2f})附近企稳'.format(last['MA20']))
print('  止损位: 跌破MA60({:.2f})'.format(last['MA60']))
print('  目标位: 前高{:.2f}'.format(df['high'].tail(20).max()))

print()
print('【六、近5日走势】')
for i in range(-5, 0):
    row = df.iloc[i]
    prev_close = float(df.iloc[i-1]['close'])
    zdf = (float(row['close']) - prev_close) / prev_close * 100
    print('  {}: {:.2f} {:+.2f}%'.format(row['date'], float(row['close']), zdf))
