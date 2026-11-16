#!/bin/bash
cd "$(dirname "$0")" || exit 1

if [ ! -x .venv/bin/python ]; then
  echo "未找到虚拟环境 webui/.venv，请先按 README.md 完成安装。"
  read -r -p "按回车键关闭..."
  exit 1
fi

./.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8820
