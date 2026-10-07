import copy
import json

import pytest

from absa import EXAMPLES, RULE_VERSION, storage_opinion, validate_absa
from repository import Repository


def setup_source(tmp_path, example=0):
    repo = Repository(tmp_path / "test.sqlite3")
    entry = copy.deepcopy(EXAMPLES[example])
    repo.import_files(
        [{"name": "sample.jsonl", "content": json.dumps({"note_id": "one", "desc": entry["source_text"]})}],
        "rednote",
    )
    return repo, repo.sources()[0]["key"], entry["result"]


def test_v2_storage_export_and_manual_sentiment(tmp_path):
    repo, key, result = setup_source(tmp_path)
    assert repo.save_ai(key, result, 1, "v2-example")
    source = repo.source(key)
    assert source["analysis_schema"] == source["routing_schema"] == RULE_VERSION
    assert source["eligible_for_insights"]
    opinion = repo.opinions()[0]
    assert opinion["target_text"] == "地图"
    assert opinion["submodule"] == "导航-路线规划"
    opinion["sentiment"] = "中性"
    opinion["theme"] = "人工整理的路线问题"
    payload = dict(
        result,
        relevance="relevant",
        review_status="reviewed",
        opinions=[opinion],
        analysis_schema=RULE_VERSION,
    )
    repo.review(key, payload)
    saved = repo.opinions()[0]
    assert saved["sentiment_code"] == "neutral"
    assert saved["theme"] == "人工整理的路线问题"
    assert not repo.save_ai(key, result, 1, "protected")
    exported = repo.export_csv({})
    assert "导航-路线规划" in exported and "人工整理的路线问题" in exported


def test_legacy_routing_saved_without_upgrading_old_opinions(tmp_path):
    repo, key, result = setup_source(tmp_path)
    opinion = {
        "module": "车机",
        "type": "问题反馈",
        "sentiment": "负面",
        "theme": "地图",
        "evidence": result["opinions"][0]["evidence"],
    }
    repo.review(
        key,
        dict(
            result,
            relevance="relevant",
            review_status="unreviewed",
            opinions=[opinion],
            analysis_schema="legacy",
        ),
    )
    saved = repo.source(key)
    assert saved["routing_schema"] == RULE_VERSION
    assert saved["analysis_schema"] == "legacy"
    assert saved["brand_relevance"] == "related"
    assert saved["product_scope"] == "cockpit"
    assert saved["content_types"] == ["product_feedback"]
    assert repo.opinions()[0]["schema_version"] == "legacy"


def test_empty_legacy_routing_saved_and_not_counted(tmp_path):
    repo, key, _ = setup_source(tmp_path)
    repo.review(
        key,
        {
            "relevance": "relevant",
            "review_status": "reviewed",
            "opinions": [],
            "analysis_schema": "legacy",
            "brand_relevance": "related",
            "product_scope": "other",
            "content_types": ["promotion"],
            "review_reasons": [],
        },
    )
    saved = repo.source(key)
    assert saved["brand_relevance"] == "related"
    assert saved["content_types"] == ["promotion"]
    assert not saved["eligible_for_insights"]


def test_implicit_target_null_preserved(tmp_path):
    repo, key, result = setup_source(tmp_path, 1)
    assert repo.save_ai(key, result, 1, "implicit")
    assert repo.opinions()[0]["target_text"] is None
    assert repo.opinions()[0]["implicit_target"] == 1


def test_v2_cannot_silently_downgrade_or_skip_routing_validation(tmp_path):
    repo, key, result = setup_source(tmp_path)
    repo.save_ai(key, result, 1, "original")
    opinion = repo.opinions()[0]
    with pytest.raises(ValueError):
        repo.review(
            key,
            {
                "relevance": "relevant",
                "review_status": "reviewed",
                "opinions": [opinion],
                "product_scope": "other",
            },
        )
    old = {k: opinion[k] for k in ("module", "type", "sentiment", "theme", "evidence")}
    with pytest.raises(ValueError):
        repo.review(key, {"relevance": "relevant", "review_status": "reviewed", "opinions": [old]})
    assert repo.source(key)["manual"] is False


def test_review_reasons_mark_source_needs_review(tmp_path):
    repo, key, _ = setup_source(tmp_path)
    result = {
        "brand_relevance": "related",
        "product_scope": "unknown",
        "content_types": ["chitchat"],
        "opinions": [],
        "review_reasons": ["仅附和，无法确定评价对象"],
    }
    repo.save_ai(key, result, 1, "review")
    assert repo.source(key)["review_status"] == "needs_review"


def test_blank_evidence_and_string_boolean_rejected(tmp_path):
    result = copy.deepcopy(EXAMPLES[1]["result"])
    opinion = result["opinions"][0]
    opinion.update(
        evidence=" ",
        target_text=None,
        opinion_text=None,
        implicit_target=True,
        implicit_opinion=True,
        needs_review=True,
    )
    result["review_reasons"] = ["不确定"]
    with pytest.raises(ValueError):
        validate_absa(result, "欢迎 来店试驾")
    repo, key, result = setup_source(tmp_path)
    opinion = storage_opinion(result["opinions"][0])
    opinion["implicit_target"] = "false"
    with pytest.raises(ValueError):
        repo.review(key, dict(result, relevance="relevant", review_status="reviewed", opinions=[opinion]))
