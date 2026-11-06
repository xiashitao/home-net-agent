#!/usr/bin/env bash
# ==============================================================
# Home Network Agent 一键更新脚本
# 用法: ./update.sh
# 流程: 环境检查 → 保护本地改动 → 拉取代码 → 配置漂移检查
#       → 重建容器 → 健康检查 → 输出回滚提示
# ==============================================================
set -euo pipefail

cd "$(dirname "$0")"

# ---------- 颜色与输出工具 ----------
GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
step() { echo -e "\n${CYAN}==> $1${NC}"; }
ok()   { echo -e "${GREEN}✅ $1${NC}"; }
warn() { echo -e "${YELLOW}⚠️  $1${NC}"; }
fail() { echo -e "${RED}❌ $1${NC}"; exit 1; }

# ---------- 0. 环境检查 ----------
step "环境检查"
command -v git >/dev/null 2>&1    || fail "未安装 git"
command -v docker >/dev/null 2>&1 || fail "未安装 docker"
docker compose version >/dev/null 2>&1 || fail "未安装 docker compose 插件 (docker-compose-plugin)"
git rev-parse --git-dir >/dev/null 2>&1 || fail "当前目录不是 git 仓库"
ok "git / docker / docker compose 就绪"

# ---------- 1. 保护本地改动 ----------
step "检查本地未提交改动"
STASHED=0
if ! git diff --quiet --ignore-submodules || ! git diff --cached --quiet --ignore-submodules; then
    warn "工作区有未提交改动，已自动暂存 (更新后可用 git stash pop 恢复)"
    git stash push -m "update.sh auto-stash $(date '+%F %T')"
    STASHED=1
fi
[ "$STASHED" -eq 0 ] && ok "工作区干净"

# ---------- 2. 拉取最新代码 ----------
OLD_REV=$(git rev-parse --short HEAD)
BRANCH=$(git rev-parse --abbrev-ref HEAD)
step "拉取远程最新代码 (分支: $BRANCH, 当前: $OLD_REV)"

git fetch origin "$BRANCH"
git reset --hard "origin/$BRANCH" >/dev/null
NEW_REV=$(git rev-parse --short HEAD)

if [ "$OLD_REV" = "$NEW_REV" ]; then
    ok "已是最新版本 ($NEW_REV)，仍将重建容器以应用本地改动"
else
    ok "代码已更新: $OLD_REV → $NEW_REV"
    echo -e "本次更新内容:"
    git log --oneline --no-decorate "$OLD_REV..$NEW_REV" | head -20 | sed 's/^/    /'
fi

# ---------- 3. 配置漂移检查 ----------
step "检查 config/config.yaml"

if [ ! -f config/config.yaml ]; then
    warn "未找到 config/config.yaml，已从模板创建"
    cp config/config.example.yaml config/config.yaml
    warn "请先编辑填入真实参数 (nikki/llm/feishu 等)，再重新运行本脚本:"
    echo -e "    nano config/config.yaml"
    exit 1
fi
ok "config/config.yaml 存在 (git 不会覆盖它，改动安全)"

# 对比模板顶层配置段，发现新增配置项时提醒 (依赖无关的纯 grep 实现)
DRIFT=0
for key in $(grep -E '^[a-z_]+:' config/config.example.yaml | cut -d: -f1); do
    if ! grep -q "^${key}:" config/config.yaml; then
        warn "config.yaml 缺少配置段 [${key}] (新版新增，参考 config.example.yaml 补充)"
        DRIFT=1
    fi
done
[ "$DRIFT" -eq 0 ] && ok "配置段与模板一致，无漂移"

# ---------- 4. 重建并启动容器 ----------
step "重建并启动容器 (首次构建因 .dockerignore 优化会明显加速)"
docker compose up -d --build

# ---------- 5. 健康检查 ----------
step "健康检查"

# 解析 config.yaml 中 web.port (无法解析时回退 8080)
WEB_PORT=$(sed -n '/^web:/,/^[a-z_]/p' config/config.yaml \
    | grep -E '^\s+port:' | head -1 | awk '{print $2}' | tr -d '"' )
WEB_PORT=${WEB_PORT:-8080}

sleep 5
HEALTHY=0
for i in $(seq 1 15); do
    if curl -sf -m 2 "http://127.0.0.1:${WEB_PORT}/api/info" >/dev/null 2>&1; then
        ok "Web 控制台响应正常 (http://127.0.0.1:${WEB_PORT})"
        HEALTHY=1
        break
    fi
    sleep 2
done

if [ "$HEALTHY" -eq 0 ]; then
    warn "Web 控制台 30 秒内未响应 (可能 web.enabled=false 或仍在启动)，最近日志:"
    docker compose logs --tail 20 | sed 's/^/    /'
fi

echo ""
docker compose ps

# ---------- 6. 收尾提示 ----------
step "更新完成"
cat <<TIP

${GREEN}🎉 Home Network Agent 已更新至 ${NEW_REV}${NC}

📌 回归测试建议 (在飞书对机器人说):
   1. "B站走的是代理吗"          ← 验证流量追踪工具
   2. "测速并切换到最快节点"      ← 验证受保护服务护栏
   3. "网络体检"                  ← 验证诊断链路

🔄 如需回滚到上一版本:
   git checkout ${OLD_REV} && docker compose up -d --build
TIP

[ "$STASHED" -eq 1 ] && warn "本地改动已暂存在 git stash，恢复命令: git stash pop"
