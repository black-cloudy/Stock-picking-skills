@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."
set TASK=StockAgent_Meirenjian
set RUN=%CD%\tools\start_scan.bat

echo ============================================
echo  注册 Windows 计划任务：%TASK%
echo  执行：%RUN%
echo  时间：每天 16:10（收盘后）
echo ============================================
echo.

schtasks /Create /TN "%TASK%" /TR "\"%RUN%\"" /SC DAILY /ST 16:10 /F
if errorlevel 1 (
    echo.
    echo [失败] 请右键「以管理员身份运行」本脚本。
) else (
    echo.
    echo [成功] 已注册。常用命令：
    echo   查看详情 : schtasks /Query /TN "%TASK%" /V /FO LIST
    echo   立即执行 : schtasks /Run   /TN "%TASK%"
    echo   暂停     : schtasks /Change /TN "%TASK%" /DISABLE
    echo   恢复     : schtasks /Change /TN "%TASK%" /ENABLE
    echo   删除     : schtasks /Delete /TN "%TASK%" /F
)
echo.
pause
