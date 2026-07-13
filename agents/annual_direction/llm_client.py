"""OpenAI-compatible LLM client (supports 小红书 MaaS / DirectLLM)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]


class LLMError(RuntimeError):
    pass


def llm_config() -> dict[str, Any]:
    load_dotenv(ROOT / ".env")
    api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
    base_url = (
        os.getenv("LLM_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or "https://api.openai.com/v1"
    ).rstrip("/")
    model = os.getenv("LLM_MODEL", "glm-5.1")
    max_tokens = int(os.getenv("LLM_MAX_TOKENS", "4096"))
    temperature = float(os.getenv("LLM_TEMPERATURE", "0.4"))

    default_headers: dict[str, str] = {}
    maas_email = os.getenv("LLM_MAAS_USER_EMAIL")
    maas_app_id = os.getenv("LLM_MAAS_APP_ID")
    if maas_email:
        default_headers["x-maas-user-email"] = maas_email
    if maas_app_id:
        default_headers["x-maas-app-id"] = maas_app_id

    return {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "default_headers": default_headers or None,
    }


def create_client():
    try:
        from openai import OpenAI
    except ImportError as e:
        raise LLMError("请安装 openai: pip install openai") from e

    cfg = llm_config()
    if not cfg["api_key"]:
        raise LLMError(
            "未配置 LLM API Key。请在 .env 中设置 LLM_API_KEY，"
            "以及 LLM_BASE_URL、LLM_MODEL；MaaS 还需 LLM_MAAS_USER_EMAIL、LLM_MAAS_APP_ID。"
        )

    kwargs: dict[str, Any] = {
        "api_key": cfg["api_key"],
        "base_url": cfg["base_url"],
        "timeout": float(os.getenv("LLM_TIMEOUT", "120")),
    }
    if cfg["default_headers"]:
        kwargs["default_headers"] = cfg["default_headers"]
    return OpenAI(**kwargs), cfg


def chat(messages: list[dict[str, str]], *, temperature: float | None = None) -> str:
    client, cfg = create_client()
    resp = client.chat.completions.create(
        model=cfg["model"],
        messages=messages,
        stream=False,
        max_tokens=cfg["max_tokens"],
        temperature=cfg["temperature"] if temperature is None else temperature,
    )
    return resp.choices[0].message.content or ""


def extract_allocation_json(text: str) -> dict[str, Any] | None:
    """Parse last JSON block from model output."""
    import re

    blocks = re.findall(r"```json\s*(\{.*?\})\s*```", text, re.S)
    for raw in reversed(blocks):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            continue
    return None
