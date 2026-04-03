@echo off
chcp 65001 >nul
REM 煤炭规程智能体后端启动脚本 (Windows)

echo ===================================
echo   煤炭规程智能体后端服务
echo ===================================
echo.

REM 检查Python版本
echo 检查Python版本...
python --version
if errorlevel 1 (
    echo 错误: 未找到Python，请先安装Python 3.10+
    pause
    exit /b 1
)

REM 进入后端目录
cd /d "%~dp0"

REM 检查虚拟环境
if not exist "venv" (
    echo 创建虚拟环境...
    python -m venv venv
)

REM 激活虚拟环境
echo 激活虚拟环境...
call venv\Scripts\activate.bat

REM 安装依赖
echo 安装依赖...
pip install -r requirements.txt

REM 检查.env文件
if not exist ".env" (
    echo 警告: 未找到.env文件，将使用默认配置
    echo 请创建.env文件并配置OPENAI_API_KEY
)

REM 启动服务
echo.
echo 启动后端服务...
echo API文档: http://localhost:8000/api/docs
echo 按 Ctrl+C 停止服务
echo.

python main.py

pause
