# -*- coding: utf-8 -*-
"""调查换手(实)为什么有大量空值"""
import csv

SNAPSHOT = "snapshot/stock_features.csv"
CACHE = "snapshot/freehold_cache.csv"

# 加载 F10 缓存
cache = {}
with open(CACHE, encoding='utf-8-sig') as f:
    for row in csv.DictReader(f):
        cache[row['code']] = float(row['ratio'])
print(f"F10缓存条目数: {len(cache)}")

# 分析快照
total = 0
has_real = 0
missing_real = 0
missing_with_cache = 0  # 缺少换手(实) 但 F10 缓存有数据
missing_no_cache = 0    # 缺少换手(实) 且 F10 缓存也没有

missing_samples = []

with open(SNAPSHOT, encoding='utf-8-sig') as f:
    for row in csv.DictReader(f):
        total += 1
        code = row['代码'].split('.')[0]
        real_str = row['换手(实)(%)'].strip()

        if real_str:
            has_real += 1
        else:
            missing_real += 1
            if code in cache:
                missing_with_cache += 1
                if len(missing_samples) < 10:
                    missing_samples.append((code, row['名称'], '缓存有', f"{cache[code]:.2f}%"))
            else:
                missing_no_cache += 1
                if len(missing_samples) < 10:
                    missing_samples.append((code, row['名称'], '缓存无', '-'))

print(f"\n快照总股票数: {total}")
print(f"有换手(实): {has_real}")
print(f"缺少换手(实): {missing_real}")
print(f"  其中 F10缓存有数据但仍为空: {missing_with_cache}  <- 异常!")
print(f"  其中 F10缓存无数据(正常):   {missing_no_cache}")
print(f"\n缺少换手(实)的样本:")
for s in missing_samples:
    print(f"  {s[0]} {s[1]:6s}  F10={s[2]:4s} {s[3]}")

# 按板块统计缺失率
print(f"\n按板块统计缺失率:")
board_stats = {}
for prefix in ['000','001','002','003','300','301','600','601','603','605','688']:
    board_stats[prefix] = {'total': 0, 'missing': 0}

with open(SNAPSHOT, encoding='utf-8-sig') as f:
    for row in csv.DictReader(f):
        code = row['代码'].split('.')[0]
        real_str = row['换手(实)(%)'].strip()
        prefix = code[:3]
        for p in board_stats:
            if code.startswith(p):
                board_stats[p]['total'] += 1
                if not real_str:
                    board_stats[p]['missing'] += 1
                break

for p, s in sorted(board_stats.items()):
    if s['total'] > 0:
        miss_rate = s['missing'] / s['total'] * 100
        print(f"  {p}xxx: {s['missing']}/{s['total']} 缺失 ({miss_rate:.1f}%)")
