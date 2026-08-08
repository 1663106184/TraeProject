# -*- coding: utf-8 -*-
"""
尾盘主力承接 多维量化选股 - PyQt5 桌面版
========================================
按 5 维量化标准筛选（全部条件 AND）：
  1. 板块热度  : 当日涨幅榜前 3 热门板块
  2. 涨幅范围  : 涨幅 ∈ [3%, 5%]
  3. 流动性    : 流通市值 50-300 亿、换手率 3%-10%、量比 > 1.5
  4. 技术形态  : 站稳5日均线、MACD柱绿转红且DIF上穿DEA、KDJ超卖区K<20拐头
  5. 分时形态  : 14:40 创日内新高后回踩不破分时均价（用「现价接近日高+≥均价」近似）

复用 zdf_strategy_gui 的暗色主题 / splitter 表格+K线 / 筹码分布 /
本地快照 / 板块+搜索筛选 / 导出 CSV 框架。

运行： py hot_board_gui.py
"""

import os
import sys
import json
import time

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

from stock_full_scan import get_kline_data
import stock_chip_filter as scf
from hot_board_scanner import (
    scan_market, compute_features, aggregate_hot_boards,
    COND_NAMES, KLINE_DAYS, MAX_WORKERS, HOT_BOARD_TOP_N,
)


# 数值型表格 Item（保证按数值排序而非字符串）
class _NumItem(QTableWidgetItem):
    def __lt__(self, other):
        try:
            return float(self.text()) < float(other.text())
        except Exception:
            return super().__lt__(other)


SNAPSHOT_DIR = "snapshot"
SNAPSHOT_FILE = os.path.join(SNAPSHOT_DIR, "hot_board_features.csv")
SNAPSHOT_META = os.path.join(SNAPSHOT_DIR, "hot_board_meta.json")


# ============ 扫描线程 ============
class ScanThread(QThread):
    progress = pyqtSignal(int, int)
    result = pyqtSignal(list)
    finished_msg = pyqtSignal(str)

    def __init__(self, codes=None, days=None, require_all=True):
        super().__init__()
        self.codes = codes
        self.days = days
        self.require_all = require_all
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
                                  stop_check=stop_check, require_all=self.require_all)
            if self._stop:
                self.finished_msg.emit('扫描已停止')
            else:
                self.result.emit(results)
        except Exception as e:
            self.finished_msg.emit(f'扫描出错: {e}')


# ============ K 线异步加载线程 ============
class KlineLoadThread(QThread):
    """子线程加载 K 线 + 筹码分布，避免主线程网络请求阻塞/崩溃"""
    loaded = pyqtSignal(str, object, list, str, object)   # code, df, hits, info, chip_info
    failed = pyqtSignal(str, str)                          # code, error

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
            # 计算筹码分布
            chip_info = None
            try:
                res = scf.build_chip_distribution(df, bins=scf.PRICE_BINS)
                if res is not None:
                    centers, chip, total = res
                    cur = float(df['close'].astype(float).iloc[-1])
                    above = centers > cur
                    trapped = float(chip[above].sum()) / total
                    profit = 1.0 - trapped
                    peak = float(centers[int(np.argmax(chip))])
                    chip_info = (profit, trapped, peak, centers, chip, total)
            except Exception:
                pass
            self.loaded.emit(self.code, df, self.hits, self.info, chip_info)
        except Exception as e:
            self.failed.emit(self.code, str(e))


