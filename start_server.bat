@echo off
chcp 65001 >nul
title 图像监控服务端
cd /d "%~dp0"
echo ========================================
echo   图像监控服务端启动中...
echo ========================================
python server.py
pause
