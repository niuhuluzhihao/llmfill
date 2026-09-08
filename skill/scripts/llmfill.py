#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""llmfill - LLMFill命令行工具（Agent Skill 主入口）。

把 merge-service 的文档智能填写与知识库管理能力暴露给 agent/终端：
上传 .docx 自动填写并下载结果、建库/传资料（供填写时检索）。

用法（agent 常用）：
    python scripts/llmfill.py config                      # 首次配置（API Key）
    python scripts/llmfill.py whoami                     # 令牌自检 + 余额
    python scripts/llmfill.py discover                   # 拉取接口清单（自动发现）
    python scripts/llmfill.py fill 表单.docx [--kb KB_ID] # AI 填写 -> 下载
    python scripts/llmfill.py kb ls                       # 知识库列表

注：不提供检索（search）命令 -- 检索是服务端填写链路的内部行为，
网关只暴露网页前端在用的接口。

认证：请求头 X-Auth-Token: aif_xxx（个人中心「API 密钥」生成）。
零第三方依赖，Python 3.10+ 标准库实现。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from config import (
    ConfigError,
    DEFAULT_BASE_URL,
    load_config,
    save_config,
    save_endpoints,
)
from api_client import ApiError, download_file, poll_until_done, request_json

# fill/kb 上传的轮询参数：3s 起步指数退避（上限 15s），总超时 30 分钟
POLL_INTERVAL = 3.0
POLL_TIMEOUT = 30 * 60

ANSWER_SOURCES = ("knowledge_base", "internet", "hybrid", "llm_only")

# 处理阶段（status.tasks[].stages 的 key）：进度反馈时提示当前在做什么，
# 解决「长时间卡在 60% 无阶段信息」的体验问题。
STAGE_LABELS = {
    "upload": "上传解析",
    "generate_questions": "生成问题",
    "retrieve_questions": "检索知识库",
    "fill_answers": "填写答案",
    "finalize": "收尾结算",
}
# 视为「进行中」的阶段状态取值（服务端字段，防御式覆盖多种写法）
_ACTIVE_STAGE_STATUS = ("processing", "in_progress", "running", "started")


def _current_stage(state: dict) -> str | None:
    """从批次状态提取当前进行中的阶段名（结构差异防御式，取不到返回 None）。"""
    for task in state.get("tasks") or []:
        stages = task.get("stages")
        if isinstance(stages, dict):
            for name, info in stages.items():
                if isinstance(info, dict) and info.get("status") in _ACTIVE_STAGE_STATUS:
                    return name
        elif isinstance(stages, list):
            for s in stages:
                if isinstance(s, dict) and s.get("status") in _ACTIVE_STAGE_STATUS:
                    return s.get("name") or s.get("stage")
    return None


# ---------------------------------------------------------------------------
# 输出工具
# ---------------------------------------------------------------------------


class Output:
    """人类可读 / --json 双模式输出。"""

    def __init__(self, as_json: bool) -> None:
        self.as_json = as_json

    def result(self, data: dict, *, ok: str = "") -> None:
        """成功输出：--json 打印对象，否则打简短摘要 + 可选提示。"""
        if self.as_json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        elif ok:
            print(ok)

    def error(self, exc: Exception) -> None:
        if self.as_json:
            print(json.dumps({"success": False, "error": {
                "code": getattr(exc, "code", "ERROR"),
                "message": str(exc),
            }}, ensure_ascii=False))
        else:
            print(f"错误：{exc}", file=sys.stderr)
            if isinstance(exc, ApiError) and exc.code in ("INVALID_TOKEN", "NETWORK_ERROR"):
                print("提示：请运行 `python scripts/llmfill.py config` 检查配置。", file=sys.stderr)


def _read_file_bytes(path: str) -> tuple[str, bytes]:
    p = Path(path)
    if not p.is_file():
        raise ApiError("FILE_NOT_FOUND", f"文件不存在：{path}")
    return p.name, p.read_bytes()


def _mk_api(cfg: dict):
    """返回绑定了 base_url/api_key 的请求函数。"""
    return lambda method, path, **kw: request_json(
        cfg["base_url"], cfg["api_key"], method, path, **kw
    )


# ---------------------------------------------------------------------------
# 子命令：config / whoami / discover
# ---------------------------------------------------------------------------


