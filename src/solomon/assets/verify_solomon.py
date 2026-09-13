#!/usr/bin/env python3
"""
Solomon 入库验证脚本 — 写完文件后运行，自动检查：
1. 所有图片路径是否指向存在的文件
2. 所有 [[wikilink]] 是否指向存在的页面
3. frontmatter 格式是否正确

用法：python3 verify_solomon.py <solomon_root>
示例：python3 verify_solomon.py ~/.solomon/vault
"""
import os, re, sys, glob

def strip_code_blocks(content):
    """剥掉 ``` 围栏代码块（Obsidian 不渲染其中的链接/embed，无需检查）"""
    out, in_fence = [], False
    for line in content.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(line)
    return "\n".join(out)

def strip_inline_code(content):
    """剥掉行内代码 `...`（其中出现的 [[ ]] 是文字说明不是链接）"""
    return re.sub(r'`[^`]*`', ' ', content)

# 链接检查排除名单：log 是历史追加记录、其余为含教学示例的文档（[[页面A]]、
# ./assets/cover.jpg 等示例文本按设计不存在，不应计入断链）
SKIP_LINKCHECK_FILES = {"log.md", "log.md.bak", "SCHEMA.md",
                        "raw/articles/bilibili-render-markdown-skill-backup-2026-06-15.md",
                        "raw/articles/obsidian-知识库图片问题排查指南.md"}

def verify_image_paths(solomon_root):
    """检查 markdown 文件中的图片路径是否指向存在的文件"""
    errors = []
    for md_file in glob.glob(os.path.join(solomon_root, "**/*.md"), recursive=True):
        rel_path = os.path.relpath(md_file, solomon_root)
        if rel_path.replace('\\', '/') in SKIP_LINKCHECK_FILES or rel_path in SKIP_LINKCHECK_FILES:
            continue
        with open(md_file, 'r', encoding='utf-8') as f:
            content = strip_code_blocks(f.read())
        
        # Find all image references: ![...](path)
        for match in re.finditer(r'!\[.*?\]\(([^)]+)\)', content):
            img_path = match.group(1)
            if img_path.startswith('http://') or img_path.startswith('https://'):
                continue  # Skip external URLs
            
            # Resolve relative path
            md_dir = os.path.dirname(md_file)
            abs_img = os.path.normpath(os.path.join(md_dir, img_path))
            
            if not os.path.exists(abs_img):
                errors.append(f"❌ 图片不存在: {rel_path} → {img_path}")
    
    return errors

def verify_wikilinks(solomon_root):
    """检查 [[wikilink]] 是否指向存在的页面或附件（图片 embed 全局解析）"""
    errors = []
    # Collect all existing page names (from filenames without .md)
    existing_pages = set()
    page_rel_paths = set()
    for md_file in glob.glob(os.path.join(solomon_root, "**/*.md"), recursive=True):
        name = os.path.splitext(os.path.basename(md_file))[0]
        existing_pages.add(name)
        page_rel_paths.add(os.path.relpath(md_file, solomon_root).replace('\\', '/'))
    # 附件（图片等）：Obsidian 的 [[file.jpg]] / [[dir/file.jpg]] 全局解析
    # basenames 命中裸文件名 embed；rel_paths 命中带目录的 embed（同名歧义场景）
    attachment_basenames = set()
    attachment_rel_paths = set()
    img_exts = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.bmp')
    for root, _dirs, files in os.walk(solomon_root):
        for fn in files:
            if fn.lower().endswith(img_exts):
                attachment_basenames.add(fn)
                attachment_rel_paths.add(os.path.relpath(os.path.join(root, fn), solomon_root).replace('\\', '/'))

    # Check all wikilinks
    for md_file in glob.glob(os.path.join(solomon_root, "**/*.md"), recursive=True):
        rel_path = os.path.relpath(md_file, solomon_root)
        if rel_path.replace('\\', '/') in SKIP_LINKCHECK_FILES or rel_path in SKIP_LINKCHECK_FILES:
            continue
        with open(md_file, 'r', encoding='utf-8') as f:
            content = strip_inline_code(strip_code_blocks(f.read()))
        # 表格内 wiki 链接的转义管道 [[page\|alias]] → 归一化后解析（别名不影响目标检查）
        content = content.replace("\\|", "|")

        for match in re.finditer(r'\[\[([^\]|]+)(?:\|[^\]]+)?\]\]', content):
            link_target = match.group(1).strip()
            if link_target.startswith('#'):
                continue  # 页内标题锚点 [[#heading]]
            if link_target in existing_pages:
                continue
            base = os.path.basename(link_target)
            if base in attachment_basenames and '/' not in link_target:
                continue  # 裸文件名 embed 且文件存在
            # 页面带路径链接：Obsidian 按「路径后缀」解析（统一剥 .md 比较）
            target_clean = link_target[:-3] if link_target.endswith('.md') else link_target
            if any(rp[:-3] == target_clean or rp.endswith('/' + target_clean)
                   for rp in page_rel_paths):
                continue
            # 带路径 embed：Obsidian 按「路径后缀」解析（如 [[dir/file.jpg]] 可命中 assets/dir/file.jpg）
            if any(rp == link_target or rp.endswith('/' + link_target) for rp in attachment_rel_paths):
                continue
            errors.append(f"❌ 断链: {rel_path} → [[{link_target}]]")

    return errors

def verify_frontmatter(solomon_root):
    """检查 frontmatter 是否存在且格式正确"""
    warnings = []
    for md_file in glob.glob(os.path.join(solomon_root, "concepts/*.md"), recursive=True):
        rel_path = os.path.relpath(md_file, solomon_root)
        with open(md_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        if not content.startswith('---'):
            warnings.append(f"⚠️ 缺少 frontmatter: {rel_path}")
            continue
        
        # Check for required fields
        end = content.find('---', 3)
        if end == -1:
            warnings.append(f"⚠️ frontmatter 未闭合: {rel_path}")
            continue
        
        fm = content[3:end]
        for field in ['title', 'created', 'type', 'tags']:
            if field + ':' not in fm:
                warnings.append(f"⚠️ frontmatter 缺少 {field}: {rel_path}")
    
    return warnings

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("用法：python3 verify_solomon.py <solomon_root>", file=sys.stderr)
        sys.exit(2)
    root = sys.argv[1]
    
    print(f"验证 Solomon 知识库: {root}\n")
    
    img_errors = verify_image_paths(root)
    link_errors = verify_wikilinks(root)
    fm_warnings = verify_frontmatter(root)
    
    if img_errors:
        print(f"=== 图片路径错误 ({len(img_errors)}) ===")
        for e in img_errors: print(f"  {e}")
        print()
    
    if link_errors:
        print(f"=== 双向链接断链 ({len(link_errors)}) ===")
        for e in link_errors: print(f"  {e}")
        print()
    
    if fm_warnings:
        print(f"=== Frontmatter 警告 ({len(fm_warnings)}) ===")
        for w in fm_warnings: print(f"  {w}")
        print()
    
    total = len(img_errors) + len(link_errors) + len(fm_warnings)
    if total == 0:
        print("✅ 全部通过！图片路径、双向链接、frontmatter 均正常。")
    else:
        print(f"共 {total} 个问题需要修复。")
        sys.exit(1)
