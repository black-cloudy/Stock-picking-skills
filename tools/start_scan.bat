@echo off
chcp 65001 >nul
cd /d "%~dp0.."
if not exist ".data\alerts" mkdir ".data\alerts"
echo [%date% %time%] 开始巡检 >> ".data\alerts\cron.log"
python tools\daily_scan.py --refresh --popup >> ".data\alerts\cron.log" 2>&1
echo [%date% %time%] 巡检结束 (exit=%errorlevel%) >> ".data\alerts\cron.log"
