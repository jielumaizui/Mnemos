#!/usr/bin/env python3

from __future__ import annotations

import os
from core.config import get_config
import sys
from datetime import datetime, timedelta

LOCK_FILE = str(get_config().data_dir / "locks" / "weekly_report.lock")
SCHEDULE_INTERVAL = 10080
SHOULD_SKIP = False

os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)

if os.path.exists(LOCK_FILE):
    with open(LOCK_FILE, 'r') as f:
        last_run = f.read().strip()
    try:
        last_time = datetime.fromisoformat(last_run)
        elapsed = datetime.now() - last_time
        # 如果超过1.5倍调度间隔，说明错过了（关机/休眠）
        if elapsed > timedelta(minutes=SCHEDULE_INTERVAL * 1.5):
            print(f"[{datetime.now().isoformat()}] Skipped: missed schedule (was off for {elapsed})")
            SHOULD_SKIP = True
    except ValueError:
        pass

if SHOULD_SKIP:
    sys.exit(0)

# 先记录启动时间，任务完成后更新为完成时间
START_TIME = datetime.now().isoformat()
with open(LOCK_FILE, 'w') as f:
    f.write(START_TIME)


import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# 使用相对路径加载脚本
import runpy
SCRIPT_PATH = Path(__file__).parent.parent / "core" / "app" / "weekly_report.py"
runpy.run_path(str(SCRIPT_PATH), run_name="__main__")

# 任务成功完成后，更新时间戳
from datetime import datetime
with open(LOCK_FILE, 'w') as f:
    f.write(datetime.now().isoformat())
