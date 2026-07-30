@echo off
chcp 65001 >nul
REM ============================================================
REM  一键更新行业数据 + 重新打包两个 GUI 为单文件 exe
REM  双击运行即可。产物在 dist\ 目录下。
REM ============================================================
cd /d "%~dp0"
set PYTHON=py -3

echo ============================================================
echo  [1/4] 从 MySQL 导出最新行业数据 -> stock_industry.csv
echo ============================================================
%PYTHON% -c "import pymysql,csv; \
conn=pymysql.connect(host='localhost',port=3306,user='root',password='root',database='stock_db',charset='utf8mb4'); \
cur=conn.cursor(); cur.execute('SELECT code,name,industry,sector,concepts FROM stock_industry'); \
rows=cur.fetchall(); conn.close(); \
open('stock_industry.csv','w',encoding='utf-8-sig',newline='').write(''); \
import io; \
w=csv.writer(open('stock_industry.csv','w',encoding='utf-8-sig',newline='')); \
w.writerow(['code','name','industry','sector','concepts']); w.writerows(rows); \
print(f'导出 {len(rows)} 行 -> stock_industry.csv')"
if errorlevel 1 (
    echo [错误] 导出失败，请确认 MySQL 已启动且 stock_db.stock_industry 表存在。
    pause & exit /b 1
)

echo.
echo ============================================================
echo  [2/4] 打包 stock_leader_scanner.exe
echo ============================================================
%PYTHON% -m PyInstaller --onefile --noconsole --name stock_leader_scanner ^
    --add-data "stock_industry.csv;." --hidden-import pymysql --collect-all PyQt5 ^
    stock_leader_scanner.py --noconfirm
if errorlevel 1 ( echo [错误] leader 打包失败 & pause & exit /b 1 )

echo.
echo ============================================================
echo  [3/4] 打包 stock_chip_filter_gui.exe
echo ============================================================
%PYTHON% -m PyInstaller --onefile --noconsole --name stock_chip_filter_gui ^
    --add-data "stock_industry.csv;." --hidden-import pymysql --collect-all PyQt5 ^
    stock_chip_filter_gui.py --noconfirm
if errorlevel 1 ( echo [错误] chip 打包失败 & pause & exit /b 1 )

echo.
echo ============================================================
echo  [4/4] 清理中间产物
echo ============================================================
rmdir /s /q build 2>nul
del /q stock_leader_scanner.spec 2>nul
del /q stock_chip_filter_gui.spec 2>nul

echo.
echo ============================================================
echo  全部完成！产物：
echo    dist\stock_leader_scanner.exe
echo    dist\stock_chip_filter_gui.exe
echo ============================================================
dir dist\*.exe
pause
