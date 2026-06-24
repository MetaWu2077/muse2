@echo off
REM ============================================================================
REM Muse Cloud Server — 一键部署到云服务器
REM 用法：deploy.bat [密码]
REM ============================================================================

set SERVER=118.24.80.184
set REMOTE_DIR=/root/muse-cloud-server

if "%1"=="" (
    echo 用法: deploy.bat 云服务器密码
    echo 示例: deploy.bat mypassword
    exit /b 1
)

echo.
echo ============================================
echo  部署 Muse Cloud Server 到 %SERVER%
echo ============================================
echo.

REM 1. 上传代码
echo [1/3] 上传代码...
pscp -pw %1 -r ^
    server.py config.py requirements.txt dashboard.html init_db.sql ^
    session_manager.py session_context.py ^
    storage ^
    root@%SERVER%:%REMOTE_DIR%/

if errorlevel 1 (
    echo 上传失败！
    exit /b 1
)
echo OK

REM 2. 上传 .env
echo [2/3] 上传 .env 配置...
pscp -pw %1 .env root@%SERVER%:%REMOTE_DIR%/
echo OK

REM 3. 远程安装并启动
echo [3/3] 远程安装依赖并启动...
plink -pw %1 root@%SERVER% ^
    "cd %REMOTE_DIR% && pip install -r requirements.txt -q && pkill -f 'python server.py' 2>/dev/null; nohup python server.py > /var/log/muse-server.log 2>&1 & sleep 2 && echo 'Server PID:' && pgrep -f 'python server.py'"

echo.
echo ============================================
echo  部署完成！
echo  仪表盘: http://%SERVER%:8000/dashboard
echo  健康检查: http://%SERVER%:8000/health
echo  SSH: plink root@%SERVER%
echo ============================================
echo.
