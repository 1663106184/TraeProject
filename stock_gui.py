# -*- coding: utf-8 -*-
"""
股票全市场扫描 - PyQt5 桌面版
复用 stock_full_scan 的扫描逻辑，结果直接显示在界面表格中，不再生成 CSV。
运行： py stock_gui.py
"""

import sys
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
from PyQt5.QtCore import Qt, QThread, pyqtSignal, QObject
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QLabel, QTableWidget, QTableWidgetItem,
    QProgressBar, QStatusBar, QHeaderView, QComboBox, QMessageBox
)

# 复用现有扫描逻辑（同一目录）
from stock_full_scan import generate_stock_codes, process_stock, format_number


# 结果列定义：(字段名, 显示表头, 是否右对齐数值, 是否需要格式化)
COLUMNS = [
    ('热度排名',   '热度排名', True,  False),
    ('代码',       '代码',     False, False),
    ('名称',       '名称',     False, False),
    ('市场板块',   '市场板块', False, False),
    ('收盘价',     '收盘价',   True,  False),
    ('涨跌幅',     '涨跌幅',   True,  True),   # 加 %
    ('热度',       '热度',     True,  False),
    ('量比',       '量比',     True,  False),
    ('委托比',     '委托比',   True,  True),   # 加 %
    ('换手率',     '换手率',   True,  False),
    ('成交额',     '成交额',   True,  True),   # 格式化 万/亿
    ('成交量(手)', '成交量(手)', True, False),
    ('成交量对比', '成交量对比', True, True),  # 加 %
    ('量能状态',   '量能状态', False, False),
    ('总市值',     '总市值',   True,  True),   # 格式化 万/亿
    ('诊股综合评分', '诊股综合', True, False),
    ('技术面评分', '技术面',   True,  False),
    ('资金面评分', '资金面',   True,  False),
    ('技术形态',   '技术形态', False, False),
    ('买入信号',   '买入信号', False, False),
    ('同花顺行业', '同花顺行业', False, False),
    ('同花顺板块', '同花顺板块', False, False),
    ('最相关概念', '最相关概念', False, False),
    ('所属概念数量', '概念数', True,  False),
    ('MA5',        'MA5',      True,  False),
    ('MA10',       'MA10',     True,  False),
    ('MA20',       'MA20',     True,  False),
    ('MA60',       'MA60',     True,  False),
    ('BBI',        'BBI',      True,  False),
    ('BBI位置',    'BBI位置',  False, False),
    ('布林线上轨', '布林上轨', True,  False),
    ('布林线中轨', '布林中轨', True,  False),
    ('布林线下轨', '布林下轨', True,  False),
    ('布林带位置', '布林位置', False, False),
    ('MACD_DIF',   'MACD_DIF', True,  False),
    ('MACD_DEA',   'MACD_DEA', True,  False),
    ('MACD柱',     'MACD柱',   True,  False),
    ('MACD信号',   'MACD信号', False, False),
    ('均线状态',   '均线状态', False, False),
    ('多头排列',   '多头排列', False, False),
    ('利好消息',   '利好消息', False, False),
    ('日期',       '日期',     False, False),
]


class ScanWorker(QObject):
    """后台扫描线程：复用 process_stock，并发拉取，通过信号回传进度与结果。"""
    progress = pyqtSignal(int, int, int, int)   # 已扫描, 总数, 有效, 上涨
    result_ready = pyqtSignal(list)             # 一批结果(dict 列表)
    finished = pyqtSignal(int, int, int, float)  # 总数, 有效, 上涨, 耗时
    error = pyqtSignal(str)

    def __init__(self, max_workers=30):
        super().__init__()
        self.max_workers = max_workers
        self._stop = False
        self._batch = []
        self._batch_size = 50

    def stop(self):
        self._stop = True

    def run(self):
        import time
        try:
            codes = generate_stock_codes()
            total = len(codes)
            stocks_data = []
            valid = rise = 0
            start = time.time()

            with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
                futures = {ex.submit(process_stock, c): c for c in codes}
                for idx, fut in enumerate(as_completed(futures), 1):
                    if self._stop:
                        # 取消未完成的任务
                        for f in futures:
                            f.cancel()
                        break
                    try:
                        res = fut.result()
                    except Exception:
                        res = None
                    if res:
                        stocks_data.append(res)
                        rise += 1
                        valid += 1
                        self._batch.append(res)
                        if len(self._batch) >= self._batch_size:
                            self.result_ready.emit(self._batch)
                            self._batch = []

                    if idx % 200 == 0 or idx == total:
                        self.progress.emit(idx, total, valid, rise)

            # flush 剩余
            if self._batch:
                self.result_ready.emit(self._batch)
                self._batch = []

            elapsed = time.time() - start
            self.finished.emit(total, valid, rise, elapsed)
        except Exception as e:
            self.error.emit(str(e))


