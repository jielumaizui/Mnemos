#!/usr/bin/env python3
"""兼容性 shim：旧版 pip / 特定环境回退。

现代安装请直接使用 pyproject.toml：
    pip install -e .

本文件仅用于向后兼容，不声明任何元数据，全部从 pyproject.toml 读取。
"""
from setuptools import setup

setup()