def cmd_config(args, out: Output) -> None:
    """首次配置：只填 API Key（base_url 内置默认，改 JSON/env 可覆盖）。"""
    cfg = load_config(require=False)

    # base_url 不提示用户输入：默认内置，需要改的场景（自建/代理部署）
    # 直接编辑 ~/.llmfill/config.json 的 base_url 字段或用 --base 参数。
    base_url = args.base or cfg.get("base_url") or DEFAULT_BASE_URL
    api_key = args.token or cfg.get("api_key") or ""

    if not args.token:
        # 未显式传 key：交互式可输入或回车保留存量；非交互时要求已有存量
        if sys.stdin.isatty():
            try:
                hint = f"（回车保留 {api_key[:12]}…）" if api_key else ""
                print("  API Key：前往 https://www.llmfill.com/profile 个人中心「API 密钥」")
                print(f"  创建令牌后，粘贴 aif_ 开头的字符串{hint}：")
                entered = input("  API Key: ").strip()
                if entered:
                    api_key = entered
            except (EOFError, KeyboardInterrupt):
                raise ApiError("CONFIG_REQUIRED", "交互输入不可用：请用 --token 参数配置")
        elif not api_key:
            raise ApiError("CONFIG_REQUIRED", "非交互环境请用 --token 参数配置")

    if not api_key:
        raise ApiError("CONFIG_REQUIRED", "缺少 API Key（--token 或交互输入）")

    cfg.update({"base_url": base_url, "api_key": api_key})
    path = save_config(cfg)

    # 自检
    try:
        whoami = request_json(base_url, api_key, "GET", "/v1/account/whoami")
    except ApiError as exc:
        out.result({"saved": str(path), "verified": False, "error": str(exc)},
                   ok=f"已保存到 {path}，但自检失败：{exc}")
        return

    user_id = whoami.get("user_id")
    cfg["user_id"] = user_id
    save_config(cfg)
    balance = whoami.get("balance")
    bal_txt = f"，余额 {balance}" if balance is not None else ""
    identity = _identity_text(whoami)
    out.result(
        {
            "saved": str(path), "verified": True, "user_id": user_id,
            "token_name": whoami.get("token_name"),
            "email_masked": whoami.get("email_masked"),
            "balance": balance,
        },
        ok=f"配置完成并验证通过：{identity}{bal_txt}（已写入 {path}）",
    )


def _identity_text(data: dict) -> str:
    """身份回显文案：账号脱敏邮箱 + 令牌名称 + user_id，帮用户确认令牌归属。"""
    parts = []
    if data.get("email_masked"):
        parts.append(f"账号 {data['email_masked']}")
    if data.get("token_name"):
        parts.append(f"令牌「{data['token_name']}」")
    parts.append(f"user_id={data.get('user_id')}")
    return "，".join(parts)


def cmd_whoami(args, out: Output) -> None:
    cfg = load_config()
    data = request_json(cfg["base_url"], cfg["api_key"], "GET", "/v1/account/whoami")
    out.result(data, ok=(
        f"令牌有效：{_identity_text(data)}"
        + (f"，余额 {data.get('balance')}" if data.get("balance") is not None else "")
    ))


def cmd_discover(args, out: Output) -> None:
    """拉取网关聚合 OpenAPI，打印端点清单并缓存到 ~/.llmfill/endpoints.json。"""
    cfg = load_config()
    spec = request_json(cfg["base_url"], cfg["api_key"], "GET", "/v1/openapi")

    summary = {}
    for path, ops in spec.get("paths", {}).items():
        methods = [m.upper() for m in ops if m in ("get", "post", "put", "delete", "patch")]
        summary[path] = methods

    save_endpoints({"base_url": cfg["base_url"], "paths": summary})
    if out.as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"发现 {len(summary)} 个端点（已缓存到 ~/.llmfill/endpoints.json）：")
        for path, methods in sorted(summary.items()):
            print(f"  {','.join(methods):<18} {path}")


# ---------------------------------------------------------------------------
# 子命令：fill（文档智能填写）
# ---------------------------------------------------------------------------


def _download_batch_results(cfg: dict, batch_id: str, out_dir: Path, *, clean_only: bool = False) -> list[Path]:
    """下载批次结果：默认同时下载标注版（_processed）与纯净版（_clean）。

    返回下载到的文件绝对路径列表（标注版在前、纯净版在后）；clean_only=True
    时只下纯净版。两个版本由网关 Content-Disposition 区分文件名（如
    `xxx_processed.docx` / `xxx_clean.docx`），互不覆盖。
    """
    result_path = f"/v1/documents/{batch_id}/result"
    targets: list[Path] = []
    if not clean_only:
        targets.append(download_file(cfg["base_url"], cfg["api_key"], result_path, out_dir))
    targets.append(download_file(cfg["base_url"], cfg["api_key"], f"{result_path}?clean=true", out_dir))
    return targets


