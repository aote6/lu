#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
view_log.py -- 查看炉的操作日志
用法:
  python3 core/view_log.py            查看最近20条(所有项目)
  python3 core/view_log.py 关键词      按cwd/target包含关键词过滤
  python3 core/view_log.py 关键词 50   过滤+指定条数
"""
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_FILE = os.path.join(BASE_DIR, "logs", "operations.jsonl")


def main():
    keyword = sys.argv[1] if len(sys.argv) > 1 else None
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20

    if not os.path.exists(LOG_FILE):
        print("暂无操作日志")
        return

    with open(LOG_FILE, "r", encoding="utf-8") as f:
        lines = [json.loads(l) for l in f if l.strip()]

    if keyword:
        lines = [e for e in lines if keyword in e.get("cwd", "") or keyword in e.get("target", "")]

    lines = lines[-limit:]

    if not lines:
        print("没有匹配的记录")
        return

    for e in lines:
        mark = "✓" if e["result"] == "success" else "✗"
        print(f"{mark} [{e['ts']}] {e['action']} | {e.get('cwd', '?')} | {e['target']}")
        if e.get("detail"):
            print(f"    {e['detail']}")


if __name__ == "__main__":
    main()
