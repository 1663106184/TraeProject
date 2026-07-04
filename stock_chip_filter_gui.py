# -*- coding: utf-8 -*-
"""
筹码峰套牢盘筛选 - PyQt5 桌面版
复用 stock_chip_filter 的筛选逻辑，结果直接显示在界面表格中。
运行： py stock_chip_filter_gui.py
"""

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QObject, QRectF, QTimer
from PyQt5.QtGui import QColor, QFont, QPainter, QPen, QBrush, QPixmap, QFontMetrics
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QLabel, QTableWidget, QTableWidgetItem,
    QProgressBar, QStatusBar, QHeaderView, QComboBox, QMessageBox,
    QDoubleSpinBox, QSpinBox, QFileDialog, QGroupBox, QCheckBox, QDialog,
    QSplitter
)

# 复用筹码筛选逻辑（同一目录）
import stock_chip_filter as scf
from stock_full_scan import format_number


# 结果列定义：(字段名, 显示表头, 是否右对齐数值, 是否需要格式化)
COLUMNS = [
    ('代码',             '代码',       False, False),
    ('名称',             '名称',       False, False),
    ('市场板块',         '市场板块',   False, False),
    ('收盘价',           '收盘价',     True,  False),
    ('涨跌幅(%)',        '涨跌幅',     True,  True),    # 加 %
    ('量比',             '量比',       True,  False),
    ('换手率(%)',        '换手率',     True,  True),    # 加 %
    ('成交额',           '成交额',     True,  True),    # 格式化 万/亿
    ('套牢盘比例(%)',    '套牢盘',     True,  False),
    ('获利盘比例(%)',    '获利盘',     True,  False),
    ('筹码峰价位',       '筹码峰',     True,  False),
    ('距筹码峰(%)',      '距筹码峰',   True,  True),    # 加 %
    ('同花顺行业',       '同花顺行业', False, False),
    ('最相关概念',       '概念板块',   False, False),
]


class ChipScanWorker(QObject):
    """后台扫描线程：复用 scf.process_one，并发拉取，通过信号回传进度与结果。"""
    progress = pyqtSignal(int, int, int)        # 已扫描, 总数, 命中
    result_ready = pyqtSignal(list)             # 一批结果(dict 列表)
    finished = pyqtSignal(int, int, int, float)  # 总数, 命中, 失败, 耗时
    error = pyqtSignal(str)

    def __init__(self, params, max_workers=30, mode="filter"):
        super().__init__()
        self.params = params            # dict: profit_min / vol_ratio_min / zdf_min / kline_days / bins
        self.max_workers = max_workers
        self.mode = mode                # "filter"=过滤显示 ; "snapshot"=存全量快照
        self._stop = False
        self._batch = []
        self._batch_size = 50

    def stop(self):
        self._stop = True

    def run(self):
        try:
            scf.KLINE_DAYS = self.params['kline_days']
            scf.PRICE_BINS = self.params['bins']
            if self.mode == "filter":
                scf.PROFIT_RATIO_MIN = self.params['profit_min']
                scf.VOLUME_RATIO_MIN = self.params['vol_ratio_min']
                scf.ZDF_MIN = self.params['zdf_min']

            codes = scf.generate_stock_codes()
            total = len(codes)
            hit = failed = 0
            start = time.time()

            # 快照模式算全量指标（不过滤）；filter 模式算过滤后的指标
            task = scf.compute_features if self.mode == "snapshot" else scf.process_one

            with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
                futures = {ex.submit(task, c): c for c in codes}
                for idx, fut in enumerate(as_completed(futures), 1):
                    if self._stop:
                        for f in futures:
                            f.cancel()
                        break
                    try:
                        res = fut.result()
                    except Exception:
                        res = None
                    if res:
                        self._batch.append(res)
                        hit += 1
                        if len(self._batch) >= self._batch_size:
                            self.result_ready.emit(self._batch)
                            self._batch = []

                    if idx % 200 == 0 or idx == total:
                        self.progress.emit(idx, total, hit)

            if self._batch:
                self.result_ready.emit(self._batch)
                self._batch = []

            elapsed = time.time() - start
            self.finished.emit(total, hit, total - hit, elapsed)
        except Exception as e:
            self.error.emit(str(e))


class SortableTableWidgetItem(QTableWidgetItem):
    """让文本列按数值排序。"""
    def __lt__(self, other):
        try:
            return float(self.data(Qt.UserRole)) < float(other.data(Qt.UserRole))
        except (TypeError, ValueError):
            return self.text() < other.text()


