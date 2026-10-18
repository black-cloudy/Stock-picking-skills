@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo   StockAgent - A股实时行情服务
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未找到 python，请先安装 Python 3.10+ 并加入 PATH
    pause
    exit /b 1
)

python -c "import fastapi, uvicorn, httpx" 2>nul
if errorlevel 1 (
    echo [1/2] 首次运行，正在安装依赖...
    python -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
    if errorlevel 1 (
        echo [错误] 依赖安装失败
        pause
        exit /b 1
    )
)

echo [2/2] 启动服务...
echo.
python run.py %*

pause
