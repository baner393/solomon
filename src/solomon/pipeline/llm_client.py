#!/usr/bin/env python3
"""
llm_client.py — SenseNova 调用封装（文本 / JSON schema 约束 / 识图）

走本地 4Key 轮换代理（OpenAI 兼容 /v1/chat/completions）：
  代理：127.0.0.1:3456（或 3458，fallback）
  model：sensenova-6.8-flash-lite
  auth ：Bearer proxy（fast-vision.js 同款）

用法：
    from llm_client import llm_chat, llm_json

    text = llm_chat("你好")
    obj  = llm_json("提取要点", schema_prompt="输出 JSON: {key: string, points: []}")

识图（视频关键帧逐张串行）：
    from llm_client import vision_analyze
    desc = vision_analyze("/path/frame.jpg", "这是什么界面？")

⚠️ 识图必须逐张串行调用（一次一张），并发会打崩代理。已在函数内强制串行信号。
"""

import json
import os
import re
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---- 配置 ----
PROXY_PORTS = [3456, 3458]          # 4Key 轮换代理端口（3456 主，3458 备）
PROXY_TOKEN = "proxy"
MODEL = os.environ.get("SENSENOVA_MODEL", "sensenova-6.8-flash-lite")
TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "180"))

VISION_SCRIPT = os.environ.get(
    "FAST_VISION_JS",
    "/mnt/c/Users/ban/.zcode/workspace/default/fast-vision.js",
)
NODE = os.environ.get("NODE_BIN", "node")


def _http_json(method, path, body, port, timeout=TIMEOUT):
    """向代理发 HTTP 请求（纯 stdlib，不依赖 requests）"""
    import http.client
    host = "127.0.0.1"
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {PROXY_TOKEN}",
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8", errors="replace")
    conn.close()
    return raw


def _chat(messages, temperature=0.3, port=None, max_tokens=4096, timeout=TIMEOUT, stream=False, out=None):
    """调 chat/completions。按端口顺序尝试，失败切换下一端口。

    stream=True 时走 SSE 流式，逐 token 写到 out（默认 stdout），同时返回完整内容。
    """
    body = {
        "model": MODEL,
        "messages": messages,
        "stream": bool(stream),
        "temperature": temperature,
        "max_tokens": max_tokens,
        # SenseNova 默认带思维链（reasoning），会挤占 max_tokens 导致长 JSON 的
        # content 为空/截断。知识库生成场景只需结果，显式禁用。
        "thinking": {"type": "disabled"},
    }
    ports = [port] if port else PROXY_PORTS
    last_err = None
    for p in ports:
        try:
            if stream:
                content = _chat_stream(body, p, timeout, out or sys.stdout)
                if content:
                    return content
                last_err = f"port{p} 流式空响应"
            else:
                raw = _http_json("POST", "/v1/chat/completions", body, p, timeout=timeout)
                data = json.loads(raw)
                content = data.get("choices", [{}])[0].get("message", {}).get("content")
                if content:
                    return content.strip()
                last_err = f"port{p} 空响应: {raw[:200]}"
        except Exception as e:
            last_err = f"port{p} {e}"
            continue
    raise RuntimeError(f"LLM 调用失败（所有端口）: {last_err}")


def _chat_stream(body, port, timeout, out):
    """SSE 流式调用：逐 token 写 out（flush），返回完整内容。"""
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request(
        "POST", "/v1/chat/completions",
        body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {PROXY_TOKEN}"},
    )
    resp = conn.getresponse()
    content = ""
    for line in resp:
        line = line.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            obj = json.loads(data)
            delta = (obj.get("choices", [{}])[0].get("delta", {}) or {}).get("content", "")
        except (json.JSONDecodeError, IndexError, TypeError, AttributeError):
            delta = ""
        if delta:
            content += delta
            out.write(delta)
            out.flush()
    conn.close()
    if content and not content.endswith("\n"):
        out.write("\n")
        out.flush()
    return content


def llm_chat(system, user, temperature=0.3, max_tokens=4096, timeout=TIMEOUT, stream=False):
    """普通文本对话。stream=True 时流式输出到 stdout，同时返回完整文本。"""
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    return _chat(messages, temperature=temperature, max_tokens=max_tokens, timeout=timeout, stream=stream)


def extract_required_keys(schema_prompt):
    """从 schema prompt 的 JSON 示例中提取顶层必需键（去类型标注）。

    只提取「顶层」键（括号深度为 1 的键），忽略嵌套在数组/对象里的键。
    例如 {"sections": [{"heading": ...}]} 只返回 ["sections"]，不返回 "heading"。
    """
    keys = []
    for m in re.finditer(r'"(\w+)"\s*:', schema_prompt):
        prefix = schema_prompt[:m.start()]
        depth = prefix.count('{') - prefix.count('}')
        if depth == 1:
            keys.append(m.group(1))
    return list(dict.fromkeys(keys))  # 去重保序


