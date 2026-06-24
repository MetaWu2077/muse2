#!/bin/bash
# ============================================================================
# Muse Cloud Server — 一键部署到云服务器
# 用法：bash deploy.sh
# ============================================================================

SERVER="118.24.80.184"
REMOTE_DIR="/root/muse-cloud-server"

echo ""
echo "============================================"
echo " 部署 Muse Cloud Server 到 $SERVER"
echo "============================================"
echo ""

# 1. 创建远程目录
echo "[1/4] 创建远程目录..."
ssh root@$SERVER "mkdir -p $REMOTE_DIR/muse_sessions $REMOTE_DIR/storage"
echo "OK"

# 2. 上传代码
echo "[2/4] 上传代码..."
scp server.py config.py requirements.txt dashboard.html init_db.sql \
    session_manager.py session_context.py \
    root@$SERVER:$REMOTE_DIR/
scp storage/*.py root@$SERVER:$REMOTE_DIR/storage/
echo "OK"

# 3. 上传 .env
echo "[3/4] 上传 .env..."
scp .env root@$SERVER:$REMOTE_DIR/
echo "OK"

# 4. 远程安装依赖并启动
echo "[4/4] 远程安装并启动..."
ssh root@$SERVER "cd $REMOTE_DIR && \
    pip install -r requirements.txt -q 2>&1 | tail -1 && \
    pkill -f 'python server.py' 2>/dev/null; sleep 1; \
    nohup python server.py > /var/log/muse-server.log 2>&1 & \
    sleep 3 && \
    echo '--- 服务状态 ---' && \
    curl -s http://localhost:8000/health && \
    echo ''"

echo ""
echo "============================================"
echo " 部署完成！"
echo " 仪表盘:    http://$SERVER:8000/dashboard"
echo " 健康检查:  http://$SERVER:8000/health"
echo " WebSocket: ws://$SERVER:8000/ws/session"
echo " 查看日志:  ssh root@$SERVER 'tail -f /var/log/muse-server.log'"
echo "============================================"
echo ""
