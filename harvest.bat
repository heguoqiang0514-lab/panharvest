@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
where python >nul 2>nul || goto nopython
python -m panharvest %*
set RC=%ERRORLEVEL%
if "%~1"=="" pause
exit /b %RC%
:nopython
echo.
echo [x] Python not found. Install Python 3.10+ and tick "Add python.exe to PATH".
echo     https://www.python.org/downloads/windows/
echo.
pause
exit /b 1
