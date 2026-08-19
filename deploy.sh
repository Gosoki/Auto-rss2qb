#!/usr/bin/env bash
# autorss 一键部署（Debian/Ubuntu LXC 或裸机，root 运行）。
#
#   bash deploy.sh
#
# 做四件事：装 uv（自带独立 Python，不依赖系统 python）→ 建 .venv 装依赖 →
# 写 .env → 生成 systemd 服务并启动。
#
# 幂等：任何一步失败会立刻停下并报错，修完重跑即可，不会留半成品；
#      重复跑（升级路径 `git pull && bash deploy.sh`）也不会在 uv venv 那一步炸掉。
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
# 【必须先判存在再建】`uv venv` 撞上已有目录会直接报错退出，而 set -e 会让整个脚本就此中止——
# README 承诺的升级路径 `git pull && bash deploy.sh` 会 100% 停在这里：新依赖没装、unit 没重写、
# 服务没重启，旧代码继续跑，而用户以为已经升级了。这正是"幂等"承诺最容易破掉的一处。
if [ ! -x "$APP/.venv/bin/python" ] || ! "$APP/.venv/bin/python" -V 2>/dev/null | grep -q "$PYVER"; then
  # 重建前先停服务：别在还在运行的解释器脚下换掉 site-packages。首次部署时服务还不存在，|| true 兜住。
  systemctl stop "$SVC" 2>/dev/null || true
  uv venv --clear --python "$PYVER"
fi
uv pip install -r requirements.txt

PY="$APP/.venv/bin/python"
[ -x "$PY" ] || { echo "❌ 虚拟环境没建出来：$PY"; exit 1; }
"$PY" -c "import nicegui, sqlmodel, httpx, feedparser" || { echo "❌ 依赖没装全"; exit 1; }
echo "✔ 依赖 OK（$("$PY" -V)）"

# ---- 4. 配置 ----
# WEB_HOST 默认 127.0.0.1（见 config.py），容器里不改成 0.0.0.0 就只有容器自己能访问。
[ -f .env ] || cp .env.example .env
grep -q '^WEB_HOST=' .env || echo "WEB_HOST=0.0.0.0" >> .env
echo "✔ $(grep '^WEB_HOST=' .env)"

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
# 本服务以 root 跑（要能往 qB 的下载目录建目录，见 core/engine.py 的跨用户目录预建）。
# 既然降不了 uid，就把 root 能干的事收窄一点。
NoNewPrivileges=yes
# 【刻意不设 UMask】曾经这里写着 UMask=0077，看着人畜无害，实际会把 engine 预建的
# 下载目录中间层变成 0700 —— 而 qB 常常是【另一个用户】（debian 包的 debian-qbittorrent、
# Docker、群晖套件），它连目录都进不去，种子恒 0%，且没有任何一处会报错。
# 敏感文件的权限改由 config.py 在启动时单独收（chmod 0700 data/），不牵连下载目录。

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SVC" >/dev/null 2>&1 || true
systemctl restart "$SVC"
sleep 3

if systemctl is-active --quiet "$SVC"; then
  # 探一下本机所在网段，只是把上面那条警告里的例子换成用户真实的网段（不自动写进配置：
  # 推错了会把用户挡在自己的面板外，这个决定留给人做）。
  # 【必须带 || true】grep 无匹配退出 1，而 set -euo pipefail 会让这条赋值直接掐死脚本——
  # 后果是服务已经起在 0.0.0.0，用户却既看不到访问地址、也收不到那条"无鉴权"的警告。
  # 无 iproute2 / 无 IPv4 scope-link 路由的机器上必然触发。
  LAN_HINT="$(ip -o -f inet route show scope link 2>/dev/null | awk '{print $1}' | grep -v '^169\.254' | head -1 || true)"
  PORT="$(sed -n 's/^WEB_PORT=\([0-9]*\).*/\1/p' .env | head -1)"; PORT="${PORT:-2333}"
  IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
  echo
  echo "✅ 部署完成 → http://${IP:-<容器IP>}:${PORT}"
  echo "   ⚠ 已绑 0.0.0.0：本工具【无鉴权】，设置页存有 qB 密码，且能让 qB 往任意目录下载、"
  echo "     能带文件删除种子。局域网内任何人访问到这个端口就等于管理员。"
  echo "     → 请立刻进『设置』页把 WEB_ALLOW_CIDRS 填成你的网段（如 ${LAN_HINT:-192.168.1.0/24}），"
  echo "       该项即时生效、回环地址恒放行，锁不死你自己。"
  echo
  echo "   日志: journalctl -u ${SVC} -f     重启: systemctl restart ${SVC}"
  echo "   升级: git pull && bash deploy.sh"
else
  echo "❌ 启动失败，最近日志："
  journalctl -u "$SVC" -n 30 --no-pager
  exit 1
fi
