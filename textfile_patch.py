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
