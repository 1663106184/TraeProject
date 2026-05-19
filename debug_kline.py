import requests
import json

def test_kline(code):
    """测试腾讯K线API"""
    print(f"\n{'='*60}")
    print(f"测试股票: {code}")
    print(f"{'='*60}")
    
    if code.startswith("6"):
        api_code = f"sh{code}"
    else:
        api_code = f"sz{code}"
    
    url = f"http://web.ifzq.gtimg.cn/appstock/app/kline/kline?_var=kline_dayqfq&param={api_code},day,,,10,qfq"
    headers = {"User-Agent": "Mozilla/5.0"}
    
    try:
        res = requests.get(url, headers=headers, timeout=10)
        res.encoding = "utf-8"
        print(f"状态码: {res.status_code}")
        
        text = res.text
        print(f"响应长度: {len(text)}")
        print(f"响应前500字符:\n{text[:500]}")
        
        if '=' in text:
            text = text.split('=', 1)[1]
        
        data = json.loads(text)
        print(f"\nJSON结构键: {list(data.keys())}")
        
        if 'data' in data:
            print(f"data键的类型: {type(data['data'])}")
            print(f"data键的内容类型:")
            if isinstance(data['data'], dict):
                for key, val in data['data'].items():
                    print(f"  {key}: {type(val)}")
                    if isinstance(val, dict):
                        print(f"    子键: {list(val.keys())}")
                        if 'day' in val:
                            klines = val['day']
                            print(f"    day数组长度: {len(klines)}")
                            if klines:
                                print(f"    最新K线数据: {klines[-1]}")
                                print(f"    K线格式说明: [日期,开盘,收盘,最高,最低,成交量,成交额,??,??,??]")
        
        return data
        
    except Exception as e:
        print(f"异常: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return None

def main():
    test_codes = ["600000", "000001", "600036"]
    for code in test_codes:
        test_kline(code)

if __name__ == "__main__":
    main()