def cmd_fill(args, out: Output) -> None:
    """上传 -> 轮询 -> 下载，端到端填写。"""
    cfg = load_config()
    api = _mk_api(cfg)

    files = [_read_file_bytes(p) for p in args.files]
    if not files:
        raise ApiError("VALIDATION_ERROR", "请至少提供一个 .docx 文件")

    # 预估费用：独立 /estimate 端点（upload 响应不含 cost_estimate）。
    # 仅供展示，实扣以完成后 status 的 tasks[].cost 为准；estimate 失败不阻塞提交。
    estimate = None
    try:
        est_resp = api("POST", "/v1/documents/estimate",
                       files=[("files", n, b) for n, b in files])
        estimate = est_resp.get("total_cost") if isinstance(est_resp, dict) else None
    except ApiError:
        estimate = None

    # 上传（multipart：files[] + answer_source + kb_ids）。
    # 费用链路：余额不足时上游入队前 402 拒单（message 带预估金额与充值
    # 指引）；实扣金额在完成后从 status 的 tasks[].cost 提取（actual_cost）。
    form: dict[str, str] = {"answer_source": args.source}
    if args.kb:
        form["kb_ids"] = json.dumps(args.kb)
    if args.mode:
        form["mode"] = args.mode

    upload_resp = api(
        "POST", "/v1/documents/upload",
        form_fields=form,
        files=[("files", n, b) for n, b in files],
    )
    batch_id = upload_resp.get("batch_id")
    if not batch_id:
        raise ApiError("PARSE_ERROR", f"上传响应缺少 batch_id：{upload_resp}")

    total = upload_resp.get("total_count", len(files))
    est_txt = f"，预估费用约 {estimate} 元（实际以处理结果为准）" if estimate is not None else ""

    if not out.as_json:
        print(f"已提交批次 {batch_id}（{total} 个文件）{est_txt}，开始轮询处理进度…", file=sys.stderr)

    # 轮询批次状态
    def fetch():
        return api("GET", f"/v1/documents/{batch_id}/status")

    def done(state):
        return state.get("status") in ("completed", "partial", "failed")

    def on_tick(ticks, state):
        if not out.as_json and ticks % 3 == 0:  # 每 3 次轮询报一次进度，避免刷屏
            stage = _current_stage(state)
            stage_txt = f"，阶段：{STAGE_LABELS.get(stage, stage)}" if stage else ""
            print(f"  进度 {state.get('progress', 0)}%"
                  f"（{state.get('completed_count', 0)}/{state.get('total_count', '?')} 完成）{stage_txt}")

    state = poll_until_done(
        interval=POLL_INTERVAL, timeout=POLL_TIMEOUT,
        poll_fn=fetch, is_done=done, on_tick=on_tick,
    )

    if state.get("status") == "failed":
        raise ApiError("TASK_FAILED", f"批次处理失败：{state.get('failed_count', '?')} 个文件失败")

    # 实际费用：status 响应的 tasks[].cost 是 finalize 阶段按真实 token
    # 用量算出的实扣金额（与 auth-service 记账同值），预估只作参考。
    actual_cost = round(sum(float(t.get("cost") or 0) for t in state.get("tasks", [])), 6)
    cost_line = f"，实际费用 {actual_cost} 元" if actual_cost else ""

    # 输出目录：未显式指定 --out 时落到第一个模板所在目录（避免结果散落到
    # 调用时的工作目录）；显式指定则原样使用。
    out_dir = Path(args.out) if args.out else Path(args.files[0]).resolve().parent

    if not args.no_download:
        targets = _download_batch_results(cfg, batch_id, out_dir, clean_only=args.clean)
        downloaded = [str(t.resolve()) for t in targets]
        out.result({"batch_id": batch_id, "status": state.get("status"),
                    "cost_estimate": estimate, "actual_cost": actual_cost,
                    "downloaded": downloaded},
                  ok=f"处理完成（{state.get('status')}）{cost_line}，结果已下载：{'、'.join(downloaded)}")
    else:
        out.result({"batch_id": batch_id, "status": state.get("status"),
                    "cost_estimate": estimate, "actual_cost": actual_cost},
                  ok=f"处理完成（{state.get('status')}）{cost_line}，用 fill-download {batch_id} 下载")


