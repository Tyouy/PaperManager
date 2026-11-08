#!/bin/bash
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "首次启动：正在创建独立 Python 环境……"
  if ! command -v python3 >/dev/null 2>&1; then
    echo "请先安装 Python 3.9 或更新版本：https://www.python.org/downloads/"
    read -r -p "按回车退出……"
    exit 1
  fi
  python3 -m venv .venv
fi
if ! .venv/bin/python -c 'import pymupdf' >/dev/null 2>&1; then
  echo "首次启动：正在安装 PDF 读取组件，需要联网……"
  if ! .venv/bin/python -m pip install -r requirements.txt; then
    echo "安装失败，请检查网络后重新启动。"
    read -r -p "按回车退出……"
    exit 1
  fi
fi
.venv/bin/python app.py "$@"
