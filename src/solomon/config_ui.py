#!/usr/bin/env python3
"""
config_ui.py — solomon 图形配置向导（Web，纯 stdlib，零依赖）

给小白用户的配置入口：不碰命令行、不碰 .env 文件，浏览器里点选/填写即可
完成 agent 不能代劳的配置（LLM 端点、知识库存放位置、渠道凭据、代理）。

场景：
- Windows 原生 / WSL / Linux / 云服务器 均可（浏览器访问 localhost）
- 知识库位置支持三形态：Windows 盘符（D:\\...）、WSL 挂载（/mnt/d/...）、
  云服务器任意绝对路径——向导自动做跨平台路径转换提示

用法：
    python -m solomon.cli config            # 默认 127.0.0.1:8811，自动开浏览器
    python -m solomon.cli config --port 9000 --host 0.0.0.0   # 云服务器场景

安全：默认只绑 127.0.0.1；不写日志；凭据只落 .env（gitignore）；无外发。
"""

from __future__ import annotations

import json
import os
import re
import sys
import webbrowser
from pathlib import Path

try:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
except ImportError:  # pragma: no cover
    from http.server import BaseHTTPRequestHandler, HTTPServer as ThreadingHTTPServer

# ── 平台检测 ─────────────────────────────────────────────────
def detect_platform() -> str:
    """返回 windows / wsl / linux。云服务器 = linux（无显示器，界面走浏览器）。"""
    if sys.platform == "win32":
        return "windows"
    try:
        with open("/proc/version", encoding="utf-8", errors="replace") as f:
            if "microsoft" in f.read().lower():
                return "wsl"
    except OSError:
        pass
    return "linux"


# ── 跨平台路径归一化（复用 classify_input 的经验，GUI 侧给提示）────
_WIN_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/](.*)$")
_WSL_MNT_RE = re.compile(r"^/mnt/([a-z])/(.*)$")
_UNC_RE = re.compile(r"^\\\\wsl(?:\.localhost|\$)\\[^\\]+\\(.+)$")


def normalize_path(path: str, platform: str | None = None) -> dict:
    """把用户输入路径转成「当前平台可用的建议路径」，附说明。

    返回 {suggested, note, detected_platform}。
    """
    platform = platform or detect_platform()
    p = (path or "").strip().replace("\\", "/")
    if not p:
        return {"suggested": "", "note": "留空则不设置", "detected_platform": platform}

    # UNC：\\wsl.localhost\\Ubuntu\\rest → /rest（WSL/Linux 侧直接用；直接 match 原始串）
    m_unc = _UNC_RE.match(path or "")
    if m_unc:
        suggested = "/" + m_unc.group(1).replace("\\", "/")
        return {"suggested": suggested, "note": "UNC 已转换为 WSL 路径", "detected_platform": platform}

    p = re.sub(r"/{2,}", "/", p)  # 折叠连续斜杠（转义/复制来源可能带双斜杠）

    if platform == "windows":
        # 用户给 /mnt/d/...（从 WSL 复制）→ 反转换为 D:/...
        m = _WSL_MNT_RE.match(p)
        if m:
            suggested = f"{m.group(1).upper()}:/{m.group(2)}"
            return {"suggested": suggested, "note": "WSL 路径已转换为 Windows 盘符路径", "detected_platform": platform}
        m = _WIN_DRIVE_RE.match(p)
        if m:
            return {"suggested": p, "note": "Windows 盘符路径，直接可用", "detected_platform": platform}
        return {"suggested": p, "note": "Windows 下建议使用盘符路径（D:\\...）；WSL 内实际路径为 /mnt/…", "detected_platform": platform}

    # wsl / linux：Windows 盘符 → /mnt/<盘>/...
    m = _WIN_DRIVE_RE.match(p)
    if m:
        suggested = f"/mnt/{m.group(1).lower()}/{m.group(2)}"
        note = "Windows 盘符已转换为 WSL 挂载路径"
        return {"suggested": suggested, "note": note, "detected_platform": platform}
    if p.startswith("/mnt/"):
        return {"suggested": p, "note": "WSL 挂载路径，直接可用", "detected_platform": platform}
    return {"suggested": p, "note": "Linux 绝对路径，直接可用", "detected_platform": platform}