def cmd_fill_status(args, out: Output) -> None:
    cfg = load_config()
    api = _mk_api(cfg)
    state = api("GET", f"/v1/documents/{args.batch_id}/status")
    actual_cost = round(sum(float(t.get("cost") or 0) for t in state.get("tasks", [])), 6)
    cost_line = f"，实际费用 {actual_cost} 元" if actual_cost else ""
    out.result(state, ok=(
        f"批次 {args.batch_id}：{state.get('status')}（进度 {state.get('progress', 0)}%，"
        f"完成 {state.get('completed_count', 0)}/{state.get('total_count', '?')}，"
        f"失败 {state.get('failed_count', 0)}{cost_line}）"
    ))


def cmd_fill_download(args, out: Output) -> None:
    cfg = load_config()
    targets = _download_batch_results(cfg, args.batch_id, Path(args.out), clean_only=args.clean)
    downloaded = [str(t.resolve()) for t in targets]
    out.result({"batch_id": args.batch_id, "downloaded": downloaded},
              ok=f"已下载：{'、'.join(downloaded)}")


# ---------------------------------------------------------------------------
# 子命令：kb（知识库管理）
# ---------------------------------------------------------------------------


def cmd_kb(args, out: Output) -> None:
    cfg = load_config()
    api = _mk_api(cfg)

    if args.kb_cmd == "ls":
        data = api("GET", "/v1/knowledge-bases")
        if out.as_json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            kbs = data.get("kbs", [])
            print(f"共 {len(kbs)} 个知识库：")
            for kb in kbs:
                print(f"  {kb.get('kb_id')}  {kb.get('name', '')}"
                      f"（{kb.get('document_count', 0)} 文档）")
        return

    if args.kb_cmd == "create":
        data = api("POST", "/v1/knowledge-bases",
                   json_body={"name": args.name, "description": args.desc or ""})
        out.result(data, ok=f"已创建知识库 {data.get('kb_id')}（{args.name}）")
        return

    if args.kb_cmd == "rm":
        data = api("DELETE", f"/v1/knowledge-bases/{args.kb_id}")
        out.result(data, ok=f"已删除知识库 {args.kb_id}")
        return

    if args.kb_cmd == "docs":
        data = api("GET", f"/v1/knowledge-bases/{args.kb_id}/documents")
        if out.as_json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            docs = data.get("documents", [])
            print(f"知识库 {args.kb_id} 共 {len(docs)} 个文档：")
            for d in docs:
                print(f"  {d.get('document_id', '')[:16]}…  {d.get('filename', '')}"
                      f"（{d.get('parse_status', '?')}）")
        return

    if args.kb_cmd == "upload":
        files = [_read_file_bytes(p) for p in args.files]
        failed: list[str] = []
        uploaded: list[dict] = []
        # 上传前文档快照：用于在列表中识别本次上传的新文档（网关不暴露
        # /tasks 任务查询，入库进度由文档列表 parse_status 派生，与前端一致）
        try:
            before = {
                (d.get("filename"), d.get("document_id"))
                for d in api("GET", f"/v1/knowledge-bases/{args.kb_id}/documents").get("documents", [])
            }
        except ApiError:
            before = set()

        for name, content in files:
            # 单文件失败（含上传被拒/入库失败）不阻塞其余文件，最终汇总退出码
            try:
                api("POST", f"/v1/knowledge-bases/{args.kb_id}/documents",
                    files=[("file", name, content)])
            except ApiError as exc:
                print(f"{name}：上传失败 - {exc.message}", file=sys.stderr)
                failed.append(name)
                continue
            if not out.as_json:
                print(f"{name}：已提交，轮询入库状态…", file=sys.stderr)

            def fetch(kb_id=args.kb_id, fname=name, snap=before):
                docs = api("GET", f"/v1/knowledge-bases/{kb_id}/documents").get("documents", [])
                # 本次上传的文档 = 同名且不在快照里的最新一条
                new_docs = [d for d in docs
                            if d.get("filename") == fname
                            and (d.get("filename"), d.get("document_id")) not in snap]
                return new_docs[0] if new_docs else {"parse_status": "parsing"}

            doc = poll_until_done(
                interval=POLL_INTERVAL, timeout=POLL_TIMEOUT, poll_fn=fetch,
                is_done=lambda d: d.get("parse_status") in ("completed", "failed"),
            )
            if doc.get("parse_status") == "failed":
                print(f"{name}：入库失败（可删除后重传）", file=sys.stderr)
                failed.append(name)
            else:
                uploaded.append({
                    "filename": name,
                    "document_id": doc.get("document_id"),
                    "parse_status": doc.get("parse_status"),
                })
                if not out.as_json:
                    print(f"{name}：入库完成（可检索）")
        if failed:
            raise ApiError("UPLOAD_FAILED", f"{len(failed)} 个文件失败：{'、'.join(failed)}")
        # --json 时同样输出结果（此前 upload 分支在 JSON 模式下无任何输出，
        # 导致 agent 只能再调 kb docs 确认入库状态）
        out.result(
            {"kb_id": args.kb_id, "uploaded": uploaded, "failed": failed},
            ok=f"已上传 {len(uploaded)} 个文件到知识库 {args.kb_id}（入库完成，可检索）",
        )
        return

    raise ApiError("VALIDATION_ERROR", f"未知 kb 子命令：{args.kb_cmd}")


