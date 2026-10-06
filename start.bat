@echo off
REM WARNING: keep this file GBK-encoded with CRLF line endings, or cmd.exe will break.
cd /d %~dp0 2>nul
title 小满 · 成长证据库
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [提示] 没有检测到 Python。
    echo  请先到 https://www.python.org/downloads/ 下载安装 Python 3.11 或更高版本，
    echo  安装时务必勾选 "Add python.exe to PATH"，装完再双击本文件。
    echo.
    pause
    exit /b 1
)

if not exist .venv (
    echo.
    echo  首次运行，正在准备运行环境（约 1-2 分钟，仅这一次）...
    echo.
    python -m venv .venv 2>nul
    if errorlevel 1 goto fail
    .venv\Scripts\python.exe -m pip install --upgrade pip -q 2>nul
    .venv\Scripts\python.exe -m pip install -r requirements.txt 2>nul
    if errorlevel 1 goto fail
)

if not exist data\logs mkdir data\logs 2>nul
netstat -ano | findstr ":52122" | findstr "LISTENING" >nul 2>nul
if not errorlevel 1 (
    start "" "http://localhost:52122" 2>nul
    echo  小满已经在运行了，已帮你打开页面，无需重复启动。
    timeout /t 4 >nul 2>&1
    exit /b 0
)
if exist data\.launching (
    start "" "http://localhost:52122" 2>nul
    echo  小满正在启动中，已帮你打开页面，稍等几秒就能用。
    timeout /t 4 >nul 2>&1
    exit /b 0
)
echo launching> data\.launching

echo  ==================================================
echo    小满 · 成长证据库
echo  ==================================================
echo.
echo    这个窗口是小满在干活的地方，请保持开着。
echo.
echo    电脑访问： http://localhost:52122
echo    手机访问： 连同一个 Wi-Fi 后，打开 设置 页扫二维码
echo.
echo    下面一行行的是小满的工作汇报，不用看懂；
echo    如果它意外停了，会自己 3 秒后重新启动。
echo    想彻底退出 = 直接关闭这个窗口。
echo  --------------------------------------------------
echo.

start "" "http://localhost:52122" 2>nul

:run
.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 52122 --log-level warning 2>>data\logs\server-error.log
echo.
echo  [%date% %time%] 小满的程序意外退出了，3 秒后自动重启……
echo  [%date% %time%] 意外退出，已自动重启 >>data\logs\server-error.log
timeout /t 3 /nobreak >nul 2>&1
goto run

:fail
echo.
echo  [出错了] 环境准备失败，请检查网络后重试；或把本窗口内容截图求助。
echo.
pause
exit /b 1
