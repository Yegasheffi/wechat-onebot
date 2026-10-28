@echo off
chcp 65001 >nul
cd /d %~dp0
set PYTHONUTF8=1
echo 正在启动 wechat-onebot Windows 代理...
python bridge_agent.py --config config.json
pause
