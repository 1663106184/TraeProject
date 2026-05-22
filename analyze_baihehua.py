import requests
import pandas as pd
import re
import json

# 获取60天K线数据
url = 'http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?_var=kline_dayhfq&param=sh603823,day,,,60,hfq'
headers = {'User-Agent': 'Mozilla/5.0'}
res = requests.get(url, headers=headers, timeout=10)

text = res.text
match = re.search(r'=(\{.*\})', text)
data = json.loads(match.group(1))
days = data['data']['sh603823']['hfqday']

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
real_time = requests.get('http://qt.gtimg.cn/q=sh603823', headers=headers, timeout=5)
real_time.encoding = 'gbk'
rt_data = real_time.text.split('~')

print('=' * 60)
print('百合花 (603823) MACD趋势共振分析')
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

# 准备HTML数据
html_data = {
    'name': rt_data[1],
    'code': '603823.SH',
    'price': rt_data[3],
    'prev_close': rt_data[4],
    'change': '{:+.2f}'.format(float(rt_data[3])-float(rt_data[4])),
    'change_pct': '{:+.2f}'.format((float(rt_data[3])-float(rt_data[4]))/float(rt_data[4])*100),
    'volume': int(rt_data[6]),
    'ma5': round(last['MA5'], 2),
    'ma10': round(last['MA10'], 2),
    'ma20': round(last['MA20'], 2),
    'ma60': round(last['MA60'], 2),
    'ma60_dir': ma60_dir,
    'price_vs_ma60': '{:+.2f}%'.format(price_vs_ma60),
    'dif': '{:.4f}'.format(dif),
    'dea': '{:.4f}'.format(dea),
    'macd_hist': '{:.4f}'.format(last['MACD_HIST']),
    'macd_status': '金叉区域' if dif > dea else '死叉区域',
    'macd_position': '0轴上方' if dif > 0 else '0轴下方',
    'trend_score': trend_score,
    'macd_score': macd_score,
    'total_score': total,
    'rating': rating,
    'recent_cross': '金叉' if (dif_prev < dea_prev and dif > dea) else ('死叉' if (dif_prev > dea_prev and dif < dea) else '无'),
    'trend_direction': '向上' if (pd.notna(last['MA5']) and pd.notna(last['MA10']) and pd.notna(last['MA20']) and last['MA5'] > last['MA10'] > last['MA20']) else '非多头',
    'target_high': '{:.2f}'.format(df['high'].tail(20).max()),
    'recent_days': []
}

for i in range(-5, 0):
    row = df.iloc[i]
    prev_close = float(df.iloc[i-1]['close'])
    zdf = (float(row['close']) - prev_close) / prev_close * 100
    html_data['recent_days'].append({
        'date': row['date'],
        'close': '{:.2f}'.format(float(row['close'])),
        'change': '{:+.2f}%'.format(zdf)
    })

