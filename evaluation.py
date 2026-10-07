"""Offline annotation drafting and human-gold ABSA evaluation (stdlib only)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROUTING = {
    "brand_relevance": {"related", "unrelated", "uncertain"},
    "product_scope": {"cockpit", "other", "mixed", "unknown", "not_applicable"},
    "content_types": {
        "product_feedback",
        "product_question",
        "promotion",
        "delivery_chat",
        "chitchat",
        "other",
        "uncertain",
    },
}
SENTIMENTS = {"positive", "negative", "neutral", "uncertain"}
FEEDBACK_TYPES = {"fault", "complaint", "suggestion", "question", "praise", "comparison", "other"}
FIELDS = ("target_text", "opinion_text", "evidence", "feedback_type", "sentiment", "module", "submodule")
ASSESSMENTS = ("valid_feedback", "is_noise", "prediction_useful")
SPLITS = {"dev", "holdout"}


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _group(row: dict) -> str:
    site = "xhs" if row.get("site") in {"rednote", "xiaohongshu", "xhs"} else row.get("site", "unknown")
    return f"{site}:note:{row.get('note_id') or row.get('id') or row.get('key')}"


def stable_split(group_id: str) -> str:
    """Fixed note-level 70/30 split; adding sources never moves existing groups."""
    return "holdout" if int(_hash("firefly-absa-v2-split:" + group_id)[:8], 16) % 10 < 3 else "dev"


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    with Path(path).open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("expected an object")
            except (ValueError, TypeError) as exc:
                raise ValueError(f"{path}: line {number}: invalid JSON object") from exc
            rows.append(row)
    return rows


def _private_write(path: Path, content: str) -> None:
    path = Path(path)
    if ".." in path.parts or not any(
        p.name == "evaluation" and p.parent.name == "data" for p in path.parents
    ):
        raise ValueError("Private output must be under data/evaluation")
    # Refuse symlinks so a local link cannot redirect private raw-text exports.
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise ValueError("Private output path must not contain symlinks")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(content)


def prepare(db_path: Path, output: Path, count: int = 80) -> list[dict]:
    """Read-only SQLite snapshot. Never import Repository or copy prior AI labels."""
    if not 1 <= count <= 80:
        raise ValueError("count must be between 1 and 80")
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(sources)")}
        required = {"key", "note_id", "site", "kind", "text"}
        if not required <= columns:
            raise ValueError("Database sources table lacks required source fields")
        allowed = sorted(
            required | (columns & {"title", "status", "relevance", "parent_id", "manual", "duplicate_of"})
        )
        rows = [
            dict(r) for r in connection.execute("SELECT " + ",".join(allowed) + " FROM sources ORDER BY key")
        ]
    finally:
        connection.close()
    notes = {_group(r): r for r in rows if r["kind"] == "note"}
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        text = row["text"]
        compact = re.sub(r"\s+", "", text)
        flags = dict(
            short=len(compact) <= 12,
            noise_candidate=not bool(re.search(r"[\w\u4e00-\u9fff]", compact)),
            failed=row.get("status") == "failed",
            note=row["kind"] == "note",
            comment=row["kind"] == "comment",
            prior_filtered=row.get("relevance") == "irrelevant",
            manual=bool(row.get("manual")),
            duplicate=bool(row.get("duplicate_of")),
        )
        group_id = _group(row)
        note = notes.get(group_id)
        sample = dict(
            id=row["key"],
            group_id=group_id,
            site=row["site"],
            note_id=row["note_id"],
            parent_id=row.get("parent_id"),
            kind=row["kind"],
            source_text=text,
            source_sha256=_hash(text),
            brand_context=dict(
                note_id=row["note_id"],
                title=note.get("title", "") if note else "",
                text=note["text"][:2000] if note else None,
                usage="Brand/product context only; never copy into opinion evidence.",
            ),
            split=stable_split(group_id),
            human_confirmed=False,
            expected=dict(brand_relevance=None, product_scope=None, content_types=None, opinions=None),
            assessment={key: None for key in ASSESSMENTS},
            slices=flags,
            annotation_notes="",
            draft_version="absa-v2-draft-1",
        )
        category = next(
            (
                name
                for name in ("failed", "noise_candidate", "prior_filtered", "short", "note")
                if flags[name]
            ),
            "other",
        )
        buckets[category].append(sample)
    for bucket in buckets.values():
        bucket.sort(key=lambda s: _hash("firefly-absa-v2-sample:" + s["id"]))
    selected: list[dict] = []
    # Round-robin sampling deliberately covers rare failure/noise/short slices.
    while len(selected) < min(count, len(rows)):
        for category in sorted(buckets):
            if buckets[category] and len(selected) < count:
                selected.append(buckets[category].pop(0))
    _private_write(output, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in selected))
    return selected


def _index(rows: list[dict], label: str) -> dict[str, dict]:
    index = {}
    for row in rows:
        key = row.get("id")
        if not isinstance(key, str) or not key:
            raise ValueError(f"{label} rows require a nonempty stable id")
        if key in index:
            raise ValueError(f"Duplicate {label} id: {key}")
        index[key] = row
    return index


def _validate_labels(labels: Any, source_text: str, partial: bool = False) -> None:
    if not isinstance(labels, dict):
        raise ValueError("ABSA labels must be an object")
    for field, options in ROUTING.items():
        value = labels.get(field)
        if value is None and partial:
            continue
        if field == "content_types":
            if (
                not isinstance(value, list)
                or not value
                or any(not isinstance(v, str) or v not in options for v in value)
            ):
                raise ValueError("Invalid content_types")
        elif not isinstance(value, str) or value not in options:
            raise ValueError(f"Invalid {field}")
    opinions = labels.get("opinions")
    if opinions is None and partial:
        return
    if not isinstance(opinions, list):
        raise ValueError("opinions must be an explicit list (or null for unannotated gold)")
    for op in opinions:
        if not isinstance(op, dict):
            raise ValueError("Opinion must be an object")
        for field in ("module", "submodule", "evidence"):
            if not isinstance(op.get(field), str) or not op[field]:
                raise ValueError(f"Invalid opinion {field}")
        if (
            not isinstance(op.get("sentiment"), str)
            or op["sentiment"] not in SENTIMENTS
            or not isinstance(op.get("feedback_type"), str)
            or op["feedback_type"] not in FEEDBACK_TYPES
        ):
            raise ValueError("Opinion requires canonical English sentiment / feedback_type")
        if "implicit_opinion" in op and type(op["implicit_opinion"]) is not bool:
            raise ValueError("implicit_opinion must be boolean")
        for field in ("implicit_target", "context_used", "needs_review"):
            if type(op.get(field)) is not bool:
                raise ValueError(f"Opinion requires boolean {field}")
        for field, implicit_flag in (
            ("target_text", "implicit_target"),
            ("opinion_text", "implicit_opinion"),
        ):
            value = op.get(field)
            if value is None:
                if not op.get(implicit_flag, False):
                    raise ValueError(f"Null {field} requires {implicit_flag}")
            elif not isinstance(value, str) or not value or value not in op["evidence"]:
                raise ValueError(f"{field} must be a verbatim evidence span")
        if op["evidence"] not in source_text:
            raise ValueError("Evidence must be a verbatim source span")


def _ratio(numerator: int, denominator: int) -> dict:
    return dict(
        value=numerator / denominator if denominator else None, numerator=numerator, denominator=denominator
    )


def _set_score(expected: set, predicted: set) -> dict:
    tp, fp, fn = len(expected & predicted), len(predicted - expected), len(expected - predicted)
    return _score_counts(tp, fp, fn)


def _score_counts(tp: int, fp: int, fn: int) -> dict:
    return dict(
        tp=tp,
        fp=fp,
        fn=fn,
        precision=tp / (tp + fp) if tp + fp else None,
        recall=tp / (tp + fn) if tp + fn else None,
        f1=2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None,
    )


def _projection(op: dict, field: str) -> Any:
    if field == "quartet":
        return op["target_text"], (op["module"], op["submodule"]), op["opinion_text"], op["sentiment"]
    if field == "submodules":
        return op["module"], op["submodule"]
    if field == "modules":
        return op["module"]
    return op[field]


def _aggregate(samples: list[dict], predictions: dict[str, dict]) -> dict:
    totals: dict[str, Counter] = defaultdict(Counter)
    known: Counter = Counter()
    macros: dict[str, list[float]] = defaultdict(list)
    routing: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    failed = invalid = missing = 0

    def add_ratio(name: str, condition: bool, eligible: bool) -> None:
        if eligible:
            routing[name][0] += int(condition)
            routing[name][1] += 1

    for sample in samples:
        expected = sample["expected"]
        record = predictions.get(sample["id"])
        predicted = None
        if record is None:
            missing += 1
        elif record.get("status", "succeeded") not in {"succeeded", "success"}:
            failed += 1
        else:
            try:
                candidate = record.get("prediction", record)
                _validate_labels(candidate, sample["source_text"])
                predicted = candidate
            except (ValueError, TypeError):
                invalid += 1
        for field in ("quartet", *FIELDS, "modules", "submodules"):
            if expected.get("opinions") is None:
                continue
            exp = {_projection(op, field) for op in expected["opinions"]}
            pred = {_projection(op, field) for op in predicted["opinions"]} if predicted else set()
            score = _set_score(exp, pred)
            known[field] += 1
            totals[field].update({name: score[name] for name in ("tp", "fp", "fn")})
            if score["f1"] is not None:
                macros[field].append(score["f1"])
        human = sample.get("assessment", {})
        related = predicted is not None and predicted["brand_relevance"] == "related"
        valid = (
            predicted is not None
            and related
            and bool(set(predicted["content_types"]) & {"product_feedback", "product_question"})
        )
        filtered = (
            predicted is not None
            and record is not None
            and (
                record.get("filtered") is True
                or predicted["brand_relevance"] == "unrelated"
                or (
                    not predicted["opinions"]
                    and not set(predicted["content_types"])
                    & {"product_feedback", "product_question", "uncertain"}
                    and bool(set(predicted["content_types"]) & {"promotion", "delivery_chat", "chitchat"})
                )
            )
        )
        add_ratio("relevance_recall", related, expected.get("brand_relevance") == "related")
        add_ratio(
            "relevance_accuracy",
            predicted is not None and predicted["brand_relevance"] == expected.get("brand_relevance"),
            expected.get("brand_relevance") in {"related", "unrelated"},
        )
        add_ratio("valid_feedback_recall", valid and not filtered, human.get("valid_feedback") is True)
        add_ratio(
            "noise_precision", human.get("is_noise") is True, filtered and type(human.get("is_noise")) is bool
        )
        add_ratio("noise_recall", filtered, human.get("is_noise") is True)
        add_ratio(
            "short_false_filter_rate",
            filtered,
            sample.get("slices", {}).get("short") is True and human.get("valid_feedback") is True,
        )
        add_ratio(
            "product_useful_ratio",
            human.get("prediction_useful") is True,
            record is not None and type(human.get("prediction_useful")) is bool,
        )
        for field in ("product_scope", "content_types"):
            expected_value = expected.get(field)
            correct = predicted is not None and (
                set(predicted[field]) == set(expected_value)
                if field == "content_types" and expected_value is not None
                else predicted[field] == expected_value
            )
            add_ratio(
                field + "_accuracy",
                correct,
                expected_value is not None and expected_value not in ("unknown",),
            )

    def metric(field: str) -> dict:
        return _score_counts(*(totals[field][key] for key in ("tp", "fp", "fn"))) | {
            "known_sources": known[field]
        }

    def multilabel(field: str) -> dict:
        values = macros[field]
        return dict(
            micro=metric(field),
            macro_f1=sum(values) / len(values) if values else None,
            macro_sources=len(values),
            empty_empty_sources=known[field] - len(values),
        )

    names = (
        "relevance_recall",
        "relevance_accuracy",
        "valid_feedback_recall",
        "noise_precision",
        "noise_recall",
        "short_false_filter_rate",
        "product_scope_accuracy",
        "content_types_accuracy",
    )
    return dict(
        source_count=len(samples),
        quartet=metric("quartet"),
        fields={field: metric(field) for field in FIELDS},
        modules=multilabel("modules"),
        submodules=multilabel("submodules"),
        routing={name: _ratio(*routing[name]) for name in names},
        product_useful_ratio=_ratio(*routing["product_useful_ratio"]),
        prediction_counts=dict(
            missing_predictions=missing, failed_predictions=failed, invalid_predictions=invalid
        ),
    )


def evaluate(gold_rows: list[dict], prediction_rows: list[dict], split: str | None = None) -> dict:
    """Evaluate only explicitly human-confirmed samples; validate all splits first."""
    if split is not None and split not in SPLITS:
        raise ValueError("Invalid split")
    gold = _index(gold_rows, "gold")
    predictions = _index(prediction_rows, "prediction")
    groups: dict[str, str] = {}
    for key, row in gold.items():
        if type(row.get("human_confirmed")) is not bool:
            raise ValueError("human_confirmed must be an explicit boolean")
        if row.get("split") not in SPLITS:
            raise ValueError("Gold split must be dev or holdout")
        if not isinstance(row.get("source_text"), str):
            raise ValueError("Gold source_text is required")
        group = _group(row)
        if row.get("group_id", group) != group:
            raise ValueError("group_id disagrees with note identity; possible leakage")
        if groups.setdefault(group, row["split"]) != row["split"]:
            raise ValueError("Cross-split note/thread leakage")
        record = predictions.get(key)
        if record is not None and record.get("split", row["split"]) != row["split"]:
            raise ValueError("Prediction split disagrees with gold split")
        if row["human_confirmed"]:
            _validate_labels(row.get("expected"), row["source_text"], partial=True)
            assessments = row.get("assessment", {})
            if not isinstance(assessments, dict) or any(
                v is not None and type(v) is not bool for v in assessments.values()
            ):
                raise ValueError("Human assessment labels must be boolean or null")
    selected = [r for r in gold.values() if split is None or r["split"] == split]
    confirmed = [r for r in selected if r["human_confirmed"]]
    report = _aggregate(confirmed, predictions)
    counts = dict(
        total=len(selected),
        confirmed=len(confirmed),
        excluded_unconfirmed=len(selected) - len(confirmed),
        evaluated=len(confirmed),
        **report.pop("prediction_counts"),
        extra_predictions=len(predictions.keys() - gold.keys()),
    )
    slices = sorted(
        {name for r in confirmed for name, active in r.get("slices", {}).items() if active is True}
    )
    report.update(
        counts=counts,
        split=split or "all",
        slices={
            name: _aggregate([r for r in confirmed if r.get("slices", {}).get(name) is True], predictions)
            for name in slices
        },
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    draft = sub.add_parser("prepare", help="Create unconfirmed annotation drafts; no model calls")
    draft.add_argument("--db", type=Path, default=Path(__file__).parent / "data/feedback.sqlite3")
    draft.add_argument(
        "--output", type=Path, default=Path(__file__).parent / "data/evaluation/annotation-draft.jsonl"
    )
    draft.add_argument("--count", type=int, default=80)
    evaluate_parser = sub.add_parser(
        "evaluate", help="Score supplied predictions against human-confirmed labels"
    )
    evaluate_parser.add_argument("--gold", type=Path, required=True)
    evaluate_parser.add_argument("--predictions", type=Path, required=True)
    evaluate_parser.add_argument("--split", choices=sorted(SPLITS))
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            root = (Path(__file__).parent / "data/evaluation").resolve()
            if not args.output.resolve().is_relative_to(root):
                raise ValueError(
                    "CLI draft output must remain in this project's ignored data/evaluation directory"
                )
            rows = prepare(args.db, args.output, args.count)
            print(
                json.dumps(
                    dict(
                        draft_count=len(rows),
                        human_confirmed=0,
                        splits=dict(Counter(r["split"] for r in rows)),
                        output=str(args.output),
                        requested=args.count,
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(
                json.dumps(
                    evaluate(read_jsonl(args.gold), read_jsonl(args.predictions), args.split),
                    ensure_ascii=False,
                    indent=2,
                )
            )
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f"Evaluation error: {exc}\n")


if __name__ == "__main__":
    main()
