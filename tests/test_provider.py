"""Provider boundary tests: all HTTP is intercepted by MockTransport."""

import json

import httpx
import pytest

from provider import ProviderClient, ProviderError

SECRET = "synthetic-secret-never-display"
SOURCE = "synthetic-private-source-never-display"


def settings(**changes):
    return (
        dict(
            base_url="https://example.invalid/v1",
            model="generic-model",
            api_key=SECRET,
            max_output_tokens=2000,
            reasoning_effort="low",
        )
        | changes
    )


def envelope(content='{"opinions": []}', finish="stop", **changes):
    return (
        dict(
            choices=[dict(finish_reason=finish, message=dict(content=content, reasoning_content=SOURCE))],
            usage=dict(prompt_tokens=23, completion_tokens=17),
        )
        | changes
    )


def invoke(handler, config=None):
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return ProviderClient(client)(config or settings(), [{"role": "user", "content": SOURCE}])


def assert_sanitized(error):
    rendered = str(error) + json.dumps(error.diagnostics)
    assert SECRET not in rendered
    assert SOURCE not in rendered
    assert set(error.diagnostics) == {"outcome", "duration_ms"}
    assert error.diagnostics["duration_ms"] >= 0


def test_success_json_diagnostics_and_request_contract():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=envelope())

    result, usage = invoke(handler)
    assert len(requests) == 1
    assert requests[0].url == "https://example.invalid/v1/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer " + SECRET
    payload = json.loads(requests[0].content)
    assert payload == dict(
        model="generic-model",
        messages=[dict(role="user", content=SOURCE)],
        max_tokens=2000,
        response_format=dict(type="json_object"),
    )
    assert result == {"opinions": []}
    assert usage["prompt_tokens"] == 23
    assert usage["completion_tokens"] == 17
    assert usage["_diagnostics"]["json_valid"] is True
    assert usage["_diagnostics"]["finish_reason"] == "stop"
    assert usage["_diagnostics"]["outcome"] == "received"
    assert SECRET not in json.dumps(usage["_diagnostics"])
    assert SOURCE not in json.dumps(usage["_diagnostics"])


@pytest.mark.parametrize("content", ["{incomplete", None, {"wrong": "type"}, "```json\n{}\n```"])
def test_invalid_json_retains_usage_for_budget_and_reports_failure(content):
    result, usage = invoke(lambda _: httpx.Response(200, json=envelope(content)))
    assert result is None
    assert usage["_diagnostics"]["json_valid"] is False
    assert usage["prompt_tokens"] == 23


@pytest.mark.parametrize("content,json_valid", [('{"opinions": []}', True), ('{"opinions":', False)])
def test_length_finish_is_preserved_even_when_json_parses(content, json_valid):
    _, usage = invoke(lambda _: httpx.Response(200, json=envelope(content, finish="length")))
    assert usage["_diagnostics"]["finish_reason"] == "length"
    assert usage["_diagnostics"]["json_valid"] is json_valid


def test_untrusted_finish_reason_does_not_leak_model_text():
    _, usage = invoke(lambda _: httpx.Response(200, json=envelope(finish=SOURCE + SECRET)))
    assert usage["_diagnostics"]["finish_reason"] == "unknown"
    assert SOURCE not in json.dumps(usage["_diagnostics"])


@pytest.mark.parametrize(
    "host,model,expected",
    [
        ("open.bigmodel.cn", "glm-5.2", True),
        ("open.bigmodel.cn", "GLM-5.3", True),
        ("open.bigmodel.cn", "glm-5.2-turbo", True),
        ("open.bigmodel.cn", "glm-4.7", False),
        ("open.bigmodel.cn", "other-model", False),
        ("example.invalid", "glm-5.2", False),
        ("open.bigmodel.cn.example.invalid", "glm-5.3", False),
        ("open.bigmodel.cn", "glm-5.20", False),
    ],
)
def test_reasoning_effort_only_on_supported_host_and_model_family(host, model, expected):
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json=envelope())

    invoke(handler, settings(base_url=f"https://{host}/api/paas/v4", model=model, reasoning_effort="high"))
    assert ("reasoning_effort" in payloads[0]) is expected
    if expected:
        assert payloads[0]["reasoning_effort"] == "high"
    assert "thinking" not in payloads[0]


def test_supported_model_reasoning_defaults_low():
    config = settings(base_url="https://open.bigmodel.cn/api/paas/v4", model="glm-5.3")
    del config["reasoning_effort"]

    def handler(request):
        assert json.loads(request.content)["reasoning_effort"] == "low"
        return httpx.Response(200, json=envelope())

    invoke(handler, config)


@pytest.mark.parametrize(
    "status,known_zero", [(401, True), (403, True), (429, False), (500, False), (307, False)]
)
def test_http_failures_use_conservative_billing_without_response_secrets(status, known_zero):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, text=SOURCE + SECRET, headers={"location": "https://elsewhere.invalid"})

    with pytest.raises(ProviderError) as caught:
        invoke(handler)
    assert len(requests) == 1
    error = caught.value
    assert error.known_zero is known_zero
    assert error.uncertain is not known_zero
    assert error.retryable is False
    assert error.diagnostics["outcome"] == "http_error"
    assert_sanitized(error)


@pytest.mark.parametrize(
    "error_class,known_zero,outcome",
    [
        (httpx.ConnectError, True, "connection_error"),
        (httpx.ReadTimeout, False, "timeout"),
        (httpx.ConnectTimeout, False, "timeout"),
        (httpx.ReadError, False, "connection_error"),
        (httpx.RemoteProtocolError, False, "connection_error"),
    ],
)
def test_transport_failures_preserve_billing_uncertainty_without_secret_exception_text(
    error_class, known_zero, outcome
):
    requests = []

    def handler(request):
        requests.append(request)
        raise error_class(SECRET + SOURCE, request=request)

    with pytest.raises(ProviderError) as caught:
        invoke(handler)
    assert len(requests) == 1
    error = caught.value
    assert error.known_zero is known_zero
    assert error.retryable is known_zero
    assert error.uncertain is not known_zero
    assert error.diagnostics["outcome"] == outcome
    assert_sanitized(error)


@pytest.mark.parametrize(
    "body",
    [
        [],
        None,
        {"choices": []},
        {"choices": ["bad"]},
        {"choices": [{"message": None}]},
        {"choices": [{"message": []}]},
    ],
)
def test_malformed_envelope_is_sanitized_provider_error(body):
    with pytest.raises(ProviderError) as caught:
        invoke(lambda _: httpx.Response(200, json=body))
    assert caught.value.uncertain is True
    assert caught.value.known_zero is False
    assert caught.value.diagnostics["outcome"] == "response_error"
    assert_sanitized(caught.value)


def test_nonjson_response_has_unknown_billing_and_no_echo():
    with pytest.raises(ProviderError) as caught:
        invoke(lambda _: httpx.Response(200, text=SOURCE + SECRET))
    assert caught.value.uncertain
    assert_sanitized(caught.value)


def test_usage_missing_or_wrong_type_does_not_invent_zero_tokens():
    for usage in (None, [], "unknown"):
        result, received = invoke(lambda _, usage=usage: httpx.Response(200, json=envelope(usage=usage)))
        assert result == {"opinions": []}
        assert "prompt_tokens" not in received
        assert "completion_tokens" not in received