class KLineWidget(QWidget):
    """自绘 K 线图（蜡烛 + 成交量）+ 筹码分布侧栏。不依赖第三方库。"""

    def __init__(self, kline_df, chip_info=None, parent=None):
        super().__init__(parent)
        self.df = kline_df
        # chip_info: (profit_ratio, trapped_ratio, peak_price, centers, chip_array, total)
        self.chip_info = chip_info
        self.setMinimumHeight(180)  # 允许拖到较小，但有下限
        self.setStyleSheet("background:#1e2433;")

    def paintEvent(self, event):
        if self.df is None or len(self.df) == 0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w = self.width()
        h = self.height()
        # 布局：左 65% K线+量，右 35% 筹码分布
        kline_w = int(w * 0.65) if self.chip_info else w
        chip_x = kline_w + 10
        chip_w = w - kline_w - 20

        df = self.df
        n = len(df)
        # 价格范围
        highs = df['high'].astype(float)
        lows = df['low'].astype(float)
        pmin, pmax = float(lows.min()), float(highs.max())
        if pmax <= pmin:
            pmax = pmin + 1
        pad = (pmax - pmin) * 0.05
        pmin -= pad; pmax += pad

        # K线区域：上 70% 价格，下 30% 成交量
        price_h = int(h * 0.70)
        vol_top = price_h + 10
        vol_h = h - vol_top - 20

        # 网格线 + 价格刻度
        p.setPen(QPen(QColor('#2a3142'), 1, Qt.DashLine))
        for i in range(5):
            y = int(price_h * i / 4)
            p.drawLine(0, y, kline_w, y)
            price = pmax - (pmax - pmin) * i / 4
            p.setPen(QColor('#7a8499'))
            p.drawText(2, y + 12, f"{price:.2f}")
            p.setPen(QPen(QColor('#2a3142'), 1, Qt.DashLine))

        # 蜡烛图
        cw = max(3, kline_w / n - 1)
        vols = df['volume'].astype(float)
        vmax = float(vols.max()) if len(vols) else 1
        if vmax <= 0: vmax = 1

        for i, (_, row) in enumerate(df.iterrows()):
            o = float(row['open']); c = float(row['close'])
            hi = float(row['high']); lo = float(row['low'])
            x = int(i * kline_w / n + cw / 2)
            up = c >= o
            color = QColor('#e23b3b') if up else QColor('#1faa52')
            p.setPen(QPen(color, 1))
            # 上下影线
            y_hi = int((pmax - hi) / (pmax - pmin) * price_h)
            y_lo = int((pmax - lo) / (pmax - pmin) * price_h)
            p.drawLine(x, y_hi, x, y_lo)
            # 实体
            y_o = int((pmax - o) / (pmax - pmin) * price_h)
            y_c = int((pmax - c) / (pmax - pmin) * price_h)
            body_h = max(1, abs(y_o - y_c))
            p.setBrush(QBrush(color) if up else QBrush(color))
            p.drawRect(QRectF(x - cw / 2, min(y_o, y_c), cw, body_h))
            # 成交量柱
            v = float(row['volume'])
            vh = int(v / vmax * vol_h)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(color))
            p.drawRect(QRectF(x - cw / 2, vol_top + vol_h - vh, cw, vh))

        # 均线 MA5/MA10/MA20/MA60
        close = df['close'].astype(float)
        ma_defs = [
            (5,  QColor('#ffcc00')),   # MA5 黄
            (10, QColor('#ff6a00')),   # MA10 橙
            (20, QColor('#a974ff')),   # MA20 紫
            (60, QColor('#36c5f0')),   # MA60 青
        ]
        for period, col in ma_defs:
            if n < period:
                continue
            ma = close.rolling(window=period).mean()
            pen = QPen(col, 1.5)
            p.setPen(pen)
            prev_y = None
            for i in range(period - 1, n):
                val = ma.iloc[i]
                if val != val:  # NaN
                    prev_y = None
                    continue
                x = int(i * kline_w / n + cw / 2)
                y = int((pmax - float(val)) / (pmax - pmin) * price_h)
                if prev_y is not None:
                    p.drawLine(x - int(kline_w / n), prev_y, x, y)
                prev_y = y
        # 均线图例（右上角，避开价格刻度）
        p.setPen(QColor('#cfd6e4'))
        legend_x = kline_w - 280
        for period, col in ma_defs:
            if n < period:
                continue
            p.setPen(col)
            p.drawLine(legend_x, 14, legend_x + 16, 14)
            p.setPen(QColor('#cfd6e4'))
            p.drawText(legend_x + 20, 18, f"MA{period}")
            legend_x += 70

        # 成交量区标题
        p.setPen(QColor('#7a8499'))
        p.drawText(2, vol_top + 12, "成交量")

        # 筹码分布侧栏
        if self.chip_info and chip_w > 30:
            profit, trapped, peak, centers, chip, total = self.chip_info
            p.setPen(QColor('#cfd6e4'))
            p.drawText(chip_x, 16, "筹码分布")
            # 筹码柱按价位从下到上画
            cmax = float(chip.max()) if len(chip) else 1
            if cmax <= 0: cmax = 1
            bar_h = max(1, price_h / len(chip))
            p.setPen(Qt.NoPen)
            for i, cv in enumerate(chip):
                if cv <= 0: continue
                # 价位从低到高 → 画在图上从下到上
                y = int(price_h - (i + 1) * price_h / len(chip))
                bw = int(cv / cmax * (chip_w - 10))
                # 当前价之上的筹码=套牢(绿)，之下=获利(红)
                price_at = float(centers[i])
                col = QColor('#1faa52') if price_at > float(self.df['close'].iloc[-1]) else QColor('#e23b3b')
                p.setBrush(QBrush(col))
                p.drawRect(QRectF(chip_x, y, bw, max(1, bar_h - 1)))
            # 峰价标线
            p.setPen(QPen(QColor('#ffcc00'), 1, Qt.DashLine))
            peak_y = int((pmax - peak) / (pmax - pmin) * price_h)
            p.drawLine(chip_x - 5, peak_y, chip_x + chip_w, peak_y)
            p.setPen(QColor('#ffcc00'))
            p.drawText(chip_x, peak_y - 4, f"峰 {peak:.2f}")
            # 当前价标线
            cur = float(self.df['close'].iloc[-1])
            cur_y = int((pmax - cur) / (pmax - pmin) * price_h)
            p.setPen(QPen(QColor('#ffffff'), 1, Qt.DashLine))
            p.drawLine(0, cur_y, kline_w, cur_y)
            # 比例文字
            p.setPen(QColor('#cfd6e4'))
            p.drawText(chip_x, price_h + 20, f"获利 {profit*100:.1f}%")
            p.drawText(chip_x, price_h + 38, f"套牢 {trapped*100:.1f}%")

        p.end()