class SortableTableWidgetItem(QTableWidgetItem):
    """让文本列按数值排序（如 '代码'、'涨跌幅' 等）。"""

    def __lt__(self, other):
        try:
            return float(self.data(Qt.UserRole)) < float(other.data(Qt.UserRole))
        except (TypeError, ValueError):
            return self.text() < other.text()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("股票全市场扫描 - 桌面版")
        self.resize(1400, 820)

        self.all_rows = []          # 全部已接收结果（dict）
        self.worker = None
        self.thread = None
        self.scanning = False

        self._build_ui()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # 顶部工具栏
        bar = QHBoxLayout()
        self.btn_scan = QPushButton("开始扫描")
        self.btn_scan.setFixedHeight(30)
        self.btn_scan.setStyleSheet("QPushButton{background:#2d8cf0;color:white;font-weight:bold;border-radius:4px;padding:0 18px} QPushButton:hover{background:#5cadff} QPushButton:disabled{background:#a0c4f0}")
        self.btn_scan.clicked.connect(self.toggle_scan)

        self.btn_export = QPushButton("导出 CSV")
        self.btn_export.setFixedHeight(30)
        self.btn_export.clicked.connect(self.export_csv)

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

        self.count_label = QLabel("共 0 只")
        self.count_label.setStyleSheet("color:#666")

        bar.addWidget(self.btn_scan)
        bar.addWidget(self.btn_export)
        bar.addWidget(self.btn_clear)
        bar.addWidget(QLabel("筛选:"))
        bar.addWidget(self.col_filter, 1)
        bar.addWidget(self.filter_box, 3)
        bar.addWidget(self.count_label)
        layout.addLayout(bar)

        # 表格
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels([h for _, h, _, _ in COLUMNS])
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionsMovable(True)
        self.table.horizontalHeader().setStretchLastSection(True)
        # 点击表头排序：升序/降序切换，表头显示 ▲/▼
        self._sort_col = None          # 当前排序列
        self._sort_order = 0           # 0未排 1升 2降
        self._orig_headers = [h for _, h, _, _ in COLUMNS]
        self.table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        # 列宽
        for i, (field, header, is_num, _) in enumerate(COLUMNS):
            if field in ('代码', '名称', '市场板块'):
                self.table.setColumnWidth(i, 90 if field != '名称' else 90)
            elif field == '利好消息':
                self.table.setColumnWidth(i, 300)
            elif field in ('技术形态', '买入信号'):
                self.table.setColumnWidth(i, 160)
            elif is_num:
                self.table.setColumnWidth(i, 85)
            else:
                self.table.setColumnWidth(i, 90)
        self.table.setColumnWidth(1, 95)   # 代码
        self.table.setColumnWidth(2, 100)  # 名称
        self.table.itemDoubleClicked.connect(self.show_detail)
        layout.addWidget(self.table, 1)

        # 进度条
        self.progress = QProgressBar()
        self.progress.setFixedHeight(18)
        self.progress.setTextVisible(True)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        # 状态栏
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.showMessage("就绪。点击「开始扫描」")

    # ---------- 扫描控制 ----------
    def toggle_scan(self):
        if self.scanning:
            self.stop_scan()
        else:
            self.start_scan()

    def start_scan(self):
        self.clear_table()
        self.scanning = True
        self.btn_scan.setText("停止扫描")
        self.btn_export.setEnabled(False)
        self.btn_clear.setEnabled(False)
        self.progress.setValue(0)
        self.status.showMessage("正在扫描…")

        self.thread = QThread()
        self.worker = ScanWorker(max_workers=30)
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

    def on_progress(self, done, total, valid, rise):
        pct = int(done / total * 100) if total else 0
        self.progress.setValue(pct)
        self.progress.setFormat(f"{pct}%  {done}/{total}")
        self.status.showMessage(f"扫描中  已扫描 {done}/{total}  有效 {valid}  上涨 {rise}")

    def on_result_batch(self, batch):
        self.all_rows.extend(batch)
        self._append_rows(batch)
        self.count_label.setText(f"共 {len(self.all_rows)} 只")

    def on_finished(self, total, valid, rise, elapsed):
        self.scanning = False
        self.btn_scan.setText("开始扫描")
        self.btn_export.setEnabled(True)
        self.btn_clear.setEnabled(True)
        self.progress.setValue(100)
        # 计算热度排名（与原脚本一致：按热度降序 dense rank）
        self._recompute_rank()
        self.status.showMessage(
            f"扫描完成  总数 {total}  有效 {valid}  上涨 {rise}  耗时 {elapsed:.1f}s", 8000
        )

    def on_error(self, msg):
        self.scanning = False
        self.btn_scan.setText("开始扫描")
        self.btn_export.setEnabled(True)
        self.btn_clear.setEnabled(True)
        QMessageBox.critical(self, "扫描出错", msg)
        self.status.showMessage("扫描出错: " + msg)

    # ---------- 排序 ----------
    def _on_header_clicked(self, col):
        """点击表头：切换升序/降序；切到新列默认降序。"""
        if self._sort_col == col:
            self._sort_order = 2 if self._sort_order == 1 else 1
        else:
            self._sort_col = col
            self._sort_order = 2     # 切到新列默认降序
        self._refresh_sort_marks()
        order = Qt.AscendingOrder if self._sort_order == 1 else Qt.DescendingOrder
        self.table.sortItems(col, order)

    def _refresh_sort_marks(self):
        """刷新表头箭头标记：升序▲、降序▼，其余列还原。"""
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

            cell = SortableTableWidgetItem(text)
            # 在每个 cell 里存完整行数据，排序后双击仍能取回正确行
            cell.setData(Qt.UserRole + 1, item)
            # 用 UserRole 存原始数值，便于排序
            sort_val = val
            if is_num and isinstance(val, (int, float)):
                sort_val = float(val)
            cell.setData(Qt.UserRole, sort_val if sort_val != '' else 0)

            if is_num:
                cell.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            else:
                cell.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)

            # 颜色：涨跌幅红涨绿跌（A 股惯例）
            if field == '涨跌幅':
                try:
                    f = float(val)
                    if f > 0:
                        cell.setForeground(QColor('#e23b3b'))
                    elif f < 0:
                        cell.setForeground(QColor('#1faa52'))
                except (TypeError, ValueError):
                    pass
            elif field == 'BBI位置':
                if text.startswith('BBI之上'):
                    cell.setForeground(QColor('#e23b3b'))
                elif text.startswith('BBI之下'):
                    cell.setForeground(QColor('#1faa52'))
            elif field == '买入信号' and text and text != '观望':
                cell.setForeground(QColor('#e23b3b'))
                f = QFont(); f.setBold(True); cell.setFont(f)

            self.table.setItem(row, col, cell)

    def _format_value(self, field, val):
        try:
            if field in ('成交额', '总市值'):
                return format_number(float(val)) if val else '0'
            if field == '涨跌幅':
                return f"{float(val):.2f}%" if val != '' else '0.00%'
            if field in ('委托比', '成交量对比'):
                f = float(val)
                return f"{f:+.2f}%" if abs(f) > 0.001 else "0.00%"
        except (TypeError, ValueError):
            pass
        return '' if val is None else str(val)

    def _recompute_rank(self):
        """按热度降序重新排名并刷新对应列。"""
        if not self.all_rows:
            return
        df = pd.DataFrame(self.all_rows)
        if '热度' in df:
            df['热度排名'] = df['热度'].rank(ascending=False, method='dense').astype(int)
            self.all_rows = df.to_dict('records')
        # 刷新表中“热度排名”列（COLUMNS 中索引 0）
        rank_col = 0
        for row, item in enumerate(self.all_rows):
            cell = self.table.item(row, rank_col)
            if cell:
                v = item.get('热度排名', 0)
                cell.setText(str(v))
                cell.setData(Qt.UserRole, float(v))

    # ---------- 筛选 ----------
    def apply_filter(self, *_):
        kw = self.filter_box.text().strip().lower()
        field = self.col_filter.currentData()
        shown = 0
        for row in range(self.table.rowCount()):
            match = True
            if kw:
                if field:
                    # 指定列筛选
                    col = next((i for i, (f, *_ ) in enumerate(COLUMNS) if f == field), None)
                    if col is not None:
                        txt = self.table.item(row, col).text().lower() if self.table.item(row, col) else ''
                        match = kw in txt
                    else:
                        match = False
                else:
                    # 全列筛选
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
        # 重置排序状态与表头标记
        self._sort_col = None
        self._sort_order = 0
        for i, header in enumerate(self._orig_headers):
            self.table.horizontalHeaderItem(i).setText(header)
        self.status.showMessage("已清空")

    def show_detail(self, item):
        # 排序后视觉行序与 all_rows 不一致，从 cell 取行数据
        row_data = item.data(Qt.UserRole + 1)
        if not row_data:
            row = item.row()
            if row >= len(self.all_rows):
                return
            row_data = self.all_rows[row]
        lines = []
        for field, header, _, _ in COLUMNS:
            val = row_data.get(field, '')
            # 概念板块：详情里展示完整概念列表
            if field == '最相关概念':
                concepts = row_data.get('概念列表', []) or []
                val = '、'.join(concepts) if concepts else '未分类'
            lines.append(f"{header}: {val}")
        QMessageBox.information(self, "股票详情", "\n".join(lines))

    def export_csv(self):
        if not self.all_rows:
            QMessageBox.information(self, "提示", "当前没有数据可导出")
            return
        from PyQt5.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 CSV", f"B点股票_{pd.Timestamp.now().strftime('%Y%m%d')}.csv",
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
    # 高 DPI 适配
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
