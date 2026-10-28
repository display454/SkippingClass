"""版本号单一来源。

两个应用同属一个仓库、同一次发布，版本号必须一致——
前端页脚、/api/health、打包脚本都读这里，避免出现 1.0.0 与 2.0.0 并存。
发版时只改这一处，并同步 CHANGELOG.md。
"""
from __future__ import annotations

__version__ = "1.1.0-rc.1"
