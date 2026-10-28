@echo off
chcp 65001 >nul
setlocal
set "PYTHONUTF8=1"
cd /d "%~dp0"

echo.
echo   不听课 · 课堂录音转复习指导
echo.

set "PYCMD="
where py >nul 2>nul && set "PYCMD=py -3"
if not defined PYCMD (
  where python >nul 2>nul && set "PYCMD=python"
)
if not defined PYCMD (
  echo [x] 没找到 Python，请安装 3.10 或更高版本并勾选 Add to PATH。
  pause
  exit /b 1
)

%PYCMD% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
  echo [x] Python 版本太低，需要 3.10 或更高。当前：
  %PYCMD% -V
  pause
  exit /b 1
)

set "VPY=.venv\Scripts\python.exe"
if not exist "%VPY%" (
  echo [1/2] 创建虚拟环境 .venv ...
  %PYCMD% -m venv .venv
  if errorlevel 1 (
    echo [x] 创建失败。
    pause
    exit /b 1
  )
)

"%VPY%" "shared\bootstrap.py" requirements.txt
if errorlevel 1 (
  pause
  exit /b 1
)

"%VPY%" "buketang\app.py"
if errorlevel 1 (
  echo.
  echo [x] 程序异常退出，上面的信息可以贴到 issue 里。
  pause
)
endlocal
