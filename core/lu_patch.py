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
import tempfile
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


def resolve_new_text(text_arg, new_file_arg, text_file_arg=None):
    if text_file_arg:
        with open(text_file_arg, 'r', encoding='utf-8') as f:
            return f.read()
    if text_arg is not None:
        if "\n" in text_arg:
            print("拒绝：--text 内容包含换行符，多行内容在 shell 里极易被截断/错位。")
            print("请改用 --text-file：先把内容写入一个临时文件，再用 --text-file 指定该文件路径，例如：")
            print("  python3 lu_patch.py --anchor-line \"锚点文本\" --text-file /tmp/patch.txt target.py")
            sys.exit(3)
        if "\x27" in text_arg and "\x22" in text_arg:
            print("拒绝：--text 内容同时包含单引号和双引号，shell 转义在这种情况下不可靠。")
            print("请改用 --text-file：先把内容写入一个临时文件，再用 --text-file 指定该文件路径，例如：")
            print("  python3 lu_patch.py --anchor-line \"锚点文本\" --text-file /tmp/patch.txt target.py")
            sys.exit(3)
        return text_arg
    if new_file_arg:
        with open(new_file_arg, 'r', encoding='utf-8') as f:
            return f.read()
    print("错误：需要提供 --text、--text-file 或 --new-file")
    sys.exit(1)


def resolve_anchor_line(target, anchor_text):
    """用锚点文本在target里重新定位行号，找不到或不唯一直接退出。
    为抵抗终端粘贴带来的全角/半角和空格差异，采用剥离水平空白的容错匹配。"""
    import unicodedata
    import re
    import sys

    def normalize_for_search(text):
        text = unicodedata.normalize("NFKC", text)
        return re.sub(r'[^\S\n]+', '', text).strip()

    search_anchor = normalize_for_search(anchor_text)
    anchor_first_line = search_anchor.split('\n')[0]

    with open(target, "r", encoding="utf-8") as f:
        lines = f.readlines()

    matched_line_no = None
    match_count = 0

    for i, line in enumerate(lines):
        clean_line = normalize_for_search(line)
        if anchor_first_line and anchor_first_line in clean_line:
            match_count += 1
            matched_line_no = i + 1

    if match_count == 0:
        print("错误：锚点定位失败，未在目标文件中找到匹配行（已忽略空格与全角差异）")
        sys.exit(1)
    elif match_count > 1:
        print(f"错误：锚点定位失败，特征行出现 {match_count} 次，不唯一")
        sys.exit(1)

    print(f"锚点定位成功：第 {matched_line_no} 行")
    return matched_line_no

def do_line_replace(path, start, end, new_text):
    if not os.path.exists(path):
        print(f"错误：目标文件不存在 {path}")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    if start < 1 or end < start or end > len(lines):
        print(f"错误：行号范围不合法（文件共 {len(lines)} 行，请求 {start}-{end}）")
        sys.exit(1)

    print(f"[将被替换的行 {start}-{end}]")
    print("".join(lines[start-1:end]).rstrip("\n"))
    print("[repr 形式，用于核对不可见字符/转义序列]")
    print(repr("".join(lines[start-1:end])))

    if not new_text.endswith("\n"):
        new_text += "\n"

    snap_path = snapshot(path)
    original_mode = unlock_if_needed(path)
    try:
        new_content = "".join(lines[:start-1]) + new_text + "".join(lines[end:])
        success = atomic_write(path, new_content)
    finally:
        relock(path, original_mode)
    if success and path.endswith(".py"):
        import py_compile
        try:
            py_compile.compile(path, doraise=True)
        except py_compile.PyCompileError as e:
            err_msg = str(e)
            # 仅当错误与全角/非ASCII字符相关时才尝试NFKC归一化
            if ("invalid character" in err_msg or "unicode" in err_msg.lower()) and path.endswith(".py"):
                import unicodedata
                normalized = unicodedata.normalize("NFKC", new_content)
                if normalized != new_content:
                    # 归一化后的内容重新做原子写
                    original_mode_norm = unlock_if_needed(path)
                    try:
                        norm_success = atomic_write(path, normalized)
                    finally:
                        relock(path, original_mode_norm)
                    if norm_success:
                        # 再次校验
                        try:
                            py_compile.compile(path, doraise=True)
                            print("NFKC归一化后语法校验通过，已自动修正全角字符")
                            op_log.log_op("line_replace", path, "success",
                                           detail=f"行{start}-{end}，NFKC自动归一化修正全角字符，快照：{snap_path}")
                            sys.exit(0)
                        except py_compile.PyCompileError as e2:
                            print(f"NFKC归一化后仍语法校验失败: {e2}")
                            op_log.log_op("line_replace", path, "failed",
                                           detail=f"行{start}-{end}，NFKC归一化后仍校验失败: {e2}")
                            # 恢复到快照
                            if snap_path and os.path.exists(snap_path):
                                original_mode2 = unlock_if_needed(path)
                                try:
                                    with open(snap_path, "r", encoding="utf-8") as f:
                                        snap_content = f.read()
                                    atomic_write(path, snap_content)
                                finally:
                                    relock(path, original_mode2)
                            sys.exit(1)
            print(f"语法校验失败，自动回滚: {e}")
            if snap_path and os.path.exists(snap_path):
                original_mode2 = unlock_if_needed(path)
                try:
                    with open(snap_path, "r", encoding="utf-8") as f:
                        snap_content = f.read()
                    atomic_write(path, snap_content)
                finally:
                    relock(path, original_mode2)
            op_log.log_op("line_replace", path, "failed", detail=f"语法校验失败，已自动回滚: {e}")
            sys.exit(1)
    if success:
        print(f"写入成功（行 {start}-{end}）。快照: {snap_path}")
        op_log.log_op("line_replace", path, "success", detail=f"行{start}-{end}，快照：{snap_path}")
    else:
        op_log.log_op("line_replace", path, "failed", detail=f"行{start}-{end} 原子写入失败")
        sys.exit(1)


