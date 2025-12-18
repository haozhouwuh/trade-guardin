import os

# ================= 配置区域 =================
# 输出文件名
OUTPUT_FILE = "project_flat_view.txt"

# 需要合并的文件后缀 (根据你的项目需求修改)
TARGET_EXTENSIONS = {'.py', '.json', '.yaml', '.yml', '.md', '.txt', '.ini', '.toml'}

# 需要忽略的目录
IGNORE_DIRS = {
    '.git', '__pycache__', 'venv', 'env', '.idea', '.vscode', 
    'node_modules', 'dist', 'build', 'cache', 'tests', 'htmlcov'
}
# ===========================================

def is_text_file(file_path):
    """简单的检查是否为文本文件，防止读取二进制文件报错"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            f.read(1024)
        return True
    except:
        return False

def merge_project_files():
    root_dir = os.getcwd()
    print(f"🚀 开始扫描目录: {root_dir}")
    print(f"📂 输出文件将保存为: {OUTPUT_FILE}\n")

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as outfile:
        # 写入文件头信息
        outfile.write(f"PROJECT_ROOT: {root_dir}\n")
        outfile.write(f"GENERATED_BY: merge_project.py\n")
        outfile.write("=" * 80 + "\n\n")

        file_count = 0

        # 遍历目录
        for subdir, dirs, files in os.walk(root_dir):
            # 1. 修改 dirs 列表以原地忽略目录 (关键步骤)
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]

            for file in files:
                # 2. 检查文件后缀
                ext = os.path.splitext(file)[1].lower()
                if ext in TARGET_EXTENSIONS:
                    # 排除掉脚本自己和输出文件
                    if file in ['merge_project.py', OUTPUT_FILE]:
                        continue

                    file_path = os.path.join(subdir, file)
                    rel_path = os.path.relpath(file_path, root_dir)

                    try:
                        # 读取内容并写入
                        with open(file_path, 'r', encoding='utf-8') as infile:
                            content = infile.read()
                            
                            # 写入分隔符和路径
                            outfile.write(f"\n{'='*80}\n")
                            outfile.write(f"FILE_PATH: {rel_path}\n")
                            outfile.write(f"{'='*80}\n")
                            outfile.write(content)
                            outfile.write("\n")
                            
                            print(f"✅ 已合并: {rel_path}")
                            file_count += 1
                    except Exception as e:
                        print(f"❌ 读取错误 (跳过): {rel_path} -> {e}")

    print(f"\n🎉 处理完成！共合并了 {file_count} 个文件。")
    print(f"👉 请将文件 [{OUTPUT_FILE}] 上传给我。")

if __name__ == "__main__":
    merge_project_files()