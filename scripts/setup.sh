#!/bin/bash
# scripts/setup.sh — 一次性服务器初始化脚本
# 在 Ubuntu 22.04 云服务器上以 root 运行
set -e

# 1. 创建目录结构
mkdir -p /var/lark-bot/config/.lark-cli
mkdir -p /var/lark-bot/users
chmod 700 /var/lark-bot/users

# 2. 写入 bot 共享 app 配置（替换实际值）
# 配置文件路径：lark-cli 默认读取 $HOME/.lark-cli/config.json
cat > /var/lark-bot/config/.lark-cli/config.json << 'EOF'
{
  "app_id": "${FEISHU_APP_ID}",
  "app_secret": "${FEISHU_APP_SECRET}"
}
EOF
echo "注意：请手动将 config.json 中的占位符替换为实际值"

# 3. 创建 Python 虚拟环境并安装依赖
cd /opt/lark-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 4. 复制 .env 文件
if [ ! -f .env ]; then
  cp .env.example .env
  echo "已创建 .env，请填写实际值后再启动服务"
fi

# 5. 创建 systemd 服务
cat > /etc/systemd/system/lark-bot.service << 'EOF'
[Unit]
Description=Feishu AI Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/lark-bot
ExecStart=/opt/lark-bot/venv/bin/python -m src.main
EnvironmentFile=/opt/lark-bot/.env
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
echo "Setup complete. Edit /opt/lark-bot/.env, then run: systemctl enable --now lark-bot"
