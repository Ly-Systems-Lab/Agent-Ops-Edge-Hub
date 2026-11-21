# -*- coding: utf-8 -*-
"""仓库内路径的唯一来源。

为什么要有这个文件（B2 引入）：搬家之前每个模块都靠 `dirname(__file__)` 往上退一层
拿到仓库根，而 `app/` 只有一层、`devices/<name>/` 有两层 —— 同一个写法搬完就会退到
`devices/`，指向并不存在的 `devices/data/pihub.db`。把"仓库根怎么算"收成一处，
以后谁也不用再数层数。

⚠️ 本文件不 import 任何项目内模块：它会被最早 import，自己绝不能有循环依赖。
"""
import os

CORE_DIR = os.path.dirname(os.path.abspath(__file__))   # <repo>/core
REPO = os.path.dirname(CORE_DIR)                        # <repo>
DATA_DIR = os.path.join(REPO, "data")
LOGS_DIR = os.path.join(REPO, "logs")
DB_PATH = os.environ.get("PIHUB_DB", os.path.join(DATA_DIR, "pihub.db"))
ENV_FILE = os.path.join(REPO, ".env")
