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
    QFileDialog, QGroupBox, QSplitter, QCheckBox, QFrame,
)

from stock_full_scan import get_kline_data, get_stock_raw, get_stock_industry
from trend_continuation_scanner import (
    scan_market, scan_one, PATTERN_DETECTORS,
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

SNAPSHOT_DIR = "snapshot"
SNAPSHOT_FILE = os.path.join(SNAPSHOT_DIR, "trend_features.csv")
SNAPSHOT_META = os.path.join(SNAPSHOT_DIR, "trend_meta.json")


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
            self.progress.emit(d, t)
        def stop_check():
            return self._stop
        try:
            results = scan_market(codes=self.codes, on_progress=on_progress, stop_check=stop_check)
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

    def __init__(self, code, hits, info):
        super().__init__()
        self.code = code
        self.hits = hits
        self.info = info

    def run(self):
        try:
            df = get_kline_data(self.code, days=KLINE_DAYS)
            if df is None or len(df) < 2:
                self.failed.emit(self.code, 'K线数据不足')
                return
            self.loaded.emit(self.code, df, self.hits, self.info)
        except Exception as e:
            self.failed.emit(self.code, str(e))


# ============ K线画布 ============
class KlineCanvas(QFrame):
    """自绘 K 线图（蜡烛+成交量+MA20+形态标注）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background:#0b1220;")
        self.setMinimumHeight(280)
        self._df = None
        self._hits = []
        self._info = ""

    def set_data(self, df, hits, info):
        self._df = df
        self._hits = hits or []
        self._info = info or ""
        self.update()

    def paintEvent(self, event):
        try:
            self._paint(event)
        except Exception:
            # 绘图异常不崩溃，画个提示
            painter = QPainter(self)
            painter.fillRect(0, 0, self.width(), self.height(), QColor('#0b1220'))
            painter.setPen(QColor('#ef4444'))
            painter.drawText(10, 20, "K线绘制异常")

    def _paint(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        h = self.height()
        painter.fillRect(0, 0, w, h, QColor('#0b1220'))

        # 信息条
        painter.setPen(QColor('#e2e8f0'))
        f = QFont(); f.setPointSize(9); painter.setFont(f)
        painter.drawText(10, 18, self._info[:120])

        if self._df is None or len(self._df) < 2:
            painter.setPen(QColor('#64748b'))
            painter.drawText(w // 2 - 60, h // 2, "点击表格行查看 K 线")
            return

        df = self._df
        n = len(df)
        pad_top = 34
        ph = (h - pad_top - 40) * 0.68  # 价格区
        vh = (h - pad_top - 40) * 0.28  # 量区
        highs = df['high'].values
        lows = df['low'].values
        pmin, pmax = lows.min(), highs.max()
        if pmax == pmin:
            return
        ma20_vals = df['close'].rolling(20).mean().values
        vols = df['volume'].values
        vmax = vols.max() if vols.max() > 0 else 1
        cw = (w - 40) / n

        # K线
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
            # 成交量
            v_y0 = pad_top + ph + 16
            v_h = vols[i] / vmax * vh
            vcolor = QColor('#475569') if i != n - 1 else QColor('#fbbf24')
            painter.fillRect(int(x - cw * 0.35), int(v_y0 + vh - v_h), int(cw * 0.7), int(v_h), vcolor)

        # MA20 线
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

        # 形态标注（右上角）
        if self._hits:
            tags = "  ".join(f"[{h['pattern']}]" for h in self._hits)
            painter.setPen(QColor('#fbbf24'))
            f2 = QFont(); f2.setPointSize(9); f2.setBold(True); painter.setFont(f2)
            painter.drawText(w - 200, 18, f"形态: {tags}")


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
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.splitter.addWidget(self.table)
        # K线区
        self.kline_container = QWidget()
        self.kline_container.setMinimumHeight(200)
        kl = QVBoxLayout(self.kline_container)
        kl.setContentsMargins(2, 2, 2, 2)
        self.kline_info = QLabel("（点击表格行查看 K 线）")
        self.kline_info.setStyleSheet("color:#cfd6e4; padding:2px;")
        kl.addWidget(self.kline_info)
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
        self.scan_thread = ScanThread(codes=codes)
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
            # 存 row_data 供 K线刷新
            for col in range(9):
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
        self._kline_loader = KlineLoadThread(code, hits, info)
        self._kline_loader.loaded.connect(self._on_kline_loaded)
        self._kline_loader.failed.connect(self._on_kline_failed)
        self._kline_loader.start()

    def _on_kline_loaded(self, code, df, hits, info):
        self.kline_info.setText(info)
        self.kline_canvas.set_data(df, hits, info)

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
