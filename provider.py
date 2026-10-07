"""Provider adapter with pooled connections and credential-free diagnostics."""

import json
import time
from typing import Any
from urllib.parse import urlparse

import httpx


class ProviderError(Exception):
    def __init__(self, message: str, *, uncertain=False, retryable=False, known_zero=False, diagnostics=None):
        super().__init__(message)
        self.uncertain, self.retryable, self.known_zero = uncertain, retryable, known_zero
        self.diagnostics = diagnostics or {}


class ProviderClient:
    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(90, connect=15),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
        )

    def close(self):
        self.client.close()

    def __call__(
        self, settings: dict[str, Any], messages: list[dict[str, str]]
    ) -> tuple[object, dict[str, Any]]:
        started = time.monotonic()
        payload = {
            "model": settings["model"],
            "messages": messages,
            "max_tokens": settings["max_output_tokens"],
            "response_format": {"type": "json_object"},
        }
        # These fields are provider-specific; do not send them to arbitrary compatible endpoints.
        model = settings["model"].lower()
        supported_family = any(
            model == family or model.startswith(family + "-") for family in ("glm-5.2", "glm-5.3")
        )
        if urlparse(settings["base_url"]).hostname == "open.bigmodel.cn" and supported_family:
            payload["reasoning_effort"] = settings.get("reasoning_effort", "low")

        def diag(outcome):
            return {"outcome": outcome, "duration_ms": round((time.monotonic() - started) * 1000)}

        try:
            response = self.client.post(
                settings["base_url"] + "/chat/completions",
                headers={"Authorization": "Bearer " + settings["api_key"]},
                json=payload,
            )
        except httpx.ConnectError:
            raise ProviderError(
                "无法连接模型服务，请检查地址或网络",
                retryable=True,
                known_zero=True,
                diagnostics=diag("connection_error"),
            ) from None
        except httpx.TimeoutException:
            raise ProviderError(
                "请求超时，是否计费未知；请核对服务商账单", uncertain=True, diagnostics=diag("timeout")
            ) from None
        except httpx.HTTPError:
            raise ProviderError(
                "连接中断，是否计费未知；请核对服务商账单",
                uncertain=True,
                diagnostics=diag("connection_error"),
            ) from None
        if response.status_code in (401, 403):
            raise ProviderError(
                "模型服务拒绝授权，请检查 API 密钥与权限", known_zero=True, diagnostics=diag("http_error")
            )
        if response.status_code >= 400 or response.is_redirect:
            raise ProviderError(
                f"模型服务返回 HTTP {response.status_code}，未自动重试",
                uncertain=True,
                diagnostics=diag("http_error"),
            )
        try:
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("Response envelope must be an object")
            usage = body.get("usage") or {}
            if not isinstance(usage, dict):
                usage = {}
            choice = body["choices"][0]
            if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
                raise ValueError("Response choice and message must be objects")
            finish = choice.get("finish_reason")
            diagnostics = diag("received")
            # Whitelist, never retain raw output, thought text or untrusted arbitrary metadata.
            diagnostics["finish_reason"] = (
                finish
                if finish
                in (
                    "stop",
                    "length",
                    "tool_calls",
                    "sensitive",
                    "network_error",
                    "model_context_window_exceeded",
                )
                else "unknown"
            )
            content = choice["message"].get("content")
            try:
                result = json.loads(content)
                diagnostics["json_valid"] = True
            except (ValueError, TypeError):
                result = None
                diagnostics["json_valid"] = False
            return result, {**usage, "_diagnostics": diagnostics}
        except (ValueError, KeyError, IndexError, TypeError):
            raise ProviderError(
                "模型响应结构无法识别，用量待核算", uncertain=True, diagnostics=diag("response_error")
            ) from None


def provider_request(
    settings: dict[str, Any], messages: list[dict[str, str]]
) -> tuple[object, dict[str, Any]]:
    provider = ProviderClient()
    try:
        return provider(settings, messages)
    finally:
        provider.close()
