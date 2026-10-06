"""冒烟测试：不依赖真实 LLM/网络，验证核心逻辑与配置隔离。

运行：python3 -m pytest tests/ -v
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

# ── 让测试可 import 仓库内代码 ─────────────────────────────
REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
sys.path.insert(0, str(SRC))


# ═══════════════════════════════════════════════════════════
# 1. config：路径解析与数据/代码分离
# ═══════════════════════════════════════════════════════════
class TestConfig:
    def test_defaults_are_generic(self):
        """默认值不含「项目所有者特定」痕迹（/mnt/d 个人盘、.hermes 内部目录、个人名）。"""
        from solomon import config

        # 数据根允许含 $HOME 用户名（跨用户正确），但不能含个人盘/内部目录
        for p in (config.VAULT, config.WORK_ROOT, config.SKILL_ASSETS, config.TEMPLATE_DIR):
            assert "/mnt/" not in str(p)
            assert ".hermes" not in str(p)

    def test_env_override(self, monkeypatch, tmp_path):
        """SOLOMON_VAULT 环境变量必须覆盖默认值。"""
        monkeypatch.setenv("SOLOMON_VAULT", str(tmp_path / "myvault"))
        # 重新加载 config（模块缓存需清）
        import importlib
        from solomon import config as cfg

        importlib.reload(cfg)
        assert cfg.VAULT == tmp_path / "myvault"

    def test_fts_db_inside_vault(self):
        """FTS 索引目录默认在 vault/.kb（隔离，不串库）。"""
        from solomon import config

        assert str(config.VAULT / ".kb") in str(
            os.environ.get("SOLOMON_FTS_DB", str(config.VAULT / ".kb"))
        )

    def test_to_env_carries_vault(self, monkeypatch, tmp_path):
        from solomon import config

        monkeypatch.setenv("SOLOMON_VAULT", str(tmp_path / "v"))
        import importlib

        importlib.reload(config)
        env = config.to_env()
        assert env["SOLOMON_VAULT"] == str(tmp_path / "v")
        assert env["SOLOMON_FTS_DB"] == str(tmp_path / "v" / ".kb")


# ═══════════════════════════════════════════════════════════
# 2. 输入分类：URL / 文件 / 名称 / 文本
# ═══════════════════════════════════════════════════════════
class TestClassifyInput:
    def _classify(self, arg):
        sys.path.insert(0, str(SRC / "solomon" / "pipeline"))
        import ingest

        return ingest.classify_input(arg)

    def test_bilibili_url(self):
        kind, val = self._classify("https://www.bilibili.com/video/BV1C69iBgEMk")
        assert kind == "url"  # 视频链接归类 url（main 里 kind==url 走视频入库）

    def test_yt_url(self):
        kind, val = self._classify("https://www.youtube.com/watch?v=abc")
        assert kind == "url"

    def test_local_md_file(self, tmp_path):
        p = tmp_path / "doc.md"
        p.write_text("# t", encoding="utf-8")
        kind, val = self._classify(str(p))
        assert kind == "file"
        assert val == str(p)

    def test_windows_path_converted(self):
        """D:\\... 应被识别为文件并转 WSL 路径。"""
        sys.path.insert(0, str(SRC / "solomon" / "pipeline"))
        import ingest

        assert ingest._to_wsl_path("D:\\foo\\bar.md") == "/mnt/d/foo/bar.md"
        assert ingest._to_wsl_path("D:/foo/bar.md") == "/mnt/d/foo/bar.md"
        assert ingest._to_wsl_path("/mnt/c/x.md") == "/mnt/c/x.md"

    def test_wsl_path_native_windows(self, monkeypatch):
        """原生 Windows 平台（sys.platform=win32）：盘符路径不再转 /mnt。"""
        sys.path.insert(0, str(SRC / "solomon" / "pipeline"))
        import ingest

        monkeypatch.setattr(ingest.sys, "platform", "win32")
        assert ingest._to_wsl_path("C:\\Users\\t\\x.md") == "c:/Users/t/x.md"

    def test_relative_doc_path_exists(self, tmp_path, monkeypatch):
        """相对路径（带目录）且文件存在（相对 cwd）→ file。"""
        d = tmp_path / "docs"
        d.mkdir()
        (d / "x.md").write_text("# t", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        kind, val = self._classify("docs/x.md")
        assert kind == "file"
        assert val.endswith("x.md")

    def test_relative_doc_path_missing(self):
        """相对路径但文件不存在 → file_missing（绝不掉进名称搜索）。"""
        kind, _ = self._classify("tests/不存在的文档xyz.md")
        assert kind == "file_missing"

    def test_doc_ext_never_name_search(self):
        """带 .md 扩展名但文件不存在 → file_missing（绝不掉名称搜索自动入库，
        2026-10-06 全量测试教训：no_such_file.md 曾自动入库不相关视频）。"""
        kind, val = self._classify("面试材料.md")
        assert kind == "file_missing"

    def test_doc_ext_single_name_exists(self, tmp_path, monkeypatch):
        """单文件名（无目录）且相对 cwd 存在 → file。"""
        (tmp_path / "README.md").write_text("# t", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        kind, val = self._classify("README.md")
        assert kind == "file"
        assert val.endswith("README.md")


class TestLlmEndpoints:
    """LLM 端点配置解析：云端 env 优先，未配置回落本地代理。"""

    def _llm(self):
        sys.path.insert(0, str(SRC / "solomon" / "pipeline"))
        import llm_client
        return llm_client

    def test_default_fallback_local_proxy(self, monkeypatch):
        for k in ("LLM_BASE_URL", "SENSENOVA_BASE_URL"):
            monkeypatch.delenv(k, raising=False)
        eps = self._llm()._endpoints()
        assert eps == ["http://127.0.0.1:3456/v1", "http://127.0.0.1:3458/v1"]

    def test_cloud_endpoint_env(self, monkeypatch):
        monkeypatch.setenv("LLM_BASE_URL", "https://token.sensenova.cn/v1")
        assert self._llm()._endpoints() == ["https://token.sensenova.cn/v1"]

    def test_sensenova_alias_env(self, monkeypatch):
        monkeypatch.setenv("SENSENOVA_BASE_URL", "https://api.example.com/v1 ")
        assert self._llm()._endpoints() == ["https://api.example.com/v1"]

    def test_api_key_default_proxy(self):
        """子进程隔离验证 API_KEY 默认值（避免 importlib.reload 的全局副作用）。"""
        import subprocess
        code = (
            "import sys; sys.path.insert(0, r'%s'); "
            "import os; [os.environ.pop(k, None) for k in ('LLM_API_KEY', 'SENSENOVA_API_KEY')]; "
            "import llm_client; print(llm_client.API_KEY)"
        ) % (SRC / "solomon" / "pipeline")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        assert out.stdout.strip() == "proxy"


class TestPreflight:
    """preflight 冒烟：不联网（mock TCP 探活）。锁住 NameError 类回归——
    preflight 引用的名字必须在 ingest 命名空间可解析（2026-10-06 P0 教训）。"""

    def test_preflight_doc_smoke(self, monkeypatch, tmp_path):
        sys.path.insert(0, str(SRC / "solomon" / "pipeline"))
        import ingest

        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setattr(ingest, "VAULT", str(vault))
        monkeypatch.setattr(ingest, "_tcp_ok", lambda *a, **k: True)
        # 不抛 SystemExit / NameError 即通过
        ingest.preflight("doc")


class TestCrossValidateFrames:
    """交叉验证帧名双格式（2026-10-06 P0：code_* 毫秒帧名走 m=None 分支曾崩，
    任何 B站视频 --images 必崩）。"""

    def test_ms_and_hms_frame_names(self, tmp_path):
        import json
        sys.path.insert(0, str(SRC / "solomon" / "pipeline"))
        import ingest

        subs = tmp_path / "subtitles.json"
        subs.write_text(
            '[{"start": 2.0, "end": 4.0, "text": "前段"}, {"start": 29.0, "end": 32.0, "text": "后段"}]',
            encoding="utf-8",
        )
        kf = tmp_path / "kf"
        kf.mkdir()
        (kf / "code_00003599_4b21efed.jpg").write_bytes(b"x")   # 毫秒格式 → 3.599s
        (kf / "keyframes_001_00:00:31.jpg").write_bytes(b"x")   # HH:MM:SS 格式
        out = ingest.cross_validate_modalities(str(tmp_path), str(subs), str(kf), "知识讲解")
        assert out, "应产出 alignment.json"
        with open(out, encoding="utf-8") as f:
            data = json.load(f)
        ts = {r["frame"]: r["ts"] for r in data}
        assert ts["code_00003599_4b21efed.jpg"] == "00:00:03"   # 毫秒帧 ts 正确还原
        assert ts["keyframes_001_00:00:31.jpg"] == "00:00:31"


# ═══════════════════════════════════════════════════════════
# 3. FTS5 索引隔离 + 检索（用临时 vault 建库）
# ═══════════════════════════════════════════════════════════
class TestFtsIsolation:
    def _build_index(self, tmp_path):
        """在临时 vault 建 FTS 索引并返回 (kb_index 模块, vault)。"""
        vault = tmp_path / "vault"
        concepts = vault / "concepts"
        concepts.mkdir(parents=True)
        (vault / ".kb").mkdir(parents=True)
        # 两页
        (concepts / "RAG原理.md").write_text(
            "---\ntitle: RAG原理\ntype: concept\n---\n\n# RAG原理\n\nRAG 是检索增强生成。",
            encoding="utf-8",
        )
        (concepts / "向量数据库.md").write_text(
            "---\ntitle: 向量数据库\ntype: concept\n---\n\n# 向量数据库\n\n向量嵌入存数据库。",
            encoding="utf-8",
        )
        os.environ["SOLOMON_VAULT"] = str(vault)
        os.environ["SOLOMON_FTS_DB"] = str(vault / ".kb")
        import importlib

        import solomon.pipeline.kb_index as kb

        importlib.reload(kb)
        kb.build_full(verbose=False)
        return kb, vault

    def test_index_inside_vault(self, tmp_path):
        kb, vault = self._build_index(tmp_path)
        assert (vault / ".kb" / "kb_fts.db").exists()
        # 索引目录必须随 vault 走（vault 是临时目录，不落到仓库外固定位置）
        assert str(vault) in str(kb.DB_PATH)
        assert ".hermes" not in str(kb.DB_PATH)

    def test_search_hits(self, tmp_path):
        kb, _ = self._build_index(tmp_path)
        hits = kb.search("RAG 原理")
        assert any("RAG原理" in h[0] for h in hits)

    def test_match_expr_chinese(self):
        import solomon.pipeline.kb_index as kb

        expr = kb.build_match_expr("概括一下RAG的基本原理")
        assert expr is not None
        # 中文自然问句应拆出有效词（≥3 字），不是整句
        assert '"RAG"' in expr or "RAG" in expr


# ═══════════════════════════════════════════════════════════
# 4. CLI 解析
# ═══════════════════════════════════════════════════════════
class TestCli:
    def test_parser_has_commands(self):
        from solomon.cli import build_parser

        ap = build_parser()
        for cmd in ("init", "doctor", "ingest", "ask", "index", "verify"):
            assert cmd in ap._subparsers._group_actions[0].choices

    def test_ingest_args(self):
        from solomon.cli import build_parser

        ap = build_parser()
        args = ap.parse_args(["ingest", "https://x/BV1", "--images", "--max-parts", "2"])
        assert args.images is True
        assert args.max_parts == 2


# ═══════════════════════════════════════════════════════════
# 5. 转写子进程命令构造（不实际跑）
# ═══════════════════════════════════════════════════════════
class TestTranscribe:
    def test_transcribe_cli_imports(self):
        import solomon.pipeline.transcribe_cli as tc

        assert hasattr(tc, "main") or hasattr(tc, "transcribe")


class TestConfigUi:
    """图形配置向导的纯函数（路径转换 / .env 幂等写入）。"""

    def _norm(self, path, platform):
        sys.path.insert(0, str(SRC / "solomon"))
        from solomon import config_ui
        return config_ui.normalize_path(path, platform)["suggested"]

    def test_wsl_normalize_win_drive(self):
        assert self._norm("D:\\foo\\bar.md", "wsl") == "/mnt/d/foo/bar.md"
        assert self._norm("D:/foo/bar.md", "wsl") == "/mnt/d/foo/bar.md"
        assert self._norm("/mnt/d/x", "wsl") == "/mnt/d/x"

    def test_windows_normalize_mnt(self):
        assert self._norm("/mnt/d/x", "windows") == "D:/x"
        assert self._norm("D:\\x", "windows") == "D:/x"

    def test_unc_normalize(self):
        assert self._norm("\\\\wsl.localhost\\Ubuntu\\home\\u\\vault", "wsl") == "/home/u/vault"

    def test_linux_absolute(self):
        assert self._norm("/data/vault", "linux") == "/data/vault"

    def test_update_env_preserves_others(self, tmp_path):
        sys.path.insert(0, str(SRC / "solomon"))
        from solomon import config_ui
        p = tmp_path / ".env"
        p.write_text("# 注释行\nSOLOMON_VAULT=/old/vault\nKEEP_ME=yes\n", encoding="utf-8")
        config_ui.update_env(p, {"SOLOMON_VAULT": "/new/vault", "LLM_API_KEY": "sk-x"})
        text = p.read_text(encoding="utf-8")
        assert "# 注释行" in text and "KEEP_ME=yes" in text
        assert "/new/vault" in text and "/old/vault" not in text
        assert "LLM_API_KEY=sk-x" in text

    def test_update_env_empty_removes(self, tmp_path):
        sys.path.insert(0, str(SRC / "solomon"))
        from solomon import config_ui
        p = tmp_path / ".env"
        p.write_text("SOLOMON_VAULT=/old/vault\n", encoding="utf-8")
        config_ui.update_env(p, {"SOLOMON_VAULT": ""})
        assert "SOLOMON_VAULT" not in p.read_text(encoding="utf-8")

    def _fake_hermes(self, tmp_path):
        """造 fake HERMES_HOME：三个 profile 各一个「本地代理形态」config.yaml。"""
        for profile in ("coordinator", "solomon", "newsolomon"):
            d = tmp_path / "hermes" / "profiles" / profile
            d.mkdir(parents=True)
            (d / "config.yaml").write_text(
                f"""model:
  provider: custom:sensenova-4key-3458
  model: sensenova-6.8-flash-lite