# ============ K 线画布 ============
class KlineCanvas(QFrame):
    """自绘 K 线图（蜡烛+成交量+MA5/10/20/60+BOLL+MACD+KDJ+RSI+筹码分布侧栏）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background:#0b1220;")
        self.setMinimumHeight(280)
        self._df = None
        self._hits = []
        self._info = ""
        self._chip_info = None
        self.show_flags = {'ma': True, 'boll': False, 'macd': True, 'kdj': True, 'rsi': False}

    def set_data(self, df, hits, info, chip_info=None):
        self._df = df
        self._hits = hits or []
        self._info = info or ""
        self._chip_info = chip_info
        self.update()

    def _y_of(self, val, pmin, pmax, price_h, pad_top=0):
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
        has_chip = self._chip_info is not None
        if has_chip:
            kw = int((w - 30) * 0.70) - x_offset
            chip_x = x_offset + kw + 20
            chip_w = w - chip_x - 10
        else:
            kw = w - 40
            chip_x = chip_w = 0

        sub_indicators = [k for k in ('macd', 'kdj', 'rsi') if self.show_flags.get(k, False)]
        n_sub = len(sub_indicators)

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

        highs = df['high'].astype(float).values
        lows = df['low'].astype(float).values
        opens = df['open'].astype(float).values
        closes = df['close'].astype(float).values
        vols = df['volume'].astype(float).values
        close_s = df['close'].astype(float)

        pmin, pmax = float(lows.min()), float(highs.max())
        if pmax <= pmin:
            pmax = pmin + 1
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

        p.setPen(QPen(QColor('#2a3142'), 1, Qt.DashLine))
        for gi in range(5):
            y = pad_top + int(price_h * gi / 4)
            p.drawLine(x_offset, y, x_offset + kw, y)
            price = pmax - (pmax - pmin) * gi / 4
            p.setPen(QColor('#7a8499'))
            p.drawText(x_offset + 2, y + 12, f"{price:.2f}")
            p.setPen(QPen(QColor('#2a3142'), 1, Qt.DashLine))

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
            v_h = int(vols[i] / vmax * vol_h)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(color))
            p.drawRect(QRectF(x - cw / 2, vol_top + vol_h - v_h, cw, v_h))

        if self.show_flags.get('ma', True):
            ma_defs = [(5, QColor('#ffcc00')), (10, QColor('#ff6a00')),
                       (20, QColor('#a974ff')), (60, QColor('#36c5f0'))]
            for period, col in ma_defs:
                if n < period:
                    continue
                ma = close_s.rolling(period).mean()
                self._draw_line(p, ma, pmin, pmax, price_h, kw, n, cw, col, 1.5, x_offset, pad_top)
            lx = x_offset + kw - 280
            for period, col in ma_defs:
                if n < period:
                    continue
                p.setPen(col)
                p.drawLine(lx, pad_top + 10, lx + 16, pad_top + 10)
                p.setPen(QColor('#cfd6e4'))
                p.drawText(lx + 20, pad_top + 14, f"MA{period}")
                lx += 70

        if self.show_flags.get('boll', False) and n >= 20:
            self._draw_line(p, boll_up, pmin, pmax, price_h, kw, n, cw, QColor('#8a8f9c'), 1, x_offset, pad_top)
            self._draw_line(p, boll_lo, pmin, pmax, price_h, kw, n, cw, QColor('#8a8f9c'), 1, x_offset, pad_top)
            self._draw_line(p, ma20, pmin, pmax, price_h, kw, n, cw, QColor('#ffcc00'), 1, x_offset, pad_top)
            p.setPen(QColor('#cfd6e4'))
            p.drawText(x_offset + kw - 70, pad_top + 14, "BOLL")

        p.setPen(QColor('#7a8499'))
        p.drawText(x_offset + 2, vol_top + 12, "成交量")

        # 副图指标
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

        # ---------- 筹码分布侧栏 ----------
        if has_chip and chip_w > 30:
            profit, trapped, peak, centers, chip, total = self._chip_info
            p.setPen(QColor('#cfd6e4'))
            p.drawText(chip_x, 18, "筹码分布")
            cmax = float(chip.max()) if len(chip) else 1
            if cmax <= 0:
                cmax = 1
            bar_h = max(1, price_h / len(chip))
            p.setPen(Qt.NoPen)
            cur_close = float(df['close'].iloc[-1])
            for i, cv in enumerate(chip):
                if cv <= 0:
                    continue
                price_at = float(centers[i])
                y = self._y_of(price_at, pmin, pmax, price_h, pad_top)
                bw = int(cv / cmax * (chip_w - 10))
                col = QColor('#22c55e') if price_at > cur_close else QColor('#ef4444')
                p.setBrush(QBrush(col))
                p.drawRect(QRectF(chip_x, y, bw, max(1, bar_h - 1)))
            p.setPen(QPen(QColor('#fbbf24'), 1, Qt.DashLine))
            peak_y = self._y_of(peak, pmin, pmax, price_h, pad_top)
            p.drawLine(chip_x - 5, peak_y, chip_x + chip_w, peak_y)
            p.setPen(QColor('#fbbf24'))
            p.drawText(chip_x, peak_y - 4, f"峰 {peak:.2f}")
            cur_y = self._y_of(cur_close, pmin, pmax, price_h, pad_top)
            p.setPen(QPen(QColor('#ffffff'), 1, Qt.DashLine))
            p.drawLine(x_offset, cur_y, x_offset + kw, cur_y)
            p.setPen(QColor('#cfd6e4'))
            p.drawText(chip_x, pad_top + price_h + 18, f"获利 {profit * 100:.1f}%")
            p.drawText(chip_x, pad_top + price_h + 36, f"套牢 {trapped * 100:.1f}%")


# ============ 主窗 ============
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("尾盘主力承接 多维量化选股 v1")
        self.resize(1320, 840)
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

        ctrl_l.addWidget(QLabel("K线天数:"))
        self.spin_days = QSpinBox()
        self.spin_days.setRange(30, 300)
        self.spin_days.setSingleStep(10)
        self.spin_days.setValue(KLINE_DAYS)
        self.spin_days.setFixedWidth(70)
        self.spin_days.setToolTip("K线回看天数（影响指标计算+显示）")
        ctrl_l.addWidget(self.spin_days)

        # 模式：仅全条件命中 / 显示全部可计算
        ctrl_l.addWidget(QLabel("模式:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("仅全条件命中", True)
        self.mode_combo.addItem("显示全部(含未达标)", False)
        self.mode_combo.setToolTip("「仅全条件命中」只展示 5 维条件全部成立的股票；\n"
                                   "「显示全部」展示所有可计算条件的股票，便于查看每条依据")
        ctrl_l.addWidget(self.mode_combo)

        ctrl_l.addWidget(QLabel("板块:"))
        self.sector_filter = QComboBox()
        self.sector_filter.setFixedHeight(30)
        self.sector_filter.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.sector_filter.setMinimumContentsLength(18)
        self.sector_filter.addItem("全部板块", "")
        self.sector_filter.currentIndexChanged.connect(self._apply_sector_filter)
        self._sector_box_ready = False
        ctrl_l.addWidget(self.sector_filter, 2)

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
        self.table = QTableWidget(0, 13)
        self.table.setHorizontalHeaderLabels(
            ["代码", "名称", "现价", "涨跌%", "流通市值", "换手%", "量比",
             "热门板块", "命中条件", "未达标项", "条件详情", "行业", "概念"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(12, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(10, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 70); self.table.setColumnWidth(1, 90)
        self.table.setColumnWidth(2, 70); self.table.setColumnWidth(3, 70)
        self.table.setColumnWidth(4, 80); self.table.setColumnWidth(5, 60)
        self.table.setColumnWidth(6, 55); self.table.setColumnWidth(7, 130)
        self.table.setColumnWidth(8, 70); self.table.setColumnWidth(9, 150)
        self.table.setColumnWidth(11, 90)
        self.table.setSortingEnabled(True)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.splitter.addWidget(self.table)
        # K线区
        self.kline_container = QWidget()
        self.kline_container.setMinimumHeight(200)
        kl = QVBoxLayout(self.kline_container)
        kl.setContentsMargins(2, 2, 2, 2)
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
        self.cb_ind_macd.setChecked(True)
        self.cb_ind_kdj = QCheckBox("KDJ")
        self.cb_ind_kdj.setChecked(True)
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
        require_all = self.mode_combo.currentData()
        self.scan_thread = ScanThread(codes=codes, days=self.spin_days.value(),
                                      require_all=require_all)
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
        self.stat_label.setText(f"扫描中... {done}/{total} ({done * 100 // total}%)")

    def on_result(self, results):
        self.results = results
        self.populate_table(results)
        self._populate_sector_filter(results)
        all_n = sum(1 for r in results if r['pass_all'])
        self.stat_label.setText(
            f"完成 | 结果 {len(results)} 只 | 全条件命中 {all_n} 只")
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
                row = {
                    'code': r['code'], 'name': r['name'], 'sector': r['sector'],
                    'close': r['close'], 'zdf': round(r['zdf'], 2),
                    'circ_mc_yi': r['circ_mc_yi'], 'turnover': r['turnover'],
                    'vol_ratio': r['vol_ratio'], 'vwap': r['vwap'],
                    'industry': r.get('industry', ''), 'concept': r.get('concept', ''),
                    'hot_board': r.get('hot_board', ''), 'board_rank': r.get('board_rank', 0),
                    'board_zt_n': r.get('board_zt_n', 0),
                    'board_avg_zdf': r.get('board_avg_zdf', 0),
                    'fail_n': r['fail_n'], 'pass_all': r['pass_all'],
                    'concept_list': ';'.join(r.get('concept_list', []) or []),
                }
                for cn in COND_NAMES:
                    row[f'条件_{cn}'] = int(r['conds'].get(cn, False))
                    row[f'依据_{cn}'] = r['meta'].get(cn, '')
                rows.append(row)
            pd.DataFrame(rows).to_csv(SNAPSHOT_FILE, index=False, encoding='utf-8-sig')
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
            results = []
            for _, row in df.iterrows():
                conds, meta_d = {}, {}
                for cn in COND_NAMES:
                    conds[cn] = bool(row.get(f'条件_{cn}', 0))
                    meta_d[cn] = str(row.get(f'依据_{cn}', ''))
                cl = row.get('concept_list', '')
                if isinstance(cl, str):
                    cl = [c.strip() for c in cl.split(';') if c.strip()]
                else:
                    cl = []
                results.append({
                    'code': str(row['code']), 'name': row['name'],
                    'sector': row.get('sector', ''), 'close': float(row['close']),
                    'zdf': float(row['zdf']),
                    'circ_mc_yi': float(row.get('circ_mc_yi', 0)),
                    'turnover': float(row.get('turnover', 0)),
                    'vol_ratio': float(row.get('vol_ratio', 0)),
                    'vwap': float(row.get('vwap', 0)),
                    'industry': row.get('industry', ''), 'concept': row.get('concept', ''),
                    'concept_list': cl, 'conds': conds, 'meta': meta_d,
                    'hot_board': row.get('hot_board', ''),
                    'board_rank': int(row.get('board_rank', 0)),
                    'board_zt_n': int(row.get('board_zt_n', 0)),
                    'board_avg_zdf': float(row.get('board_avg_zdf', 0)),
                    'pass_all': bool(row.get('pass_all', 0)),
                    'fail_n': int(row.get('fail_n', 0)),
                })
            self.results = results
            self.populate_table(results)
            self._populate_sector_filter(results)
            self.stat_label.setText(
                f"已加载本地快照 | {len(results)} 只 | 保存于 {meta.get('saved_at', '未知')}")
        except Exception as e:
            QMessageBox.critical(self, "加载快照失败", str(e))

    # ---------- 表格 ----------
    def populate_table(self, results):
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(results))
        for row, r in enumerate(results):
            self._set_item(row, 0, r['code'])
            self._set_item(row, 1, r['name'])
            close_item = _NumItem(f"{r['close']:.2f}")
            close_item.setTextAlignment(Qt.AlignRight)
            self.table.setItem(row, 2, close_item)
            zdf = r['zdf']
            zdf_item = _NumItem(f"{zdf:+.2f}")
            zdf_item.setForeground(QColor('#ef4444') if zdf > 0 else QColor('#22c55e'))
            zdf_item.setTextAlignment(Qt.AlignRight)
            self.table.setItem(row, 3, zdf_item)
            # 流通市值
            mc_item = _NumItem(f"{r.get('circ_mc_yi', 0):.0f}")
            mc_item.setTextAlignment(Qt.AlignRight)
            self.table.setItem(row, 4, mc_item)
            # 换手
            turn_item = _NumItem(f"{r.get('turnover', 0):.2f}")
            turn_item.setTextAlignment(Qt.AlignRight)
            self.table.setItem(row, 5, turn_item)
            # 量比
            vr_item = _NumItem(f"{r.get('vol_ratio', 0):.2f}")
            vr_item.setTextAlignment(Qt.AlignRight)
            self.table.setItem(row, 6, vr_item)
            # 热门板块
            hb_item = QTableWidgetItem(r.get('hot_board', ''))
            if r.get('board_rank', 99) <= HOT_BOARD_TOP_N:
                hb_item.setForeground(QColor('#fbbf24'))
                hb_item.setFont(QFont('', -1, QFont.Bold))
            self.table.setItem(row, 7, hb_item)
            # 命中条件数 / 总数
            pass_n = sum(1 for v in r['conds'].values() if v)
            hit_item = _NumItem(f"{pass_n}/{len(COND_NAMES)}")
            if r['pass_all']:
                hit_item.setForeground(QColor('#22c55e'))
                hit_item.setFont(QFont('', -1, QFont.Bold))
            else:
                hit_item.setForeground(QColor('#94a3b8'))
            hit_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 8, hit_item)
            # 未达标项
            fails = [cn for cn in COND_NAMES if not r['conds'].get(cn)]
            fail_item = QTableWidgetItem('、'.join(fails) if fails else '全部达标')
            fail_item.setForeground(QColor('#fca5a5') if fails else QColor('#22c55e'))
            self.table.setItem(row, 9, fail_item)
            # 条件详情
            details = []
            for cn in COND_NAMES:
                mark = '✓' if r['conds'].get(cn) else '✗'
                details.append(f"{cn}{mark}({r['meta'].get(cn, '')})")
            detail_item = QTableWidgetItem('  '.join(details))
            self.table.setItem(row, 10, detail_item)
            # 行业 / 概念
            self._set_item(row, 11, r.get('industry', ''))
            self._set_item(row, 12, r.get('concept', '')[:40])
            # 存 row_data 供 K线刷新
            for col in range(13):
                it = self.table.item(row, col)
                if it:
                    it.setData(Qt.UserRole + 1, r)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(8, 2)   # 默认按命中条件数降序

    def _set_item(self, row, col, text, align=Qt.AlignLeft):
        item = QTableWidgetItem(str(text))
        item.setTextAlignment(align | Qt.AlignVCenter)
        self.table.setItem(row, col, item)

    # ---------- 板块筛选 ----------
    def _populate_sector_filter(self, results):
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
        sector = self.sector_filter.currentData() if self._sector_box_ready else ""
        kw = self.filter_box.text().strip().lower()
        shown = 0
        for row in range(self.table.rowCount()):
            match = True
            if sector:
                it = self.table.item(row, 0)
                row_data = it.data(Qt.UserRole + 1) if it else None
                concepts = (row_data or {}).get('concept_list', []) if row_data else []
                if isinstance(concepts, str):
                    concepts = [c.strip() for c in concepts.split(';') if c.strip()]
                if sector not in (concepts or []):
                    match = False
            if match and kw:
                it = self.table.item(row, 0)
                row_data = it.data(Qt.UserRole + 1) if it else None
                if row_data:
                    haystack = ' '.join(str(v) for v in [
                        row_data.get('code', ''), row_data.get('name', ''),
                        row_data.get('industry', ''), row_data.get('concept', ''),
                        row_data.get('hot_board', ''),
                        ','.join(row_data.get('concept_list', []) or [])
                    ]).lower()
                else:
                    haystack = ''
                match = kw in haystack
            self.table.setRowHidden(row, not match)
            if match:
                shown += 1
        self.stat_label.setText(
            f"显示 {shown} / 共 {len(self.results) if self.results else 0} 只")

    # ---------- 实时K线 ----------
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
        self._kline_timer.start(150)

    def _flush_pending_kline(self):
        if self._pending_row is None:
            return
        r = self._pending_row
        code = r['code']
        hits = [{'pattern': cn, 'pass': r['conds'].get(cn, False)} for cn in COND_NAMES]
        info = (f"{r['name']} {code} | 现价 {r['close']:.2f} | 涨跌 {r['zdf']:+.2f}% | "
                f"流通市值 {r.get('circ_mc_yi', 0):.0f}亿 | 量比 {r.get('vol_ratio', 0):.2f} | "
                f"{r.get('hot_board', '')}")
        self.kline_info.setText(info + "  (加载中...)")

        days = self.spin_days.value()
        self._kline_loader = KlineLoadThread(code, hits, info, days=days)
        self._kline_loader.loaded.connect(self._on_kline_loaded)
        self._kline_loader.failed.connect(self._on_kline_failed)
        self._kline_loader.start()

    def _on_kline_loaded(self, code, df, hits, info, chip_info):
        self.kline_info.setText(info)
        self.kline_canvas.set_data(df, hits, info, chip_info)

    def _on_indicator_toggled(self):
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
        path, _ = QFileDialog.getSaveFileName(
            self, "导出CSV", "hot_board_results.csv", "CSV (*.csv)")
        if not path:
            return
        rows = []
        for r in self.results:
            row = {
                'code': r['code'], 'name': r['name'], 'sector': r['sector'],
                'close': r['close'], 'zdf': round(r['zdf'], 2),
                'circ_mc_yi': r.get('circ_mc_yi', 0), 'turnover': r.get('turnover', 0),
                'vol_ratio': r.get('vol_ratio', 0), 'vwap': r.get('vwap', 0),
                'industry': r.get('industry', ''), 'concept': r.get('concept', ''),
                'hot_board': r.get('hot_board', ''),
                'board_rank': r.get('board_rank', 0),
                'board_zt_n': r.get('board_zt_n', 0),
                'board_avg_zdf': r.get('board_avg_zdf', 0),
                'fail_n': r['fail_n'], 'pass_all': r['pass_all'],
            }
            for cn in COND_NAMES:
                row[f'条件_{cn}'] = int(r['conds'].get(cn, False))
                row[f'依据_{cn}'] = r['meta'].get(cn, '')
            rows.append(row)
        pd.DataFrame(rows).to_csv(path, index=False, encoding='utf-8-sig')
        self.stat_label.setText(f"已导出: {path} ({len(rows)} 条)")


def run_gui():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == '__main__':
    run_gui()
