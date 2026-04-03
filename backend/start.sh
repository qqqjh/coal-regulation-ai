#!/bin/bash

# 煤炭规程智能体后端启动脚本

echo "==================================="
echo "  煤炭规程智能体后端服务"
echo "==================================="
echo ""

# 检查Python版本
echo "检查Python版本..."
python3 --version

if [ $? -ne 0 ]; then
    echo "错误: 未找到Python3，请先安装Python 3.10+"
    exit 1
fi

# 进入后端目录
cd "$(dirname "$0")"

# 检查虚拟环境
if [ ! -d "venv" ]; then
    echo "创建虚拟环境..."
    python3 -m venv venv
fi

# 激活虚拟环境
echo "激活虚拟环境..."
source venv/bin/activate

# 安装依赖
echo "安装依赖..."
pip install -r requirements.txt

# 检查.env文件
if [ ! -f ".env" ]; then
    echo "警告: 未找到.env文件，将使用默认配置"
    echo "请创建.env文件并配置OPENAI_API_KEY"
fi

# 启动服务
echo ""
echo "启动后端服务..."
echo "API文档: http://localhost:8000/api/docs"
echo "按 Ctrl+C 停止服务"
echo ""

python main.py
