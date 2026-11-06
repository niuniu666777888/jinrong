#!/usr/bin/env bash
# 一键部署：把 src/ 打包推到学校服务器，并在远端重启服务。
#
# 用法：
#   ./deploy.sh user@host            # 默认走 Docker
#   ./deploy.sh user@host systemd    # 走 systemd + venv
#   PORT=2222 ./deploy.sh user@host  # 自定义 SSH 端口
#
# 前置（远端）：
#   - 已安装 docker + docker compose，或 python3.10+ / nginx
#   - 部署目录 /opt/jinrong（首次会自动创建，需要 sudo）
set -euo pipefail

TARGET="${1:?用法: ./deploy.sh user@host [docker|systemd]}"
MODE="${2:-docker}"
PORT="${PORT:-22}"
REMOTE_DIR="${REMOTE_DIR:-/opt/jinrong}"

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(dirname "$HERE")"          # src/

echo "==> 打包 src/ 并上传到 ${TARGET}:${REMOTE_DIR}"
tar czf - --exclude='__pycache__' --exclude='*.pyc' --exclude='.venv' \
         --exclude='mvp.db' -C "$SRC" . \
  | ssh -p "$PORT" "$TARGET" "sudo mkdir -p '${REMOTE_DIR}/src' && sudo tar xzf - -C '${REMOTE_DIR}/src' --strip-components=1 && sudo chown -R \$(id -u):\$(id -g) '${REMOTE_DIR}'"

if [ "$MODE" = "systemd" ]; then
  echo "==> systemd 模式：创建 venv 并安装依赖"
  ssh -p "$PORT" "$TARGET" bash -s <<'REMOTE'
set -euo pipefail
cd /opt/jinrong/src
[ -d ../.venv ] || python3 -m venv ../.venv
../.venv/bin/pip install -q --upgrade pip
../.venv/bin/pip install -q -r requirements.txt
sudo mkdir -p /var/lib/jinrong
sudo chown -R "$(id -u):$(id -g)" /var/lib/jinrong
sudo cp deploy/jinrong-mvp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jinrong-mvp
sudo systemctl restart jinrong-mvp
REMOTE
else
  echo "==> Docker 模式：构建并启动"
  ssh -p "$PORT" "$TARGET" bash -s <<'REMOTE'
set -euo pipefail
cd /opt/jinrong/src/deploy
[ -f .env ] || { echo "MVP_SECRET=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')" > .env; chmod 600 .env; }
docker compose up -d --build
docker compose ps
REMOTE
fi

echo "==> 远端健康检查"
ssh -p "$PORT" "$TARGET" 'for i in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8080/api/health >/dev/null 2>&1; then
    echo "OK: $(curl -s http://127.0.0.1:8080/api/health)"; exit 0
  fi; sleep 2; done; echo "健康检查超时"; exit 1'

echo "==> 完成。对外地址取决于 Nginx / 端口映射，见 deploy/README.md"
