# 部署手册（学校服务器）

把 MVP 部署到学校服务器，对外提供 `http://<服务器地址>:8080` 或经 Nginx 的 80 端口。

## 一、环境要求

| 项 | 要求 | 检查命令 |
|---|---|---|
| 操作系统 | Linux（Ubuntu 20.04+ / CentOS 7+） | `cat /etc/os-release` |
| Python | 3.10+（Docker 方式不需要） | `python3 -V` |
| Docker | 20.10+ 与 compose v2（推荐方式） | `docker -v && docker compose version` |
| 端口 | 8080 可绑定；若走 Nginx 则 80/443 可达 | `ss -lntp` |
| 权限 | 能 `sudo` | `sudo -v` |

## 二、方式 A：Docker（推荐）

```bash
cd /opt/jinrong/src/deploy
cp .env.example .env          # 首次：写入 MVP_SECRET
docker compose up -d --build
docker compose ps
curl -s http://127.0.0.1:8080/api/health
```

`.env` 最小内容：

```ini
MVP_SECRET=<32 字节随机串，可用 openssl rand -hex 32>
```

> `MVP_SECRET` 决定核验凭证的签名密钥。不设置会每次启动随机生成，重启后旧凭证全部失效。

## 三、方式 B：systemd + venv

```bash
sudo useradd -r -s /bin/false jinrong
sudo mkdir -p /opt/jinrong /var/lib/jinrong
sudo chown jinrong:jinrong /var/lib/jinrong

# 把 src/ 拷到 /opt/jinrong/src
cd /opt/jinrong/src
python3 -m venv ../.venv
../.venv/bin/pip install -r requirements.txt

# 写密钥（权限 600）
sudo mkdir -p /etc/jinrong
echo 'MVP_SECRET=...' | sudo tee /etc/jinrong/mvp.env
sudo chmod 600 /etc/jinrong/mvp.env

sudo cp deploy/jinrong-mvp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jinrong-mvp
journalctl -u jinrong-mvp -f
```

## 四、Nginx 反向代理（可选但推荐）

```bash
sudo cp deploy/nginx.conf /etc/nginx/conf.d/jinrong.conf
sudo nginx -t && sudo systemctl reload nginx
```

**必须保留 `proxy_buffering off`**——否则 SSE 事件会被 Nginx 缓存，前端只能一次性收到全部内容，
「正在检索制度」这类中间状态会消失。

## 五、配置项

| 变量 | 默认 | 说明 |
|---|---|---|
| `MVP_HOST` / `MVP_PORT` | `0.0.0.0` / `8080` | 监听地址 |
| `MVP_DB` | `/data/mvp.db` | SQLite 路径，Docker 下挂卷持久化 |
| `MVP_SECRET` | 随机 | **生产必须显式设置** |
| `LLM_PROVIDER` / `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | `mock` | 留空即离线 mock |
| `MVP_TOP_K` / `MVP_MIN_SCORE` | `3` / `0.35` | 检索条数与兜底阈值 |

## 六、验证部署成功

```bash
curl -s http://127.0.0.1:8080/api/health
# {"code":0,"message":"ok","data":{"status":"ready","policies":9,"model":"mock","db":"mvp.db"}}
```

浏览器打开首页，依次确认：

1. 制度问答能返回条款引用；
2. 差错处理能列出 2 个批次；
3. Agent 代办能跑到「待确认」，点确认后工单进入复核。

## 七、升级与回滚

```bash
# 升级
./deploy.sh user@host            # 重新打包上传并重启

# 回滚（Docker）
docker compose down
docker image tag jinrong/mvp:previous jinrong/mvp:latest
docker compose up -d
```

数据在 `mvp-data` 卷或 `/var/lib/jinrong`，升级不覆盖。

## 八、常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 页面能开但问答不流式 | Nginx 开了缓冲 | 关掉 `proxy_buffering` |
| 8080 端口不通 | 防火墙 / 安全组未放行 | `sudo ufw allow 8080` 或联系网络组 |
| 重启后凭证全失效 | 未设 `MVP_SECRET` | 写入 `.env` 后重启 |
| 数据库只读 | 挂载目录权限不对 | `chown` 给运行用户 |
| 服务反复重启 | 端口被占用 | `ss -lntp \| grep 8080` |

## 九、部署完成标志

- [ ] `curl /api/health` 返回 `status: ready`
- [ ] 浏览器能打开首页，三个页签可用
- [ ] 数据落在持久化目录，重启不丢
- [ ] 服务开机自启（systemd enabled / Docker restart=unless-stopped）
- [ ] 日志可查（journalctl 或 docker logs）
- [ ] 对外地址可访问（校内或公网，按学校网络策略）
