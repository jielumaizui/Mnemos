@echo off
chcp 65001 >nul
REM Mnemos 一键安装脚本 (Windows)
REM 用法: setup.bat [--yes] [--skip-memos] [--skip-obsidian]

set "PROJECT_ROOT=%~dp0"
set "PROJECT_ROOT=%PROJECT_ROOT:~0,-1%"

echo Mnemos 一键安装
echo 项目路径: %PROJECT_ROOT%
echo.

REM 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    python3 --version >nul 2>&1
    if errorlevel 1 (
        echo ✗ 未找到 python 或 python3，请先安装 Python ^>= 3.10
        exit /b 1
    )
    set "PY_CMD=python3"
) else (
    set "PY_CMD=python"
)

for /f "tokens=2" %%v in ('%PY_CMD% --version') do set "PYTHON_VERSION=%%v"
echo Python: %PYTHON_VERSION%

for /f "tokens=1,2 delims=." %%a in ("%PYTHON_VERSION%") do (
    set "PY_MAJOR=%%a"
    set "PY_MINOR=%%b"
)

if %PY_MAJOR% LSS 3 (
    echo ✗ Python 版本过低 ^(%PYTHON_VERSION%^)，需要 ^>= 3.10
    exit /b 1
)
if %PY_MAJOR% EQU 3 (
    if %PY_MINOR% LSS 10 (
        echo ✗ Python 版本过低 ^(%PYTHON_VERSION%^)，需要 ^>= 3.10
        exit /b 1
    )
)

echo ✓ Python 版本满足 ^>= 3.10
echo.

REM 运行 Python 部署脚本
cd /d "%PROJECT_ROOT%"
%PY_CMD% scripts\auto_setup.py %*
