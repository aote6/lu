#!/usr/bin/env python3
"""
炉 (Lu) — lu_patch.py
核心强制规则第一版：唯一性锚点校验 + 自动可回滚快照
"""

import sys
import argparse
import pathlib
import datetime


def find_snapshot_dir(target: pathlib.Path) -> pathlib.Path:
    lu_home = pathlib.Path.home() / "lu"
    snap_dir = lu_home / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    return snap_dir


def make_snapshot(target: pathlib.Path, original_text: str) -> pathlib.Path:
    snap_dir = find_snapshot_dir(target)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_name = target.name.replace("/", "_")
    snap_path = snap_dir / f"{safe_name}.{ts}.bak"
    snap_path.write_text(original_text, encoding="utf-8")
    return snap_path


def find_latest_snapshot(target: pathlib.Path):
    snap_dir = find_snapshot_dir(target)
    safe_name = target.name.replace("/", "_")
    candidates = sorted(snap_dir.glob(f"{safe_name}.*.bak"))
    if not candidates:
        return None
    return candidates[-1]


def do_rollback(target: pathlib.Path) -> int:
    if not target.exists():
        print(f"[参数错误] 目标文件不存在，无法确定要回滚哪个文件: {target}")
        return 3
    latest = find_latest_snapshot(target)
    if latest is None:
        print(f"[未找到] 没有找到 {target.name} 对应的任何快照，无法回滚。")
        return 1
    backup_text = latest.read_text(encoding="utf-8")
    target.write_text(backup_text, encoding="utf-8")
    print(f"[成功] 已用快照恢复 {target}")
    print(f"[来源] 恢复自 {latest}")
    return 0


def show_context(text: str, needle: str, max_hits: int = 5) -> None:
    count = text.count(needle)
    if count == 0:
        print("[未找到] 目标文件中不存在这段原文，请确认是否逐字复制、有无多余空格或换行差异。")
        return
    print(f"[不唯一] 目标文件中这段原文共出现 {count} 次，以下是各处位置附近的上下文：")
    start = 0
    hit_no = 0
    while True:
        idx = text.find(needle, start)
        if idx == -1 or hit_no >= max_hits:
            break
        hit_no += 1
        ctx_start = max(0, idx - 40)
        ctx_end = min(len(text), idx + len(needle) + 40)
        snippet = text[ctx_start:ctx_end].replace("\n", "\\n")
        print(f"  第{hit_no}处（字符位置{idx}附近）: ...{snippet}...")
        start = idx + 1
    if count > max_hits:
        print(f"  （还有 {count - max_hits} 处未列出）")
    print("请调整 --old-file 的内容，加入更多上下文使其唯一匹配，或改用别的锚点。")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="炉 — 唯一性锚点校验 + 自动快照的安全文件替换工具"
    )
    parser.add_argument("target", help="要修改的目标文件路径")
    parser.add_argument("--rollback", action="store_true", help="回滚模式")
    parser.add_argument("--old-file", help="包含原文的纯文本文件路径")
    parser.add_argument("--new-file", help="包含替换后新文本的纯文本文件路径")
    parser.add_argument("--count", type=int, default=1, help="期望原文出现的次数，默认1")
    args = parser.parse_args()

    target = pathlib.Path(args.target)

    if args.rollback:
        return do_rollback(target)

    if not args.old_file or not args.new_file:
        print("[参数错误] 写入模式必须同时提供 --old-file 和 --new-file，或改用 --rollback。")
        return 3

    old_file = pathlib.Path(args.old_file)
    new_file = pathlib.Path(args.new_file)

    for p, label in [(target, "目标文件"), (old_file, "--old-file"), (new_file, "--new-file")]:
        if not p.exists():
            print(f"[参数错误] {label} 不存在: {p}")
            return 3

    original_text = target.read_text(encoding="utf-8")
    old_text = old_file.read_text(encoding="utf-8")
    new_text = new_file.read_text(encoding="utf-8")

    occurrences = original_text.count(old_text)

    if occurrences != args.count:
        show_context(original_text, old_text)
        return 1 if occurrences == 0 else 2

    snap_path = make_snapshot(target, original_text)
    updated_text = original_text.replace(old_text, new_text, args.count)
    target.write_text(updated_text, encoding="utf-8")

    print(f"[成功] 已写入 {target}")
    print(f"[快照] 原文件已备份至 {snap_path}")
    print(f"如需回滚，运行：python3 core/lu_patch.py {target} --rollback")
    return 0


if __name__ == "__main__":
    sys.exit(main())
