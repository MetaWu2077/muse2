#!/bin/bash
# ============================================================================
# Muse Cloud Server — 一键部署到云服务器
# 用法：bash deploy.sh
# ============================================================================

SERVER="118.24.80.184"
REMOTE_USER="ubuntu"
REMOTE_DIR="/home/ubuntu/muse-cloud-server"

echo ""
echo "============================================"
echo " 部署 Muse Cloud Server 到 $SERVER"
echo "============================================"
echo ""

# 1. 创建远程目录
echo "[1/4] 创建远程目录..."
ssh $REMOTE_USER@$SERVER "mkdir -p $REMOTE_DIR/muse_sessions $REMOTE_DIR/storage"
echo "OK"

# 2. 上传代码
echo "[2/4] 上传代码..."
scp server.py config.py requirements.txt dashboard.html init_db.sql \
    session_manager.py session_context.py \
    $REMOTE_USER@$SERVER:$REMOTE_DIR/
scp storage/*.py $REMOTE_USER@$SERVER:$REMOTE_DIR/storage/
echo "OK"

# 3. 上传 .env
echo "[3/4] 上传 .env..."
scp .env $REMOTE_USER@$SERVER:$REMOTE_DIR/
echo "OK"

# 4. 远程安装依赖并注册 systemd 服务
echo "[4/4] 远程安装并注册服务..."
ssh $REMOTE_USER@$SERVER "cd $REMOTE_DIR && pip3 install -r requirements.txt -q 2>&1 | tail -1"

# 写入 systemd service 文件
ssh $REMOTE_USER@$SERVER "sudo tee /etc/systemd/system/muse-server.service << 'EOF'
[Unit]
Description=Muse Cloud Server
After=network.target

[Service]
Type=simple
WorkingDirectory=$REMOTE_DIR
ExecStart=/usr/bin/python3 server.py
Restart=always
RestartSec=5
StandardOutput=append:/var/log/muse-server.log
StandardError=append:/var/log/muse-server.log

[Install]
WantedBy=multi-user.target
EOF"

# 启用并启动服务
ssh $REMOTE_USER@$SERVER "sudo systemctl daemon-reload && \
    sudo systemctl enable muse-server && \
    sudo systemctl restart muse-server && \
    sleep 3 && \
    echo '--- 服务状态 ---' && \
    sudo systemctl status muse-server --no-pager -l | head -20 && \
    echo '--- 健康检查 ---' && \
    curl -s http://localhost:8000/health && \
    echo ''"

echo ""
echo "============================================"
echo " 部署完成！（systemd 守护进程，开机自启）"
echo " 仪表盘:    http://$SERVER:8000/dashboard"
echo " 健康检查:  http://$SERVER:8000/health"
echo " WebSocket: ws://$SERVER:8000/ws/session"
echo ""
echo " 常用管理命令（在服务器上执行）："
echo "   查看状态:  systemctl status muse-server"
echo "   查看日志:  tail -f /var/log/muse-server.log"
echo "   重启服务:  systemctl restart muse-server"
echo "   停止服务:  systemctl stop muse-server"
echo "============================================"
echo ""
