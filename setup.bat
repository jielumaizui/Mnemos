@echo off
chcp 65001 >nul
REM Mnemos 一键安装脚本 (Windows)
REM 用法: .\setup.bat [--yes] [--skip-backend] [--skip-daemon] [--skip-scheduler] [--skip-hooks] [--dry-run]

REM [P1-FIX] Enable delayed expansion so variables set inside if-blocks are visible.
setlocal enabledelayedexpansion

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

REM [P1-FIX] Robust Python version parsing: handles "Python X.Y.Z", "X.Y.Z", and leading whitespace.
for /f "delims=" %%v in ('%PY_CMD% --version 2^>^&1') do (
    set "PY_VER_RAW=%%v"
)
REM Extract the first token that looks like a version number (contains a dot).
for %%a in (%PY_VER_RAW%) do (
    echo %%a | findstr /r "^[0-9][0-9]*\.[0-9]" >nul
    if not errorlevel 1 (
        set "PYTHON_VERSION=%%a"
    )
)
echo Python: %PYTHON_VERSION%

for /f "tokens=1,2 delims=." %%a in ("%PYTHON_VERSION%") do (
    set "PY_MAJOR=%%a"
    set "PY_MINOR=%%b"
)

if not defined PYTHON_VERSION (
    echo ✗ 无法解析 Python 版本，请确认 Python ^>= 3.10 可用
    exit /b 1
)
if not defined PY_MAJOR (
    echo ✗ 无法解析 Python 主版本，请确认 Python ^>= 3.10 可用
    exit /b 1
)
if not defined PY_MINOR (
    echo ✗ 无法解析 Python 次版本，请确认 Python ^>= 3.10 可用
    exit /b 1
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
set "SETUP_STATUS=%ERRORLEVEL%"
if not "%SETUP_STATUS%"=="0" (
    exit /b %SETUP_STATUS%
)
exit /b 0
