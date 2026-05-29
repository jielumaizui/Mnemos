#!/usr/bin/env bash
# Mnemos 一键安装脚本
# 用法: ./setup.sh [--yes] [--skip-memos] [--skip-obsidian]

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
echo "Mnemos 一键安装"
echo "项目路径: $PROJECT_ROOT"
echo ""

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo "✗ 未找到 python3，请先安装 Python >= 3.10"
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "Python: $PYTHON_VERSION"

# 运行 Python 部署脚本
cd "$PROJECT_ROOT"
python3 scripts/auto_setup.py "$@"