# ── .env 读写（幂等：保留既有键/注释，只更新表单键）─────────────
def load_env(path: Path) -> dict:
    """读 .env 现有键值（不覆盖 os.environ）。"""
    out = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def update_env(path: Path, updates: dict) -> int:
    """更新 .env：表单键有值则更新，值为空串则删除该键。返回写入键数。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    lines = existing.splitlines()
    # 先收集要保留的键（不在 updates 中或 updates 值为空=删除）
    drop = {k for k, v in updates.items() if v == ""}
    hit, written = set(), 0
    new_lines = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.partition("=")[0].strip()
            if k in updates:
                if updates[k] == "":
                    continue  # 删除该键
                new_lines.append(f"{k}={updates[k]}")
                hit.add(k)
                written += 1
                continue
            if k in drop:
                continue
        new_lines.append(line)
    # 追加未命中且非空的新键
    for k, v in updates.items():
        if k not in hit and v != "":
            new_lines.append(f"{k}={v}")
            written += 1
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    return written


# ── 配置目标（写入位置）────────────────────────────────────────
def target_env_files() -> list[tuple[str, Path]]:
    """可写的 .env 目标：HERMES_HOME/profiles/*/.env（存在才写）+ solomon 根 .env。"""
    targets = []
    hermes_home = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
    for profile in ("coordinator", "solomon", "newsolomon"):
        p = Path(hermes_home) / "profiles" / profile / ".env"
        if p.exists():
            targets.append((f"profile: {profile}", p))
    root_env = Path(__file__).resolve().parents[2] / ".env"  # solomon 仓库根
    if root_env.exists() or not targets:
        targets.append(("solomon 根 .env（管线/纯 CLI 用）", root_env))
    return targets


def hermes_home_dir() -> Path:
    return Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))


# ── LLM 配置统一（.env 管线层 + config.yaml agent 层）──────────
# 2026-10-06 全量测试暴露：GUI 只写 .env，hermes agent 的模型调用读 config.yaml
# providers（默认指向本机 3456/3458 代理）——朋友部署无本地代理时 @问答必然失败。
# 规则：填了云端 LLM_BASE_URL → 两层统一走云端；清空 → 恢复本机代理（整文件快照还原）。

def _config_yaml_files() -> list[tuple[str, Path]]:
    out = []
    for profile in ("coordinator", "solomon", "newsolomon"):
        p = hermes_home_dir() / "profiles" / profile / "config.yaml"
        if p.exists():
            out.append((profile, p))
    return out


def _cloud_backup_dir() -> Path:
    d = hermes_home_dir() / ".config-ui" / "backup"
    d.mkdir(parents=True, exist_ok=True)
    return d


def apply_llm_unified(cloud_base: str, cloud_key: str) -> list[str]:
    """统一 LLM 配置到 config.yaml（agent 层）。

    cloud_base 非空：三层全走云端——先备份本地形态 config.yaml（首次），再文本替换
    base_url(127.0.0.1:3456/3458→云端) + api_key(proxy→key)。
    cloud_base 为空：从备份还原本地代理形态（测试完切回的对称操作）。
    返回每 profile 的动作描述。
    """
    cloud_base = (cloud_base or "").strip().rstrip("/")
    reports = []
    for profile, p in _config_yaml_files():
        text = p.read_text(encoding="utf-8", errors="replace")
        backup = _cloud_backup_dir() / f"{profile}.config.yaml"
        if cloud_base:
            if not backup.exists() and "127.0.0.1:345" in text:
                backup.write_text(text, encoding="utf-8")  # 首次切云端前备份
            new = re.sub(r"base_url:\s*http://127\.0\.0\.1:345[68]/v1\b",
                         f"base_url: {cloud_base}/v1" if not cloud_base.endswith("/v1") else f"base_url: {cloud_base}",
                         text)
            if cloud_key:
                new = re.sub(r"api_key:\s*\S+", f"api_key: {cloud_key}", new)
            if new != text:
                p.write_text(new, encoding="utf-8")
                reports.append(f"{profile}: config.yaml → 云端（{cloud_base}）")
            else:
                reports.append(f"{profile}: config.yaml 已是云端形态，未变更")
        else:
            if backup.exists():
                p.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")
                backup.unlink()
                reports.append(f"{profile}: config.yaml → 恢复本地代理（快照还原）")
            else:
                reports.append(f"{profile}: 无本地快照，config.yaml 未动")
    return reports


# ── Web 服务 ──────────────────────────────────────────────────
_HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Solomon 配置向导</title>
<style>
body{font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif;max-width:720px;margin:24px auto;padding:0 16px;color:#1f2328}
h1{font-size:22px;margin-bottom:4px}p.sub{color:#666;font-size:13px;margin-top:0}
section{border:1px solid #d0d7de;border-radius:10px;padding:16px 20px;margin:14px 0}
h2{font-size:15px;margin:0 0 10px}
label{display:block;font-size:13px;margin:10px 0 4px;color:#444}
input[type=text],input[type=password],select{width:100%;box-sizing:border-box;padding:8px;border:1px solid #d0d7de;border-radius:6px;font-size:14px}
.hint{font-size:12px;color:#6b7280;margin-top:4px}
.note{font-size:12px;color:#1a7f37;margin-top:2px}
button{padding:10px 18px;border-radius:8px;border:none;cursor:pointer;font-size:14px}
.primary{background:#0969da;color:#fff}.primary:hover{background:#0b5db0}
.ghost{background:#f6f8fa;color:#24292f;border:1px solid #d0d7de;margin-left:8px}
#status{margin-top:12px;font-size:14px;min-height:20px}
#status.ok{color:#1a7f37}#status.err{color:#cf222e}
details{margin-top:6px}details summary{cursor:pointer;font-size:13px;color:#57606a}
.env-target{font-size:12px;color:#6b7280;margin-top:10px}
</style></head><body>
<h1>Solomon 配置向导</h1>
<p class="sub">填完点「保存配置」即可——凭据只写入本机 .env 文件，不会上传任何地方。</p>
<div id="envdetect" style="font-size:13px;color:#0969da;margin:6px 0"></div>

<section><h2>① LLM 端点（必填）</h2>
<label>端点地址 LLM_BASE_URL <input type="text" id="LLM_BASE_URL" placeholder="https://token.sensenova.cn/v1（不填则用默认本地代理）"></label>
<label>API Key <input type="password" id="LLM_API_KEY" placeholder="你的密钥"></label>
<label>模型名（可选）<input type="text" id="SENSENOVA_MODEL" placeholder="sensenova-6.8-flash-lite"></label>
<div class="hint">不填端点=连本地轮换代理 127.0.0.1:3456（自建代理用户）。<br>
<b>填写云端端点后，问答/入库/agent 思考层会统一走云端</b>（改动 agent 层 config.yaml，需重启网关生效）。</div></section>

<section><h2>② 知识库位置（必填）</h2>
<p class="hint" style="margin-top:0">支持三种存放：<b>本地 Windows 盘</b> / <b>WSL 文件系统</b> / <b>云服务器</b>。选「我运行在」后输入路径，下方自动给出转换建议。</p>
<label>我运行在
<select id="platform"><option value="auto">自动检测</option><option value="windows">Windows 原生</option><option value="wsl">WSL / Linux</option><option value="linux">云服务器 / Linux</option></select></label>
<label>知识库目录 SOLOMON_VAULT <input type="text" id="SOLOMON_VAULT" placeholder="D:\\all\\my_vault 或 /mnt/d/... 或 /data/..."></label>
<div class="note" id="vaulthint"></div>
<label>视频工作目录 WORK_ROOT <input type="text" id="WORK_ROOT" placeholder="中间产物目录，可与知识库同盘不同目录"></label>
<div class="note" id="workhint"></div></section>

<section><h2>③ 渠道凭据（可选，跳过可后续再配）</h2>
<details><summary>展开填写 QQ / 飞书机器人凭据</summary>
<label>QQ_APP_ID <input type="text" id="QQ_APP_ID" placeholder="qq开放平台的机器人 AppID"></label>
<label>QQ_CLIENT_SECRET <input type="password" id="QQ_CLIENT_SECRET" placeholder="AppSecret"></label>
<label>QQBOT_HOME_CHANNEL <input type="text" id="QQBOT_HOME_CHANNEL" placeholder="你的QQ号/沙箱成员号，即机器人发消息的目标"></label>
<label>FEISHU_APP_ID <input type="text" id="FEISHU_APP_ID" placeholder="cli_xxx"></label>
<label>FEISHU_APP_SECRET <input type="password" id="FEISHU_APP_SECRET" placeholder="飞书应用密钥"></label>
<label>FEISHU_HOME_CHANNEL <input type="text" id="FEISHU_HOME_CHANNEL" placeholder="oc_xxx（你的飞书 open_id / 群）"></label>
</details></section>

<section><h2>④ 出网代理 / 杂项（可选）</h2>
<label>HTTP_PROXY <input type="text" id="HTTP_PROXY" placeholder="http://127.0.0.1:7890（YouTube 等海外源需要；B站直连不需要）"></label>
<label>SOLOMON_LOCATE_ROOTS <input type="text" id="SOLOMON_LOCATE_ROOTS" placeholder="~:/mnt/d（文件找不到时的自动定位搜索根，冒号分隔）"></label></section>

<div style="margin:18px 0"><button class="primary" onclick="save()">保存配置</button><button class="ghost" onclick="loadEnv()">重新读取当前配置</button></div>
<div id="status"></div>
<div class="env-target" id="targets"></div>

<script>
async function j(url,opt){const r=await fetch(url,opt);return r.json()}
async function norm(id,noteId){const p=document.getElementById(id).value;if(!p){document.getElementById(noteId).innerText='';return}
  const plat=document.getElementById('platform').value;
  const r=await j('/api/normalize?path='+encodeURIComponent(p)+'&platform='+plat);
  document.getElementById(noteId).innerText=r.note+' → '+r.suggested;
  document.getElementById(id).value=r.suggested;}
document.getElementById('SOLOMON_VAULT').addEventListener('change',()=>norm('SOLOMON_VAULT','vaulthint'));
document.getElementById('WORK_ROOT').addEventListener('change',()=>norm('WORK_ROOT','workhint'));
document.getElementById('platform').addEventListener('change',()=>{norm('SOLOMON_VAULT','vaulthint');norm('WORK_ROOT','workhint')});
const FIELDS=['LLM_BASE_URL','LLM_API_KEY','SENSENOVA_MODEL','SOLOMON_VAULT','WORK_ROOT','QQ_APP_ID','QQ_CLIENT_SECRET','QQBOT_HOME_CHANNEL','FEISHU_APP_ID','FEISHU_APP_SECRET','FEISHU_HOME_CHANNEL','HTTP_PROXY','SOLOMON_LOCATE_ROOTS'];
async function loadEnv(){const d=await j('/api/env');
  FIELDS.forEach(k=>{document.getElementById(k).value=d.env[k]||''});
  if(d.detected){document.getElementById('platform').value=d.detected_platform==='windows'?'windows':(d.detected_platform==='wsl'?'wsl':'linux')}
  document.getElementById('envdetect').innerText='检测到运行环境：'+d.platform_desc+'　·　Python '+d.python;
  document.getElementById('targets').innerText='配置将写入：'+d.targets.join('、');}
async function save(){const body={};FIELDS.forEach(k=>body[k]=document.getElementById(k).value.trim());
  const st=document.getElementById('status');
  if(!body.SOLOMON_VAULT&&!body.LLM_BASE_URL){st.className='err';st.innerText='至少填知识库目录或 LLM 端点之一';return}
  const d=await j('/api/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  st.className=d.ok?'ok':'err';st.innerText=d.message;}
loadEnv();
</script></body></html>"""


def _form_fields(body: dict) -> dict:
    keys = ("LLM_BASE_URL", "LLM_API_KEY", "SENSENOVA_MODEL", "SOLOMON_VAULT", "WORK_ROOT",
            "QQ_APP_ID", "QQ_CLIENT_SECRET", "QQBOT_HOME_CHANNEL",
            "FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_HOME_CHANNEL",
            "HTTP_PROXY", "SOLOMON_LOCATE_ROOTS")
    return {k: (body.get(k) or "").strip() for k in keys}


def _collect_env() -> dict:
    """汇总各目标 .env 的当前值（第一个非空为准）。"""
    merged, seen = {}, set()
    for _, p in target_env_files():
        for k, v in load_env(p).items():
            if k not in seen and v:
                merged[k] = v
                seen.add(k)
    return merged


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: str, ctype: str = "application/json") -> None:
        data = payload.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj: dict, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def log_message(self, *a):  # 静默（不落日志，防凭据泄漏到终端）
        pass

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            return self._send(200, _HTML, "text/html")
        if self.path == "/api/env":
            d = detect_platform()
            desc = {"windows": "Windows 原生", "wsl": "WSL (Ubuntu)", "linux": "Linux / 云服务器"}[d]
            return self._json({
                "env": _collect_env(), "detected_platform": d,
                "platform_desc": desc, "python": sys.version.split()[0],
                "targets": [name for name, _ in target_env_files()],
            })
        if self.path.startswith("/api/normalize"):
            import urllib.parse
            q = urllib.parse.parse_qs(self.path.split("?", 1)[1])
            r = normalize_path(q.get("path", [""])[0], q.get("platform", [None])[0] or None)
            return self._json(r)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/api/save":
            try:
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception as e:
                return self._json({"ok": False, "message": f"请求解析失败: {e}"}, 400)
            fields = _form_fields(body)
            targets = target_env_files()
            written_total = 0
            for _, p in targets:
                written_total += update_env(p, fields)
            # LLM 配置统一：填云端端点 → agent 层 config.yaml 同步云端；清空 → 恢复本地代理
            yaml_reports = apply_llm_unified(fields.get("LLM_BASE_URL", ""), fields.get("LLM_API_KEY", ""))
            msg = f"已保存到 {len(targets)} 个配置文件（更新 {written_total} 个键）"
            if yaml_reports:
                msg += "；" + "；".join(yaml_reports)
            if fields.get("LLM_BASE_URL"):
                msg += "。⚠️ 改了 agent 层 config.yaml，需重启网关才生效（hermes --profile coordinator gateway restart）"
            return self._json({
                "ok": True,
                "message": msg,
            })
        return self._json({"error": "not found"}, 404)


def serve(host: str = "127.0.0.1", port: int = 8811) -> None:
    srv = ThreadingHTTPServer((host, port), _Handler)
    url = f"http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}"
    print(f"[solomon config] 配置向导已启动: {url}")
    print("[solomon config] 按 Ctrl+C 退出。凭据只写本机 .env，不对外发送。")
    if host in ("127.0.0.1", "localhost"):
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[solomon config] 已退出")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(prog="solomon config")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8811)
    a = ap.parse_args()
    serve(a.host, a.port)
