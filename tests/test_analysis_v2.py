import copy
import json

import pytest
from test_repository import note, upload

from absa import EXAMPLES
from analysis import Analyzer, Settings
from repository import Repository


@pytest.fixture
def repo(tmp_path):
    return Repository(tmp_path / "analysis-v2.sqlite3")


def configured(repo):
    settings = Settings(repo.db)
    settings.update(
        dict(
            base_url="https://example.org/v1",
            model="stub",
            api_key="test-only",
            input_price=1,
            output_price=2,
            concurrency=4,
        )
    )
    return settings


def run(engine):
    estimate = engine.estimate({"mode": "pending"})
    assert estimate["can_start"], estimate["reason"]
    engine.start(estimate["estimate_token"])
    engine.wait(5)
    assert not engine.state()["running"]
    return estimate


def test_duplicate_failures_share_the_estimated_attempt_sequence(repo):
    upload(repo, [note(i, EXAMPLES[0]["source_text"]) for i in range(5)])
    calls = []

    def transport(*args):
        calls.append(args)
        return None, {"prompt_tokens": 100, "completion_tokens": 20}

    engine = Analyzer(repo, configured(repo), transport)
    run(engine)
    assert len(calls) == 2
    assert len(engine.budget.ledger()) == 2
    assert engine.state()["failed"] == engine.state()["completed"] == 5
    assert {s["status"] for s in repo.sources()} == {"failed"}
    assert len({s["error"] for s in repo.sources()}) == 1


def test_schema_repair_has_its_own_reservation_and_settlement(repo):
    upload(repo, [note(1, EXAMPLES[0]["source_text"])])
    calls = []

    def transport(config, messages):
        calls.append(messages)
        result = copy.deepcopy(EXAMPLES[0]["result"])
        if len(calls) == 1:
            result["opinions"][0]["evidence"] = "fabricated"
        return result, {"prompt_tokens": 100, "completion_tokens": 20}

    engine = Analyzer(repo, configured(repo), transport)
    estimate = run(engine)
    ledger = sorted(engine.budget.ledger(), key=lambda row: row["retry_index"])
    assert len(calls) == len(ledger) == 2
    assert len(calls[1]) == len(calls[0]) + 1
    assert [row["outcome"] for row in ledger] == ["schema_error", "success"]
    assert [row["retry_index"] for row in ledger] == [0, 1]
    assert all(row["status"] == "settled" and row["amount_micros"] == 140 for row in ledger)
    assert sum(row["reserved"] for row in ledger) == pytest.approx(estimate["estimated_cost"])
    assert engine.state()["succeeded"] == 1


def test_truncation_is_not_retried_for_duplicate_inputs(repo):
    upload(repo, [note(i, EXAMPLES[0]["source_text"]) for i in range(3)])
    calls = []

    def transport(*args):
        calls.append(args)
        return None, {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "_diagnostics": {"finish_reason": "length"},
        }

    engine = Analyzer(repo, configured(repo), transport)
    run(engine)
    assert len(calls) == 1
    assert engine.budget.ledger()[0]["outcome"] == "truncated"
    assert engine.state()["failed"] == 3


def test_noise_only_needs_neither_credentials_nor_api(repo):
    upload(repo, [note(1, "👍"), note(2, "[赞R]"), note(3, "https://example.org")])

    def transport(*_):
        pytest.fail("Pure noise must never call the model")

    engine = Analyzer(repo, Settings(repo.db), transport)
    estimate = run(engine)
    assert estimate["noise_count"] == 3
    assert estimate["estimated_cost"] == 0
    assert engine.state()["skipped"] == 3
    assert not engine.budget.ledger()
    assert {s["status"] for s in repo.sources()} == {"skipped"}


@pytest.mark.parametrize("text", ["死机", "没声音", "不跟车"])
def test_short_faults_are_forwarded_to_the_model(repo, text):
    upload(repo, [note(1, text)])
    calls = []

    def transport(config, messages):
        calls.append(json.loads(messages[1]["content"])["source_text"])
        return {
            "brand_relevance": "uncertain",
            "product_scope": "unknown",
            "content_types": ["uncertain"],
            "opinions": [],
            "review_reasons": ["品牌需确认"],
        }, {"prompt_tokens": 10, "completion_tokens": 20}

    engine = Analyzer(repo, configured(repo), transport)
    estimate = run(engine)
    assert estimate["noise_count"] == 0
    assert calls == [text]
    assert engine.state()["succeeded"] == 1


def test_malformed_free_cache_does_not_trigger_duplicate_paid_calls(repo):
    upload(repo, [note(i, EXAMPLES[0]["source_text"]) for i in range(3)])

    def transport(*_):
        pytest.fail("Malformed free cache requires a new estimate before any paid call")

    engine = Analyzer(repo, configured(repo), transport)
    item = engine._item(repo.sources()[0], engine.settings.snapshot())
    with repo.db.connect() as connection:
        connection.execute("INSERT INTO cache VALUES (?,?)", (item["fingerprint"], "{}"))
    estimate = run(engine)
    assert estimate["estimated_cost"] == 0
    assert engine.state()["failed"] == 3
    assert not engine.budget.ledger()