providers:
  sensenova-4key-3456:
    base_url: http://127.0.0.1:3456/v1
    api_key: proxy
  sensenova-4key-3458:
    base_url: http://127.0.0.1:3458/v1
    api_key: proxy
""", encoding="utf-8")
        return tmp_path / "hermes"

    def test_llm_unified_cloud(self, tmp_path, monkeypatch):
        """填云端端点 → 三个 config.yaml 的 base_url/api_key 统一走云端 + 首次备份。"""
        sys.path.insert(0, str(SRC / "solomon"))
        from solomon import config_ui
        monkeypatch.setenv("HERMES_HOME", str(self._fake_hermes(tmp_path)))
        reports = config_ui.apply_llm_unified("https://token.sensenova.cn/v1", "sk-test")
        assert len(reports) == 3
        for profile in ("coordinator", "solomon", "newsolomon"):
            text = (tmp_path / "hermes" / "profiles" / profile / "config.yaml").read_text(encoding="utf-8")
            assert "127.0.0.1:345" not in text
            assert "base_url: https://token.sensenova.cn/v1" in text
            assert "api_key: sk-test" in text
        # 备份存在（可还原）
        assert (tmp_path / "hermes" / ".config-ui" / "backup" / "solomon.config.yaml").exists()

    def test_llm_unified_local_restore(self, tmp_path, monkeypatch):
        """清空端点 → 从备份还原本地代理（测试完切回的对称操作）。"""
        sys.path.insert(0, str(SRC / "solomon"))
        from solomon import config_ui
        monkeypatch.setenv("HERMES_HOME", str(self._fake_hermes(tmp_path)))
        config_ui.apply_llm_unified("https://token.sensenova.cn/v1", "sk-test")
        config_ui.apply_llm_unified("", "")
        for profile in ("coordinator", "solomon", "newsolomon"):
            text = (tmp_path / "hermes" / "profiles" / profile / "config.yaml").read_text(encoding="utf-8")
            assert "base_url: http://127.0.0.1:3456/v1" in text
            assert "base_url: http://127.0.0.1:3458/v1" in text
            assert "api_key: proxy" in text
        assert not (tmp_path / "hermes" / ".config-ui" / "backup" / "solomon.config.yaml").exists()


class TestFeishuDoc:
    """飞书文档读取（无网络：classify 识别 + 内容键驱动渲染 + ordered 编号）。"""

    def test_classify_feishu_url(self):
        sys.path.insert(0, str(SRC / "solomon" / "pipeline"))
        import ingest
        kind, val = ingest.classify_input("https://mcnurwzg3ql6.feishu.cn/wiki/ZxJrwPJUGikjNDkrPyncy9f9nrP")
        assert kind == "feishu"
        assert "feishu.cn" in val

    def test_render_blocks_content_keys(self):
        sys.path.insert(0, str(SRC / "solomon" / "pipeline"))
        import feishu_doc
        blocks = [
            {"block_id": "b1", "block_type": 1, "children": ["b2", "b3", "b4", "b5"]},
            {"block_id": "b2", "block_type": 3, "heading1": {"elements": [{"text_run": {"content": "标题一"}}]}},
            {"block_id": "b3", "block_type": 2, "text": {"elements": [{"text_run": {"content": "正文段落"}}]}},
            {"block_id": "b4", "block_type": 13, "ordered": {"elements": [{"text_run": {"content": "步骤A"}}]}},
            {"block_id": "b5", "block_type": 13, "ordered": {"elements": [{"text_run": {"content": "步骤B"}}]}},
        ]
        md = feishu_doc.render_blocks(blocks)
        assert "# 标题一" in md
        assert "正文段落" in md
        assert "1. 步骤A" in md
        assert "2. 步骤B" in md

    def test_render_image_placeholder(self):
        sys.path.insert(0, str(SRC / "solomon" / "pipeline"))
        import feishu_doc
        blocks = [{"block_id": "b1", "block_type": 1, "children": ["b2"]},
                  {"block_id": "b2", "block_type": 27, "image": {"token": "TOK123", "width": 100, "height": 100}}]
        md = feishu_doc.render_blocks(blocks)
        assert "TOK123" in md and "图片" in md


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))