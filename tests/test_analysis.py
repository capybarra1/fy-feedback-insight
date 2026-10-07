import copy
import json
import threading

import pytest
from test_repository import note, upload

from absa import EXAMPLES
from analysis import Analyzer, Budget, ProviderError, Settings, redact, validate_result
from repository import Repository


@pytest.fixture
def repo(tmp_path):
    return Repository(tmp_path / "db.sqlite3")


def configured(repo):
    settings = Settings(repo.db)
    settings.update(
        {
            "base_url": "https://example.org/v1",
            "model": "test-model",
            "input_price": 1,
            "output_price": 2,
            "monthly_budget": 20,
            "api_key": "private-key",
        }
    )
    return settings


def test_budget_atomic_reservation_and_unknown_conservative(repo):
    budget = Budget(repo.db)
    lid = budget.reserve("source", 12, 20)
    with pytest.raises(ValueError, match="预算"):
        budget.reserve("other", 9, 20)
    budget.unknown(lid, "请求结果未知")
    assert budget.summary(20)["reserved"] == 12
    budget.settle(lid, 5)
    assert budget.summary(20)["remaining"] == 15


def test_concurrent_budget_never_overbooks(repo):
    budget = Budget(repo.db)
    successes = []

    def reserve():
        try:
            successes.append(budget.reserve("s", 12, 20))
        except ValueError:
            pass

    threads = [threading.Thread(target=reserve) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(successes) == 1


def test_key_not_persisted_or_in_public_settings(repo):
    settings = configured(repo)
    assert "private-key" not in json.dumps(settings.public())
    with repo.db.connect() as c:
        assert "private-key" not in c.execute("SELECT value FROM settings").fetchone()[0]
    assert Settings(repo.db).public()["has_api_key"] is False


def test_redaction_and_structured_validation():
    text, replacements = redact("联系 13812345678 或 a@b.com")
    assert "13812345678" not in text and "a@b.com" not in text
    assert replacements
    with pytest.raises(ValueError):
        validate_result(
            {
                "relevance": "relevant",
                "opinions": [
                    {
                        "module": "车机",
                        "type": "问题反馈",
                        "sentiment": "负面",
                        "theme": "虚构",
                        "evidence": "不存在",
                    }
                ],
            },
            text,
        )


def test_estimate_does_not_call_and_user_start_calls_once(repo):
    upload(repo, [note(1, nickname="private name")])
    calls = []

    def transport(settings, messages):
        calls.append(messages)
        result = copy.deepcopy(EXAMPLES[0]["result"])
        result["opinions"][0].update(
            module="语音助手（lumo）",
            submodule="语义理解",
            target_text="lumo",
            opinion_text="很好用",
            evidence="lumo 很好用",
            sentiment="positive",
            feedback_type="praise",
        )
        return result, {"prompt_tokens": 100, "completion_tokens": 60}

    engine = Analyzer(repo, configured(repo), transport=transport)
    estimate = engine.estimate({"mode": "pending"})
    assert calls == [] and estimate["can_start"]
    engine.start(estimate["estimate_token"])
    engine.wait(5)
    assert len(calls) == 1
    assert len(repo.opinions()) == 1
    assert engine.estimate({"mode": "pending"})["count"] == 0
    assert engine.budget.summary(20)["spent"] == pytest.approx(0.00022)


def test_budget_blocks_before_transport(repo):
    upload(repo, [note(1)])
    settings = configured(repo)
    settings.update({"monthly_budget": 0.000001})
    engine = Analyzer(repo, settings, transport=lambda *_: pytest.fail("should not call"))
    estimate = engine.estimate({"mode": "pending"})
    assert not estimate["can_start"]
    with pytest.raises(ValueError):
        engine.start(estimate["estimate_token"])


def test_timeout_keeps_reservation_and_no_automatic_retry(repo):
    upload(repo, [note(1)])
    calls = []

    def transport(*_):
        calls.append(1)
        raise ProviderError("超时，结果未知", uncertain=True)

    engine = Analyzer(repo, configured(repo), transport=transport)
    estimate = engine.estimate({"mode": "pending"})
    engine.start(estimate["estimate_token"])
    engine.wait(5)
    assert calls == [1]
    assert repo.sources()[0]["status"] == "unknown"
    assert engine.budget.summary(20)["reserved"] > 0


def test_estimate_invalidated_by_data_change(repo):
    upload(repo, [note(1)])
    engine = Analyzer(repo, configured(repo), transport=lambda *_: pytest.fail("stale"))
    estimate = engine.estimate({"mode": "pending"})
    upload(repo, [note(2)])
    with pytest.raises(ValueError, match="重新预估"):
        engine.start(estimate["estimate_token"])


def test_manual_review_protected_while_request_inflight(repo):
    upload(repo, [note(1)])
    key = repo.sources()[0]["key"]
    started, finish = threading.Event(), threading.Event()

    def transport(*_):
        started.set()
        finish.wait(3)
        return {
            "brand_relevance": "unrelated",
            "product_scope": "not_applicable",
            "content_types": ["other"],
            "opinions": [],
            "review_reasons": [],
        }, {"prompt_tokens": 20, "completion_tokens": 10}

    engine = Analyzer(repo, configured(repo), transport=transport)
    engine.start(engine.estimate({"mode": "pending"})["estimate_token"])
    assert started.wait(2)
    repo.review(
        key,
        {
            "relevance": "relevant",
            "review_status": "reviewed",
            "opinions": [
                {
                    "module": "语音助手（lumo）",
                    "type": "体验评价",
                    "sentiment": "正面",
                    "theme": "人工",
                    "evidence": "lumo 很好用",
                }
            ],
        },
    )
    finish.set()
    engine.wait(5)
    assert repo.opinions()[0]["theme"] == "人工"


def test_cache_private_evidence_cannot_leak_across_original_sources(repo):
    calls = []

    def transport(settings, messages):
        payload = json.loads(messages[-1]["content"])
        calls.append(1)
        result = copy.deepcopy(EXAMPLES[0]["result"])
        result["opinions"][0].update(
            submodule="导航", target_text="导航", opinion_text="卡顿", evidence=payload["source_text"]
        )
        return result, {"prompt_tokens": 10, "completion_tokens": 20}

    engine = Analyzer(repo, configured(repo), transport=transport)
    for n, phone in [(1, "13812345678"), (2, "13912345678")]:
        upload(repo, [note(n, text="导航卡顿，联系" + phone)])
        engine.start(engine.estimate({"mode": "pending"})["estimate_token"])
        engine.wait(5)
    assert len(repo.opinions()) == 2
    assert "13912345678" in next(o["evidence"] for o in repo.opinions() if o["source_key"] == "xhs:note:2")


def test_same_run_identical_inputs_reuse_cache(repo):
    upload(repo, [note(1), note(2)])
    calls = []

    def transport(*_):
        calls.append(1)
        result = copy.deepcopy(EXAMPLES[0]["result"])
        result["opinions"][0].update(
            module="语音助手（lumo）",
            submodule="语义理解",
            target_text="lumo",
            opinion_text="很好用",
            evidence="lumo 很好用",
            sentiment="positive",
            feedback_type="praise",
        )
        return result, {"prompt_tokens": 10, "completion_tokens": 20}

    engine = Analyzer(repo, configured(repo), transport=transport)
    engine.start(engine.estimate({"mode": "pending"})["estimate_token"])
    engine.wait(5)
    assert len(calls) == 1
    assert len(repo.opinions()) == 2


def test_env_key_loaded_on_restart_but_never_exposed_or_saved(repo, tmp_path):
    env_path = tmp_path / ".env"
    # Quoting and literal dollar signs must not interpolate another environment value.
    secret = "env-secret-${HOME}-literal"
    env_path.write_text(f'FIREFLY_API_KEY="{secret}"\n', encoding="utf-8")
    settings = Settings(repo.db, env_path=env_path)
    assert settings.snapshot()["api_key"] == secret
    assert settings.public()["key_source"] == "env"
    assert settings.public()["env_file"] == str(env_path)
    assert secret not in json.dumps(settings.public())
    settings.update({"model": "test", "api_key": "temporary-key"})
    assert settings.public()["key_source"] == "session"
    assert settings.snapshot()["api_key"] == "temporary-key"
    assert secret not in json.dumps(settings.public())
    with repo.db.connect() as c:
        stored = c.execute("SELECT value FROM settings").fetchone()[0]
    assert secret not in stored and "temporary-key" not in stored
    settings.update({"clear_key": True})
    assert not settings.public()["has_api_key"]
    assert settings.public()["key_source"] == "unset"
    assert secret in env_path.read_text()
    assert Settings(repo.db, env_path=env_path).snapshot()["api_key"] == secret


def test_missing_or_empty_env_is_optional(repo, tmp_path):
    env_path = tmp_path / ".env"
    assert not Settings(repo.db, env_path=env_path).public()["has_api_key"]
    env_path.write_text("# Fill in locally\nFIREFLY_API_KEY=\n", encoding="utf-8")
    assert not Settings(repo.db, env_path=env_path).public()["has_api_key"]


def test_bad_env_key_error_does_not_reveal_secret(repo, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text('FIREFLY_API_KEY="secret\ninvalid"', encoding="utf-8")
    with pytest.raises(ValueError) as error:
        Settings(repo.db, env_path=env_path)
    assert "secret" not in str(error.value)