def test_failure_reuse_preserves_concurrent_manual_correction(repo):
    upload(repo, [note(i, EXAMPLES[0]["source_text"]) for i in range(3)])
    ordered = repo.sources()
    manual_key = ordered[1]["key"]
    calls = []

    def transport(*_):
        calls.append(1)
        if len(calls) == 1:
            repo.review(
                manual_key,
                {"relevance": "relevant", "review_status": "reviewed", "opinions": []},
            )
        return None, {"prompt_tokens": 10, "completion_tokens": 20}

    engine = Analyzer(repo, configured(repo), transport)
    run(engine)
    assert len(calls) == 2
    assert repo.source(manual_key)["manual"]
    assert repo.source(manual_key)["status"] == "done"
    assert engine.state()["skipped"] == 1
    assert engine.state()["failed"] == 2


def test_json_metrics_use_known_parse_results_and_do_not_count_active_as_legacy(repo):
    engine = Analyzer(repo, configured(repo), lambda *_: None)
    budget = engine.budget
    historical = budget.reserve("historical", 0, 20)
    budget.settle(historical, 0)
    active = budget.reserve("active", 0, 20)
    budget.record_attempt(active, {"model": "stub", "rule_version": "absa-v2", "retry_index": 0})
    for outcome, json_valid in [("success", None), ("truncated", True), ("json_error", False)]:
        ledger_id = budget.reserve(outcome, 0, 20)
        budget.record_attempt(
            ledger_id,
            {"outcome": outcome, "rule_version": "absa-v2", "json_valid": json_valid, "retry_index": 0},
        )
        budget.settle(ledger_id, 0)
    metrics = budget.metrics()
    assert metrics["requests"] == 3
    assert metrics["legacy_requests"] == 1
    assert metrics["json_success_rate"] == 0.5


def test_attempt_metadata_is_recorded_before_transport_and_provider_json_flag_is_preserved(repo):
    upload(repo, [note(1, EXAMPLES[0]["source_text"])])

    def transport(*_):
        ledger = engine.budget.ledger()
        assert len(ledger) == 1
        assert ledger[0]["status"] == "reserved"
        assert ledger[0]["rule_version"] == "absa-v2"
        assert ledger[0]["model"] == "stub"
        assert ledger[0]["retry_index"] == 0
        assert engine.budget.metrics()["legacy_requests"] == 0
        return None, {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "_diagnostics": {"json_valid": True, "finish_reason": "length"},
        }

    engine = Analyzer(repo, configured(repo), transport)
    run(engine)
    assert engine.budget.ledger()[0]["json_valid"] == 1
    assert engine.budget.metrics()["json_success_rate"] == 1


def test_invalid_result_with_unknown_usage_requires_explicit_confirmation(repo):
    upload(repo, [note(1, EXAMPLES[0]["source_text"])])
    calls = []

    def transport(*args):
        calls.append(1)
        return None, {}

    engine = Analyzer(repo, configured(repo), transport)
    run(engine)
    assert len(calls) == 1
    assert repo.sources()[0]["status"] == "unknown"
    assert engine.budget.ledger()[0]["status"] == "unknown"
    assert not engine.estimate({"mode": "failed"})["can_start"]


def test_parent_comment_not_sent_as_model_context(repo):
    from test_repository import comment

    upload(
        repo,
        [
            note(1, "萤火虫汽车笔记"),
            comment("parent", text="父评论导航故障"),
            comment("child", text="我也是", parent_comment_id="parent"),
        ],
    )
    engine = Analyzer(repo, configured(repo), lambda *_: None)
    source = next(s for s in repo.sources() if s["external_id"] == "child")
    item = engine._item(source, engine.settings.snapshot())
    assert item["preview"]["brand_context"] == ["萤火虫汽车笔记"]
    assert "父评论导航故障" not in json.dumps(item["messages"], ensure_ascii=False)
    assert item["preview"]["source_text"] == "我也是"


def test_configured_parallelism_is_used_without_exceeding_limit(repo):
    import threading

    upload(repo, [note(1, EXAMPLES[0]["source_text"]), note(2, EXAMPLES[1]["source_text"])])
    settings = configured(repo)
    settings.update({"concurrency": 2})
    barrier = threading.Barrier(2)

    def transport(config, messages):
        payload = json.loads(messages[-1]["content"])
        barrier.wait(timeout=3)
        result = next(
            copy.deepcopy(x["result"]) for x in EXAMPLES if x["source_text"] == payload["source_text"]
        )
        return result, {"prompt_tokens": 1, "completion_tokens": 1}

    engine = Analyzer(repo, settings, transport)
    run(engine)
    assert engine.state()["succeeded"] == 2
    assert len(engine.budget.ledger()) == 2
