"""Synthetic offline evaluation fixtures; no real source text or model requests."""

import copy
import sqlite3

import pytest

import evaluation


def opinion(**changes):
    row = dict(
        module="车机",
        submodule="系统流畅度",
        target_text="车机",
        opinion_text="卡",
        evidence="车机卡",
        feedback_type="complaint",
        sentiment="negative",
        implicit_target=False,
        context_used=False,
        needs_review=False,
    )
    return row | changes


def gold(key="one", **changes):
    return (
        dict(
            id=key,
            note_id=key,
            site="xiaohongshu",
            split="holdout",
            human_confirmed=True,
            source_text="车机卡",
            slices={"short": True},
            expected=dict(
                brand_relevance="related",
                product_scope="cockpit",
                content_types=["product_feedback"],
                opinions=[opinion()],
            ),
            assessment=dict(valid_feedback=True, is_noise=False, prediction_useful=None),
        )
        | changes
    )


def prediction(sample, **changes):
    return dict(id=sample["id"], status="succeeded", prediction=copy.deepcopy(sample["expected"])) | changes


def test_perfect_and_missing_predictions_have_real_denominators():
    a, b = gold(), gold("two")
    report = evaluation.evaluate([a, b], [prediction(a)])
    assert report["counts"] == dict(
        total=2,
        confirmed=2,
        excluded_unconfirmed=0,
        evaluated=2,
        missing_predictions=1,
        failed_predictions=0,
        invalid_predictions=0,
        extra_predictions=0,
    )
    assert report["quartet"]["precision"] == 1
    assert report["quartet"]["recall"] == 0.5
    assert report["quartet"]["f1"] == pytest.approx(2 / 3)
    assert report["routing"]["valid_feedback_recall"] == dict(value=0.5, numerator=1, denominator=2)
    assert report["routing"]["relevance_recall"]["value"] == 0.5
    assert report["product_useful_ratio"]["value"] is None


def test_failed_and_invalid_output_count_misses():
    a, b = gold(), gold("two")
    bad = prediction(b)
    bad["prediction"]["opinions"][0]["sentiment"] = "负面"
    report = evaluation.evaluate([a, b], [prediction(a, status="failed"), bad])
    assert report["counts"]["failed_predictions"] == 1
    assert report["counts"]["invalid_predictions"] == 1
    assert report["quartet"]["fn"] == 2
    assert report["quartet"]["f1"] == 0


def test_unconfirmed_and_unknown_labels_are_excluded_not_perfect():
    a = gold(human_confirmed=False)
    b = gold(
        "two",
        expected=dict(brand_relevance=None, product_scope=None, content_types=None, opinions=None),
        assessment=dict(valid_feedback=None, is_noise=None, prediction_useful=None),
    )
    report = evaluation.evaluate([a, b], [])
    assert report["counts"]["excluded_unconfirmed"] == 1
    assert report["quartet"]["f1"] is None
    assert report["quartet"]["known_sources"] == 0
    assert report["routing"]["valid_feedback_recall"]["denominator"] == 0
    assert report["routing"]["relevance_accuracy"]["denominator"] == 0


def test_explicit_human_usefulness_and_false_short_filter():
    a = gold()
    a["assessment"]["prediction_useful"] = False
    p = prediction(a)
    p["prediction"] = dict(
        brand_relevance="unrelated", product_scope="not_applicable", content_types=["chitchat"], opinions=[]
    )
    report = evaluation.evaluate([a], [p])
    assert report["routing"]["noise_precision"] == dict(value=0.0, numerator=0, denominator=1)
    assert report["routing"]["short_false_filter_rate"]["value"] == 1
    assert report["product_useful_ratio"] == dict(value=0.0, numerator=0, denominator=1)


def test_field_sets_and_multilabel_macro_are_not_single_label_accuracy():
    a = gold()
    a["expected"]["opinions"].append(
        opinion(module="智能硬件", submodule="扬声器／音响", target_text=None, implicit_target=True)
    )
    p = prediction(a)
    p["prediction"]["opinions"].pop()
    report = evaluation.evaluate([a], [p])
    assert report["quartet"]["fn"] == 1
    assert report["fields"]["target_text"]["fn"] == 1
    assert report["modules"]["micro"]["f1"] == pytest.approx(2 / 3)
    assert report["submodules"]["macro_f1"] == pytest.approx(2 / 3)


@pytest.mark.parametrize("which", ["gold", "prediction"])
def test_duplicate_ids_rejected(which):
    a = gold()
    with pytest.raises(ValueError, match="Duplicate"):
        evaluation.evaluate(
            [a, a] if which == "gold" else [a],
            [prediction(a), prediction(a)] if which == "prediction" else [],
        )


def test_leakage_rejected_even_when_only_one_split_is_requested():
    a, b = gold(), gold("two", note_id="one", split="dev")
    with pytest.raises(ValueError, match="leakage"):
        evaluation.evaluate([a, b], [], split="holdout")
    with pytest.raises(ValueError, match="split"):
        evaluation.evaluate([a], [prediction(a, split="dev")])


