@echo off
rem zcode2api Windows 一键启动：自动创建虚拟环境、安装依赖并启动网关。
rem 需已安装 Python（py 启动器）与本机 Chrome 或 Edge（验证码求解会自动探测使用）。
chcp 65001 >nul
cd /d %~dp0

where py >nul 2>nul
if errorlevel 1 (
  where python >nul 2>nul
  if errorlevel 1 (
    echo [!] 未找到 Python，请先安装：https://www.python.org/downloads/
    pause
    exit /b 1
)

if not exist .venv\Scripts\python.exe (
  echo [*] 首次运行：创建虚拟环境并安装依赖（需几分钟）...
  py -3 -m venv .venv || python -m venv .venv
  call .venv\Scripts\activate.bat
  python -m pip install -r requirements.txt || (
    echo [!] 依赖安装失败，请检查网络后重试
    pause
    exit /b 1
) else (
  call .venv\Scripts\activate.bat
)

echo [*] 启动 zcode2api（默认 http://127.0.0.1:3000 ，后台默认密码 zcode）
python main.py serve
pause
