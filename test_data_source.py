import requests
import pandas as pd

def test_tx_api(code):
    """测试腾讯财经API"""
    print(f"\n{'='*60}")
    print(f"测试腾讯API: {code}")
    print(f"{'='*60}")
    
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
        
        print(f"状态码: {res.status_code}")
        print(f"数据长度: {len(data)}")
        
        if len(data) >= 50:
            info = {
                '名称': data[1],
                '现价': data[3],
                '昨收': data[4],
                '开盘': data[5],
                '成交量': data[6],
                '最高': data[33],
                '最低': data[34],
                '换手率': data[37],
                '成交额': data[36],
                '总市值': data[44]
            }
            
            print("可获取字段:")
            for k, v in info.items():
                print(f"  {k}: {v}")
            
            return info
        else:
            print(f"数据不足，返回前20个字段:")
            for i, v in enumerate(data[:20]):
                print(f"  [{i}] {v}")
            return None
            
    except Exception as e:
        print(f"腾讯API异常: {type(e).__name__}: {e}")
        return None

def test_akshare(code):
    """测试akshare"""
    print(f"\n{'='*60}")
    print(f"测试akshare: {code}")
    print(f"{'='*60}")
    
    try:
        import akshare as ak
        today = pd.Timestamp.now().strftime('%Y%m%d')
        start_date = (pd.Timestamp.now() - pd.Timedelta(days=5)).strftime('%Y%m%d')
        
        print(f"查询日期: {start_date} ~ {today}")
        
        df = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start_date, end_date=today, adjust="qfq")
        
        if df is not None and not df.empty:
            print(f"返回行数: {len(df)}")
            print(f"列名: {list(df.columns)}")
            print("\n最新数据:")
            print(df.tail(2))
            return df
        else:
            print("返回空数据")
            return None
            
    except Exception as e:
        print(f"akshare异常: {type(e).__name__}: {e}")
        return None

def test_tx_kline(code):
    """测试腾讯K线接口"""
    print(f"\n{'='*60}")
    print(f"测试腾讯K线API: {code}")
    print(f"{'='*60}")
    
    try:
        if code.startswith("6"):
            api_code = f"sh{code}"
        else:
            api_code = f"sz{code}"
        
        url = f"http://web.ifzq.gtimg.cn/appstock/app/kline/kline?_var=kline_dayqfq&param={api_code},day,,,10,qfq"
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get(url, headers=headers, timeout=5)
        res.encoding = "utf-8"
        
        print(f"状态码: {res.status_code}")
        
        text = res.text
        if 'kline_dayqfq=' in text:
            text = text.split('kline_dayqfq=')[1]
        
        import json
        data = json.loads(text)
        
        if 'data' in data and api_code in data['data']:
            klines = data['data'][api_code]['day']
            print(f"K线数据条数: {len(klines)}")
            
            if klines:
                print("\nK线数据格式(日期,开盘,收盘,最高,最低,成交量,成交额,...):")
                for i, kline in enumerate(klines[-3:]):
                    print(f"  {kline[0]}: 开盘={kline[1]}, 收盘={kline[2]}, 最高={kline[3]}, 最低={kline[4]}, 成交量={kline[5]}")
            
            return klines
        else:
            print("数据结构不符合预期")
            print(f"返回键: {list(data.keys())}")
            return None
            
    except Exception as e:
        print(f"腾讯K线API异常: {type(e).__name__}: {e}")
        return None

def main():
    print("="*70)
    print("📊 数据源测试")
    print("="*70)
    
    test_codes = ["600000", "000001", "600036"]
    
    for code in test_codes:
        print(f"\n{'#'*70}")
        print(f"测试股票代码: {code}")
        print(f"{'#'*70}")
        
        # 测试腾讯API
        test_tx_api(code)
        
        # 测试腾讯K线API
        test_tx_kline(code)
        
        # 测试akshare
        test_akshare(code)
        
        print("\n" + "="*70)

if __name__ == "__main__":
    main()