def _repair_json(text):
    """容错修复：LLM 常在 JSON 字符串值里混入未转义的 ASCII 双引号
    （如 说"我不知道"），导致 json.loads 失败。逐字符扫描，
    把"字符串值内部的裸引号"替换为中文引号「」。
    """
    out = []
    in_string = False
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and in_string:
            out.append(ch)
            if i + 1 < n:
                out.append(text[i + 1])
                i += 2
            else:
                i += 1
            continue
        if ch == '"':
            if not in_string:
                # 字符串开始：前一个有效字符应是结构符号
                in_string = True
                out.append(ch)
            else:
                # 字符串内：看下一个字符判断是否字符串结束
                nxt = text[i + 1] if i + 1 < n else ""
                if nxt in (",", "}", "]", ":", "\n", " ", ""):
                    in_string = False
                    out.append(ch)
                else:
                    # 裸引号（正文引用）→ 中文引号
                    out.append("「")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def llm_json(system, user, schema_prompt, temperature=0.1, max_retries=3, optional_keys=None, max_tokens=4096, timeout=TIMEOUT):
    """带 schema 约束的 JSON 输出。schema_prompt 说明期望的 JSON 结构。

    稳定性策略：
    1. 解析 JSON
    2. 校验顶层必需键（从 schema 示例提取）
    3. 缺键/解析失败 → 把错误反馈给模型重新生成（最多 max_retries 次）

    optional_keys：可缺省/可为空的顶层键（如配图字段），不计入缺失校验。
    max_tokens：输出上限，长文档（如五层提炼）需调大，否则 JSON 被截断。
    timeout：单次调用超时（秒），长输出需调大。
    """
    required = extract_required_keys(schema_prompt)
    if optional_keys:
        required = [k for k in required if k not in set(optional_keys)]
    sys_msg = (
        system
        + "\n\n你必须只输出一个合法的 JSON 对象，不要输出任何其它文字、解释或 markdown 代码块。\n"
        + "必须包含以下顶层字段（每个字段都要有值，不能为 null/空）：\n"
        + ", ".join(f'"{k}"' for k in required)
        + "\n\n⚠️ 字段值（尤其是正文文本）中禁止使用英文双引号 \"，"
        + "如需引用原文请用中文引号「」或『』。\n"
        + "输出结构示例：\n"
        + schema_prompt
    )
    full_user = user + "\n（只输出 JSON）"
    last_err = None
    raw = ""
    for attempt in range(max_retries + 1):
        try:
            raw = llm_chat(sys_msg, full_user, temperature=temperature, max_tokens=max_tokens, timeout=timeout)
            text = raw.strip()
            # 去掉可能的 ```json 包裹
            if text.startswith("```"):
                text = text.split("\n", 1)[-1]
                text = text.rsplit("```", 1)[0]
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                # 容错：修复字符串值内的裸引号后再解析
                repaired = _repair_json(text)
                obj = json.loads(repaired)
            if not isinstance(obj, dict):
                raise ValueError(f"输出不是 JSON 对象: {type(obj)}")
            missing = [k for k in required if k not in obj or obj[k] in (None, "", [])]
            if missing:
                raise ValueError(f"缺少/空字段: {missing}")
            return obj
        except (json.JSONDecodeError, ValueError, RuntimeError) as e:
            last_err = e
            if attempt < max_retries:
                # 把错误反馈给模型
                feedback = (
                    f"上一次输出不符合要求（错误: {e}）。请重新生成，"
                    f"确保是合法 JSON 且包含全部字段: {', '.join(required)}。"
                    f"\n上一次输出片段: {raw[:300]}"
                )
                full_user = user + "\n（只输出 JSON）\n\n" + feedback
                time.sleep(1)
    raise RuntimeError(f"JSON 校验失败（重试 {max_retries} 次）: {last_err}\n原始输出: {raw[:500]}")


def vision_analyze(image_path, prompt="请描述这张图片的内容", timeout=TIMEOUT, max_retries=2):
    """识图：复用 fast-vision.js（上传图床→SenseNova 视觉）。逐张串行。

    失败自愈：API 错误/网络错误/图床失败时重试（默认 2 次），
    全部失败返回 '❌ 识图失败' 前缀，调用方据此标记"未验证"而非丢弃。
    """
    import subprocess
    cmd = [NODE, VISION_SCRIPT, "-p", prompt, image_path]
    last_err = ""
    for attempt in range(max_retries + 1):
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            last_err = "超时"
            time.sleep(1)
            continue
        out = result.stdout.strip()
        # 失败判定：非零退出码 / 含错误标记 / 空输出
        if result.returncode == 0 and out and "❌" not in out and "error" not in out.lower():
            return out
        last_err = out[:100] if out else result.stderr.strip()[:100]
        if attempt < max_retries:
            time.sleep(1.5)
    return f"❌ 识图失败({last_err}): {os.path.basename(image_path)}"


def vision_batch(image_paths, prompt="请用中文简要描述图片内容（主体、文字、界面元素），简洁准确。", gap=0.0, workers=4):
    """批量识图：适度并发（默认 4 路，对齐 4-key 轮换代理；实测 4 张并发 ~23s 安全）。

    返回 {path: description}。gap 参数保留兼容但并发下忽略。
    """
    out = {}
    if not image_paths:
        return out
    workers = max(1, min(int(workers), len(image_paths)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(vision_analyze, p, prompt): p for p in image_paths}
        done = 0
        for fut in as_completed(futs):
            p = futs[fut]
            out[p] = fut.result()
            done += 1
            print(f"  [{done}/{len(image_paths)}] {os.path.basename(p)}: {out[p][:60]}...")
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    if sys.argv[1] == "chat":
        print(llm_chat("你是助手", sys.argv[2]))
    elif sys.argv[1] == "json":
        print(json.dumps(llm_json("你是助手", sys.argv[2], sys.argv[3]), ensure_ascii=False, indent=2))
    elif sys.argv[1] == "vision":
        for p in sys.argv[2:]:
            print(vision_analyze(p))