# -*- coding: utf-8 -*-
"""
行业/概念回填脚本
==================
用新浪 hq.sinajs.cn 的 _i 扩展接口批量拉取全市场股票的「行业 + 概念板块」，
回填到 MySQL 的 stock_industry 表，补全 stock_full_scan 里 get_stock_industry 的数据缺口。

数据源说明：
- 接口：http://hq.sinajs.cn/list=sh600519_i,sz000858_i,...
- 该域名公司内网 WAF 不拦截（URL 不含 finance/stock/datacenter 等关键词）
- 单次请求可批量（实测 100 只/0.06s），返回字段中 [34]=行业, [40]=概念(以 | 分隔)
- 需要 Referer: https://finance.sina.com.cn/ 否则 403

表结构（已存在）：
    stock_industry(id, code, name, industry, sector, concepts, created_at, updated_at)
本脚本把 industry=新浪行业, concepts=新浪概念(以 ; 分隔), sector 暂留空。
"""

import time
import requests
import pymysql

from stock_full_scan import MYSQL_CONFIG, generate_stock_codes

# ---------- 参数 ----------
BATCH_SIZE = 80          # 单次请求股票数（新浪 list 上限约 100，留余量）
MAX_WORKERS = 20         # 并发线程
SINA_URL = "http://hq.sinajs.cn/list="
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://finance.sina.com.cn/",
}
TIMEOUT = 10


def fetch_batch(codes):
    """
    批量请求新浪 _i 接口，返回 {code: {industry, concepts}}。
    codes: ['600519', '000858', ...]（6 位纯代码）
    """
    items = []
    for c in codes:
        items.append(f"sh{c}_i" if c.startswith("6") else f"sz{c}_i")
    url = SINA_URL + ",".join(items)
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.encoding = "gbk"
    except Exception:
        return {}

    result = {}
    for line in r.text.splitlines():
        if '="' not in line or "_i" not in line:
            continue
        try:
            var_part, val_part = line.split('="', 1)
            # var hq_str_sh600519_i -> sh600519_i -> 600519
            key = var_part.replace("var hq_str_", "").replace("_i", "").strip()
            code = key[2:] if key.startswith(("sh", "sz")) else key
            val = val_part.rstrip('";\n')
            if not val:
                continue
            parts = val.split(",")
            # 标准 _i 至少 42 字段：[22]=名称 [34]=行业 [40]=概念(|分隔)
            # 退市/特殊情况字段不足或 [40] 为空，跳过避免存错值
            if len(parts) < 41:
                continue
            name = parts[22] if len(parts) > 22 else ""
            industry = parts[34] if len(parts) > 34 else ""
            concepts_raw = parts[40] if len(parts) > 40 else ""
            # 校验行业字段：不能含 【】（那是板块标签如「创业板」，非行业）
            if not industry or "【" in industry or "】" in industry:
                continue
            concepts = [c.strip() for c in concepts_raw.split("|") if c.strip()] if concepts_raw else []
            if industry or concepts:
                result[code] = {
                    "name": name,
                    "industry": industry,
                    "concepts": concepts,
                }
        except Exception:
            continue
    return result


def upsert_industry(conn, rows):
    """
    批量写入 MySQL：存在则更新 industry/concepts，不存在则插入。
    rows: list of (code, name, industry, concepts_str)
    """
    if not rows:
        return 0
    sql = """
        INSERT INTO stock_industry (code, name, industry, sector, concepts, updated_at)
        VALUES (%s, %s, %s, %s, %s, NOW())
        ON DUPLICATE KEY UPDATE
            name=VALUES(name),
            industry=VALUES(industry),
            sector=VALUES(sector),
            concepts=VALUES(concepts),
            updated_at=NOW()
    """
    cur = conn.cursor()
    data = [(c, n, ind, "", conc) for c, n, ind, conc in rows]
    cur.executemany(sql, data)
    conn.commit()
    cur.close()
    return len(data)


def main():
    print("=" * 60)
    print("行业/概念回填（新浪 _i 接口 → MySQL stock_industry）")
    print("=" * 60)

    codes = generate_stock_codes()
    print(f"共 {len(codes)} 只股票代码")

    # 分批
    batches = [codes[i:i + BATCH_SIZE] for i in range(0, len(codes), BATCH_SIZE)]
    print(f"分 {len(batches)} 批，每批 {BATCH_SIZE} 只，并发 {MAX_WORKERS}")

    conn = pymysql.connect(**MYSQL_CONFIG)
    print("MySQL 连接成功")

    total = 0
    fail = 0
    start = time.time()

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def handle(batch):
        res = fetch_batch(batch)
        rows = []
        for code in batch:
            if code in res:
                r = res[code]
                rows.append((code, r["name"], r["industry"], ";".join(r["concepts"])))
        return rows

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(handle, b): b for b in batches}
        for idx, fut in enumerate(as_completed(futures), 1):
            try:
                rows = fut.result()
                n = upsert_industry(conn, rows)
                total += n
            except Exception as e:
                fail += 1
                print(f"\n批次 {idx} 出错: {e}")
            if idx % 20 == 0 or idx == len(batches):
                el = time.time() - start
                print(f"\r已处理 {idx}/{len(batches)} 批  写入 {total} 条  耗时 {el:.1f}s", end="")

    # 覆盖率统计
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM stock_industry WHERE industry IS NOT NULL AND industry<>'未分类'")
    has_ind = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM stock_industry")
    total_rows = cur.fetchone()[0]
    cur.close()
    conn.close()

    el = time.time() - start
    print("\n" + "=" * 60)
    print(f"完成！写入/更新 {total} 条，失败批次 {fail}，耗时 {el:.1f}s")
    print(f"stock_industry 表共 {total_rows} 条，其中行业非空 {has_ind} 条")
    print(f"全市场 {len(codes)} 只，覆盖率 {has_ind/len(codes)*100:.1f}%")


if __name__ == "__main__":
    main()