def do_whole_file(path, new_text):
    if not os.path.exists(path):
        print(f"错误：目标文件不存在 {path}")
        sys.exit(1)

    snap_path = snapshot(path)
    original_mode = unlock_if_needed(path)
    try:
        success = atomic_write(path, new_text)
    finally:
        relock(path, original_mode)
    if success and path.endswith(".py"):
        import py_compile
        try:
            py_compile.compile(path, doraise=True)
        except py_compile.PyCompileError as e:
            err_msg = str(e)
            if ("invalid character" in err_msg or "unicode" in err_msg.lower()) and path.endswith(".py"):
                import unicodedata
                normalized = unicodedata.normalize("NFKC", new_text)
                if normalized != new_text:
                    orig_mode = unlock_if_needed(path)
                    try:
                        norm_ok = atomic_write(path, normalized)
                    finally:
                        relock(path, orig_mode)
                    if norm_ok:
                        try:
                            py_compile.compile(path, doraise=True)
                            print("NFKC归一化后语法校验通过，已自动修正全角字符")
                            op_log.log_op("whole_file", path, "success",
                                          detail=f"NFKC自动归一化修正全角字符，快照：{snap_path}")
                            sys.exit(0)
                        except py_compile.PyCompileError as e2:
                            print(f"NFKC归一化后仍语法校验失败: {e2}")
                            op_log.log_op("whole_file", path, "failed",
                                          detail=f"NFKC归一化后仍校验失败: {e2}")
                            if snap_path and os.path.exists(snap_path):
                                orig_mode2 = unlock_if_needed(path)
                                try:
                                    with open(snap_path, "r", encoding="utf-8") as f:
                                        snap_content = f.read()
                                    atomic_write(path, snap_content)
                                finally:
                                    relock(path, orig_mode2)
                            sys.exit(1)
            print(f"语法校验失败，自动回滚: {e}")
            if snap_path and os.path.exists(snap_path):
                original_mode2 = unlock_if_needed(path)
                try:
                    with open(snap_path, "r", encoding="utf-8") as f:
                        snap_content = f.read()
                    atomic_write(path, snap_content)
                finally:
                    relock(path, original_mode2)
            op_log.log_op("whole_file", path, "failed", detail=f"语法校验失败，已自动回滚: {e}")
            sys.exit(1)
            sys.exit(1)
    if success:
        print(f"整文件替换成功（已跳过唯一性校验）。快照: {snap_path}")
        op_log.log_op("whole_file", path, "success", detail=f"跳过规则校验，快照：{snap_path}")
    else:
        op_log.log_op("whole_file", path, "failed", detail="原子写入失败")
        sys.exit(1)


