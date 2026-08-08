# -*- coding: utf-8 -*-
"""
趋势中继多策略选股扫描器 - PyQt5 桌面版 v2
==========================================
v2 修复：
  1. 实时K线（选中行即显示，不用双击）- splitter 分栏 + 防抖定时器
  2. 本地快照（扫描存全量特征，可离线加载筛选）
  3. 修复闪退（异常捕获 + 线程安全）

运行： py trend_continuation_gui.py
"""

import os
import sys
import json
import time
from collections import Counter

import numpy as np
import pandas as pd
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QColor, QFont, QPainter, QPen
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QLabel, QTableWidget, QTableWidgetItem,
    QProgressBar, QStatusBar, QHeaderView, QComboBox, QMessageBox,
    QFileDialog, QGroupBox, QSplitter, QCheckBox, QFrame, QSpinBox,
)

from stock_full_scan import get_kline_data, get_stock_raw, get_stock_industry
from trend_continuation_scanner import (
    scan_market, scan_one, PATTERN_DETECTORS,
    KLINE_DAYS, MAX_WORKERS,
)

# 数值型表格Item（保证按数值排序而非字符串）
class _NumItem(QTableWidgetItem):
    def __lt__(self, other):
        try:
            return float(self.text()) < float(other.text())
        except Exception:
            return super().__lt__(other)


# 形态中文名 + 颜色
PATTERN_INFO = {
    'P1': ('放量涨后缩量回调', '#4ade80'),
    'P2': ('缩量横盘后放量突破', '#38bdf8'),
    'P3': ('温和放量走趋势', '#fbbf24'),
    'P4': ('缩量回踩MA20支撑', '#a78bfa'),
    'P5': ('放量破前高后缩量回踩', '#f472b6'),
    'P6': ('老鸭头形态', '#fb923c'),
    'P7': ('沿5日线缓慢上升', '#2dd4bf'),
    'P8': ('均线吻(飞吻/唇吻/湿吻)', '#facc15'),
    'P9': ('2B底部反转', '#f87171'),
    'P10': ('缩量十字星', '#94a3b8'),
    'P11': ('红三兵', '#ef4444'),
    'P12': ('早晨之星', '#fbbf24'),
    'P13': ('上升三法', '#34d399'),
    'P14': ('量价齐升', '#60a5fa'),
    'P15': ('多头疏散后飞吻', '#e879f9'),
    'P16': ('死叉后企稳放量', '#fca5a5'),
    'P17': ('主升后调整企稳放量', '#c084fc'),
}

SNAPSHOT_DIR = "snapshot"
SNAPSHOT_FILE = os.path.join(SNAPSHOT_DIR, "trend_features.csv")
SNAPSHOT_META = os.path.join(SNAPSHOT_DIR, "trend_meta.json")


# ============ 扫描线程 ============
class ScanThread(QThread):
    progress = pyqtSignal(int, int)
    result = pyqtSignal(list)
    finished_msg = pyqtSignal(str)

    def __init__(self, codes=None, days=None):
        super().__init__()
        self.codes = codes
        self.days = days
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        def on_progress(d, t):
            self.progress.emit(d, t)
        def stop_check():
            return self._stop
        try:
            results = scan_market(codes=self.codes, on_progress=on_progress,
                                  stop_check=stop_check, days=self.days)
            if self._stop:
                self.finished_msg.emit('扫描已停止')
            else:
                self.result.emit(results)
        except Exception as e:
            self.finished_msg.emit(f'扫描出错: {e}')


# ============ K线异步加载线程 ============
class KlineLoadThread(QThread):
    """子线程加载K线数据，避免主线程网络请求阻塞/崩溃"""
    loaded = pyqtSignal(str, object, list, str)   # code, df, hits, info
    failed = pyqtSignal(str, str)                  # code, error

    def __init__(self, code, hits, info, days=120):
        super().__init__()
        self.code = code
        self.hits = hits
        self.info = info
        self.days = days

    def run(self):
        try:
            df = get_kline_data(self.code, days=self.days)
            if df is None or len(df) < 2:
                self.failed.emit(self.code, 'K线数据不足')
                return
            self.loaded.emit(self.code, df, self.hits, self.info)
        except Exception as e:
            self.failed.emit(self.code, str(e))


