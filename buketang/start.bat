@echo off
chcp 65001 >nul
setlocal
set "PYTHONUTF8=1"
cd /d "%~dp0"

echo.
echo   语音转录工坊 / 不听课
echo   首次运行会创建虚拟环境并安装依赖，需要联网。
echo.

rem ---- 找 Python：优先 py 启动器，其次 PATH 里的 python ----
set "PYCMD="
where py >nul 2>nul && set "PYCMD=py -3"
if not defined PYCMD (
  where python >nul 2>nul && set "PYCMD=python"
)
if not defined PYCMD (
  echo [x] 没找到 Python。
  echo     到 https://www.python.org/downloads/ 安装 3.10 或更高版本，
  echo     安装时勾选 "Add python.exe to PATH"，然后重新双击本文件。
  pause
  exit /b 1
)

%PYCMD% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
  echo [x] Python 版本太低，本程序需要 3.10 或更高。当前版本：
  %PYCMD% -V
  pause
  exit /b 1
)

set "VPY=.venv\Scripts\python.exe"

if not exist "%VPY%" (
  echo [1/2] 创建虚拟环境 .venv ...
  %PYCMD% -m venv .venv
  if errorlevel 1 (
    echo [x] 创建失败，请确认 Python 安装完整。
    pause
    exit /b 1
  )

)

if not exist ".venv\.deps-ready" (
  echo [2/2] 安装依赖，第一次可能要几分钟 ...
  "%VPY%" -m pip install -q --upgrade pip
  "%VPY%" -m pip install -q -r requirements.txt
  if errorlevel 1 (
    echo     默认源较慢，改用清华镜像重试 ...
    "%VPY%" -m pip install -q -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
  )
  if errorlevel 1 (
    echo [x] 依赖安装失败，请检查网络后重新启动。
    pause
    exit /b 1
  )
  type nul > ".venv\.deps-ready"
)

"%VPY%" -c "import sys; sys.path.insert(0,'..'); from shared.audio_utils import find_ffmpeg; sys.exit(0 if find_ffmpeg() else 1)" >nul 2>nul
if errorlevel 1 (
  echo     [提示] 没检测到 ffmpeg，长音频切分和转码会失败。
  echo            Windows: winget install Gyan.FFmpeg
)

"%VPY%" app.py
if errorlevel 1 (
  echo.
  echo [x] 程序异常退出，上面的信息可以贴到 issue 里。
  pause
)
endlocal
