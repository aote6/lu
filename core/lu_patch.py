#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lu_patch.py -- 炉的写入编排器

设计原则:
1. 唯一性校验等所有判断类规则,全部以独立子进程运行,
   只允许读 target/old/new,不给写权限,校验节点没有能力绕过自己去写文件。
2. 加新规则 = 在 rules/ 下新增一个文件夹(node.json + entry),
   不需要改这个文件本身。
3. 真正的写入动作,只在这一个文件里发生,且只在所有规则都通过之后发生。
4. 写入用原子写(tmp + os.replace + flock 目标文件),不用裸 open/write。
"""
import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import fcntl
import op_log

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES_DIR = os.path.join(BASE_DIR, "rules")
SNAPSHOT_DIR = os.path.join(BASE_DIR, "snapshots")


# ==================== 规则加载与执行 ====================

def load_rules():
    """扫描 rules/ 目录，按文件夹名排序（保证执行顺序稳定）。"""
    rules = []
    if not os.path.isdir(RULES_DIR):
        return rules
    for name in sorted(os.listdir(RULES_DIR)):
        rule_dir = os.path.join(RULES_DIR, name)
        node_file = os.path.join(rule_dir, "node.json")
        if not os.path.isdir(rule_dir) or not os.path.exists(node_file):
            continue
        with open(node_file, "r", encoding="utf-8") as f:
            meta = json.load(f)
        meta["_dir"] = rule_dir
        rules.append(meta)
    return rules


def run_rules(target, old_file, new_file):
    """
    依次运行所有规则节点（各自独立子进程）。
    任意一条不通过就立即停止，返回失败的规则名和输出。
    全部通过返回 True。
    """
    for rule in load_rules():
        entry = os.path.join(rule["_dir"], rule.get("entry", "check.py"))
        if not os.path.exists(entry):
            print(f"警告：规则 {rule.get('name')} 的入口文件不存在，跳过")
            continue
        result = subprocess.run(
            ["python3", entry, "--target", target, "--old", old_file, "--new", new_file],
            capture_output=True, text=True
        )
        print(f"[规则] {rule.get('name', rule['_dir'])}")
        if result.stdout.strip():
            print(result.stdout.strip())
        if result.returncode != 0:
            if result.stderr.strip():
                print(result.stderr.strip())
            return False, rule.get("name", rule["_dir"]), result.stdout
    return True, None, None


# ==================== 原子写入 / 快照 / 锁 ====================

def snapshot(path):
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
    name = os.path.basename(path)
    dest = os.path.join(SNAPSHOT_DIR, f"{name}.{ts}.bak")
    shutil.copy2(path, dest)
    return dest


def find_latest_snapshot(path):
    name = os.path.basename(path)
    if not os.path.isdir(SNAPSHOT_DIR):
        return None
    candidates = [f for f in os.listdir(SNAPSHOT_DIR) if f.startswith(name + ".")]
    if not candidates:
        return None
    candidates.sort()
    return os.path.join(SNAPSHOT_DIR, candidates[-1])


def unlock_if_needed(path):
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if not (mode & stat.S_IWUSR):
        os.chmod(path, 0o644)
        return mode
    return None


def relock(path, original_mode):
    if original_mode is not None:
        os.chmod(path, original_mode)


def atomic_write(path, new_content):
    """
    原子写入：tmp 文件 + os.replace，flock 锁目标文件本身（不是锁 tmp）。
    移植自 zhongqu 内核 v2.4 的修复经验。
    """
    tmp = path + ".tmp." + str(os.getpid())
    try:
        with open(path, "a", encoding="utf-8") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(new_content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        return True
    except Exception as e:
        print(f"原子写入失败：{e}")
        if os.path.exists(tmp):
            os.remove(tmp)
        return False


# ==================== 对外命令 ====================

def do_replace(path, old_file, new_file):
    if not os.path.exists(path):
        print(f"错误：目标文件不存在 {path}")
        sys.exit(1)

    ok, failed_rule, _ = run_rules(path, old_file, new_file)
    if not ok:
        print(f"拒绝写入：规则「{failed_rule}」未通过")
        op_log.log_op("replace", path, "failed", detail=f"规则未通过：{failed_rule}")
        sys.exit(2)

    with open(old_file, "r", encoding="utf-8") as f:
        old_text = f.read()
    with open(new_file, "r", encoding="utf-8") as f:
        new_text = f.read()
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    snap_path = snapshot(path)
    original_mode = unlock_if_needed(path)
    try:
        new_content = content.replace(old_text, new_text)
        success = atomic_write(path, new_content)
    finally:
        relock(path, original_mode)

    if success:
        print(f"写入成功。快照: {snap_path}")
        op_log.log_op("replace", path, "success", detail=f"快照：{snap_path}")
    else:
        op_log.log_op("replace", path, "failed", detail="原子写入失败")
        sys.exit(1)


def do_rollback(path):
    snap = find_latest_snapshot(path)
    if not snap:
        print(f"错误：找不到 {path} 的快照")
        op_log.log_op("rollback", path, "failed", detail="找不到快照")
        sys.exit(1)
    original_mode = unlock_if_needed(path)
    try:
        with open(snap, "r", encoding="utf-8") as f:
            snap_content = f.read()
        atomic_write(path, snap_content)
    finally:
        relock(path, original_mode)
    print(f"已回滚: {path} <- {snap}")
    op_log.log_op("rollback", path, "success", detail=f"来自快照：{snap}")


def do_lock(path):
    os.chmod(path, 0o444)
    print(f"已锁定（只读）: {path}")


def do_unlock(path):
    os.chmod(path, 0o644)
    print(f"已解锁（可写，仅供调试用，正常操作不要用）: {path}")


def do_list_rules():
    rules = load_rules()
    if not rules:
        print("当前没有任何规则节点")
        return
    for r in rules:
        print(f"- {r.get('name')}：{r.get('desc', '')}")
        if r.get("对应真实坑"):
            print(f"  对应真实坑：{r['对应真实坑']}")


def main():
    parser = argparse.ArgumentParser(description="lu_patch - 炉的写入编排器")
    parser.add_argument("target", nargs="?")
    parser.add_argument("--old-file")
    parser.add_argument("--new-file")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--lock", action="store_true")
    parser.add_argument("--unlock", action="store_true")
    parser.add_argument("--list-rules", action="store_true")
    args = parser.parse_args()

    if args.list_rules:
        do_list_rules()
    elif args.rollback:
        do_rollback(args.target)
    elif args.lock:
        do_lock(args.target)
    elif args.unlock:
        do_unlock(args.target)
    elif args.old_file and args.new_file:
        do_replace(args.target, args.old_file, args.new_file)
    else:
        print("错误：需要 --old-file/--new-file，或 --rollback，或 --lock/--unlock，或 --list-rules")
        sys.exit(1)


if __name__ == "__main__":
    main()
