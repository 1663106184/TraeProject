import requests
import time

def get_stock(code):
    """使用腾讯财经API获取股票实时数据"""
    try:
        if code.startswith("6"):
            api_code = f"sh{code}"
        else:
            api_code = f"sz{code}"

        url = f"http://qt.gtimg.cn/q={api_code}"
        headers = {"User-Agent": "Mozilla/5.0"}
        res = requests.get(url, headers=headers, timeout=3)
        res.encoding = "gbk"
        data = res.text.split("~")

        if len(data) < 10 or data[1] == "" or data[3] == "":
            return None

        return {
            "code": code,
            "name": data[1],
            "now": float(data[3]),
            "yes": float(data[4]),
            "volume": int(data[6]),
            "zdf": (float(data[3]) - float(data[4])) / float(data[4]) * 100
        }
    except Exception as e:
        return None

def generate_all_stock_codes():
    """生成A股所有股票代码"""
    codes = []
    
    # 沪市主板 (600xxx, 601xxx, 603xxx, 605xxx)
    for prefix in ["600", "601", "603", "605"]:
        for i in range(0, 1000):
            codes.append(f"{prefix}{i:03d}")
    
    # 科创板 (688xxx)
    for i in range(688000, 688999):
        codes.append(str(i))
    
    # 深市主板 (000xxx, 001xxx, 002xxx, 003xxx)
    for i in range(1, 1000):
        codes.append(f"000{i:03d}")
    for i in range(2000, 2999):
        codes.append(f"00{i:04d}")
    
    # 创业板 (300xxx)
    for i in range(300000, 300999):
        codes.append(str(i))
    
    # 北交所 (8xxxxx, 4xxxxx)
    for i in range(830000, 830999):
        codes.append(str(i))
    for i in range(430000, 430999):
        codes.append(str(i))
    
    return codes

def main():
    print("📈 量价齐升股票筛选程序（全市场快速扫描）")
    print("=" * 80)
    print("数据源: 腾讯财经API (http://qt.gtimg.cn)")
    print("=" * 80)

    # 生成A股所有股票代码
    stock_codes = generate_all_stock_codes()
    print(f"\n共 {len(stock_codes)} 只股票待扫描...")
    print("正在扫描...\n")

    rise_stocks = []
    success_count = 0
    fail_count = 0

    for idx, code in enumerate(stock_codes):
        stock = get_stock(code)
        
        if stock:
            success_count += 1
            
            # 筛选上涨的股票
            if stock['zdf'] > 0:
                rise_stocks.append(stock)
                print(f"✅ {code}  {stock['name']}   涨幅: {stock['zdf']:+.2f}%  成交量: {stock['volume']:,}")
        else:
            fail_count += 1
        
        # 显示进度
        if idx % 500 == 0:
            print(f"\n⏳ 已扫描: {idx}/{len(stock_codes)} ({idx/len(stock_codes)*100:.1f}%)")
        
        time.sleep(0.05)

    print("\n" + "=" * 80)
    print(f"\n🎉 扫描完成！")
    print(f"成功: {success_count} 只")
    print(f"失败: {fail_count} 只")
    print(f"上涨股票: {len(rise_stocks)} 只")

    if rise_stocks:
        # 按涨幅排序
        rise_stocks.sort(key=lambda x: x['zdf'], reverse=True)
        
        print("\n📊 涨幅排行榜 (前50只）:")
        print("-" * 80)
        print(f"{'排名':<6} {'代码':<8} {'名称':<10} {'最新价':<8} {'涨幅':<8} {'成交量':<12}")
        print("-" * 80)
        for i, stock in enumerate(rise_stocks[:50], 1):
            print(f"{i:<6} {stock['code']:<8} {stock['name']:<10} {stock['now']:<8.2f} {stock['zdf']:>+6.2f}% {stock['volume']:<12,}")

        # 保存到CSV
        import pandas as pd
        df = pd.DataFrame(rise_stocks)
        output_file = f"上涨股票_{pd.Timestamp.now().strftime('%Y%m%d')}.csv"
        df.to_csv(output_file, index=False, encoding='utf-8-sig')
        print(f"\n结果已保存到文件: {output_file}")

    print("\n" + "=" * 80)
    print("注意: 本程序仅筛选上涨股票")
    print("如需筛选量价齐升（价格上涨+成交量放大），请使用 stock_full_scan.py")
    print("=" * 80)

if __name__ == "__main__":
    main()
