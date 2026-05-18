import requests
import pandas as pd
import time

def get_stock(code):
    """使用腾讯财经API获取股票数据"""
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

        name = data[1]
        now = float(data[3])
        yes = float(data[4])
        volume = int(data[6])  # 修复：成交量在字段6，不是字段5！
        zdf = (now - yes) / yes * 100
        
        return {
            "code": code,
            "name": name,
            "now": now,
            "yes": yes,
            "volume": volume,
            "zdf": zdf
        }
    except Exception as e:
        return None

def main():
    print("📈 量价齐升股票筛选程序")
    print("=" * 70)
    print("数据源: 腾讯财经API (http://qt.gtimg.cn)")
    print("=" * 70)

    stocks = [
        '600000', '600007', '600022', '600030', '600036',
        '000001', '000002', '000858', '000333', '002594',
        '300003', '300750', '600519', '601318', '601360'
    ]

    rise_stocks = []

    print(f"\n正在分析 {len(stocks)} 只股票...")
    print("-" * 70)

    for code in stocks:
        stock = get_stock(code)
        
        if stock:
            print(f"✅ {code}  {stock['name']}   最新价: {stock['now']:.2f}  涨幅: {stock['zdf']:+.2f}%  成交量: {stock['volume']:,}")
            if stock['zdf'] > 0:
                rise_stocks.append(stock)
        else:
            print(f"❌ {code}  获取失败")
        
        time.sleep(0.5)

    print("\n" + "=" * 70)
    print(f"🎉 扫描完成！今日上涨股票共：{len(rise_stocks)} 只")
    
    if rise_stocks:
        print("\n📊 上涨股票列表:")
        print("-" * 70)
        print(f"{'代码':<8} {'名称':<10} {'最新价':<8} {'涨幅':<8} {'成交量':<12}")
        print("-" * 70)
        for stock in rise_stocks:
            print(f"{stock['code']:<8} {stock['name']:<10} {stock['now']:<8.2f} {stock['zdf']:>+6.2f}% {stock['volume']:<12,}")

    print("\n" + "=" * 70)

if __name__ == "__main__":
    main()
