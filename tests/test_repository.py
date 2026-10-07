import csv
import io
import json

import pytest

from repository import Repository


def note(n, text="lumo 很好用", **extra):
    return {"note_id": str(n), "title": "", "desc": text, **extra}


def comment(n, parent="1", text="lumo 听不懂我说话", **extra):
    return {"comment_id": str(n), "note_id": parent, "content": text, **extra}


def upload(repo, rows, name="sample.jsonl"):
    return repo.import_files(
        [{"name": name, "content": "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)}], "rednote"
    )


@pytest.fixture
def repo(tmp_path):
    return Repository(tmp_path / "test.sqlite3")


def test_incremental_two_days_and_comments(repo):
    assert upload(repo, [note(i) for i in range(20)])["totals"]["new"] == 20
    b = upload(repo, [note(i) for i in range(5, 25)])
    assert b["totals"]["new"] == 5
    assert b["totals"]["duplicate"] == 15
    assert len(repo.sources()) == 25
    upload(repo, [comment(i) for i in range(100)])
    b = upload(repo, [comment(i) for i in range(40, 130)])
    assert b["totals"]["new"] == 30
    assert b["totals"]["duplicate"] == 60
    assert len([s for s in repo.sources() if s["kind"] == "comment"]) == 130


def test_invalid_lines_do_not_abort_and_no_fake_timestamp(repo):
    result = repo.import_files(
        [
            {
                "name": "bad.jsonl",
                "content": json.dumps(note(1)) + "\n{bad}\n" + json.dumps(comment(2, text="")),
            }
        ],
        "rednote",
    )
    assert result["totals"] == {"new": 1, "updated": 0, "duplicate": 0, "invalid": 2}
    assert len(result["batches"][0]["errors"]) == 2
    assert repo.sources()[0]["published_at"] is None


def test_edit_and_ai_cannot_overwrite_manual(repo):
    upload(repo, [note(1)])
    key = repo.sources()[0]["key"]
    opinions = [
        {
            "module": "语音助手（lumo）",
            "type": "体验评价",
            "sentiment": "正面",
            "theme": "好用",
            "evidence": "lumo 很好用",
        }
    ]
    repo.review(key, {"relevance": "relevant", "review_status": "reviewed", "opinions": opinions})
    upload(repo, [note(1, text="lumo 很好用，屏幕很清楚")])
    s = repo.source(key)
    assert s["manual"] and s["review_status"] == "needs_review"
    assert s["version"] == 2
    assert len(repo.opinions()) == 1
    assert repo.save_ai(key, {"relevance": "irrelevant", "opinions": []}, 2, "test") is False
    assert len(repo.opinions()) == 1


def test_metrics_update_not_reanalysis_and_other_not_irrelevant(repo):
    upload(repo, [note(1, text="价格偏高", liked_count="1")])
    key = repo.sources()[0]["key"]
    result = {
        "relevance": "relevant",
        "opinions": [
            {
                "module": "其他",
                "type": "体验评价",
                "sentiment": "负面",
                "theme": "价格",
                "evidence": "价格偏高",
            }
        ],
    }
    repo.save_ai(key, result, 1, "test")
    assert upload(repo, [note(1, text="价格偏高", liked_count="20")])["totals"]["duplicate"] == 1
    assert repo.source(key)["status"] == "done"
    assert len(repo.filtered_opinions({"module": "其他"})) == 1
    assert len(repo.filtered_opinions({"module": "focus"})) == 0


def test_review_requires_source_evidence(repo):
    upload(repo, [note(1)])
    with pytest.raises(ValueError, match="原文"):
        repo.review(
            repo.sources()[0]["key"],
            {
                "relevance": "relevant",
                "review_status": "reviewed",
                "opinions": [
                    {
                        "module": "车机",
                        "type": "问题反馈",
                        "sentiment": "负面",
                        "theme": "故障",
                        "evidence": "编造不存在的话",
                    }
                ],
            },
        )


def test_csv_formula_safety_and_exact_filter(repo):
    upload(repo, [note(1, text="=你好 lumo")])
    key = repo.sources()[0]["key"]
    repo.review(
        key,
        {
            "relevance": "relevant",
            "review_status": "reviewed",
            "opinions": [
                {
                    "module": "其他",
                    "type": "其他",
                    "sentiment": "中性",
                    "theme": "=SUM(1)",
                    "evidence": "=你好 lumo",
                }
            ],
        },
    )
    raw = repo.export_csv({"module": "其他"})
    assert "'=SUM(1)" in raw
    assert "'=你好 lumo" in raw
    assert len(list(csv.reader(io.StringIO(repo.export_csv({"module": "车机"}))))) == 1


def test_delete_batch_retains_shared_source(repo):
    first = upload(repo, [note(1), note(2)])["batches"][0]["id"]
    upload(repo, [note(1)])
    result = repo.delete_batch(first)
    assert result == {"deleted_sources": 1, "retained_sources": 1}
    assert len(repo.sources()) == 1


def test_parent_context_and_no_private_fields(repo):
    upload(
        repo,
        [
            note(1, nickname="secret name", xsec_token="secret token"),
            comment(2, text="语音很好"),
            comment(3, parent_comment_id="2", text="我也是"),
        ],
    )
    detail = repo.detail("xhs:comment:3")
    assert len(detail["context"]) == 2
    assert "secret token" not in json.dumps(detail)
    assert "secret name" not in json.dumps(detail)


def test_body_update_keeps_previous_effective_result_until_replaced(repo):
    upload(repo, [note(1, text="导航卡顿")])
    key = "xhs:note:1"
    repo.save_ai(
        key,
        {
            "relevance": "relevant",
            "opinions": [
                {
                    "module": "车机",
                    "type": "问题反馈",
                    "sentiment": "负面",
                    "theme": "卡顿",
                    "evidence": "导航卡顿",
                }
            ],
        },
        1,
        "original",
    )
    upload(repo, [note(1, text="今天修好了")])
    assert len(repo.filtered_opinions({})) == 1
    assert repo.source(key)["analysis_text"] == "导航卡顿"
    assert repo.source(key)["review_status"] == "needs_review"
    assert "导航卡顿" in repo.export_csv({})


def test_new_and_changed_context_marks_review_not_auto_paid(repo):
    upload(repo, [comment(2, text="这个不好用")])
    key = "xhs:comment:2"
    repo.save_ai(
        key,
        {
            "relevance": "relevant",
            "opinions": [
                {
                    "module": "待确认",
                    "type": "体验评价",
                    "sentiment": "负面",
                    "theme": "不明功能",
                    "evidence": "不好用",
                }
            ],
        },
        1,
        "ctx",
    )
    repo.review(key, {"relevance": "relevant", "review_status": "reviewed", "opinions": []})
    upload(repo, [note(1, text="lumo 功能")])
    assert repo.source(key)["review_status"] == "needs_review"
    assert repo.source(key)["status"] == "done"


def test_deleting_note_batch_marks_surviving_comment_context(repo):
    bid = upload(repo, [note(1)])["batches"][0]["id"]
    upload(repo, [comment(2)])
    key = "xhs:comment:2"
    repo.save_ai(key, {"relevance": "relevant", "opinions": []}, 1, "ctx")
    repo.delete_batch(bid)
    assert repo.source(key)["status"] == "done"
    assert repo.source(key)["review_status"] == "needs_review"


def test_delete_removes_cached_analysis_evidence(repo):
    bid = upload(repo, [note(1, text="导航卡顿 联系13812345678")])["batches"][0]["id"]
    repo.save_ai(
        "xhs:note:1",
        {
            "relevance": "relevant",
            "opinions": [
                {
                    "module": "车机",
                    "type": "问题反馈",
                    "sentiment": "负面",
                    "theme": "卡顿",
                    "evidence": "导航卡顿 联系13812345678",
                }
            ],
        },
        1,
        "cached",
    )
    repo.delete_batch(bid)
    with repo.db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM cache").fetchone()[0] == 0
