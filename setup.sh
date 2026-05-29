#!/usr/bin/env bash
# Mnemos 一键安装脚本 (macOS / Linux)
# 用法: ./setup.sh [--yes] [--skip-memos] [--skip-obsidian]

PROJECT_ROOT="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

echo "Mnemos 一键安装"
echo "项目路径: $PROJECT_ROOT"
echo ""

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo "✗ 未找到 python3，请先安装 Python >= 3.10"
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; v = sys.version_info; print(f"{v.major}.{v.minor}")')
echo "Python: $PYTHON_VERSION"

MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [ "$MAJOR" -lt 3 ] || ([ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 10 ]); then
    echo "✗ Python 版本过低 ($PYTHON_VERSION)，需要 >= 3.10"
    exit 1
fi

echo "✓ Python 版本满足 >= 3.10"
echo ""

# 运行 Python 部署脚本
cd "$PROJECT_ROOT"
python3 scripts/auto_setup.py "$@"