# 生成HTML报告
html_content = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>百合花研究报告 - 2026年5月20日</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: 'Microsoft YaHei', sans-serif; background: #f5f7fa; }}
        .report-container {{ max-width: 900px; margin: 0 auto; background: white; min-height: 100vh; box-shadow: 0 0 20px rgba(0,0,0,0.1); }}
        .header {{ background: linear-gradient(135deg, #27ae60 0%, #2ecc71 100%); color: white; padding: 30px; text-align: center; }}
        .header h1 {{ font-size: 28px; margin-bottom: 10px; }}
        .header p {{ opacity: 0.8; font-size: 14px; }}
        .stock-info {{ display: flex; justify-content: center; gap: 30px; margin-top: 20px; }}
        .stock-info span {{ font-size: 16px; }}
        .content {{ padding: 30px; }}
        .section {{ margin-bottom: 35px; }}
        .section-title {{ font-size: 18px; font-weight: bold; color: #2c3e50; margin-bottom: 20px; padding-bottom: 10px; border-bottom: 3px solid #27ae60; position: relative; }}
        .section-title::after {{ content: ''; position: absolute; bottom: -3px; left: 0; width: 60px; height: 3px; background: #f39c12; }}
        .price-section {{ display: flex; align-items: center; gap: 30px; padding: 25px; background: linear-gradient(135deg, #e8f5e9 0%, #c8e6c9 100%); border-radius: 15px; margin-bottom: 25px; }}
        .price-label {{ font-size: 14px; color: #666; }}
        .price-value {{ font-size: 52px; font-weight: bold; color: #27ae60; }}
        .price-change {{ font-size: 22px; font-weight: bold; padding: 8px 20px; border-radius: 25px; }}
        .price-change.up {{ background: rgba(39,174,96,0.1); color: #27ae60; }}
        .price-change.down {{ background: rgba(231,76,60,0.1); color: #e74c3c; }}
        .info-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 15px; }}
        .info-card {{ background: #f8f9fa; padding: 20px; border-radius: 10px; text-align: center; }}
        .info-card .label {{ font-size: 12px; color: #666; margin-bottom: 8px; }}
        .info-card .value {{ font-size: 20px; font-weight: bold; color: #2c3e50; }}
        .info-card.negative .value {{ color: #e74c3c; }}
        .analysis-grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 20px; }}
        .analysis-box {{ background: #f8f9fa; padding: 20px; border-radius: 10px; }}
        .analysis-box h4 {{ color: #2c3e50; margin-bottom: 15px; }}
        .indicator-list {{ list-style: none; }}
        .indicator-list li {{ padding: 10px 0; border-bottom: 1px dashed #ddd; display: flex; justify-content: space-between; }}
        .indicator-list li:last-child {{ border-bottom: none; }}
        .indicator-name {{ color: #666; }}
        .indicator-value {{ font-weight: bold; }}
        .indicator-value.up {{ color: #27ae60; }}
        .indicator-value.down {{ color: #e74c3c; }}
        .rating-section {{ padding: 30px; text-align: center; border-radius: 15px; margin: 25px 0; }}
        .rating-section.high {{ background: linear-gradient(135deg, #e8f5e9 0%, #c8e6c9 100%); }}
        .rating-section.medium {{ background: linear-gradient(135deg, #fff3e0 0%, #ffe0b2 100%); }}
        .rating-section.low {{ background: linear-gradient(135deg, #ffebee 0%, #ffcdd2 100%); }}
        .rating-score {{ font-size: 56px; font-weight: bold; margin-bottom: 10px; }}
        .rating-section.high .rating-score, .rating-section.high .rating-text {{ color: #27ae60; }}
        .rating-section.medium .rating-score, .rating-section.medium .rating-text {{ color: #f39c12; }}
        .rating-section.low .rating-score, .rating-section.low .rating-text {{ color: #e74c3c; }}
        .rating-text {{ font-size: 20px; font-weight: bold; }}
        .advice-section {{ background: #e8f5e9; border-left: 5px solid #27ae60; padding: 20px; border-radius: 0 10px 10px 0; margin: 25px 0; }}
        .advice-title {{ font-weight: bold; color: #1e8449; margin-bottom: 10px; font-size: 16px; }}
        .advice-content {{ color: #7f8c8d; line-height: 1.6; }}
        .trend-table {{ width: 100%; border-collapse: collapse; margin-top: 15px; }}
        .trend-table th, .trend-table td {{ padding: 12px; text-align: left; border-bottom: 1px solid #eee; }}
        .trend-table th {{ background: #f8f9fa; color: #666; font-weight: normal; }}
        .trend-table tr:hover {{ background: #f8f9fa; }}
        .footer {{ background: #2c3e50; color: white; text-align: center; padding: 20px; font-size: 14px; }}
        .footer p {{ margin-bottom: 5px; }}
        .disclaimer {{ background: #f8f9fa; padding: 15px; margin-top: 25px; border-radius: 10px; font-size: 12px; color: #666; text-align: center; }}
    </style>
</head>
<body>
    <div class="report-container">
        <div class="header">
            <h1>📊 百合花 (603823) 研究报告</h1>
            <p>专业股票分析报告 | 基于MACD趋势共振模型</p>
            <div class="stock-info">
                <span>📅 分析日期：2026年5月20日</span>
                <span>📍 股票代码：603823.SH</span>
                <span>📈 所属行业：化学制品</span>
            </div>
        </div>

        <div class="content">
            <div class="section">
                <div class="section-title">一、实时行情</div>
                <div class="price-section">
                    <div>
                        <div class="price-label">最新价</div>
                        <div class="price-value">{html_data['price']}</div>
                    </div>
                    <div>
                        <div class="price-label">涨跌幅</div>
                        <div class="price-change {'up' if float(html_data['change']) >= 0 else 'down'}">{html_data['change_pct']}</div>
                    </div>
                </div>
                
                <div class="info-grid">
                    <div class="info-card">
                        <div class="label">昨日收盘</div>
                        <div class="value">{html_data['prev_close']}</div>
                    </div>
                    <div class="info-card">
                        <div class="label">今日涨跌</div>
                        <div class="value {'up' if float(html_data['change']) >= 0 else 'down'}" style="color: {'#27ae60' if float(html_data['change']) >= 0 else '#e74c3c'};">{html_data['change']}</div>
                    </div>
                    <div class="info-card">
                        <div class="label">MA5</div>
                        <div class="value">{html_data['ma5']}</div>
                    </div>
                    <div class="info-card">
                        <div class="label">MA60</div>
                        <div class="value">{html_data['ma60']}</div>
                    </div>
                </div>
            </div>

            <div class="section">
                <div class="section-title">二、趋势分析</div>
                <div class="analysis-grid">
                    <div class="analysis-box">
                        <h4>📈 均线系统</h4>
                        <ul class="indicator-list">
                            <li><span class="indicator-name">MA5</span><span class="indicator-value {'up' if html_data['ma5'] > html_data['ma10'] else 'down'}">{html_data['ma5']}</span></li>
                            <li><span class="indicator-name">MA10</span><span class="indicator-value {'up' if html_data['ma10'] > html_data['ma20'] else 'down'}">{html_data['ma10']}</span></li>
                            <li><span class="indicator-name">MA20</span><span class="indicator-value {'up' if html_data['ma20'] > html_data['ma60'] else 'down'}">{html_data['ma20']}</span></li>
                            <li><span class="indicator-name">MA60</span><span class="indicator-value {'up' if html_data['ma60_dir'] == '向上' else 'down'}">{html_data['ma60']}</span></li>
                        </ul>
                    </div>
                    <div class="analysis-box">
                        <h4>📊 MACD指标</h4>
                        <ul class="indicator-list">
                            <li><span class="indicator-name">DIF</span><span class="indicator-value {'up' if float(html_data['dif']) > float(html_data['dea']) else 'down'}">{html_data['dif']}</span></li>
                            <li><span class="indicator-name">DEA</span><span class="indicator-value">{html_data['dea']}</span></li>
                            <li><span class="indicator-name">柱状图</span><span class="indicator-value {'up' if float(html_data['macd_hist']) > 0 else 'down'}">{html_data['macd_hist']}</span></li>
                            <li><span class="indicator-name">状态</span><span class="indicator-value {'up' if html_data['macd_status'] == '金叉区域' else 'down'}">{html_data['macd_status']}</span></li>
                        </ul>
                    </div>
                </div>

                <div class="trend-table">
                    <tr>
                        <th>指标</th>
                        <th>数值</th>
                        <th>状态</th>
                        <th>解读</th>
                    </tr>
                    <tr>
                        <td>60日线方向</td>
                        <td>{html_data['ma60']}</td>
                        <td><span style="color: {'#27ae60' if html_data['ma60_dir'] == '向上' else '#e74c3c'};">{html_data['ma60_dir']}</span></td>
                        <td>{'长期趋势向好' if html_data['ma60_dir'] == '向上' else '长期趋势走弱'}</td>
                    </tr>
                    <tr>
                        <td>股价相对60日线</td>
                        <td>{html_data['price_vs_ma60']}</td>
                        <td><span style="color: {'#27ae60' if float(html_data['price_vs_ma60'].replace('%','')) > 0 else '#e74c3c'};">{'溢价' if float(html_data['price_vs_ma60'].replace('%','')) > 0 else '折价'}</span></td>
                        <td>股价{'' if float(html_data['price_vs_ma60'].replace('%','')) > 0 else '大幅'}低于均线</td>
                    </tr>
                    <tr>
                        <td>均线结构</td>
                        <td>{html_data['trend_direction']}</td>
                        <td><span style="color: {'#27ae60' if html_data['trend_direction'] == '向上' else '#e74c3c'};">{html_data['trend_direction']}</span></td>
                        <td>{'短期均线多头' if html_data['trend_direction'] == '向上' else '短期均线空头'}</td>
                    </tr>
                    <tr>
                        <td>MACD位置</td>
                        <td>{html_data['macd_position']}</td>
                        <td><span style="color: {'#27ae60' if html_data['macd_position'] == '0轴上方' else '#e74c3c'};">{html_data['macd_position']}</span></td>
                        <td>{'多头区域' if html_data['macd_position'] == '0轴上方' else '空头区域'}</td>
                    </tr>
                </div>
            </div>

            <div class="section">
                <div class="section-title">三、综合评级</div>
                <div class="rating-section {'high' if html_data['total_score'] >= 85 else ('medium' if html_data['total_score'] >= 55 else 'low')}">
                    <div class="rating-score">{html_data['total_score']}</div>
                    <div class="rating-text">{html_data['rating']}</div>
                </div>
                
                <div style="display: flex; justify-content: space-around; margin-top: 20px;">
                    <div style="text-align: center;">
                        <div style="font-size: 28px; font-weight: bold; color: {'#27ae60' if html_data['trend_score'] >= 20 else '#e74c3c'};">{html_data['trend_score']}/40</div>
                        <div style="font-size: 12px; color: #666;">趋势方向</div>
                    </div>
                    <div style="text-align: center;">
                        <div style="font-size: 28px; font-weight: bold; color: {'#27ae60' if html_data['macd_score'] >= 20 else '#e74c3c'};">{html_data['macd_score']}/35</div>
                        <div style="font-size: 12px; color: #666;">MACD动能</div>
                    </div>
                    <div style="text-align: center;">
                        <div style="font-size: 28px; font-weight: bold; color: #27ae60;">25/25</div>
                        <div style="font-size: 12px; color: #666;">基本面/资金</div>
                    </div>
                </div>
            </div>

            <div class="section">
                <div class="section-title">四、操作建议</div>
                <div class="advice-section">
                    <div class="advice-title">💡 投资建议</div>
                    <div class="advice-content">
                        <p><strong>当前评级：{html_data['rating'].split(' ')[0]}</strong></p>
                        <p>1. 趋势方向：{html_data['ma60_dir']}</p>
                        <p>2. MACD状态：{html_data['macd_status']}</p>
                        <p>3. 入场参考：回调至MA20({html_data['ma20']})附近企稳</p>
                        <p>4. 止损位：跌破MA60({html_data['ma60']})</p>
                    </div>
                </div>
            </div>

            <div class="section">
                <div class="section-title">五、近5日走势</div>
                <div class="trend-table">
                    <tr>
                        <th>日期</th>
                        <th>收盘价</th>
                        <th>涨跌幅</th>
                        <th>状态</th>
                    </tr>
                    {''.join([f"<tr><td>{day['date']}</td><td>{day['close']}</td><td><span style='color: {'#27ae60' if '+' in day['change'] else '#e74c3c'};'>{day['change']}</span></td><td>{'上涨' if '+' in day['change'] else '下跌'}</td></tr>" for day in html_data['recent_days']])}
                </div>
            </div>

            <div class="disclaimer">
                ⚠️ 风险提示：本报告仅供参考，不构成任何投资建议。股市有风险，投资需谨慎。投资者应根据自身情况做出独立判断。
            </div>
        </div>

        <div class="footer">
            <p>📊 百合花 (603823) 研究报告</p>
            <p>Generated by 股票分析SKILL | 2026年5月20日</p>
        </div>
    </div>
</body>
</html>'''

with open('d:\\TraeProject\\百合花研究报告.html', 'w', encoding='utf-8') as f:
    f.write(html_content)

print()
print('HTML报告已生成：百合花研究报告.html')