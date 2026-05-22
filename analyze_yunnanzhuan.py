import requests
import pandas as pd
import re
import json

url = 'http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?_var=kline_dayhfq&param=sz002428,day,,,60,hfq'
headers = {'User-Agent': 'Mozilla/5.0'}
res = requests.get(url, headers=headers, timeout=10)

text = res.text
match = re.search(r'=(\{.*\})', text)
data = json.loads(match.group(1))
days = data['data']['sz002428']['hfqday']

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

last = df.iloc[-1]
prev = df.iloc[-2]

print('=' * 60)
print('云南锗业 (002428) MACD趋势共振深度分析')
print('=' * 60)

print()
print('【一、趋势方向 - 均线定方向】')
print(f'  现价: {last["close"]:.2f}')
print(f'  5日均线(MA5): {last["MA5"]:.2f}')
print(f'  10日均线(MA10): {last["MA10"]:.2f}')
print(f'  20日均线(MA20): {last["MA20"]:.2f}')
print(f'  60日均线(MA60): {last["MA60"]:.2f}')

ma60_now = last['MA60'] if pd.notna(last['MA60']) else 0
ma60_prev = prev['MA60'] if pd.notna(prev['MA60']) else 0
ma_dir = '向上' if ma60_now > ma60_prev else '向下'
print(f'  60日线方向: {ma_dir}')

price_vs_ma60 = (float(last['close']) / ma60_now - 1) * 100 if ma60_now > 0 else 0
print(f'  股价相对60日线位置: {price_vs_ma60:+.2f}%')

if pd.notna(last['MA5']) and pd.notna(last['MA10']) and pd.notna(last['MA20']):
    if last['MA5'] > last['MA10'] > last['MA20']:
        print('  均线结构: 多头排列')
    else:
        print('  均线结构: 非多头排列')

print()
print('【二、MACD节奏 - MACD定节奏】')
dif = last['MACD_DIF']
dea = last['MACD_DEA']
dif_prev = prev['MACD_DIF']
dea_prev = prev['MACD_DEA']
print(f'  DIF: {dif:.4f}')
print(f'  DEA: {dea:.4f}')

if dif > dea:
    print('  MACD状态: 金叉区域 (DIF > DEA)')
else:
    print('  MACD状态: 死叉区域 (DIF < DEA)')

if dif > 0:
    print('  MACD位置: 0轴上方')
else:
    print('  MACD位置: 0轴下方')

if dif_prev < dea_prev and dif > dea:
    print('  近期是否发生金叉: 是')
elif dif_prev > dea_prev and dif < dea:
    print('  近期是否发生死叉: 是')

print()
print('【三、综合评级】')
trend_score = 0
macd_score = 0

if ma_dir == '向上':
    trend_score += 20
if price_vs_ma60 > 0:
    trend_score += 10
if pd.notna(last['MA5']) and pd.notna(last['MA10']) and pd.notna(last['MA20']):
    if last['MA5'] > last['MA10'] > last['MA20']:
        trend_score += 10

if dif > 0:
    macd_score += 15
if dif > dea:
    macd_score += 10
if dif_prev < dea_prev and dif > dea:
    macd_score += 10

total = trend_score + macd_score + 25
print(f'  趋势方向得分: {trend_score}/40')
print(f'  MACD节奏得分: {macd_score}/35')
print(f'  总分: {total}/100')

if total >= 85:
    rating = 'EXECUTE (强烈关注)'
elif total >= 70:
    rating = 'LIGHT (轻仓试错)'
elif total >= 55:
    rating = 'OBSERVE (观察)'
else:
    rating = 'AVOID (回避)'
print(f'  评级: {rating}')

print()
print('【四、买卖点建议】')
print(f'  入场触发: 回调至20日均线({last["MA20"]:.2f})附近企稳再上')
print(f'  止损位: 跌破60日均线({last["MA60"]:.2f})')
print('  失效条件: DIF再次拐头向下跌破DEA')
print('  目标位: 前高95.55元')

print()
print('【五、近5日走势】')
for i in range(-5, 0):
    row = df.iloc[i]
    prev_close = float(df.iloc[i-1]['close'])
    zdf = (float(row['close']) - prev_close) / prev_close * 100
    print(f'  {row["date"]}: 收盘{float(row["close"]):.2f} 涨幅{zdf:+.2f}%')

print()
print('【六、综合结论】')
if 'EXECUTE' in rating or 'LIGHT' in rating:
    print('  短期趋势向好，可考虑逢低布局，但需严格止损。')
elif 'OBSERVE' in rating:
    print('  目前趋势不明朗，建议观望为主，等待更明确信号。')
else:
    print('  趋势走弱，建议回避，等待底部确认。')