# ============ K线画布 ============
class KlineCanvas(QFrame):
    """自绘 K 线图（蜡烛+成交量+MA5/10/20/60+BOLL+MACD+KDJ+RSI+形态标注）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background:#0b1220;")
        self.setMinimumHeight(280)
        self._df = None
        self._hits = []
        self._info = ""
        self.show_flags = {'ma': True, 'boll': False, 'macd': False, 'kdj': False, 'rsi': False}

    def set_data(self, df, hits, info):
        self._df = df
        self._hits = hits or []
        self._info = info or ""
        self.update()

    def _y_of(self, val, pmin, pmax, price_h, pad_top=0):
        """价格 -> y 坐标（含 pad_top 偏移）。价格越高 y 越小（Qt y 轴向下）。"""
        return int((pmax - val) / (pmax - pmin) * price_h) + pad_top

    def _draw_line(self, p, series, pmin, pmax, price_h, kw, n, cw, color, width=1.5, x_offset=20, pad_top=0):
        pen = QPen(color, width); p.setPen(pen)
        prev = None
        step = kw / n
        for i in range(n):
            val = series.iloc[i] if hasattr(series, 'iloc') else series[i]
            if val != val:
                prev = None; continue
            x = int(x_offset + i * step + cw / 2)
            y = self._y_of(float(val), pmin, pmax, price_h, pad_top)
            if prev:
                p.drawLine(prev[0], prev[1], x, y)
            prev = (x, y)

    def _draw_sub_line(self, p, series, sub_pmin, sub_pmax, sub_h, sub_top, kw, n, cw, color, width=1.5, x_offset=20):
        pen = QPen(color, width); p.setPen(pen)
        prev = None
        step = kw / n
        for i in range(n):
            val = series.iloc[i] if hasattr(series, 'iloc') else series[i]
            if val != val:
                prev = None; continue
            x = int(x_offset + i * step + cw / 2)
            y = int((sub_pmax - float(val)) / (sub_pmax - sub_pmin) * sub_h) + sub_top
            if prev:
                p.drawLine(prev[0], prev[1], x, y)
            prev = (x, y)

    def paintEvent(self, event):
        try:
            self._paint(event)
        except Exception as e:
            painter = QPainter(self)
            painter.fillRect(0, 0, self.width(), self.height(), QColor('#0b1220'))
            painter.setPen(QColor('#ef4444'))
            painter.drawText(10, 20, f"K线绘制异常: {e}")

    def _paint(self, event):
        from PyQt5.QtGui import QBrush
        from PyQt5.QtCore import QRectF
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w = self.width()
        h = self.height()
        p.fillRect(0, 0, w, h, QColor('#0b1220'))

        # 信息条
        p.setPen(QColor('#e2e8f0'))
        f = QFont(); f.setPointSize(9); p.setFont(f)
        p.drawText(10, 18, self._info[:120])

        if self._df is None or len(self._df) < 2:
            p.setPen(QColor('#64748b'))
            p.drawText(w // 2 - 60, h // 2, "点击表格行查看 K 线")
            return

        df = self._df
        n = len(df)
        x_offset = 20
        kw = w - 40  # 画图宽度

        # 确定副图数量
        sub_indicators = [k for k in ('macd', 'kdj', 'rsi') if self.show_flags.get(k, False)]
        n_sub = len(sub_indicators)

        # 垂直布局：信息(24) + 主图 + 量图 + N副图
        pad_top = 28
        avail = h - pad_top - 10
        if n_sub == 0:
            price_h = int(avail * 0.68)
            vol_h = int(avail * 0.28)
        else:
            price_h = int(avail * max(0.38, 0.55 - n_sub * 0.06))
            vol_h = int(avail * 0.15)
            sub_h_each = (avail - price_h - vol_h) // n_sub if n_sub else 0
        vol_top = pad_top + price_h + 6
        sub_regions = []
        cur_top = vol_top + vol_h + 6
        for name in sub_indicators:
            sub_regions.append((name, cur_top, sub_h_each))
            cur_top += sub_h_each + 4

        # 数据转 float
        highs = df['high'].astype(float).values
        lows = df['low'].astype(float).values
        opens = df['open'].astype(float).values
        closes = df['close'].astype(float).values
        vols = df['volume'].astype(float).values
        close_s = df['close'].astype(float)

        pmin, pmax = float(lows.min()), float(highs.max())
        if pmax <= pmin:
            pmax = pmin + 1
        # BOLL 纳入范围
        if self.show_flags.get('boll', False) and n >= 20:
            ma20 = close_s.rolling(20).mean()
            std20 = close_s.rolling(20).std()
            boll_lo = ma20 - 2 * std20
            boll_up = ma20 + 2 * std20
            pmin = min(pmin, float(np.nanmin(boll_lo.values)))
            pmax = max(pmax, float(np.nanmax(boll_up.values)))
        pad = (pmax - pmin) * 0.05
        pmin -= pad; pmax += pad

        cw = max(3, kw / n - 1)
        vmax = float(vols.max()) if vols.max() > 0 else 1.0

        # 网格线 + 价格刻度
        p.setPen(QPen(QColor('#2a3142'), 1, Qt.DashLine))
        for gi in range(5):
            y = pad_top + int(price_h * gi / 4)
            p.drawLine(x_offset, y, x_offset + kw, y)
            price = pmax - (pmax - pmin) * gi / 4
            p.setPen(QColor('#7a8499'))
            p.drawText(x_offset + 2, y + 12, f"{price:.2f}")
            p.setPen(QPen(QColor('#2a3142'), 1, Qt.DashLine))

        # 蜡烛图 + 成交量
        for i in range(n):
            x = int(x_offset + i * kw / n + cw / 2)
            o, c = float(opens[i]), float(closes[i])
            hi, lo = float(highs[i]), float(lows[i])
            up = c >= o
            color = QColor('#ef4444') if up else QColor('#22c55e')
            p.setPen(QPen(color, 1))
            y_hi = self._y_of(hi, pmin, pmax, price_h) + pad_top
            y_lo = self._y_of(lo, pmin, pmax, price_h) + pad_top
            p.drawLine(x, y_hi, x, y_lo)
            y_o = self._y_of(o, pmin, pmax, price_h) + pad_top
            y_c = self._y_of(c, pmin, pmax, price_h) + pad_top
            body_h = max(1, abs(y_o - y_c))
            p.setBrush(QBrush(color))
            p.drawRect(QRectF(x - cw / 2, min(y_o, y_c), cw, body_h))
            # 成交量
            v_h = int(vols[i] / vmax * vol_h)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(color))
            p.drawRect(QRectF(x - cw / 2, vol_top + vol_h - v_h, cw, v_h))

        # ---- 主图叠加指标 ----
        # 均线 MA5/10/20/60
        if self.show_flags.get('ma', True):
            ma_defs = [(5, QColor('#ffcc00')), (10, QColor('#ff6a00')),
                       (20, QColor('#a974ff')), (60, QColor('#36c5f0'))]
            for period, col in ma_defs:
                if n < period:
                    continue
                ma = close_s.rolling(period).mean()
                self._draw_line(p, ma, pmin, pmax, price_h, kw, n, cw, col, 1.5, x_offset, pad_top)
            # 图例
            lx = x_offset + kw - 280
            for period, col in ma_defs:
                if n < period:
                    continue
                p.setPen(col)
                p.drawLine(lx, pad_top + 10, lx + 16, pad_top + 10)
                p.setPen(QColor('#cfd6e4'))
                p.drawText(lx + 20, pad_top + 14, f"MA{period}")
                lx += 70

        # BOLL
        if self.show_flags.get('boll', False) and n >= 20:
            self._draw_line(p, boll_up, pmin, pmax, price_h, kw, n, cw, QColor('#8a8f9c'), 1, x_offset, pad_top)
            self._draw_line(p, boll_lo, pmin, pmax, price_h, kw, n, cw, QColor('#8a8f9c'), 1, x_offset, pad_top)
            self._draw_line(p, ma20, pmin, pmax, price_h, kw, n, cw, QColor('#ffcc00'), 1, x_offset, pad_top)
            p.setPen(QColor('#cfd6e4'))
            p.drawText(x_offset + kw - 70, pad_top + 14, "BOLL")

        # 成交量标题
        p.setPen(QColor('#7a8499'))
        p.drawText(x_offset + 2, vol_top + 12, "成交量")

        # ---- 副图指标 ----
        for name, sub_top, sub_h in sub_regions:
            if name == 'macd':
                ema12 = close_s.ewm(span=12, adjust=False).mean()
                ema26 = close_s.ewm(span=26, adjust=False).mean()
                dif = ema12 - ema26
                dea = dif.ewm(span=9, adjust=False).mean()
                macd = (dif - dea) * 2
                allvals = list(dif.dropna()) + list(dea.dropna()) + list(macd.dropna())
                if allvals:
                    spmin, spmax = min(allvals), max(allvals)
                    if spmax <= spmin: spmax = spmin + 1
                    pad2 = (spmax - spmin) * 0.1; spmin -= pad2; spmax += pad2
                    zero_y = int((spmax - 0) / (spmax - spmin) * sub_h) + sub_top
                    p.setPen(QPen(QColor('#2a3142'), 1, Qt.DashLine))
                    p.drawLine(x_offset, zero_y, x_offset + kw, zero_y)
                    # MACD 柱
                    p.setPen(Qt.NoPen)
                    for i in range(n):
                        val = macd.iloc[i]
                        if val != val: continue
                        x = int(x_offset + i * kw / n + cw / 2)
                        y = int((spmax - float(val)) / (spmax - spmin) * sub_h) + sub_top
                        col = QColor('#ef4444') if float(val) >= 0 else QColor('#22c55e')
                        p.setBrush(QBrush(col))
                        p.drawRect(QRectF(x - cw / 2, min(y, zero_y), max(1, cw), abs(y - zero_y)))
                    self._draw_sub_line(p, dif, spmin, spmax, sub_h, sub_top, kw, n, cw, QColor('#ffcc00'), 1.5, x_offset)
                    self._draw_sub_line(p, dea, spmin, spmax, sub_h, sub_top, kw, n, cw, QColor('#ff6a00'), 1.5, x_offset)
                    p.setPen(QColor('#7a8499'))
                    p.drawText(x_offset + 2, sub_top + 12, f"MACD  DIF={float(dif.iloc[-1]):.3f}  DEA={float(dea.iloc[-1]):.3f}")

            elif name == 'kdj':
                low_n = df['low'].astype(float).rolling(9, min_periods=1).min()
                high_n = df['high'].astype(float).rolling(9, min_periods=1).max()
                rsv = (close_s - low_n) / (high_n - low_n) * 100
                rsv = rsv.fillna(50)
                k = rsv.ewm(com=2, adjust=False).mean()
                d = k.ewm(com=2, adjust=False).mean()
                j = 3 * k - 2 * d
                spmin, spmax = float(j.min()), float(j.max())
                if spmax <= spmin: spmax = spmin + 1
                pad2 = (spmax - spmin) * 0.1; spmin -= pad2; spmax += pad2
                for ref in (20, 50, 80):
                    ry = int((spmax - ref) / (spmax - spmin) * sub_h) + sub_top
                    p.setPen(QPen(QColor('#2a3142'), 1, Qt.DashLine))
                    p.drawLine(x_offset, ry, x_offset + kw, ry)
                self._draw_sub_line(p, k, spmin, spmax, sub_h, sub_top, kw, n, cw, QColor('#ffcc00'), 1.5, x_offset)
                self._draw_sub_line(p, d, spmin, spmax, sub_h, sub_top, kw, n, cw, QColor('#ff6a00'), 1.5, x_offset)
                self._draw_sub_line(p, j, spmin, spmax, sub_h, sub_top, kw, n, cw, QColor('#a974ff'), 1.5, x_offset)
                p.setPen(QColor('#7a8499'))
                p.drawText(x_offset + 2, sub_top + 12, f"KDJ  K={float(k.iloc[-1]):.2f}  D={float(d.iloc[-1]):.2f}  J={float(j.iloc[-1]):.2f}")

            elif name == 'rsi':
                delta = close_s.diff()
                gain = delta.clip(lower=0)
                loss = -delta.clip(upper=0)
                avg_gain = gain.ewm(alpha=1 / 6, adjust=False).mean()
                avg_loss = loss.ewm(alpha=1 / 6, adjust=False).mean()
                rs = avg_gain / avg_loss.replace(0, np.nan)
                rsi = (100 - 100 / (1 + rs)).fillna(50)
                spmin, spmax = 0, 100
                for ref in (20, 50, 80):
                    ry = int((spmax - ref) / (spmax - spmin) * sub_h) + sub_top
                    p.setPen(QPen(QColor('#2a3142'), 1, Qt.DashLine))
                    p.drawLine(x_offset, ry, x_offset + kw, ry)
                self._draw_sub_line(p, rsi, spmin, spmax, sub_h, sub_top, kw, n, cw, QColor('#36c5f0'), 1.5, x_offset)
                p.setPen(QColor('#7a8499'))
                p.drawText(x_offset + 2, sub_top + 12, f"RSI(6)  {float(rsi.iloc[-1]):.2f}")

        # 形态标注（右上角）
        if self._hits:
            tags = "  ".join(f"[{h['pattern']}]" for h in self._hits)
            p.setPen(QColor('#fbbf24'))
            f2 = QFont(); f2.setPointSize(9); f2.setBold(True); p.setFont(f2)
            p.drawText(w - 200, 18, f"形态: {tags}")


# ============ 主窗 ============
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("趋势中继多策略选股扫描器 v2")
        self.resize(1280, 820)
        self.results = []
        self.scan_thread = None
        self._pending_row = None
        self.init_ui()
        self.setStyleSheet(self._dark_style())

    def _dark_style(self):
        return """
        QMainWindow { background: #0f172a; }
        QGroupBox { color: #e2e8f0; border: 1px solid #334155; border-radius: 6px;
                     margin-top: 12px; padding-top: 12px; font-weight: bold; }
        QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
        QLabel { color: #e2e8f0; }
        QLineEdit { background: #1e293b; color: #e2e8f0; border: 1px solid #334155;
                     border-radius: 4px; padding: 5px; }
        QPushButton { background: #334155; color: #e2e8f0; border: none;
                       border-radius: 4px; padding: 6px 14px; font-weight: bold; }
        QPushButton:hover { background: #475569; }
        QPushButton:pressed { background: #1e293b; }
        QPushButton#scanBtn { background: #fbbf24; color: #000; }
        QPushButton#scanBtn:hover { background: #f59e0b; }
        QPushButton#stopBtn { background: #ef4444; color: #fff; }
        QTableWidget { background: #151c2c; color: #e2e8f0; gridline-color: #2a3550;
                        border: 1px solid #334155; }
        QHeaderView::section { background: #1e293b; color: #fbbf24; border: none;
                                padding: 6px; font-weight: bold; }
        QProgressBar { background: #1e293b; border: 1px solid #334155; border-radius: 4px;
                        text-align: center; color: #e2e8f0; }
        QProgressBar::chunk { background: #fbbf24; border-radius: 3px; }
        QStatusBar { background: #1e293b; color: #94a3b8; }
        QComboBox { background: #1e293b; color: #e2e8f0; border: 1px solid #334155;
                     border-radius: 4px; padding: 4px; }
        """

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # ---- 顶部控制栏 ----
        ctrl = QGroupBox("扫描控制")
        ctrl_l = QHBoxLayout(ctrl)
        ctrl_l.addWidget(QLabel("指定代码(逗号分隔,空=全市场):"))
        self.code_edit = QLineEdit()
        self.code_edit.setPlaceholderText("例: 603296,300718  留空=全市场扫描")
        ctrl_l.addWidget(self.code_edit)
        ctrl_l.addWidget(QLabel("形态筛选:"))
        self.pat_combo = QComboBox()
        self.pat_combo.addItem("全部形态", "")
        for k, (name, _) in PATTERN_INFO.items():
            self.pat_combo.addItem(f"{k} {name}", k)
        self.pat_combo.currentIndexChanged.connect(self._apply_pattern_filter)
        ctrl_l.addWidget(self.pat_combo)

        ctrl_l.addWidget(QLabel("K线天数:"))
        self.spin_days = QSpinBox()
        self.spin_days.setRange(30, 300)
        self.spin_days.setSingleStep(10)
        self.spin_days.setValue(KLINE_DAYS)
        self.spin_days.setFixedWidth(70)
        self.spin_days.setToolTip("K线回看天数（影响形态识别+显示）")
        ctrl_l.addWidget(self.spin_days)

        ctrl_l.addWidget(QLabel("板块:"))
        self.sector_filter = QComboBox()
        self.sector_filter.setFixedHeight(30)
        self.sector_filter.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.sector_filter.setMinimumContentsLength(18)   # 按内容自适应宽度
        self.sector_filter.addItem("全部板块", "")
        self.sector_filter.currentIndexChanged.connect(self._apply_sector_filter)
        self._sector_box_ready = False
        ctrl_l.addWidget(self.sector_filter, 2)   # stretch=2 让它占更多空间

        ctrl_l.addWidget(QLabel("搜索:"))
        self.filter_box = QLineEdit()
        self.filter_box.setPlaceholderText("代码/名称/概念 实时筛选...")
        self.filter_box.setFixedWidth(180)
        self.filter_box.textChanged.connect(self._apply_sector_filter)
        ctrl_l.addWidget(self.filter_box)

        self.scan_btn = QPushButton("扫描")
        self.scan_btn.setObjectName("scanBtn")
        self.scan_btn.clicked.connect(self.toggle_scan)
        ctrl_l.addWidget(self.scan_btn)

        self.snap_btn = QPushButton("扫描并存快照")
        self.snap_btn.clicked.connect(self.start_scan_snapshot)
        ctrl_l.addWidget(self.snap_btn)

        self.load_btn = QPushButton("加载本地快照")
        self.load_btn.clicked.connect(self.load_snapshot)
        ctrl_l.addWidget(self.load_btn)

        self.export_btn = QPushButton("导出CSV")
        self.export_btn.clicked.connect(self.export_csv)
        ctrl_l.addWidget(self.export_btn)
        layout.addWidget(ctrl)

        # ---- splitter: 表格 + K线 ----
        self.splitter = QSplitter(Qt.Vertical)
        # 表格
        self.table = QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels(
            ["代码", "名称", "现价", "涨跌%", "命中形态", "评分", "评分依据", "行业", "概念", "形态详情"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(9, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 70); self.table.setColumnWidth(1, 90)
        self.table.setColumnWidth(2, 70); self.table.setColumnWidth(3, 70)
        self.table.setColumnWidth(4, 110); self.table.setColumnWidth(5, 55)
        self.table.setColumnWidth(6, 180); self.table.setColumnWidth(7, 90)
        self.table.setColumnWidth(8, 180)
        self.table.setSortingEnabled(True)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.splitter.addWidget(self.table)
        # K线区
        self.kline_container = QWidget()
        self.kline_container.setMinimumHeight(200)
        kl = QVBoxLayout(self.kline_container)
        kl.setContentsMargins(2, 2, 2, 2)
        # 顶部信息条 + 指标勾选
        top_bar = QHBoxLayout()
        top_bar.setContentsMargins(0, 0, 0, 0)
        self.kline_info = QLabel("（点击表格行查看 K 线）")
        self.kline_info.setStyleSheet("color:#cfd6e4; padding:2px;")
        top_bar.addWidget(self.kline_info, 1)
        top_bar.addWidget(QLabel("指标:"))
        self.cb_ind_ma = QCheckBox("均线")
        self.cb_ind_ma.setChecked(True)
        self.cb_ind_boll = QCheckBox("BOLL")
        self.cb_ind_macd = QCheckBox("MACD")
        self.cb_ind_kdj = QCheckBox("KDJ")
        self.cb_ind_rsi = QCheckBox("RSI")
        for cb in (self.cb_ind_ma, self.cb_ind_boll, self.cb_ind_macd,
                    self.cb_ind_kdj, self.cb_ind_rsi):
            cb.setFixedHeight(24)
            cb.setStyleSheet("QCheckBox{color:#cfd6e4; padding:0 4px}")
            cb.toggled.connect(self._on_indicator_toggled)
            top_bar.addWidget(cb)
        kl.addLayout(top_bar)
        self.kline_canvas = KlineCanvas()
        kl.addWidget(self.kline_canvas)
        self.kline_container.setVisible(False)
        self.splitter.addWidget(self.kline_container)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes([500, 320])
        layout.addWidget(self.splitter, 1)

        # ---- 进度条 ----
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        # ---- 状态栏 ----
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.stat_label = QLabel("就绪")
        self.status.addWidget(self.stat_label)

        # 防抖定时器
        self._kline_timer = QTimer(self)
        self._kline_timer.setSingleShot(True)
        self._kline_timer.timeout.connect(self._flush_pending_kline)
        # K线加载线程引用（避免被GC）
        self._kline_loader = None

    # ---------- 扫描控制 ----------
    def toggle_scan(self):
        if self.scan_thread and self.scan_thread.isRunning():
            self.stop_scan()
        else:
            self.start_scan()

    def start_scan(self, snapshot=False):
        code_text = self.code_edit.text().strip()
        codes = [c.strip() for c in code_text.split(',') if c.strip()] if code_text else None
        self.scan_btn.setText("停止")
        self.snap_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.table.setRowCount(0)
        self.stat_label.setText("扫描中（存快照）..." if snapshot else "扫描中...")
        self._snapshot_mode = snapshot
        self.scan_thread = ScanThread(codes=codes, days=self.spin_days.value())
        self.scan_thread.progress.connect(self.on_progress)
        self.scan_thread.result.connect(self.on_result)
        self.scan_thread.finished_msg.connect(self.on_finished)
        self.scan_thread.start()

    def start_scan_snapshot(self):
        self.start_scan(snapshot=True)

    def stop_scan(self):
        if self.scan_thread:
            self.scan_thread.stop()
        self.stat_label.setText("停止中...")

    def on_progress(self, done, total):
        self.progress.setMaximum(total)
        self.progress.setValue(done)
        self.stat_label.setText(f"扫描中... {done}/{total} ({done*100//total}%)")

    def on_result(self, results):
        self.results = results
        self.populate_table(results)
        self._populate_sector_filter(results)
        pc = Counter()
        for r in results:
            for h in r['hits']:
                pc[h['pattern']] += 1
        stat = " | ".join(f"{PATTERN_INFO[k][0]}: {v}" for k, v in sorted(pc.items()))
        self.stat_label.setText(f"完成 | 命中 {len(results)} 只 | {stat}")

        # 快照模式：存盘
        if getattr(self, '_snapshot_mode', False) and results:
            self._save_snapshot(results)
        self.on_finished("")

    def on_finished(self, msg):
        if msg:
            self.stat_label.setText(msg)
        self.scan_btn.setText("扫描")
        self.snap_btn.setEnabled(True)
        self.progress.setVisible(False)

    # ---------- 快照 ----------
    def _save_snapshot(self, results):
        try:
            os.makedirs(SNAPSHOT_DIR, exist_ok=True)
            rows = []
            for r in results:
                for h in r['hits']:
                    rows.append({
                        'code': r['code'], 'name': r['name'], 'close': r['close'],
                        'zdf': round(r['zdf'], 2), 'industry': r.get('industry', ''),
                        'concept': r.get('concept', ''),
                        'pattern': h['pattern'], 'pattern_name': PATTERN_INFO[h['pattern']][0],
                        'score': round(h['score'], 1),
                        **{k: v for k, v in h.items() if k not in ('pattern', 'name', 'score')}
                    })
            df = pd.DataFrame(rows)
            df.to_csv(SNAPSHOT_FILE, index=False, encoding='utf-8-sig')
            meta = {'saved_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                    'count': len(results), 'kline_days': KLINE_DAYS}
            with open(SNAPSHOT_META, 'w', encoding='utf-8') as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            self.stat_label.setText(
                f"快照已保存: {SNAPSHOT_FILE} | {len(rows)} 条 | 现在可离线加载筛选")
        except Exception as e:
            QMessageBox.warning(self, "快照保存失败", str(e))

    def load_snapshot(self):
        if not os.path.exists(SNAPSHOT_FILE):
            QMessageBox.information(self, "提示", "无本地快照，请先「扫描并存快照」")
            return
        try:
            df = pd.read_csv(SNAPSHOT_FILE, encoding='utf-8-sig')
            meta = {}
            if os.path.exists(SNAPSHOT_META):
                with open(SNAPSHOT_META, encoding='utf-8') as f:
                    meta = json.load(f)
            # 重建 results 结构
            results_map = {}
            for _, row in df.iterrows():
                code = row['code']
                if code not in results_map:
                    results_map[code] = {
                        'code': code, 'name': row['name'], 'close': row['close'],
                        'zdf': row['zdf'], 'industry': row.get('industry', ''),
                        'concept': row.get('concept', ''), 'hits': [], 'patterns': ''
                    }
                h = {k: row[k] for k in ['pattern', 'score'] if k in row}
                h['name'] = PATTERN_INFO.get(h['pattern'], ('',))[0]
                # 补充形态字段
                for k in ['big_date', 'big_close', 'pullback_days', 'pullback_pct',
                          'tail_vol_ratio', 'shrink_days', 'range_pct', 'break_vol_ratio',
                          'break_zdf', 'trend_days', 'vol_steps', 'zdf', 'dev_ma20',
                          'ma20', 'dist_ma20', 'zdf20', 'break_date', 'prev_high',
                          'near_high_pct']:
                    if k in row and pd.notna(row[k]):
                        h[k] = row[k]
                results_map[code]['hits'].append(h)
            results = list(results_map.values())
            for r in results:
                r['best_score'] = max(h['score'] for h in r['hits'])
                r['patterns'] = ','.join(sorted({h['pattern'] for h in r['hits']}))
            self.results = results
            self.populate_table(results)
            self._populate_sector_filter(results)
            self.stat_label.setText(
                f"已加载本地快照 | {len(results)} 只 | 保存于 {meta.get('saved_at','未知')}")
        except Exception as e:
            QMessageBox.critical(self, "加载快照失败", str(e))

    # ---------- 表格 ----------
    def populate_table(self, results):
        pat_filter = self.pat_combo.currentData()
        filtered = [r for r in results if not pat_filter or pat_filter in r['patterns']] if pat_filter else results
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(filtered))
        for row, r in enumerate(filtered):
            self._set_item(row, 0, r['code'])
            self._set_item(row, 1, r['name'])
            # 现价：数值排序
            close_item = _NumItem(f"{r['close']:.2f}")
            close_item.setTextAlignment(Qt.AlignRight)
            self.table.setItem(row, 2, close_item)
            # 涨跌%：数值排序
            zdf = r['zdf']
            zdf_item = _NumItem(f"{zdf:+.2f}")
            zdf_item.setForeground(QColor('#ef4444') if zdf > 0 else QColor('#22c55e'))
            zdf_item.setTextAlignment(Qt.AlignRight)
            self.table.setItem(row, 3, zdf_item)
            pat_item = QTableWidgetItem(r['patterns'])
            color = QColor(PATTERN_INFO[r['patterns'].split(',')[0]][1]) if r['patterns'] else QColor('#94a3b8')
            pat_item.setForeground(color)
            self.table.setItem(row, 4, pat_item)
            # 评分：数值排序
            score_item = _NumItem(f"{r['best_score']:.1f}")
            score_item.setTextAlignment(Qt.AlignRight)
            self.table.setItem(row, 5, score_item)
            # 评分依据：列出各形态评分构成
            score_basis = " + ".join(f"{h['pattern']}:{h['score']:.0f}" for h in r['hits'])
            self._set_item(row, 6, score_basis)
            self._set_item(row, 7, r.get('industry', ''))
            self._set_item(row, 8, r.get('concept', '')[:40])
            detail = " | ".join(f"{PATTERN_INFO[h['pattern']][0]}" for h in r['hits'])
            self._set_item(row, 9, detail)
            # 存 row_data 供 K线刷新
            for col in range(10):
                it = self.table.item(row, col)
                if it:
                    it.setData(Qt.UserRole + 1, r)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(5, 2)

    def _set_item(self, row, col, text, align=Qt.AlignLeft):
        item = QTableWidgetItem(str(text))
        item.setTextAlignment(align | Qt.AlignVCenter)
        self.table.setItem(row, col, item)

    def _apply_pattern_filter(self):
        if self.results:
            self.populate_table(self.results)
            self._apply_sector_filter()   # 形态变化后重新应用板块+搜索筛选

    # ---------- 板块筛选 ----------
    def _populate_sector_filter(self, results):
        """从扫描结果提取所有概念，按数量排序填入板块下拉"""
        from collections import Counter
        cnt = Counter()
        for r in results:
            concepts = r.get('concept_list', []) or []
            if isinstance(concepts, str):
                concepts = [c.strip() for c in concepts.split(';') if c.strip()]
            for c in (concepts or []):
                cnt[c] += 1
        cur = self.sector_filter.currentData()
        self.sector_filter.blockSignals(True)
        self.sector_filter.clear()
        self.sector_filter.addItem(f"全部板块 ({len(results)})", "")
        for name, n in cnt.most_common():
            self.sector_filter.addItem(f"{name} ({n})", name)
        if cur:
            idx = self.sector_filter.findData(cur)
            if idx >= 0:
                self.sector_filter.setCurrentIndex(idx)
        self.sector_filter.blockSignals(False)
        self._sector_box_ready = True

    def _apply_sector_filter(self, *_):
        """板块+搜索框 实时筛选（隐藏不匹配行）"""
        sector = self.sector_filter.currentData() if self._sector_box_ready else ""
        kw = self.filter_box.text().strip().lower()
        shown = 0
        for row in range(self.table.rowCount()):
            match = True
            # 板块筛选
            if sector:
                it = self.table.item(row, 0)
                row_data = it.data(Qt.UserRole + 1) if it else None
                concepts = (row_data or {}).get('concept_list', []) if row_data else []
                if isinstance(concepts, str):
                    concepts = [c.strip() for c in concepts.split(';') if c.strip()]
                if sector not in (concepts or []):
                    match = False
            # 搜索框筛选（代码/名称/概念/行业）
            if match and kw:
                it = self.table.item(row, 0)
                row_data = it.data(Qt.UserRole + 1) if it else None
                if row_data:
                    haystack = ' '.join(str(v) for v in [
                        row_data.get('code', ''), row_data.get('name', ''),
                        row_data.get('industry', ''), row_data.get('concept', ''),
                        ','.join(row_data.get('concept_list', []) or [])
                    ]).lower()
                else:
                    haystack = ''
                match = kw in haystack
            self.table.setRowHidden(row, not match)
            if match:
                shown += 1
        self.stat_label.setText(f"显示 {shown} / 共 {len(self.results) if self.results else 0} 只")

    # ---------- 实时K线（选中行即显示）----------
    def _on_selection_changed(self):
        items = self.table.selectedItems()
        if not items:
            return
        item = items[0]
        row_data = item.data(Qt.UserRole + 1)
        if not row_data:
            return
        if not self.kline_container.isVisible():
            self.kline_container.setVisible(True)
        self._pending_row = row_data
        self._kline_timer.start(150)  # 防抖

    def _flush_pending_kline(self):
        if self._pending_row is None:
            return
        r = self._pending_row
        code = r['code']
        hits = r.get('hits', [])
        info = f"{r['name']} {code} | 现价 {r['close']:.2f} | 涨跌 {r['zdf']:+.2f}% | {r.get('industry','')}"
        self.kline_info.setText(info + "  (加载中...)")

        # 异步加载K线（子线程，不阻塞GUI）
        days = self.spin_days.value()
        self._kline_loader = KlineLoadThread(code, hits, info, days=days)
        self._kline_loader.loaded.connect(self._on_kline_loaded)
        self._kline_loader.failed.connect(self._on_kline_failed)
        self._kline_loader.start()

    def _on_kline_loaded(self, code, df, hits, info):
        self.kline_info.setText(info)
        self.kline_canvas.set_data(df, hits, info)

    def _on_indicator_toggled(self):
        """指标勾选变化 -> 更新画布 show_flags 并重绘"""
        self.kline_canvas.show_flags = {
            'ma': self.cb_ind_ma.isChecked(),
            'boll': self.cb_ind_boll.isChecked(),
            'macd': self.cb_ind_macd.isChecked(),
            'kdj': self.cb_ind_kdj.isChecked(),
            'rsi': self.cb_ind_rsi.isChecked(),
        }
        self.kline_canvas.update()

    def _on_kline_failed(self, code, error):
        self.kline_info.setText(f"{code} K线加载失败: {error}")
        self.kline_canvas.set_data(None, [], "")

    # ---------- 导出 ----------
    def export_csv(self):
        if not self.results:
            QMessageBox.information(self, "提示", "无数据可导出")
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出CSV", "trend_continuation_results.csv", "CSV (*.csv)")
        if not path:
            return
        rows = []
        for r in self.results:
            for h in r['hits']:
                rows.append({
                    'code': r['code'], 'name': r['name'], 'close': r['close'],
                    'zdf': round(r['zdf'], 2), 'industry': r.get('industry', ''),
                    'pattern': h['pattern'], 'pattern_name': PATTERN_INFO[h['pattern']][0],
                    'score': round(h['score'], 1), 'concept': r.get('concept', ''),
                    **{k: v for k, v in h.items() if k not in ('pattern', 'name', 'score')}
                })
        pd.DataFrame(rows).to_csv(path, index=False, encoding='utf-8-sig')
        self.stat_label.setText(f"已导出: {path} ({len(rows)} 条)")


def run_gui():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == '__main__':
    run_gui()
