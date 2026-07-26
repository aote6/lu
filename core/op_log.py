#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
op_log.py -- 炉的操作日志

只做一件事：往 logs/operations.jsonl 追加一条记录，只增不改。
不影响写入主流程——记录失败时打印警告，不中断 patch 操作本身。
"""
import json
import os
import time
import fcntl

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "operations.jsonl")


def log_op(action, target, result, detail=None):
    """
    action: "replace" / "rollback"
    target: 被操作的文件路径
    result: "success" / "failed"
    detail: 补充信息（失败原因、规则名、快照路径等），可选
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    entry = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "cwd": os.getcwd(),
        "target": target,
        "result": result,
    }
    if detail:
        entry["detail"] = detail
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            fcntl.flock(f, fcntl.LOCK_UN)
    except Exception as e:
        print(f"警告：操作日志写入失败：{e}")
