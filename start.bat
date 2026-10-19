@echo off
chcp 65001 >nul
cd /d "%~dp0"
title StockAgent - A股形态量化选股

echo ==================================================
echo    StockAgent . A股形态量化选股
echo    经典美人肩 / 广义美人肩 / 低位票
echo ==================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 python，请先安装 Python 3.10+ 并勾选 "Add Python to PATH"
    pause
    exit /b 1
)

python -c "import fastapi, uvicorn, httpx" 2>nul
if errorlevel 1 (
    echo [1/2] 首次运行，正在安装依赖（约 1 分钟）...
    python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
    if errorlevel 1 (
        echo [错误] 依赖安装失败
        pause
        exit /b 1
    )
    echo.
)

rem ---------- 解析参数：可选的页面关键字 + 其余参数原样透传给 run.py ----------
set "SEL="
set "EXTRA="
:parse
if "%~1"=="" goto parsed
if /i "%~1"=="classic" (set "SEL=1" & shift & goto parse)
if /i "%~1"=="broad"   (set "SEL=2" & shift & goto parse)
if /i "%~1"=="lowpos"  (set "SEL=3" & shift & goto parse)
if /i "%~1"=="index"   (set "SEL=4" & shift & goto parse)
if /i "%~1"=="none"    (set "SEL=0" & shift & goto parse)
set "EXTRA=%EXTRA% %~1"
shift
goto parse

:parsed
if defined SEL goto pick

echo   启动后打开哪个页面？
echo     [1] 经典美人肩   ^(默认^)
echo     [2] 广义美人肩
echo     [3] 低位票
echo     [4] 实时行情工作台
echo     [0] 不打开浏览器
echo.
set /p "SEL=  请输入序号 [1]: "
if not defined SEL set "SEL=1"

:pick
set "PAGE=/mrj?mode=classic"
if "%SEL%"=="2" set "PAGE=/mrj?mode=broad"
if "%SEL%"=="3" set "PAGE=/mrj?mode=lowpos"
if "%SEL%"=="4" set "PAGE=/"
if "%SEL%"=="0" set "PAGE="

rem ---------- 端口 8000 已被占用时：直接打开已有服务，不重复启动 ----------
if not "%EXTRA%"=="" goto launch
python -c "import socket,sys; s=socket.socket(); sys.exit(0 if s.connect_ex(('127.0.0.1',8000))==0 else 1)"
if not errorlevel 1 (
    echo.
    echo [提示] 服务已经在运行  ^(http://127.0.0.1:8000^)，直接打开页面
    if not "%PAGE%"=="" start "" "http://127.0.0.1:8000%PAGE%"
    echo [提示] 本窗口可以直接关闭
    "%SystemRoot%\System32\timeout.exe" /t 3 >nul 2>&1
    exit /b 0
)

:launch
echo.
echo [2/2] 启动服务 ...  ^(Ctrl+C 或关闭本窗口即停止^)
echo.

if "%PAGE%"=="" (
    python run.py --no-open %EXTRA%
) else (
    python run.py --page "%PAGE%" %EXTRA%
)

echo.
echo 服务已停止。
pause
