#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
规则节点：唯一性校验
职责：只判断，不写入。只允许读 target/old/new 三个文件，不给写权限。
"""
import argparse
import sys


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--target", required=True)
    p.add_argument("--old", required=True)
    p.add_argument("--new", required=True)
    args = p.parse_args()

    with open(args.old, "r", encoding="utf-8") as f:
        old_text = f.read()
    with open(args.target, "r", encoding="utf-8") as f:
        content = f.read()

    count = content.count(old_text)
    if count == 0:
        print("唯一性校验：未在目标文件中找到原文")
        sys.exit(1)
    if count > 1:
        print(f"唯一性校验：原文出现 {count} 次，不唯一")
        idx = 0
        for i in range(count):
            idx = content.find(old_text, idx)
            start = max(0, idx - 40)
            end = min(len(content), idx + len(old_text) + 40)
            print(f"--- 匹配位置 {i+1} ---")
            print(content[start:end])
            idx += 1
        sys.exit(2)

    print("唯一性校验：通过")
    idx = content.find(old_text)
    line_no = content.count("\n", 0, idx) + 1
    print(f"位置：offset={idx} line={line_no}")
    sys.exit(0)


if __name__ == "__main__":
    main()
