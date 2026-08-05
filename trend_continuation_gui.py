# -*- coding: utf-8 -*-
"""
趋势中继多策略选股扫描器 - PyQt5 桌面版
========================================
复用 trend_continuation_scanner 的 5 种形态识别算法。
功能：全市场扫描 / 指定股票 / 形态筛选 / 双击查看K线详情 / 导出CSV。

运行： py trend_continuation_gui.py
"""

import sys
from collections import Counter

import numpy as np
import pandas as pd
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QPainter, QPen
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QLabel, QTableWidget, QTableWidgetItem,
    QProgressBar, QStatusBar, QHeaderView, QComboBox, QMessageBox,
    QFileDialog, QGroupBox, QSplitter, QCheckBox, QDialog, QFrame,
)

from stock_full_scan import get_kline_data, get_stock_raw, get_stock_industry
from trend_continuation_scanner import (
    scan_market, scan_one,
    KLINE_DAYS, MAX_WORKERS,
)

# 形态中文名 + 颜色
PATTERN_INFO = {
    'P1': ('放量涨后缩量回调', '#4ade80'),
    'P2': ('缩量横盘后放量突破', '#38bdf8'),
    'P3': ('温和放量走趋势', '#fbbf24'),
    'P4': ('缩量回踩MA20支撑', '#a78bfa'),
    'P5': ('放量破前高后缩量回踩', '#f472b6'),
}


# ============ 扫描线程 ============
class ScanThread(QThread):
    progress = pyqtSignal(int, int)
    result = pyqtSignal(list)
    finished_msg = pyqtSignal(str)

    def __init__(self, codes=None):
        super().__init__()
        self.codes = codes
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        def on_progress(d, t):
            if self._stop:
                raise RuntimeError('stopped')
            self.progress.emit(d, t)
        try:
            results = scan_market(codes=self.codes, on_progress=on_progress)
            self.result.emit(results)
        except RuntimeError:
            self.finished_msg.emit('扫描已停止')
        except Exception as e:
            self.finished_msg.emit(f'扫描出错: {e}')


# ============ K线详情窗 ============
class KlineDialog(QDialog):
    def __init__(self, code, name, parent=None):
        super().__init__(parent)
        self.code = code
        self.name = name
        self.setWindowTitle(f"{name} {code} - K线详情")
        self.resize(900, 560)
        self._df = None
        self._hits = None
        self.init_ui()
        self.load_data()

    def init_ui(self):
        layout = QVBoxLayout(self)
        self.info_label = QLabel("加载中...")
        self.info_label.setStyleSheet("color:#e2e8f0; background:#151c2c; padding:8px;")
        layout.addWidget(self.info_label)
        self.canvas = QFrame()
        self.canvas.setStyleSheet("background:#0b1220;")
        self.canvas.setMinimumHeight(440)
        layout.addWidget(self.canvas)

    def load_data(self):
        df = get_kline_data(self.code, days=KLINE_DAYS)
        if df is None or len(df) < 15:
            self.info_label.setText("K线数据不足")
            return
        self._df = df
        r = scan_one(self.code)
        self._hits = r['hits'] if r else []
        self.info_label.setText(self._build_info())
        self.update()

    def _build_info(self):
        r = get_stock_raw(self.code)
        if not r:
            return f"{self.name} {self.code}"
        ind = get_stock_industry(self.code)
        ind_str = f" | {ind.get('industry','')}" if ind else ""
        hits_str = " | ".join(f"{PATTERN_INFO[h['pattern']][0]}({h['pattern']})评分{h['score']:.0f}"
                              for h in self._hits) if self._hits else "无形态命中"
        return f"{self.name} {self.code} | 现价 {r.get('now',0):.2f} | 涨跌 {((r.get('now',0)/r.get('open',1)-1)*100):.2f}%{ind_str}\n命中: {hits_str}"

    def paintEvent(self, event):
        if self._df is None:
            return
        painter = QPainter(self.canvas)
        painter.setRenderHint(QPainter.Antialiasing)
        w = self.canvas.width()
        h = self.canvas.height()
        painter.fillRect(0, 0, w, h, QColor('#0b1220'))
        df = self._df
        n = len(df)
        if n < 2:
            return
        highs = df['high'].values
        lows = df['low'].values
        ph = h * 0.65
        vh = h * 0.25
        pad_top = 40
        pmin, pmax = lows.min(), highs.max()
        if pmax == pmin:
            return
        ma20_vals = df['close'].rolling(20).mean().values
        vols = df['volume'].values
        vmax = vols.max() if vols.max() > 0 else 1
        cw = (w - 40) / n
        for i in range(n):
            x = 20 + i * cw + cw / 2
            o, c = df['open'].iloc[i], df['close'].iloc[i]
            hi, lo = df['high'].iloc[i], df['low'].iloc[i]
            y_o = pad_top + (pmax - o) / (pmax - pmin) * ph
            y_c = pad_top + (pmax - c) / (pmax - pmin) * ph
            y_h = pad_top + (pmax - hi) / (pmax - pmin) * ph
            y_l = pad_top + (pmax - lo) / (pmax - pmin) * ph
            color = QColor('#ef4444') if c < o else QColor('#22c55e')
            painter.setPen(QPen(color, 1))
            painter.drawLine(int(x), int(y_h), int(x), int(y_l))
            body_h = max(abs(y_c - y_o), 1)
            painter.fillRect(int(x - cw * 0.35), int(min(y_o, y_c)), int(cw * 0.7), int(body_h), color)
            v_y0 = pad_top + ph + 30
            v_h = vols[i] / vmax * vh
            painter.fillRect(int(x - cw * 0.35), int(v_y0 + vh - v_h), int(cw * 0.7), int(v_h),
                             QColor('#475569') if i != n - 1 else QColor('#fbbf24'))
        # MA20
        painter.setPen(QPen(QColor('#fbbf24'), 1.5))
        prev = None
        for i in range(n):
            if np.isnan(ma20_vals[i]):
                continue
            x = 20 + i * cw + cw / 2
            y = pad_top + (pmax - ma20_vals[i]) / (pmax - pmin) * ph
            if prev:
                painter.drawLine(int(prev[0]), int(prev[1]), int(x), int(y))
            prev = (x, y)
        if self._hits:
            tags = "  ".join(f"[{h['pattern']} {PATTERN_INFO[h['pattern']][0]}]" for h in self._hits)
            painter.setPen(QColor('#fbbf24'))
            f = QFont(); f.setPointSize(9); f.setBold(True); painter.setFont(f)
            painter.drawText(10, 22, f"形态: {tags}")


