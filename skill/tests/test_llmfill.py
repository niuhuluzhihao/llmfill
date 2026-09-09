# -*- coding: utf-8 -*-
"""llmfill skill 脚本层测试：配置读写 / 错误解析 / multipart / 轮询 / CLI。

不依赖真实服务：api_client 的网络函数用 monkeypatch 替换。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import api_client  # noqa: E402
import config as config_mod  # noqa: E402
from api_client import ApiError, _encode_multipart, _filename_from_disposition, _parse_error_body, _safe_filename, poll_until_done  # noqa: E402


class _FakeStdin:
    """模拟非交互 stdin（isatty=False），用于测试需确认/拒绝的分支。"""

    def isatty(self):
        return False


# ---------------------------------------------------------------------------
# config.py
# ---------------------------------------------------------------------------


class TestConfig:
    def test_save_load_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.json")
        monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
        config_mod.save_config({"base_url": "https://www.llmfill.com", "api_key": "aif_x"})
        cfg = config_mod.load_config()
        assert cfg["base_url"] == "https://www.llmfill.com"
        assert cfg["api_key"] == "aif_x"

    def test_env_overrides_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.json")
        config_mod.save_config({"api_key": "aif_from_file", "base_url": "https://www.llmfill.com"})
        monkeypatch.setenv("LLMFILL_API_KEY", "aif_from_env")
        cfg = config_mod.load_config()
        assert cfg["api_key"] == "aif_from_env"
        assert cfg["base_url"] == "https://www.llmfill.com"

    def test_require_without_key_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "nonexistent.json")
        monkeypatch.delenv("LLMFILL_API_KEY", raising=False)
        with pytest.raises(config_mod.ConfigError, match="未配置"):
            config_mod.load_config()

    def test_corrupted_file_raises(self, tmp_path, monkeypatch):
        p = tmp_path / "config.json"
        p.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(config_mod, "CONFIG_PATH", p)
        monkeypatch.delenv("LLMFILL_API_KEY", raising=False)
        with pytest.raises(config_mod.ConfigError, match="损坏"):
            config_mod.load_config()

    def test_default_base_url(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "nonexistent.json")
        monkeypatch.setenv("LLMFILL_API_KEY", "aif_x")
        cfg = config_mod.load_config()
        assert cfg["base_url"] == config_mod.DEFAULT_BASE_URL

    # ---- validate_base_url（T09：未校验外发目的地）----

    def test_validate_rejects_http(self):
        with pytest.raises(config_mod.ConfigError, match="HTTPS"):
            config_mod.validate_base_url("http://www.llmfill.com")

    def test_validate_rejects_userinfo(self):
        with pytest.raises(config_mod.ConfigError, match="用户名/密码"):
            config_mod.validate_base_url("https://user:pass@www.llmfill.com")

    def test_validate_rejects_fragment(self):
        with pytest.raises(config_mod.ConfigError, match="锚点"):
            config_mod.validate_base_url("https://www.llmfill.com/#x")

    def test_validate_rejects_query(self):
        with pytest.raises(config_mod.ConfigError, match="查询串"):
            config_mod.validate_base_url("https://www.llmfill.com/?redirect=evil")

    def test_validate_rejects_path(self):
        with pytest.raises(config_mod.ConfigError, match="origin"):
            config_mod.validate_base_url("https://www.llmfill.com/evil")

    def test_validate_normalizes(self):
        assert config_mod.validate_base_url("https://www.llmfill.com/") == "https://www.llmfill.com"
        assert config_mod.validate_base_url("https://www.llmfill.com:8443/") == "https://www.llmfill.com:8443"

    def test_load_config_rejects_bad_env_base(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "none.json")
        monkeypatch.setenv("LLMFILL_API_KEY", "aif_x")
        monkeypatch.setenv("LLMFILL_BASE_URL", "http://evil.example.com")
        with pytest.raises(config_mod.ConfigError, match="HTTPS"):
            config_mod.load_config()

    def test_validate_allows_http_when_insecure(self):
        assert config_mod.validate_base_url(
            "http://127.0.0.1:8000", allow_insecure=True
        ) == "http://127.0.0.1:8000"

    def test_validate_still_rejects_http_by_default(self):
        with pytest.raises(config_mod.ConfigError, match="HTTPS"):
            config_mod.validate_base_url("http://127.0.0.1:8000")

    # ---- ensure_allowed_base（fail-closed 批准模型）----

    def test_ensure_allowed_base_default_ok(self):
        config_mod.ensure_allowed_base({"base_url": "https://www.llmfill.com"})
        config_mod.ensure_allowed_base({"base_url": "https://llmfill.com"})

    def test_ensure_allowed_base_custom_unapproved_raises(self):
        with pytest.raises(config_mod.ConfigError, match="未获批准"):
            config_mod.ensure_allowed_base({"base_url": "https://private.example"})

    def test_ensure_allowed_base_custom_approved_ok(self):
        config_mod.ensure_allowed_base({
            "base_url": "https://private.example",
            "approved_origins": ["https://private.example"],
        })

    def test_load_config_custom_env_unapproved_raises(self, tmp_path, monkeypatch):
        """LLMFILL_BASE_URL 单独不能授权自定义 origin（fail-closed）。"""
        monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "none.json")
        monkeypatch.setenv("LLMFILL_API_KEY", "aif_x")
        monkeypatch.setenv("LLMFILL_BASE_URL", "https://private.example")
        with pytest.raises(config_mod.ConfigError, match="未获批准"):
            config_mod.load_config()

    def test_load_config_custom_approved_ok(self, tmp_path, monkeypatch):
        """approved_origins 已批准的自定义 origin 可正常 load。"""
        monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "c.json")
        monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
        config_mod.save_config({
            "base_url": "https://private.example",
            "api_key": "aif_x",
            "approved_origins": ["https://private.example"],
        })
        cfg = config_mod.load_config()
        assert cfg["base_url"] == "https://private.example"


# ---------------------------------------------------------------------------
# api_client.py：错误解析
# ---------------------------------------------------------------------------


class TestErrorParsing:
    def test_unified_gateway_error(self):
        raw = json.dumps({"success": False, "error": {
            "code": "INVALID_TOKEN", "message": "API 令牌无效", "request_id": "req_x",
        }}).encode("utf-8")
        err = _parse_error_body(raw, 401)
        assert err.code == "INVALID_TOKEN"
        assert err.message == "API 令牌无效"
        assert err.status == 401

    def test_legacy_detail_error(self):
        raw = json.dumps({"detail": "批次不存在: b_x"}).encode("utf-8")
        err = _parse_error_body(raw, 404)
        assert err.code == "HTTP_ERROR"
        assert err.message == "批次不存在: b_x"

    def test_legacy_401_maps_to_invalid_token(self):
        raw = json.dumps({"detail": "凭证无效"}).encode("utf-8")
        err = _parse_error_body(raw, 401)
        assert err.code == "INVALID_TOKEN"

    def test_non_json_body(self):
        err = _parse_error_body(b"<html>502</html>", 502)
        assert err.code == "HTTP_ERROR"
        assert "502" in err.message


# ---------------------------------------------------------------------------
# api_client.py：multipart 与文件名
# ---------------------------------------------------------------------------


class TestMultipart:
    def test_encode_fields_and_file(self):
        body = _encode_multipart(
            "BOUNDARY",
            {"mode": "fast", "kb_ids": '["kb-1"]'},
            [("files", "报告.docx", b"docx-bytes")],
        )
        text = body.decode("utf-8")
        assert 'name="mode"' in text and "fast" in text
        assert 'name="kb_ids"' in text and "kb-1" in text
        assert 'name="files"' in text
        # filename= 直接包含 UTF-8 中文名（服务端 latin-1 解码后 repair 还原）
        assert 'filename="报告.docx"' in text
        # 同时携带 RFC 5987 filename*= 作为兼容
        assert "filename*=UTF-8''" in text
        assert "%E6%8A%A5%E5%91%8A.docx" in text  # "报告" 的 UTF-8 URL 编码
        assert body.endswith(b"--BOUNDARY--\r\n")

    def test_filename_quoting(self):
        body = _encode_multipart("B", {}, [("file", 'we"ird\nname.txt', b"x")])
        text = body.decode("utf-8")
        assert '"' not in text.split('filename="')[1].split('"')[0]

    def test_disposition_plain(self):
        assert _filename_from_disposition('attachment; filename="result.zip"') == "result.zip"

    def test_disposition_utf8(self):
        assert _filename_from_disposition(
            "attachment; filename*=UTF-8''%E6%8A%A5%E5%91%8A.docx"
        ) == "报告.docx"

    def test_disposition_missing(self):
        assert _filename_from_disposition("") is None


class TestSafeFilename:
    """_safe_filename：服务端文件名视为不可信输入，防路径穿越。"""

    def test_plain(self):
        assert _safe_filename("result.docx") == "result.docx"

    def test_posix_traversal(self):
        assert _safe_filename("../../etc/passwd") == "passwd"

    def test_absolute_posix(self):
        assert _safe_filename("/etc/passwd") == "passwd"

    def test_windows_traversal(self):
        assert _safe_filename("..\\..\\system32\\x.exe") == "x.exe"

    def test_windows_drive(self):
        assert _safe_filename("C:\\Windows\\System32\\cmd.exe") == "cmd.exe"

    def test_mixed_separators(self):
        assert _safe_filename("../a\\b/结果.docx") == "结果.docx"

    def test_dotdot_only(self):
        assert _safe_filename("..") == "download.bin"

    def test_empty(self):
        assert _safe_filename("") == "download.bin"

    def test_control_chars_stripped(self):
        assert _safe_filename("a\r\nb\x00c") == "abc"


class TestRedirectHandler:
    """_SameOriginRedirectHandler：仅同源重定向，防 X-Auth-Token 跨域泄露。"""

    def _req(self):
        return urllib.request.Request(
            "https://www.llmfill.com/v1/x", headers={"X-Auth-Token": "aif_x"}
        )

    def test_same_origin_allowed(self):
        h = api_client._SameOriginRedirectHandler()
        new = h.redirect_request(
            self._req(), None, 302, "Found", {}, "https://www.llmfill.com/v1/y"
        )
        assert new.full_url == "https://www.llmfill.com/v1/y"

    def test_cross_origin_rejected(self):
        h = api_client._SameOriginRedirectHandler()
        with pytest.raises(urllib.error.HTTPError):
            h.redirect_request(
                self._req(), None, 302, "Found", {}, "https://evil.com/v1/x"
            )

    def test_https_downgrade_rejected(self):
        h = api_client._SameOriginRedirectHandler()
        with pytest.raises(urllib.error.HTTPError):
            h.redirect_request(
                self._req(), None, 301, "Moved", {}, "http://www.llmfill.com/v1/x"
            )

    def test_port_change_rejected(self):
        h = api_client._SameOriginRedirectHandler()
        with pytest.raises(urllib.error.HTTPError):
            h.redirect_request(
                self._req(), None, 302, "Found", {}, "https://www.llmfill.com:8443/v1/x"
            )


# ---------------------------------------------------------------------------
# api_client.py：轮询
# ---------------------------------------------------------------------------


class TestPolling:
    def test_poll_until_done(self):
        states = [{"status": "processing"}, {"status": "processing"}, {"status": "completed"}]
        calls = iter(states)
        state = poll_until_done(
            interval=0, timeout=5,
            poll_fn=lambda: next(calls),
            is_done=lambda s: s["status"] == "completed",
        )
        assert state["status"] == "completed"

    def test_poll_timeout(self):
        with pytest.raises(ApiError, match="超时"):
            poll_until_done(
                interval=0, timeout=0.01,
                poll_fn=lambda: {"status": "processing"},
                is_done=lambda s: False,
            )


# ---------------------------------------------------------------------------
# api_client.py：request_json 错误路径（真实 urllib，坏 URL）
# ---------------------------------------------------------------------------


class TestDownloadResults:
    """fill 默认同时下载标注版 + 纯净版；--clean 只下纯净版。"""

    def test_default_downloads_both_versions(self, tmp_path, monkeypatch):
        import llmfill

        calls = []

        def fake_download(base, key, path, out_dir, **kw):
            calls.append(path)
            suffix = "_clean.docx" if "clean=true" in path else "_processed.docx"
            return Path(out_dir) / f"表单{suffix}"

        monkeypatch.setattr(llmfill, "download_file", fake_download)
        cfg = {"base_url": "https://x", "api_key": "aif_x"}
        targets = llmfill._download_batch_results(cfg, "b_1", Path(tmp_path))
        assert [t.name for t in targets] == ["表单_processed.docx", "表单_clean.docx"]
        assert calls == [
            "/v1/documents/b_1/result",
            "/v1/documents/b_1/result?clean=true",
        ]

    def test_clean_only_skips_annotated(self, tmp_path, monkeypatch):
        import llmfill

        calls = []

        def fake_download(base, key, path, out_dir, **kw):
            calls.append(path)
            return Path(out_dir) / "表单_clean.docx"

        monkeypatch.setattr(llmfill, "download_file", fake_download)
        cfg = {"base_url": "https://x", "api_key": "aif_x"}
        targets = llmfill._download_batch_results(cfg, "b_1", Path(tmp_path), clean_only=True)
        assert [t.name for t in targets] == ["表单_clean.docx"]
        assert calls == ["/v1/documents/b_1/result?clean=true"]


class TestRequestJsonNetwork:
    def test_network_error(self):
        with pytest.raises(ApiError) as ei:
            api_client.request_json(
                "http://127.0.0.1:1", "aif_x", "GET", "/v1/knowledge-bases", timeout=2
            )
        assert ei.value.code == "NETWORK_ERROR"


# ---------------------------------------------------------------------------
# CLI：--json 输出与退出码（mock 网络）
# ---------------------------------------------------------------------------


class TestCli:
    def _run(self, argv, monkeypatch, responses=None):
        import llmfill

        # mock request_json：按 (method, path) 返回预设响应
        def fake_request(base, key, method, path, **kw):
            for (m, p), resp in (responses or {}).items():
                if method == m and path == p:
                    if isinstance(resp, ApiError):
                        raise resp
                    return resp
            raise ApiError("NOT_MOCKED", f"{method} {path} 未 mock")

        monkeypatch.setattr(llmfill, "request_json", fake_request)
        return llmfill.main(argv)

    def test_whoami_json(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "c.json")
        monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
        monkeypatch.setenv("LLMFILL_API_KEY", "aif_x")
        rc = self._run(["--json", "whoami"], monkeypatch, responses={
            ("GET", "/v1/account/whoami"): {"user_id": "u-1", "token_valid": True, "balance": 5.5},
        })
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["user_id"] == "u-1"
        assert out["balance"] == 5.5

    def test_fill_status_json(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "c.json")
        monkeypatch.setenv("LLMFILL_API_KEY", "aif_x")
        rc = self._run(["--json", "fill-status", "b_1"], monkeypatch, responses={
            ("GET", "/v1/documents/b_1/status"): {
                "batch_id": "b_1", "status": "completed", "progress": 100,
                "completed_count": 2, "total_count": 2, "failed_count": 0,
            },
        })
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["status"] == "completed"

    def test_search_command_removed(self, tmp_path, monkeypatch):
        """search 子命令已移除（纯后端接口不暴露），argparse 报无效命令。"""
        monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "c.json")
        monkeypatch.setenv("LLMFILL_API_KEY", "aif_x")
        import llmfill
        with pytest.raises(SystemExit):
            llmfill.main(["search", "--kb", "k", "--query", "q"])

    def test_kb_upload_polls_documents_parse_status(self, tmp_path, monkeypatch, capsys):
        """kb upload 通过文档列表 parse_status 轮询（网关不暴露 /tasks）。"""
        monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "c.json")
        monkeypatch.setenv("LLMFILL_API_KEY", "aif_x")
        import llmfill

        # 上传前列表为空；上传后文档出现，前两次 parsing、第三次 completed
        doc_lists = [
            {"documents": []},  # 上传前快照
            {"documents": [{"document_id": "d1", "filename": "手册.md", "parse_status": "parsing"}]},
            {"documents": [{"document_id": "d1", "filename": "手册.md", "parse_status": "parsing"}]},
            {"documents": [{"document_id": "d1", "filename": "手册.md", "parse_status": "completed"}]},
        ]
        calls = iter(doc_lists)

        def fake_request(base, key, method, path, **kw):
            if method == "GET" and path.endswith("/documents"):
                return next(calls)
            if method == "POST":
                return {"task_id": "t-1", "status": "pending", "message": "Task accepted"}
            raise ApiError("NOT_MOCKED", f"{method} {path} 未 mock")

        monkeypatch.setattr(llmfill, "request_json", fake_request)
        f = tmp_path / "手册.md"
        f.write_text("# x", encoding="utf-8")
        rc = llmfill.main(["kb", "upload", "kb-1", str(f)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "入库完成" in out

    def test_kb_upload_json_output(self, tmp_path, monkeypatch, capsys):
        """kb upload --json 应输出机器可读结果（修复：此前 JSON 模式无任何输出）。"""
        monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "c.json")
        monkeypatch.setenv("LLMFILL_API_KEY", "aif_x")
        import llmfill

        doc_lists = iter([
            {"documents": []},
            {"documents": [{"document_id": "d1", "filename": "手册.md", "parse_status": "completed"}]},
        ])

        def fake_request(base, key, method, path, **kw):
            if method == "GET" and path.endswith("/documents"):
                return next(doc_lists)
            if method == "POST":
                return {"task_id": "t-1", "status": "pending"}
            raise ApiError("NOT_MOCKED", f"{method} {path} 未 mock")

        monkeypatch.setattr(llmfill, "request_json", fake_request)
        f = tmp_path / "手册.md"
        f.write_text("# x", encoding="utf-8")
        rc = llmfill.main(["--json", "kb", "upload", "kb-1", str(f)])
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["kb_id"] == "kb-1"
        assert out["uploaded"][0]["document_id"] == "d1"
        assert out["uploaded"][0]["parse_status"] == "completed"
        assert out["failed"] == []

    def test_kb_upload_failed_doc_reported(self, tmp_path, monkeypatch, capsys):
        """入库 parse_status=failed 时计入失败并汇总非零退出。"""
        monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "c.json")
        monkeypatch.setenv("LLMFILL_API_KEY", "aif_x")
        import llmfill

        doc_lists = iter([
            {"documents": []},
            {"documents": [{"document_id": "d1", "filename": "坏.pdf", "parse_status": "failed"}]},
        ])

        def fake_request(base, key, method, path, **kw):
            if method == "GET" and path.endswith("/documents"):
                return next(doc_lists)
            if method == "POST":
                return {"task_id": "t-1", "status": "pending"}
            raise ApiError("NOT_MOCKED", f"{method} {path} 未 mock")

        monkeypatch.setattr(llmfill, "request_json", fake_request)
        f = tmp_path / "坏.pdf"
        f.write_bytes(b"%PDF-fake")
        rc = llmfill.main(["kb", "upload", "kb-1", str(f)])
        capsys.readouterr()
        assert rc == 1

    def test_api_error_exit_code(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "c.json")
        monkeypatch.setenv("LLMFILL_API_KEY", "aif_x")
        rc = self._run(["whoami"], monkeypatch, responses={
            ("GET", "/v1/account/whoami"): ApiError("INVALID_TOKEN", "令牌无效", 401),
        })
        assert rc == 1

    def test_missing_config_exit_code(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "none.json")
        monkeypatch.delenv("LLMFILL_API_KEY", raising=False)
        import llmfill
        rc = llmfill.main(["whoami"])
        assert rc == 2

    def test_kb_ls_json(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "c.json")
        monkeypatch.setenv("LLMFILL_API_KEY", "aif_x")
        rc = self._run(["--json", "kb", "ls"], monkeypatch, responses={
            ("GET", "/v1/knowledge-bases"): {
                "total": 1, "kbs": [{"kb_id": "kb-1", "name": "资料库", "document_count": 3}],
            },
        })
        out = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert out["kbs"][0]["kb_id"] == "kb-1"

    def test_kb_rm_requires_yes_noninteractive(self, tmp_path, monkeypatch, capsys):
        """非交互环境 kb rm 不加 --yes 应报 CONFIRM_REQUIRED，且不实际删除。"""
        monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "c.json")
        monkeypatch.setenv("LLMFILL_API_KEY", "aif_x")
        import llmfill

        calls = []

        def fake_request(base, key, method, path, **kw):
            calls.append((method, path))
            return {"success": True}

        monkeypatch.setattr(llmfill, "request_json", fake_request)
        monkeypatch.setattr(sys, "stdin", _FakeStdin())
        rc = llmfill.main(["kb", "rm", "kb-1"])
        capsys.readouterr()
        assert rc == 1
        assert calls == []  # 未调用 DELETE

    def test_kb_rm_with_yes_deletes(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "c.json")
        monkeypatch.setenv("LLMFILL_API_KEY", "aif_x")
        import llmfill

        calls = []

        def fake_request(base, key, method, path, **kw):
            calls.append((method, path))
            return {"success": True}

        monkeypatch.setattr(llmfill, "request_json", fake_request)
        monkeypatch.setattr(sys, "stdin", _FakeStdin())
        rc = llmfill.main(["kb", "rm", "kb-1", "--yes"])
        assert rc == 0
        assert calls == [("DELETE", "/v1/knowledge-bases/kb-1")]

    def test_config_custom_base_requires_approval_noninteractive(self, tmp_path, monkeypatch, capsys):
        """非交互环境 config --base 自定义地址、不加 --allow-custom，应 fail-closed。"""
        monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "c.json")
        monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
        monkeypatch.delenv("LLMFILL_API_KEY", raising=False)
        import llmfill

        monkeypatch.setattr(sys, "stdin", _FakeStdin())
        rc = llmfill.main(["config", "--base", "https://private.example", "--token", "aif_x"])
        capsys.readouterr()
        assert rc == 1

    def test_config_custom_base_allow_custom_persists(self, tmp_path, monkeypatch, capsys):
        """--allow-custom 批准自定义地址并持久化 approved_origins。"""
        monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "c.json")
        monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
        monkeypatch.delenv("LLMFILL_API_KEY", raising=False)
        import llmfill

        def fake_request(base, key, method, path, **kw):
            return {"user_id": "u-1", "balance": 1.0}

        monkeypatch.setattr(llmfill, "request_json", fake_request)
        monkeypatch.setattr(sys, "stdin", _FakeStdin())
        rc = llmfill.main([
            "config", "--base", "https://private.example",
            "--token", "aif_x", "--allow-custom",
        ])
        capsys.readouterr()
        assert rc == 0
        cfg = config_mod.load_config()
        assert cfg["base_url"] == "https://private.example"
        assert "https://private.example" in cfg["approved_origins"]
