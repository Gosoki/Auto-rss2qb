#!/usr/bin/env bash
# autorss 一键部署（Debian/Ubuntu LXC 或裸机，root 运行）。
#
#   bash deploy.sh
#
# 做四件事：装 uv（自带独立 Python，不依赖系统 python）→ 建 .venv 装依赖 →
# 写 .env / 关热重载 → 生成 systemd 服务并启动。
#
# 幂等：任何一步失败会立刻停下并报错，修完重跑即可，不会留半成品。
# 路径全部由脚本自身位置推导，不写死，所以仓库克隆到哪都行。
set -euo pipefail

SVC=autorss
PYVER=3.12

# ---- 1. 定位项目（脚本就在项目根目录，自我定位，无需 cd 或传参）----
APP="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$APP/main.py" ]          || { echo "❌ $APP 下没有 main.py —— deploy.sh 必须放在项目根目录"; exit 1; }
[ -f "$APP/requirements.txt" ] || { echo "❌ $APP 下没有 requirements.txt"; exit 1; }
[ "$(id -u)" -eq 0 ]           || { echo "❌ 需要 root（要写 /etc/systemd/system 并安装依赖）"; exit 1; }
echo "✔ 项目目录: $APP"

# ---- 2. 确保 uv 可用（始终显式加 PATH，不依赖登录 shell 的 source）----
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  echo "→ 安装 uv …"
  apt-get update -qq
  apt-get install -y -qq curl ca-certificates
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
command -v uv >/dev/null 2>&1 || { echo "❌ uv 安装失败，检查容器能否联网"; exit 1; }
echo "✔ uv $(uv --version)"

# ---- 3. 虚拟环境 + 依赖（uv 下载独立 CPython，与系统 python 版本无关）----
# 项目用了 PEP 604 的 `str | None`，需要 Python ≥ 3.10；Debian 11 自带 3.9 跑不了，故这里固定装 3.12。
cd "$APP"
uv python install "$PYVER"
uv venv --python "$PYVER"
uv pip install -r requirements.txt

PY="$APP/.venv/bin/python"
[ -x "$PY" ] || { echo "❌ 虚拟环境没建出来：$PY"; exit 1; }
"$PY" -c "import nicegui, sqlmodel, httpx, feedparser" || { echo "❌ 依赖没装全"; exit 1; }
echo "✔ 依赖 OK（$("$PY" -V)）"

# ---- 4. 配置 ----
# WEB_HOST 默认 127.0.0.1（见 config.py），容器里不改成 0.0.0.0 就只有容器自己能访问。
# main.py 里 reload=True 是开发用热重载（会多起 watchfiles 进程），做服务要关掉。
[ -f .env ] || cp .env.example .env
grep -q '^WEB_HOST=' .env || echo "WEB_HOST=0.0.0.0" >> .env
sed -i 's/reload=True/reload=False/' main.py
echo "✔ $(grep '^WEB_HOST=' .env) / $(grep -o 'reload=[A-Za-z]*' main.py)"

# ---- 5. systemd（路径用上面检测到的，杜绝写错）----
cat > "/etc/systemd/system/${SVC}.service" <<EOF
[Unit]
Description=autorss
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$APP
ExecStart=$PY main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SVC" >/dev/null 2>&1 || true
systemctl restart "$SVC"
sleep 3

if systemctl is-active --quiet "$SVC"; then
  PORT="$(sed -n 's/^WEB_PORT=\([0-9]*\).*/\1/p' .env | head -1)"; PORT="${PORT:-8080}"
  IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
  echo
  echo "✅ 部署完成 → http://${IP:-<容器IP>}:${PORT}"
  echo "   ⚠ 本工具无鉴权、设置页存有 qB 密码：进设置页把 WEB_ALLOW_CIDRS"
  echo "     填成你的网段（如 192.168.1.0/24）收窄访问，回环地址恒放行、锁不死自己。"
  echo
  echo "   日志: journalctl -u ${SVC} -f     重启: systemctl restart ${SVC}"
  echo "   升级: git pull && bash deploy.sh"
else
  echo "❌ 启动失败，最近日志："
  journalctl -u "$SVC" -n 30 --no-pager
  exit 1
fi
