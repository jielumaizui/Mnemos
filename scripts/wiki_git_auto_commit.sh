#!/bin/bash
# Wiki Git 自动提交脚本
# 用途: 由 chronos 或 cron 调用，自动提交 Wiki 变更，支持 5 分钟内回滚
# 安装: chmod +x scripts/wiki_git_auto_commit.sh
# 配置: 在 crontab 中添加 */5 * * * * /path/to/scripts/wiki_git_auto_commit.sh

# 请通过环境变量 WIKI_DIR 指定 Wiki 目录，例如：
#   WIKI_DIR="~/Documents/Obsidian Vault/wiki" ./scripts/wiki_git_auto_commit.sh
WIKI_DIR="${WIKI_DIR:-~/Documents/Obsidian Vault/wiki}"
WIKI_DIR_EXPANDED="$(eval echo "$WIKI_DIR")"

if [ ! -d "$WIKI_DIR_EXPANDED" ]; then
    echo "[wiki-git] Wiki 目录不存在: $WIKI_DIR"
    echo "[wiki-git] 请设置环境变量 WIKI_DIR 为实际 Wiki Git 仓库路径"
    exit 1
fi

cd "$WIKI_DIR_EXPANDED" || exit 1

# 检查是否有变更
if git diff --quiet && git diff --cached --quiet; then
    exit 0
fi

# 生成提交信息（带时间戳）
TIMESTAMP=$(date +"%Y-%m-%d %H:%M:%S")
CHANGED=$(git diff --name-only | wc -l | tr -d ' ')
STAGED=$(git diff --cached --name-only | wc -l | tr -d ' ')

MSG="auto: ${CHANGED} modified, ${STAGED} staged @ ${TIMESTAMP}"

# 提交
git add -A
git commit -m "$MSG"

# 保留最近 50 条提交，自动清理旧历史（可选）
# git reflog expire --expire=30.days --all
# git gc --prune=30.days

echo "[wiki-git] Committed: $MSG"