def test_falsey_nonboolean_confirmation_rejected():
    with pytest.raises(ValueError, match="human_confirmed"):
        evaluation.evaluate([gold(human_confirmed="true")], [])


def test_prepare_readonly_no_gold_stable_threads_and_private_files(tmp_path):
    db = tmp_path / "sources.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE sources (key TEXT, note_id TEXT, site TEXT, kind TEXT, title TEXT, "
            "text TEXT, status TEXT, relevance TEXT, parent_id TEXT)"
        )
        for i in range(100):
            conn.execute(
                "INSERT INTO sources VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    f"key{i}",
                    f"note{i // 3}",
                    "rednote",
                    "note" if i % 3 == 0 else "comment",
                    "萤火虫",
                    "车机卡" if i % 2 else "😀",
                    "failed" if i % 4 == 0 else "pending",
                    "unknown",
                    None,
                ),
            )
    before = db.read_bytes()
    output = tmp_path / "data" / "evaluation" / "draft.jsonl"
    samples = evaluation.prepare(db, output, count=80)
    assert len(samples) == 80
    assert db.read_bytes() == before
    assert output.stat().st_mode & 0o777 == 0o600
    assert output.parent.stat().st_mode & 0o777 == 0o700
    groups = {}
    for s in samples:
        assert s["human_confirmed"] is False
        assert all(v is None for v in s["expected"].values())
        assert all(v is None for v in s["assessment"].values())
        assert s["split"] == groups.setdefault(s["group_id"], s["split"])
        assert s["source_text"] and s["brand_context"] is not None
    assert any(s["slices"]["failed"] for s in samples)
    assert any(s["slices"]["noise_candidate"] for s in samples)
    second = evaluation.prepare(db, output.with_name("other.jsonl"), count=80)
    assert samples == second
    with pytest.raises(FileExistsError):
        evaluation.prepare(db, output, count=80)
    with pytest.raises(ValueError, match="data/evaluation"):
        evaluation.prepare(db, tmp_path / "public.jsonl", count=80)


def test_jsonl_rejects_nonobjects_and_reports_line(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_text("{}\n[]\n")
    with pytest.raises(ValueError, match="line 2"):
        evaluation.read_jsonl(p)


def test_empty_and_subset_reporting():
    report = evaluation.evaluate([], [])
    assert report["quartet"]["f1"] is None
    assert report["counts"]["evaluated"] == 0
    a = gold()
    report = evaluation.evaluate([a], [prediction(a)], split="dev")
    assert report["counts"]["evaluated"] == 0
    assert "short" in evaluation.evaluate([a], [prediction(a)])["slices"]


def test_flat_canonical_predictions_supported_and_missing_usefulness_excluded():
    a = gold()
    a["assessment"]["prediction_useful"] = True
    report = evaluation.evaluate([a], [dict(id=a["id"], **a["expected"])])
    assert report["quartet"]["f1"] == 1
    assert report["product_useful_ratio"]["value"] == 1
    assert evaluation.evaluate([a], [])["product_useful_ratio"]["denominator"] == 0


def test_prediction_empty_list_hallucinations_penalized_and_empty_empty_undefined():
    a = gold()
    a["expected"]["opinions"] = []
    p = dict(id=a["id"], prediction=gold()["expected"])
    assert evaluation.evaluate([a], [p])["quartet"]["fp"] == 1
    report = evaluation.evaluate([a], [prediction(a)])
    assert report["quartet"]["f1"] is None
    assert report["modules"]["empty_empty_sources"] == 1


def test_noncanonical_failed_schema_and_fabricated_spans_are_misses():
    a = gold()
    for changes in (
        {"evidence": "伪造"},
        {"target_text": None},
        {"context_used": "false"},
        {"sentiment": []},
    ):
        p = prediction(a)
        p["prediction"]["opinions"][0].update(changes)
        report = evaluation.evaluate([a], [p])
        assert report["counts"]["invalid_predictions"] == 1
        assert report["quartet"]["fn"] == 1


def test_prepare_refuses_symlink_output_and_oversized_sample(tmp_path):
    target = tmp_path / "public"
    target.mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "data/evaluation").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        evaluation._private_write(tmp_path / "data/evaluation/private.jsonl", "private")
    with pytest.raises(ValueError, match="count"):
        evaluation.prepare(tmp_path / "missing.db", tmp_path / "data/evaluation/a.jsonl", count=81)


def test_human_usability_can_mark_failed_output_unusable():
    a = gold()
    a["assessment"]["prediction_useful"] = False
    report = evaluation.evaluate([a], [prediction(a, status="failed")])
    assert report["product_useful_ratio"] == dict(value=0.0, numerator=0, denominator=1)


def test_private_output_rejects_path_traversal(tmp_path):
    with pytest.raises(ValueError, match="data/evaluation"):
        evaluation._private_write(tmp_path / "data/evaluation/../../public.jsonl", "private")


def test_implicit_opinion_requires_boolean_marker():
    a = gold()
    p = prediction(a)
    p["prediction"]["opinions"][0].update(opinion_text=None, implicit_opinion="true")
    assert evaluation.evaluate([a], [p])["counts"]["invalid_predictions"] == 1
