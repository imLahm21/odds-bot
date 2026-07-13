#!/usr/bin/env bash
# 服务器一键提交实战教训 —— bot 跑 /lesson 归档后, 把 rules/实战教训/ 的改动
# add + commit + push 到 GitHub, 让教训进库不丢、本地能 pull 到。
#
# 为什么需要它: bot /lesson 运行时会往 rules/实战教训/ 写文件(案例卡/主题补丁/索引)。
# 这些是 git 跟踪的文件, 堆在服务器工作区不提交, 迟早和远程分叉、导致 git pull 冲突。
# 本脚本按【安全顺序】处理, 避免分叉:
#   1. 先 fetch 看远程有没有新提交(本地代码是否落后)
#   2. 若落后且工作区有教训改动 → 先 stash → pull → pop → 再提交(防"未pull就push"分叉)
#   3. add 仅限 rules/实战教训/(不误提交 .env 以外的其它改动)
#   4. commit(自动生成双语信息) → push
#
# 用法:
#   bash deploy/commit-lessons.sh                    # 自动生成提交信息
#   bash deploy/commit-lessons.sh "自定义提交标题"    # 用自定义标题
#
# 依赖: git(已配 user.name/user.email); 只操作 rules/实战教训/ 目录。
set -uo pipefail   # 不加 -e: 需按步骤判定, 单步失败要给出明确提示而非直接退出

LESSON_DIR="rules/实战教训"
BRANCH="${BRANCH:-main}"

# 定位到项目根(脚本在 deploy/ 下, 根在其上一级), 确保相对路径正确
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.." || { echo "[error] 无法进入项目根目录"; exit 1; }

# 前置检查: 是 git 仓库、身份已配
if [ ! -d .git ]; then
    echo "[error] 当前不是 git 仓库: $(pwd)"; exit 1
fi
if ! git config user.email >/dev/null 2>&1 || [ -z "$(git config user.email)" ]; then
    echo "[error] git 身份未配置。先跑一次(仅本库):"
    echo "        git config user.name \"imLahm21\""
    echo "        git config user.email \"imLahm21@users.noreply.github.com\""
    exit 1
fi

# 1. 看 rules/实战教训/ 有没有改动(含未跟踪新文件)
CHANGES="$(git status --porcelain -- "$LESSON_DIR")"
if [ -z "$CHANGES" ]; then
    echo "[ok] $LESSON_DIR 无改动, 无需提交。"
    exit 0
fi
echo "[..] 检测到教训改动:"
echo "$CHANGES" | sed 's/^/    /'

# 2. 安全同步: 先 fetch, 若本地落后远程, 先把教训改动 stash 起来再 pull, 避免分叉
echo "[..] 检查远程是否有新提交…"
if git fetch origin "$BRANCH" 2>/dev/null; then
    BEHIND="$(git rev-list --count HEAD..origin/$BRANCH 2>/dev/null || echo 0)"
    if [ "${BEHIND:-0}" -gt 0 ]; then
        echo "[..] 本地落后远程 $BEHIND 个提交, 先暂存教训改动再 pull…"
        git stash push -u -m "commit-lessons 临时暂存" -- "$LESSON_DIR" >/dev/null 2>&1
        if git pull --ff-only origin "$BRANCH" 2>&1 | tail -3; then
            echo "[ok] 已同步远程"
        else
            echo "[error] pull 失败, 已把改动 stash。手动处理后 git stash pop。"; exit 1
        fi
        # 恢复教训改动; 有冲突则提示手动处理
        if ! git stash pop 2>&1 | tail -3; then
            echo "[error] 恢复暂存时冲突, 请手动解决后再跑本脚本。"; exit 1
        fi
    else
        echo "[ok] 本地已是最新"
    fi
else
    echo "[warn] fetch 失败(网络?), 跳过同步检查直接提交(若稍后 push 被拒, 重跑本脚本)"
fi

# 3. 只 add 教训目录(绝不 git add . , 防误提交其它改动)
git add "$LESSON_DIR"
if git diff --cached --quiet; then
    echo "[ok] 暂存后无净改动(可能已被同步), 无需提交。"
    exit 0
fi

# 4. 生成提交信息: 优先用参数标题; 否则按新增案例卡自动拟(双语, 贴合开源风格)
TITLE="${1:-}"
if [ -z "$TITLE" ]; then
    # 找本次新增的案例卡文件名(如 20260713_case_11_XXX.md), 提取用于标题。
    # -c core.quotepath=false: 否则中文路径会被转义并裹上引号, 污染标题(结尾多个 ")。
    NEWCARD="$(git -c core.quotepath=false diff --cached --name-only \
               --diff-filter=A -- "$LESSON_DIR" \
               | grep -E 'case_[0-9]+' | head -1)"
    if [ -n "$NEWCARD" ]; then
        BASE="$(basename "$NEWCARD" .md)"
        TITLE="Archive lesson: $BASE"
    else
        TITLE="Update lesson archive ($(date +%Y-%m-%d))"
    fi
fi

# 双语提交信息(英文标题在前 + 中文说明), 与项目开源提交风格一致
STAT="$(git -c core.quotepath=false diff --cached --stat | tail -8)"
git commit -F - <<EOF
$TITLE

服务器归档实战教训 ($(date '+%Y-%m-%d %H:%M'))

- 由 bot /lesson 归档写入, 服务器一键提交(deploy/commit-lessons.sh)。
- 改动文件:
$(echo "$STAT" | sed 's/^/  /')
EOF
if [ $? -ne 0 ]; then
    echo "[error] commit 失败"; exit 1
fi
echo "[ok] 已提交: $(git log -1 --format='%h %s')"

# 5. 推送
echo "[..] 推送到 origin/$BRANCH …"
if git push origin "$BRANCH" 2>&1 | tail -3; then
    echo "[done] 教训已推送到 GitHub。本地可 git pull 同步。"
else
    echo "[error] push 失败(网络? 或远程又有新提交)。稍后重跑本脚本即可(提交已在本地)。"
    exit 1
fi
