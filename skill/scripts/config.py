# -*- coding: utf-8 -*-
"""llmfill skill - 本地配置读写。

配置存于 ``~/.llmfill/config.json``（权限 600）。
环境变量 ``LLMFILL_BASE_URL`` / ``LLMFILL_API_KEY`` 优先级高于配置文件，
便于 CI 或临时覆盖而不落盘。
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

CONFIG_DIR = Path.home() / ".llmfill"
CONFIG_PATH = CONFIG_DIR / "config.json"
ENDPOINTS_PATH = CONFIG_DIR / "endpoints.json"

DEFAULT_BASE_URL = "https://www.llmfill.com"

_VALID_KEYS = {"base_url", "api_key", "user_id", "default_kb", "created_at"}


class ConfigError(Exception):
    """配置缺失或格式错误。"""


def _tighten_perms(path: Path) -> None:
    """尽力收紧文件权限为 600（Windows 上 chmod 语义有限，静默失败）。"""
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:  # Windows / 受限文件系统
        pass


def load_config(*, require: bool = True) -> dict:
    """读取配置；环境变量覆盖 base_url / api_key。

    require=True 且无 api_key 时抛 ConfigError（提示先运行 config）。
    """
    cfg: dict = {}
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise ConfigError(f"配置文件损坏（{CONFIG_PATH}）：{exc}\n请重新运行 llmfill config") from exc
        if not isinstance(cfg, dict):
            raise ConfigError(f"配置文件格式错误（{CONFIG_PATH}）：顶层应为 JSON 对象")

    env_base = os.environ.get("LLMFILL_BASE_URL")
    env_key = os.environ.get("LLMFILL_API_KEY")
    if env_base:
        cfg["base_url"] = env_base
    if env_key:
        cfg["api_key"] = env_key

    cfg.setdefault("base_url", DEFAULT_BASE_URL)

    if require and not cfg.get("api_key"):
        raise ConfigError(
            "未配置 API Key。请运行 `python scripts/llmfill.py config`，"
            "并前往 https://www.llmfill.com/profile 个人中心「API 密钥」创建令牌后粘贴。"
        )
    return cfg


def save_config(cfg: dict) -> Path:
    """写入配置文件（仅保留白名单字段），权限 600。"""
    clean = {k: cfg.get(k) for k in _VALID_KEYS if cfg.get(k) is not None}
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _tighten_perms(CONFIG_PATH)
    return CONFIG_PATH


def save_endpoints(data: dict) -> Path:
    """discover 命令缓存的接口清单。"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    ENDPOINTS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return ENDPOINTS_PATH
