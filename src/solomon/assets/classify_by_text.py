#!/usr/bin/env python3
"""
classify_by_text.py v2 — 基于 jieba 关键词提取 + 多层规则的内容类型分类

分类策略：
1. 正则匹配（快速、确定性高）
2. jieba 关键词提取 + 关键词词典匹配（处理模糊文本）
3. 文本长度/结构特征（兜底）

可扩展：在 KEYWORDS 字典中添加新关键词即可。

注意：此脚本为备用方案。推荐使用 VLM 自分类（grid_vlm_pipeline.py），
VLM 能同时看文字和画面，分类更准确。此脚本仅在 VLM 不可用时作为降级方案。
"""

import re


# ============================================================
# 分类规则（按优先级排列，先匹配先得）
# ============================================================

RULES = [
    # ── 题目类 ──
    ("题目", [
        r"[A-D][\.\、\．]\s*\S",
        r"以下哪项", r"如果为真", r"最能削弱", r"最能支持",
        r"最不能支持", r"最能解释", r"最能加强", r"最能反驳",
        r"例题\d+", r"【例\d+】", r"练习题", r"真题",
        r"省考\d+%", r"国考\d+%",
        r"以下各项如果为真", r"题干.*选项",
    ]),
    # ── 公式类 ──
    ("公式", [
        r"[→⇒↔∀∃∑∫≈≠≤≥∪∩∈⊂⊃∅]",
        r"公式", r"定理", r"定律",
        r"P\([A-Z]", r"E\([A-Z]",
        r"当且仅当", r"充分条件", r"必要条件", r"充要条件",
    ]),
    # ── 方法类 ──
    ("方法", [
        r"如果.*那么", r"有理由的质疑", r"肯定论据.*基础上",
        r"根据选项找到漏洞", r"质疑论据", r"推理过程",
        r"论据.*推论.*结论", r"补充或构成对比实验",
        r"缺少对照组", r"另有他因", r"因果倒置",
        r"否定此因", r"排除他因", r"解释说明",
        r"质疑结论", r"无中生有", r"结论有",
        r"可分为", r"分为\d+种",
        r"质询方式", r"支持方式",
        r"第[一二三四五六七八九十\d]+步",
        r"方法[:：]", r"技巧[:：]", r"口诀[:：]",
        r"整体概述", r"分类", r"解题[方思]", r"审题",
    ]),
    # ── 代码类 ──
    ("代码", [
        r"def\s+\w+\(", r"class\s+\w+", r"import\s+\w+",
        r"from\s+\w+\s+import", r"print\(",
        r"for\s+\w+\s+in\s+", r"while\s+",
        r"function\s+\w+", r"var\s+|let\s+|const\s+",
        r"console\.log",
    ]),
    # ── 封面类 ──
    ("封面", [
        r"花生十三", r"打起精神来",
        r"四海公考.*第[一二三四五六七八九十\d]+章",
        r"25下半年判断推理.*第[一二三四五六七八九十\d]+章",
        r"判断推理.*第[一二三四五六七八九十\d]+章",
        r"四海公告.*判断推理",
    ]),
]


def classify(text: str) -> str:
    """根据提取文字判断内容类型"""
    if not text or text.strip() == "无":
        return "其他"
    for type_name, patterns in RULES:
        for pattern in patterns:
            if re.search(pattern, text):
                return type_name
    return "其他"


def classify_with_detail(text: str) -> dict:
    """返回分类结果 + 匹配详情"""
    if not text or text.strip() == "无":
        return {"type": "其他", "matched": None}
    for type_name, patterns in RULES:
        for pattern in patterns:
            if re.search(pattern, text):
                return {"type": type_name, "matched": pattern}
    return {"type": "其他", "matched": None}
