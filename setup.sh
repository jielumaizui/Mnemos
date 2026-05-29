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

# 优先使用虚拟环境 Python（如果存在）
if [ -f "$PROJECT_ROOT/.venv/bin/python" ]; then
    PYTHON="$PROJECT_ROOT/.venv/bin/python"
    echo "✓ 使用虚拟环境 Python: $PYTHON"
elif [ -f "$PROJECT_ROOT/.venv/Scripts/python.exe" ]; then
    PYTHON="$PROJECT_ROOT/.venv/Scripts/python.exe"
    echo "✓ 使用虚拟环境 Python: $PYTHON"
else
    PYTHON="python3"
    echo "使用系统 Python: $PYTHON"
fi
echo ""

# 运行 Python 部署脚本
cd "$PROJECT_ROOT"
"$PYTHON" scripts/auto_setup.py "$@"

# 如果虚拟环境不存在但 auto_setup.py 创建了它，提示用户
cd "$PROJECT_ROOT"
if [ -f ".venv/bin/python" ] && [ "$PYTHON" = "python3" ]; then
    echo ""
    echo "提示: 已创建虚拟环境 .venv"
    echo "后续请使用以下命令运行 Mnemos:"
    echo "  .venv/bin/python mnemos_cli.py ..."
    echo "  source .venv/bin/activate && python mnemos_cli.py ..."
fi
