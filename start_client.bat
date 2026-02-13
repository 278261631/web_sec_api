@echo off
chcp 65001 >nul
title 图像监控客户端
cd /d "%~dp0"
echo ========================================
echo   图像监控客户端启动中...
echo ========================================
python client.py
pause