# ---------------------------------------------------------------------------
# argparse 装配
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llmfill",
        description="LLMFill：Word 智能填写 + 知识库管理（远程服务调用）。"
                    " 任意位置加 --json 输出机器可读 JSON（agent 友好）。",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # config
    p = sub.add_parser("config", help="首次配置（API Key + Base URL）并自检")
    p.add_argument("--base", help="覆盖服务地址（默认内置 www.llmfill.com；也可直接改 ~/.llmfill/config.json）")
    p.add_argument("--token", help="API Key（aif_ 开头）")
    p.set_defaults(func=cmd_config)

    # whoami
    p = sub.add_parser("whoami", help="令牌自检与余额查询")
    p.set_defaults(func=cmd_whoami)

    # discover
    p = sub.add_parser("discover", help="拉取接口清单（agent 自动发现）")
    p.set_defaults(func=cmd_discover)

    # fill
    p = sub.add_parser("fill", help="上传 .docx 智能填写并下载结果")
    p.add_argument("files", nargs="+", help=".docx 文件路径（可多个）")
    p.add_argument("--source", default="hybrid", choices=ANSWER_SOURCES,
                   help="答案来源（默认 hybrid）")
    p.add_argument("--kb", action="append", help="挂载的知识库 ID（可多次）")
    p.add_argument("--mode", default="fast", choices=("fast", "economy"),
                   help="处理模式（默认 fast）")
    p.add_argument("--clean", action="store_true", help="只下载纯净版（默认同时下载标注版+纯净版）")
    p.add_argument("--no-download", action="store_true", help="完成后不自动下载")
    p.add_argument("--out", default=None, help="下载目录（默认：第一个模板所在目录）")
    p.set_defaults(func=cmd_fill)

    # fill-status / fill-download
    p = sub.add_parser("fill-status", help="查询批次处理状态")
    p.add_argument("batch_id")
    p.set_defaults(func=cmd_fill_status)

    p = sub.add_parser("fill-download", help="下载批次结果")
    p.add_argument("batch_id")
    p.add_argument("--clean", action="store_true", help="只下载纯净版（默认同时下载标注版+纯净版）")
    p.add_argument("--out", default=".", help="下载目录（默认当前目录）")
    p.set_defaults(func=cmd_fill_download)

    # kb
    p = sub.add_parser("kb", help="知识库管理")
    kb_sub = p.add_subparsers(dest="kb_cmd", required=True)
    kb_sub.add_parser("ls", help="列出知识库")
    q = kb_sub.add_parser("create", help="创建知识库")
    q.add_argument("--name", required=True)
    q.add_argument("--desc", default="")
    q = kb_sub.add_parser("rm", help="删除知识库")
    q.add_argument("kb_id")
    q = kb_sub.add_parser("docs", help="列出知识库下文档")
    q.add_argument("kb_id")
    q = kb_sub.add_parser("upload", help="上传文件到知识库（异步，自动轮询）")
    q.add_argument("kb_id")
    q.add_argument("files", nargs="+")
    p.set_defaults(func=cmd_kb)

    return parser


def main(argv: list[str] | None = None) -> int:
    # --json 是全局开关，手动从 argv 剥离后单独处理，使其可放在任意位置
    # （whoami --json 与 --json whoami 等价）。argparse 的全局参数只能放
    # 在子命令前，会拒绝最自然的 `whoami --json` 写法。
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]

    parser = build_parser()
    args = parser.parse_args(argv)
    out = Output(as_json)
    try:
        args.func(args, out)
        return 0
    except ConfigError as exc:
        out.error(exc)
        return 2
    except ApiError as exc:
        out.error(exc)
        return 1
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
