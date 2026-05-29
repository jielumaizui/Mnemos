#!/usr/bin/env bash
# Mnemos 一键安装脚本 (macOS / Linux)
# 用法: ./setup.sh [--yes] [--skip-memos] [--skip-obsidian] [--skip-daemon] [--skip-scheduler] [--skip-hooks] [--dry-run]

PROJECT_ROOT="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

# --help / -h 直接透传给 Python 脚本，避免先执行环境检查
for arg in "$@"; do
    if [ "$arg" = "--help" ] || [ "$arg" = "-h" ]; then
        cd "$PROJECT_ROOT"
        python3 scripts/auto_setup.py --help
        exit 0
    fi
done

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

# 优先使用虚拟环境 Python（如果存在），否则创建
if [ -f "$PROJECT_ROOT/.venv/bin/python" ]; then
    PYTHON="$PROJECT_ROOT/.venv/bin/python"
    echo "✓ 使用虚拟环境 Python: $PYTHON"
elif [ -f "$PROJECT_ROOT/.venv/Scripts/python.exe" ]; then
    PYTHON="$PROJECT_ROOT/.venv/Scripts/python.exe"
    echo "✓ 使用虚拟环境 Python: $PYTHON"
else
    echo "创建虚拟环境 .venv ..."
    python3 -m venv "$PROJECT_ROOT/.venv"
    if [ -f "$PROJECT_ROOT/.venv/bin/python" ]; then
        PYTHON="$PROJECT_ROOT/.venv/bin/python"
        echo "✓ 使用虚拟环境 Python: $PYTHON"
    elif [ -f "$PROJECT_ROOT/.venv/Scripts/python.exe" ]; then
        PYTHON="$PROJECT_ROOT/.venv/Scripts/python.exe"
        echo "✓ 使用虚拟环境 Python: $PYTHON"
    else
        echo "✗ 虚拟环境创建失败"
        exit 1
    fi
fi
echo ""

# 运行 Python 部署脚本
cd "$PROJECT_ROOT"
"$PYTHON" scripts/auto_setup.py "$@"

# 安装完成提示
cd "$PROJECT_ROOT"
echo ""
echo "========================================"
echo "安装完成！"
echo ""
echo "后续请使用以下命令运行 Mnemos:"
echo "  .venv/bin/python mnemos_cli.py ..."
echo "  或: source .venv/bin/activate && python mnemos_cli.py ..."
echo "========================================"