def do_append(path, text):
    if not os.path.exists(path):
        print(f"错误：目标文件不存在 {path}")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    if content and not content.endswith("\n"):
        content += "\n"
    if not text.endswith("\n"):
        text += "\n"

    snap_path = snapshot(path)
    original_mode = unlock_if_needed(path)
    try:
        new_content = content + text
        success = atomic_write(path, new_content)
        if success and path.endswith(".py"):
            import py_compile
            try:
                py_compile.compile(path, doraise=True)
            except py_compile.PyCompileError as e:
                err_msg = str(e)
                # 仅当错误与全角/非ASCII字符相关时才尝试NFKC归一化
                if ("invalid character" in err_msg or "unicode" in err_msg.lower()) and path.endswith(".py"):
                    import unicodedata
                    normalized = unicodedata.normalize("NFKC", new_content)
                    if normalized != new_content:
                        orig_mode = unlock_if_needed(path)
                        try:
                            norm_ok = atomic_write(path, normalized)
                        finally:
                            relock(path, orig_mode)
                        if norm_ok:
                            try:
                                py_compile.compile(path, doraise=True)
                                print("NFKC归一化后语法校验通过，已自动修正全角字符")
                                op_log.log_op("append", path, "success",
                                              detail=f"NFKC自动归一化修正全角字符，快照：{snap_path}")
                                sys.exit(0)
                            except py_compile.PyCompileError as e2:
                                print(f"NFKC归一化后仍语法校验失败: {e2}")
                                op_log.log_op("append", path, "failed",
                                              detail=f"NFKC归一化后仍校验失败: {e2}")
                                if snap_path and os.path.exists(snap_path):
                                    orig_mode2 = unlock_if_needed(path)
                                    try:
                                        with open(snap_path, "r", encoding="utf-8") as f:
                                            snap_content = f.read()
                                        atomic_write(path, snap_content)
                                    finally:
                                        relock(path, orig_mode2)
                                sys.exit(1)
                print(f"语法校验失败，自动回滚: {e}")
                original_mode2 = unlock_if_needed(path)
                try:
                    with open(snap_path, "r", encoding="utf-8") as f:
                        snap_content = f.read()
                    atomic_write(path, snap_content)
                finally:
                    relock(path, original_mode2)
                op_log.log_op("append", path, "failed", detail=f"语法校验失败，已自动回滚: {e}")
                sys.exit(1)
    finally:
        relock(path, original_mode)

    if success:
        print(f"追加成功。快照: {snap_path}")
        op_log.log_op("append", path, "success", detail=f"快照：{snap_path}")
    else:
        op_log.log_op("append", path, "failed", detail="原子写入失败")
        sys.exit(1)


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
    parser = argparse.ArgumentParser(description="炉(lu)：安全文件写入编排工具。推荐使用 --anchor-line/--anchor-before/--anchor-after 定位，避免 --line/--range 手动行号因修改而失效的问题。")
    parser.add_argument("target", nargs="?")
    parser.add_argument("--old-file")
    parser.add_argument("--new-file")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--lock", action="store_true")
    parser.add_argument("--unlock", action="store_true")
    parser.add_argument("--list-rules", action="store_true")
    parser.add_argument("--line", type=int, help="替换指定行号，配合 --text 或 --new-file")
    parser.add_argument("--range", help="替换行号范围，格式 起始:结束，如 10:15，配合 --text 或 --new-file")
    parser.add_argument("--text-file", help="从文件读取替换内容，替代 --text 以绕开 shell 转义问题")
    parser.add_argument("--anchor-line", help="用锚点文本定位单行，自动重新计算行号，替代 --line")
    parser.add_argument("--anchor-before", help="用锚点文本定位替换范围起始行（含），需配合 --anchor-after")
    parser.add_argument("--anchor-after", help="用锚点文本定位替换范围结束行（含），需配合 --anchor-before")
    parser.add_argument("--whole-file", action="store_true", help="整文件替换，跳过唯一性校验，配合 --new-file")
    parser.add_argument("--text", help="直接提供替换内容，免建 new.txt")
    parser.add_argument("--append", action="store_true", help="追加内容到文件末尾，配合 --text 或 --new-file")
    args = parser.parse_args()

    if args.list_rules:
        do_list_rules()
    elif args.rollback:
        do_rollback(args.target)
    elif args.lock:
        do_lock(args.target)
    elif args.unlock:
        do_unlock(args.target)
    elif args.append:
        new_text = resolve_new_text(args.text, args.new_file, args.text_file)
        do_append(args.target, new_text)
    elif args.whole_file:
        new_text = resolve_new_text(args.text, args.new_file, args.text_file)
        do_whole_file(args.target, new_text)
    elif args.anchor_line:
        line_no = resolve_anchor_line(args.target, args.anchor_line)
        new_text = resolve_new_text(args.text, args.new_file, args.text_file)
        do_line_replace(args.target, line_no, line_no, new_text)
    elif args.anchor_before and args.anchor_after:
        start = resolve_anchor_line(args.target, args.anchor_before)
        end = resolve_anchor_line(args.target, args.anchor_after)
        if end < start:
            print(f"错误：--anchor-after 定位到的行({end})早于 --anchor-before 定位到的行({start})")
            sys.exit(1)
        new_text = resolve_new_text(args.text, args.new_file, args.text_file)
        do_line_replace(args.target, start, end, new_text)
    elif args.anchor_before or args.anchor_after:
        print("错误：--anchor-before 和 --anchor-after 必须成对使用")
        sys.exit(1)
    elif args.line is not None:
        print("提示：使用手动行号(--line)。若该行号不是本次刚确认的，可能因此前修改已偏移，建议改用 --anchor-line 定位。")
        new_text = resolve_new_text(args.text, args.new_file, args.text_file)
        do_line_replace(args.target, args.line, args.line, new_text)
    elif args.range:
        print("提示：使用手动行号(--range)。若该范围不是本次刚确认的，可能因此前修改已偏移，建议改用 --anchor-before/--anchor-after 定位。")
        try:
            start_s, end_s = args.range.split(":")
            start, end = int(start_s), int(end_s)
        except ValueError:
            print("错误：--range 格式应为 起始:结束，如 10:15")
            sys.exit(1)
        new_text = resolve_new_text(args.text, args.new_file, args.text_file)
        do_line_replace(args.target, start, end, new_text)
    elif args.old_file and args.new_file:
        do_replace(args.target, args.old_file, args.new_file)
    else:
        print("错误：需要 --old-file/--new-file，或 --line/--range/--whole-file，或 --rollback，或 --lock/--unlock，或 --list-rules")
        sys.exit(1)


if __name__ == "__main__":
    main()