class StockDetailDialog(QDialog):
    """双击行弹出的详情窗：K线 + 筹码分布 + 文字详情。"""

    def __init__(self, row_data, parent=None):
        super().__init__(parent)
        code_raw = row_data.get('代码', '')
        # 去掉 .SH/.SZ 后缀
        code = code_raw.split('.')[0]
        name = row_data.get('名称', '')
        self.setWindowTitle(f"{code} {name} - K线详情")
        self.resize(900, 560)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)

        # 顶部信息条
        info = (
            f"{name} ({code})   收盘 {row_data.get('收盘价','')}   "
            f"涨跌幅 {row_data.get('涨跌幅(%)','')}%   量比 {row_data.get('量比','')}   "
            f"获利盘 {row_data.get('获利盘比例(%)','')}%   套牢盘 {row_data.get('套牢盘比例(%)','')}%"
        )
        lbl = QLabel(info)
        lbl.setStyleSheet("color:#cfd6e4; padding:4px;")
        lay.addWidget(lbl)

        # 拉 K 线 + 筹码
        import numpy as np
        kline_df = scf.get_kline_data(code, days=scf.KLINE_DAYS)
        chip_info = None
        if kline_df is not None and len(kline_df) >= 5:
            res = scf.build_chip_distribution(kline_df, bins=scf.PRICE_BINS)
            if res is not None:
                centers, chip, total = res
                cur = float(kline_df['close'].iloc[-1])
                above = centers > cur
                trapped = float(chip[above].sum()) / total
                profit = 1.0 - trapped
                peak = float(centers[int(np.argmax(chip))])
                chip_info = (profit, trapped, peak, centers, chip, total)

        if kline_df is None or len(kline_df) < 5:
            QLabel("K线数据获取失败").setStyleSheet("color:#aaa; padding:20px;")
            lay.addWidget(QLabel("K线数据获取失败"))
        else:
            self.kw = KLineWidget(kline_df, chip_info)
            lay.addWidget(self.kw, 1)

        # 底部文字详情
        detail_lines = []
        for field, header, _, _ in COLUMNS:
            v = row_data.get(field, '')
            detail_lines.append(f"{header}: {v}")
        detail = QLabel("\n".join(detail_lines))
        detail.setStyleSheet("color:#aab; font-size:12px; padding:6px;")
        detail.setWordWrap(True)
        lay.addWidget(detail)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("筹码峰套牢盘筛选 - 桌面版")
        self.resize(1280, 760)

        self.all_rows = []
        self.worker = None
        self.thread = None
        self.scanning = False
        self._pending_row = None
        self._kline_timer = QTimer(self)
        self._kline_timer.setSingleShot(True)
        self._kline_timer.timeout.connect(self._flush_pending_kline)
        # 本地快照：存全市场特征（不过滤），供离线筛选
        self.local_snapshot = None      # list of dict（指标）
        self.local_meta = {}            # 快照元信息 saved_at/kline_days/bins
        self.scan_mode = "filter"       # "filter"=扫描过滤显示 ; "snapshot"=扫描存快照

        self._build_ui()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # ---------- 参数区 ----------
        param_group = QGroupBox("筛选参数")
        pl = QHBoxLayout(param_group)
        pl.setContentsMargins(10, 10, 10, 10)
        pl.setSpacing(10)

        def make_double(label, val, step=0.05, lo=0.0, hi=1.0, suffix=""):
            box = QDoubleSpinBox()
            box.setRange(lo, hi)
            box.setSingleStep(step)
            box.setDecimals(2)
            box.setValue(val)
            box.setSuffix(suffix)
            box.setFixedHeight(28)
            pl.addWidget(QLabel(label))
            pl.addWidget(box)
            return box

        def make_int(label, val, lo=10, hi=500, step=10):
            box = QSpinBox()
            box.setRange(lo, hi)
            box.setSingleStep(step)
            box.setValue(val)
            box.setFixedHeight(28)
            pl.addWidget(QLabel(label))
            pl.addWidget(box)
            return box

        self.spin_profit = make_double("获利盘下限:", scf.PROFIT_RATIO_MIN, 0.05, 0.0, 1.0, "")
        self.spin_profit.setToolTip("0.60 = 60%（当前价之下筹码占比）")
        self.spin_vol = make_double("量比下限:", scf.VOLUME_RATIO_MIN, 0.1, 0.0, 20.0, "")
        self.spin_zdf = make_double("涨跌幅下限(%):", scf.ZDF_MIN, 0.5, -99.0, 20.0, "")
        self.spin_zdf.setToolTip("默认 -99，即上涨下跌都收；填 0 则只要上涨")
        self.spin_days = make_int("K线天数:", scf.KLINE_DAYS, 30, 300, 10)
        self.spin_bins = make_int("价位切片:", scf.PRICE_BINS, 50, 1000, 50)
        # 筛选参数变化 → 本地重筛（K线天数/切片锁定，不连）
        for box in (self.spin_profit, self.spin_vol, self.spin_zdf):
            box.valueChanged.connect(self._on_param_changed_for_local)

        pl.addStretch()
        layout.addWidget(param_group)

        # ---------- 工具栏 ----------
        bar = QHBoxLayout()
        self.btn_scan = QPushButton("开始扫描")
        self.btn_scan.setFixedHeight(30)
        self.btn_scan.setStyleSheet(
            "QPushButton{background:#2d8cf0;color:white;font-weight:bold;border-radius:4px;padding:0 18px}"
            "QPushButton:hover{background:#5cadff}"
            "QPushButton:disabled{background:#a0c4f0}"
        )
        self.btn_scan.clicked.connect(self.toggle_scan)

        self.btn_export = QPushButton("导出 CSV")
        self.btn_export.setFixedHeight(30)
        self.btn_export.clicked.connect(self.export_csv)

        self.btn_scan_save = QPushButton("扫描并存快照")
        self.btn_scan_save.setFixedHeight(30)
        self.btn_scan_save.setToolTip("扫描全市场并把所有股票特征存到本地 snapshot/，之后可离线筛选")
        self.btn_scan_save.clicked.connect(self.toggle_scan_save)

        self.btn_load_local = QPushButton("加载本地快照")
        self.btn_load_local.setFixedHeight(30)
        self.btn_load_local.setToolTip("从本地快照加载，按当前参数筛选（不联网）")
        self.btn_load_local.clicked.connect(self.load_local_snapshot)

        self.btn_clear = QPushButton("清空")
        self.btn_clear.setFixedHeight(30)
        self.btn_clear.clicked.connect(self.clear_table)

        self.filter_box = QLineEdit()
        self.filter_box.setPlaceholderText("输入 代码 / 名称 / 行业 / 概念 实时筛选…")
        self.filter_box.setFixedHeight(30)
        self.filter_box.textChanged.connect(self.apply_filter)

        self.col_filter = QComboBox()
        self.col_filter.setFixedHeight(30)
        self.col_filter.addItem("全部列", "")
        for field, header, _, _ in COLUMNS:
            self.col_filter.addItem(header, field)
        self.col_filter.currentIndexChanged.connect(self.apply_filter)

        # 板块筛选：从当前数据提取所有概念，按数量排序
        self.sector_filter = QComboBox()
        self.sector_filter.setFixedHeight(30)
        self.sector_filter.addItem("全部板块", "")
        self.sector_filter.currentIndexChanged.connect(self.apply_filter)
        self._sector_box_ready = False   # 首次填充数据后才启用

        self.count_label = QLabel("共 0 只")
        self.count_label.setStyleSheet("color:#666")

        bar.addWidget(self.btn_scan)
        bar.addWidget(self.btn_scan_save)
        bar.addWidget(self.btn_load_local)
        bar.addWidget(self.btn_export)
        bar.addWidget(self.btn_clear)
        bar.addWidget(QLabel("列:"))
        bar.addWidget(self.col_filter, 1)
        bar.addWidget(QLabel("板块:"))
        bar.addWidget(self.sector_filter, 2)
        bar.addWidget(self.filter_box, 3)
        bar.addWidget(self.count_label)
        layout.addLayout(bar)

        # ---------- 表格 ----------
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels([h for _, h, _, _ in COLUMNS])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionsMovable(True)
        self.table.horizontalHeader().setStretchLastSection(True)

        self._sort_col = None
        self._sort_order = 0
        self._orig_headers = [h for _, h, _, _ in COLUMNS]
        self.table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)

        for i, (field, header, is_num, _) in enumerate(COLUMNS):
            if field in ('代码', '名称'):
                self.table.setColumnWidth(i, 95)
            elif field == '最相关概念':
                self.table.setColumnWidth(i, 200)
            elif field == '同花顺行业':
                self.table.setColumnWidth(i, 130)
            elif field == '市场板块':
                self.table.setColumnWidth(i, 90)
            elif is_num:
                self.table.setColumnWidth(i, 90)
            else:
                self.table.setColumnWidth(i, 90)
        self.table.itemDoubleClicked.connect(self.show_detail)
        # 选中行变化（含键盘上下键）→ 实时刷新下方 K 线
        self.table.itemSelectionChanged.connect(self._on_selection_changed)

        # ---------- 表格 + K线区 用 splitter 垂直分隔 ----------
        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.addWidget(self.table)
        # K 线容器
        self.kline_container = QWidget()
        self.kline_container.setMinimumHeight(200)  # 防止拖没
        kl = QVBoxLayout(self.kline_container)
        kl.setContentsMargins(2, 2, 2, 2)
        # 顶部信息条 + 关闭按钮
        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(0, 0, 0, 0)
        self.kline_info_label = QLabel("（点击表格行查看 K 线）")
        self.kline_info_label.setStyleSheet("color:#cfd6e4; padding:2px;")
        top_bar.addWidget(self.kline_info_label, 1)
        self.btn_close_kline = QPushButton("关闭 K 线")
        self.btn_close_kline.setFixedHeight(24)
        self.btn_close_kline.setStyleSheet(
            "QPushButton{background:#3a4252;color:#cfd6e4;border-radius:3px;padding:0 10px}"
            "QPushButton:hover{background:#4a5266}"
        )
        self.btn_close_kline.clicked.connect(self.hide_kline)
        top_bar.addWidget(self.btn_close_kline)
        kl.addLayout(top_bar)
        self.kline_widget = None  # 首次选中后创建
        self.splitter.addWidget(self.kline_container)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes([500, 300])
        # 初始隐藏 K 线区
        self.kline_container.setVisible(False)
        layout.addWidget(self.splitter, 1)

        # ---------- 进度条 ----------
        self.progress = QProgressBar()
        self.progress.setFixedHeight(18)
        self.progress.setTextVisible(True)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        # ---------- 状态栏 ----------
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("就绪。设置参数后点击「开始扫描」")

    # ---------- 扫描控制 ----------
    def toggle_scan(self):
        if self.scanning:
            self.stop_scan()
        else:
            self.start_scan()

    def _collect_params(self):
        return {
            'profit_min': float(self.spin_profit.value()),
            'vol_ratio_min': float(self.spin_vol.value()),
            'zdf_min': float(self.spin_zdf.value()),
            'kline_days': int(self.spin_days.value()),
            'bins': int(self.spin_bins.value()),
        }

    def start_scan(self):
        self._start_scan_internal(mode="filter")

    def toggle_scan_save(self):
        if self.scanning:
            self.stop_scan()
        else:
            self._start_scan_internal(mode="snapshot")

    def _start_scan_internal(self, mode):
        self.clear_table()
        self.scanning = True
        self.scan_mode = mode
        self.btn_scan.setText("停止扫描" if mode == "filter" else "开始扫描")
        self.btn_scan_save.setText("停止扫描" if mode == "snapshot" else "扫描并存快照")
        self.btn_export.setEnabled(False)
        self.btn_clear.setEnabled(False)
        for w in (self.spin_profit, self.spin_vol, self.spin_zdf, self.spin_days, self.spin_bins):
            w.setEnabled(False)
        self.progress.setValue(0)
        self.status.showMessage("正在扫描（存快照模式）…" if mode == "snapshot" else "正在扫描…")

        params = self._collect_params()
        self.thread = QThread()
        self.worker = ChipScanWorker(params=params, max_workers=scf.MAX_WORKERS, mode=mode)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.on_progress)
        self.worker.result_ready.connect(self.on_result_batch)
        self.worker.finished.connect(self.on_finished)
        self.worker.error.connect(self.on_error)
        self.worker.finished.connect(self.thread.quit)
        self.thread.finished.connect(self._cleanup_thread)
        self.thread.start()

    def stop_scan(self):
        if self.worker:
            self.worker.stop()
        self.status.showMessage("正在停止…")

    def _cleanup_thread(self):
        self.thread = None
        self.worker = None

    def on_progress(self, done, total, hit):
        pct = int(done / total * 100) if total else 0
        self.progress.setValue(pct)
        self.progress.setFormat(f"{pct}%  {done}/{total}")
        self.status.showMessage(f"扫描中  已扫描 {done}/{total}  命中 {hit}")

    def on_result_batch(self, batch):
        # 快照模式：只攒数据不显示，结束时统一存盘
        if self.scan_mode == "snapshot":
            self.all_rows.extend(batch)
            self.count_label.setText(f"已采集 {len(self.all_rows)} 只")
            return
        self.all_rows.extend(batch)
        self._append_rows(batch)
        self.count_label.setText(f"共 {len(self.all_rows)} 只")

    def on_finished(self, total, hit, failed, elapsed):
        self.scanning = False
        self.btn_scan.setText("开始扫描")
        self.btn_scan_save.setText("扫描并存快照")
        self.btn_export.setEnabled(True)
        self.btn_clear.setEnabled(True)
        for w in (self.spin_profit, self.spin_vol, self.spin_zdf, self.spin_days, self.spin_bins):
            w.setEnabled(True)
        self.progress.setValue(100)

        # 快照模式：把算好的指标 dict 列表存 CSV
        if self.scan_mode == "snapshot":
            try:
                import datetime
                params = self._collect_params()
                meta = {
                    'saved_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
                    'kline_days': params['kline_days'],
                    'price_bins': params['bins'],
                    'count': len(self.all_rows),
                }
                path = scf.save_snapshot(self.all_rows, meta=meta)
                self.local_snapshot = self.all_rows[:]   # 内存缓存指标
                self.local_meta = meta
                self._lock_snapshot_params(True)   # 锁定 K线天数/切片
                self.status.showMessage(
                    f"快照已保存: {path}  共 {len(self.all_rows)} 只  耗时 {elapsed:.1f}s  现在可调获利盘/量比/涨跌幅离线筛选", 10000
                )
                self._refilter_local()
            except Exception as e:
                QMessageBox.critical(self, "保存快照失败", str(e))
                self.status.showMessage("保存快照失败: " + str(e))
            return

        self.status.showMessage(
            f"扫描完成  总数 {total}  命中 {hit}  耗时 {elapsed:.1f}s", 8000
        )
        self.populate_sector_filter()

    def on_error(self, msg):
        self.scanning = False
        self.btn_scan.setText("开始扫描")
        self.btn_export.setEnabled(True)
        self.btn_clear.setEnabled(True)
        for w in (self.spin_profit, self.spin_vol, self.spin_zdf, self.spin_days, self.spin_bins):
            w.setEnabled(True)
        QMessageBox.critical(self, "扫描出错", msg)
        self.status.showMessage("扫描出错: " + msg)

    # ---------- 排序 ----------
    def _on_header_clicked(self, col):
        if self._sort_col == col:
            self._sort_order = 2 if self._sort_order == 1 else 1
        else:
            self._sort_col = col
            self._sort_order = 2
        self._refresh_sort_marks()
        order = Qt.AscendingOrder if self._sort_order == 1 else Qt.DescendingOrder
        self.table.sortItems(col, order)

    def _refresh_sort_marks(self):
        for i, header in enumerate(self._orig_headers):
            mark = ''
            if i == self._sort_col and self._sort_order == 1:
                mark = ' ▲'
            elif i == self._sort_col and self._sort_order == 2:
                mark = ' ▼'
            self.table.horizontalHeaderItem(i).setText(header + mark)

    # ---------- 表格填充 ----------
    def _append_rows(self, rows):
        start_row = self.table.rowCount()
        self.table.setRowCount(start_row + len(rows))
        for r, item in enumerate(rows):
            self._fill_row(start_row + r, item)

    def _fill_row(self, row, item):
        for col, (field, header, is_num, need_fmt) in enumerate(COLUMNS):
            val = item.get(field, '')
            if need_fmt:
                text = self._format_value(field, val)
            else:
                text = '' if val is None else str(val)

            # 概念板块列：展示完整概念列表（、分隔），多于 4 个则附“等N个”
            if field == '最相关概念':
                concepts = item.get('概念列表', []) or []
                if isinstance(concepts, str):
                    concepts = [c.strip() for c in concepts.split(';') if c.strip()]
                if concepts:
                    shown = '、'.join(concepts[:4])
                    if len(concepts) > 4:
                        shown += f' 等{len(concepts)}个'
                    text = shown
                else:
                    text = '未分类'

            cell = SortableTableWidgetItem(text)
            # 在每个 cell 里存完整行数据，排序后双击仍能取回正确行
            cell.setData(Qt.UserRole + 1, item)
            sort_val = val
            if is_num and isinstance(val, (int, float)):
                sort_val = float(val)
            cell.setData(Qt.UserRole, sort_val if sort_val != '' else 0)

            if is_num:
                cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            else:
                cell.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)

            # 颜色：获利盘越大越红（好），越小越绿；涨跌幅红涨绿跌
            try:
                if field == '获利盘比例(%)':
                    f = float(val)
                    if f >= 80:
                        cell.setForeground(QColor('#e23b3b')); cell.setFont(QFont('', -1, QFont.Bold))
                    elif f >= 60:
                        cell.setForeground(QColor('#d4882f'))
                    else:
                        cell.setForeground(QColor('#1faa52'))
                elif field == '套牢盘比例(%)':
                    f = float(val)
                    if f <= 20:
                        cell.setForeground(QColor('#e23b3b'))
                    elif f <= 40:
                        cell.setForeground(QColor('#d4882f'))
                    else:
                        cell.setForeground(QColor('#1faa52'))
                elif field == '涨跌幅(%)':
                    f = float(val)
                    if f > 0:
                        cell.setForeground(QColor('#e23b3b'))
                    elif f < 0:
                        cell.setForeground(QColor('#1faa52'))
                elif field == '距筹码峰(%)':
                    f = float(val)
                    if f > 0:
                        cell.setForeground(QColor('#e23b3b'))
                    elif f < 0:
                        cell.setForeground(QColor('#1faa52'))
            except (TypeError, ValueError):
                pass

            self.table.setItem(row, col, cell)

    def _format_value(self, field, val):
        try:
            if field == '成交额':
                return format_number(float(val)) if val else '0'
            if field == '涨跌幅(%)':
                return f"{float(val):.2f}%" if val != '' else '0.00%'
            if field == '换手率(%)':
                return f"{float(val):.2f}%" if val != '' else '0.00%'
            if field == '距筹码峰(%)':
                f = float(val)
                return f"{f:+.2f}%" if abs(f) > 0.001 else "0.00%"
        except (TypeError, ValueError):
            pass
        return '' if val is None else str(val)

    # ---------- 筛选 ----------
    def populate_sector_filter(self):
        """从当前 all_rows 提取所有概念，按数量排序填入板块下拉。"""
        from collections import Counter
        cnt = Counter()
        for r in self.all_rows:
            concepts = r.get('概念列表', [])
            if isinstance(concepts, str):
                concepts = [c.strip() for c in concepts.split(';') if c.strip()]
            for c in (concepts or []):
                cnt[c] += 1
        # 记住当前选中
        cur = self.sector_filter.currentData()
        self.sector_filter.blockSignals(True)
        self.sector_filter.clear()
        self.sector_filter.addItem(f"全部板块 ({len(self.all_rows)})", "")
        for name, n in cnt.most_common():
            self.sector_filter.addItem(f"{name} ({n})", name)
        # 恢复选中
        if cur:
            idx = self.sector_filter.findData(cur)
            if idx >= 0:
                self.sector_filter.setCurrentIndex(idx)
        self.sector_filter.blockSignals(False)
        self._sector_box_ready = True

    def apply_filter(self, *_):
        kw = self.filter_box.text().strip().lower()
        field = self.col_filter.currentData()
        sector = self.sector_filter.currentData() if self._sector_box_ready else ""
        shown = 0
        for row in range(self.table.rowCount()):
            match = True
            # 板块筛选：该行概念列表需含选中板块
            if sector:
                row_data = self.table.item(row, 0).data(Qt.UserRole + 1) if self.table.item(row, 0) else None
                concepts = (row_data or {}).get('概念列表', []) if row_data else []
                if isinstance(concepts, str):
                    concepts = [c.strip() for c in concepts.split(';') if c.strip()]
                if sector not in (concepts or []):
                    match = False
            # 关键词筛选
            if match and kw:
                if field:
                    col = next((i for i, (f, *_) in enumerate(COLUMNS) if f == field), None)
                    if col is not None:
                        c_item = self.table.item(row, col)
                        txt = c_item.text().lower() if c_item else ''
                        match = kw in txt
                    else:
                        match = False
                else:
                    match = any(
                        kw in (self.table.item(row, c).text().lower() if self.table.item(row, c) else '')
                        for c in range(self.table.columnCount())
                    )
            self.table.setRowHidden(row, not match)
            if match:
                shown += 1
        self.count_label.setText(f"显示 {shown} / 共 {len(self.all_rows)} 只")

    # ---------- 其他 ----------
    def clear_table(self):
        self.table.setRowCount(0)
        self.all_rows = []
        self.count_label.setText("共 0 只")
        self.progress.setValue(0)
        self._sort_col = None
        self._sort_order = 0
        for i, header in enumerate(self._orig_headers):
            self.table.horizontalHeaderItem(i).setText(header)
        # 清空本地快照引用 + 解锁参数（但不删磁盘文件）
        self.local_snapshot = None
        self.local_meta = {}
        self._lock_snapshot_params(False)
        # 清空板块下拉
        self.sector_filter.blockSignals(True)
        self.sector_filter.clear()
        self.sector_filter.addItem("全部板块", "")
        self._sector_box_ready = False
        self.sector_filter.blockSignals(False)
        self.status.showMessage("已清空")

    # ---------- 本地快照筛选 ----------
    def load_local_snapshot(self):
        """从本地 CSV 加载指标快照，按当前参数筛选显示。"""
        rows, meta = scf.load_snapshot()
        if not rows:
            QMessageBox.information(self, "提示", "未找到本地快照,请先点「扫描并存快照」。")
            return
        self.local_snapshot = rows
        self.local_meta = meta or {}
        n = len(rows)
        self.status.showMessage(
            f"已加载本地快照: {n} 只  采集于 {self.local_meta.get('saved_at','?')}  按当前参数筛选…"
        )
        # 同步 K线天数/切片到快照值，并锁定
        if self.local_meta.get('kline_days'):
            self.spin_days.setValue(int(self.local_meta['kline_days']))
        if self.local_meta.get('price_bins'):
            self.spin_bins.setValue(int(self.local_meta['price_bins']))
        self._lock_snapshot_params(True)
        self._refilter_local()

    def _lock_snapshot_params(self, locked):
        """加载快照后锁定 K线天数/切片（改它们需要重新扫描）。locked=True 禁用。"""
        # locked=True 表示「已锁定」，控件禁用
        self.spin_days.setEnabled(not locked)
        self.spin_bins.setEnabled(not locked)
        tip = "（已锁定，改此项需重新扫描存快照）" if locked else ""
        self.spin_days.setToolTip(f"K线天数{tip}")
        self.spin_bins.setToolTip(f"价位切片{tip}")

    def _on_param_changed_for_local(self):
        """参数变化时，若已有本地快照则实时重筛。"""
        if self.local_snapshot:
            self._refilter_local()

    def _refilter_local(self):
        """用当前界面参数在本地指标快照上筛选（纯数值比较，毫秒级）。"""
        if not self.local_snapshot:
            return
        profit_min = float(self.spin_profit.value())
        vol_min = float(self.spin_vol.value())
        zdf_min = float(self.spin_zdf.value())
        filtered = scf.filter_local(self.local_snapshot, profit_min, vol_min, zdf_min)
        # 按获利盘降序、量比降序排
        filtered.sort(key=lambda r: (float(r.get('获利盘比例(%)', 0)), float(r.get('量比', 0))), reverse=True)
        self.table.setRowCount(0)
        self.all_rows = filtered[:]
        self._append_rows(filtered)
        self.populate_sector_filter()
        n_total = len(self.local_snapshot)
        self.count_label.setText(f"显示 {len(filtered)} / 快照 {n_total} 只")
        self.status.showMessage(
            f"本地筛选: {len(filtered)} 只符合  采集于 {self.local_meta.get('saved_at','?')}  快照共 {n_total} 只"
        )


    def show_detail(self, item):
        # 双击仍弹独立大窗（可选）
        row_data = item.data(Qt.UserRole + 1)
        if not row_data:
            row = item.row()
            if row >= len(self.all_rows):
                return
            row_data = self.all_rows[row]
        dlg = StockDetailDialog(row_data, self)
        dlg.exec_()

    def _on_selection_changed(self):
        """表格选中行变化（含键盘上下键）→ 防抖刷新下方 K 线。"""
        items = self.table.selectedItems()
        if not items:
            return
        item = items[0]
        row_data = item.data(Qt.UserRole + 1)
        if not row_data:
            return
        # 首次点击时显示 K 线区
        if not self.kline_container.isVisible():
            self.show_kline()
        # 防抖：快速切换时只刷新最后一行，避免连按上下键卡顿
        self._pending_row = row_data
        self._kline_timer.start(150)

    def _flush_pending_kline(self):
        if self._pending_row is None:
            return
        self._refresh_kline(self._pending_row)
        self._pending_row = None

    def show_kline(self):
        """显示下方 K 线区。"""
        self.kline_container.setVisible(True)

    def hide_kline(self):
        """隐藏并清空下方 K 线区。"""
        self.kline_container.setVisible(False)
        # 清掉选中，避免上下键又把它弄出来；同时清空 K 线内容
        self.table.clearSelection()
        self.kline_info_label.setText("（点击表格行查看 K 线）")
        if self.kline_widget is not None:
            kl = self.kline_container.layout()
            # top_bar 是 layout 的第 0 项，kline_widget 是第 1 项
            kl.removeWidget(self.kline_widget)
            self.kline_widget.deleteLater()
            self.kline_widget = None

    def _refresh_kline(self, row_data):
        """刷新下方 K 线区。"""
        import numpy as np
        code_raw = row_data.get('代码', '')
        code = code_raw.split('.')[0]
        name = row_data.get('名称', '')

        # 顶部信息条
        self.kline_info_label.setText(
            f"{name} ({code})   收盘 {row_data.get('收盘价','')}   "
            f"涨跌幅 {row_data.get('涨跌幅(%)','')}%   量比 {row_data.get('量比','')}   "
            f"获利盘 {row_data.get('获利盘比例(%)','')}%   套牢盘 {row_data.get('套牢盘比例(%)','')}%"
        )

        # 拉 K 线 + 筹码
        kline_df = scf.get_kline_data(code, days=scf.KLINE_DAYS)
        chip_info = None
        if kline_df is not None and len(kline_df) >= 5:
            res = scf.build_chip_distribution(kline_df, bins=scf.PRICE_BINS)
            if res is not None:
                centers, chip, total = res
                cur = float(kline_df['close'].iloc[-1])
                above = centers > cur
                trapped = float(chip[above].sum()) / total
                profit = 1.0 - trapped
                peak = float(centers[int(np.argmax(chip))])
                chip_info = (profit, trapped, peak, centers, chip, total)

        # 替换 K 线 widget
        kl = self.kline_container.layout()
        if self.kline_widget is not None:
            kl.removeWidget(self.kline_widget)
            self.kline_widget.deleteLater()
        if kline_df is None or len(kline_df) < 5:
            self.kline_widget = QLabel("K线数据获取失败")
            self.kline_widget.setStyleSheet("color:#aaa; padding:20px;")
        else:
            self.kline_widget = KLineWidget(kline_df, chip_info)
        kl.addWidget(self.kline_widget, 1)
        self.kline_container.update()


    def export_csv(self):
        if not self.all_rows:
            QMessageBox.information(self, "提示", "当前没有数据可导出")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 CSV", f"筹码峰套牢盘_{pd.Timestamp.now().strftime('%Y%m%d')}.csv",
            "CSV 文件 (*.csv)"
        )
        if not path:
            return
        try:
            df = pd.DataFrame(self.all_rows)
            df.to_csv(path, index=False, encoding='utf-8-sig')
            QMessageBox.information(self, "导出成功", f"已保存到:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    def closeEvent(self, event):
        if self.scanning and self.worker:
            self.worker.stop()
            if self.thread:
                self.thread.quit()
                self.thread.wait(3000)
        event.accept()


def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
