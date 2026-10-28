@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
where python >nul 2>nul || goto nopython
echo Starting local UI, the browser will open automatically ...
python -m panharvest serve
pause
exit /b 0
:nopython
echo [x] Python not found. Install Python 3.10+ first.
pause
exit /b 1