# ============ 主窗 ============
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("趋势中继多策略选股扫描器")
        self.resize(1280, 760)
        self.results = []
        self.scan_thread = None
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
                       border-radius: 4px; padding: 6px 16px; font-weight: bold; }
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
        ctrl_l.addWidget(self.pat_combo)
        self.scan_btn = QPushButton("开始扫描")
        self.scan_btn.setObjectName("scanBtn")
        self.scan_btn.clicked.connect(self.start_scan)
        ctrl_l.addWidget(self.scan_btn)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.clicked.connect(self.stop_scan)
        self.stop_btn.setEnabled(False)
        ctrl_l.addWidget(self.stop_btn)
        self.export_btn = QPushButton("导出CSV")
        self.export_btn.clicked.connect(self.export_csv)
        ctrl_l.addWidget(self.export_btn)
        layout.addWidget(ctrl)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(
            ["代码", "名称", "现价", "涨跌%", "命中形态", "评分", "行业", "概念", "形态详情"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(8, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 70); self.table.setColumnWidth(1, 90)
        self.table.setColumnWidth(2, 70); self.table.setColumnWidth(3, 70)
        self.table.setColumnWidth(4, 130); self.table.setColumnWidth(5, 60)
        self.table.setColumnWidth(6, 100); self.table.setColumnWidth(7, 200)
        self.table.setSortingEnabled(True)
        self.table.doubleClicked.connect(self.on_double_click)
        layout.addWidget(self.table, 1)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.stat_label = QLabel("就绪")
        self.status.addWidget(self.stat_label)

    def start_scan(self):
        code_text = self.code_edit.text().strip()
        codes = [c.strip() for c in code_text.split(',') if c.strip()] if code_text else None
        self.scan_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.table.setRowCount(0)
        self.stat_label.setText("扫描中...")
        self.scan_thread = ScanThread(codes=codes)
        self.scan_thread.progress.connect(self.on_progress)
        self.scan_thread.result.connect(self.on_result)
        self.scan_thread.finished_msg.connect(self.on_finished)
        self.scan_thread.start()

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
        pc = Counter()
        for r in results:
            for h in r['hits']:
                pc[h['pattern']] += 1
        stat = " | ".join(f"{PATTERN_INFO[k][0]}: {v}" for k, v in sorted(pc.items()))
        self.stat_label.setText(f"完成 | 命中 {len(results)} 只 | {stat}")
        self.on_finished("")

    def on_finished(self, msg):
        if msg:
            self.stat_label.setText(msg)
        self.scan_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.progress.setVisible(False)

    def populate_table(self, results):
        pat_filter = self.pat_combo.currentData()
        filtered = [r for r in results if not pat_filter or pat_filter in r['patterns']] if pat_filter else results
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(filtered))
        for row, r in enumerate(filtered):
            self._set_item(row, 0, r['code'])
            self._set_item(row, 1, r['name'])
            self._set_item(row, 2, f"{r['close']:.2f}", align=Qt.AlignRight)
            zdf = r['zdf']
            zdf_item = QTableWidgetItem(f"{zdf:+.2f}")
            zdf_item.setForeground(QColor('#ef4444') if zdf > 0 else QColor('#22c55e'))
            zdf_item.setTextAlignment(Qt.AlignRight)
            self.table.setItem(row, 3, zdf_item)
            pat_item = QTableWidgetItem(r['patterns'])
            color = QColor(PATTERN_INFO[r['patterns'].split(',')[0]][1]) if r['patterns'] else QColor('#94a3b8')
            pat_item.setForeground(color)
            self.table.setItem(row, 4, pat_item)
            score_item = QTableWidgetItem(f"{r['best_score']:.1f}")
            score_item.setTextAlignment(Qt.AlignRight)
            self.table.setItem(row, 5, score_item)
            self._set_item(row, 6, r.get('industry', ''))
            self._set_item(row, 7, r.get('concept', '')[:40])
            detail = " | ".join(f"{PATTERN_INFO[h['pattern']][0]}" for h in r['hits'])
            self._set_item(row, 8, detail)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(5, 2)

    def _set_item(self, row, col, text, align=Qt.AlignLeft):
        item = QTableWidgetItem(str(text))
        item.setTextAlignment(align | Qt.AlignVCenter)
        self.table.setItem(row, col, item)

    def on_double_click(self, idx):
        row = idx.row()
        code = self.table.item(row, 0).text()
        name = self.table.item(row, 1).text()
        dlg = KlineDialog(code, name, self)
        dlg.exec_()

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
