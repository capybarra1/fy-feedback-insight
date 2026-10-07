"""Incremental imports and effective opinions, independent of model calls."""

import csv
import hashlib
import io
import json
import re
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from absa import (
    BRAND_LEGACY,
    RULE_VERSION,
    TAXONOMY,
    Opinion,
    canonical_opinion,
    storage_opinion,
    validate_absa,
)
from db import TZ, Database, now, packed

MODULES = list(TAXONOMY) + ["其他", "待确认"]
TYPES = ["体验评价", "问题反馈", "改进建议", "咨询疑问", "购买意向", "对比评价", "其他"]
SENTIMENTS = ["正面", "负面", "中性", "无法判断"]
RELEVANCE = ["unknown", "relevant", "irrelevant", "uncertain"]
REVIEWS = ["unreviewed", "needs_review", "reviewed"]


def digest(value: object) -> str:
    return hashlib.sha256(packed(value).encode()).hexdigest()


def timestamp(value: object) -> str | None:
    if value is None or value == "":
        return None
    try:
        n = float(str(value))
        if n > 100_000_000_000:
            n /= 1000
        return datetime.fromtimestamp(n, TZ).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return dt.replace(tzinfo=TZ).isoformat() if dt.tzinfo is None else dt.astimezone(TZ).isoformat()
        except ValueError:
            return None


def clean(text: str) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b\ufeff]", "", text).strip()


def validate_opinions(items: object, source_text: str) -> list[dict[str, Any]]:
    if not isinstance(items, list) or len(items) > 30:
        raise ValueError("观点必须是列表，单条来源最多 30 个观点")
    result = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("观点格式不正确")
        if item.get("schema_version") == RULE_VERSION or item.get("submodule"):
            try:
                parsed = Opinion.model_validate(canonical_opinion(item)).model_dump()
            except ValueError:
                raise ValueError("ABSA 观点字段不正确，请核对功能、对象、评价及隐含标记") from None
            if parsed["evidence"] not in source_text:
                raise ValueError("证据必须是当前来源原文中的连续片段")
            row_v2 = storage_opinion(parsed)
            theme = item.get("theme")
            if theme is not None:
                if not isinstance(theme, str) or len(theme.strip()) > 120:
                    raise ValueError("主题需为不超过 120 字的文字")
                row_v2["theme"] = theme.strip() or parsed["submodule"]
            sig = packed(row_v2)
            if sig not in seen:
                result.append(row_v2)
                seen.add(sig)
            continue
        row = {k: item.get(k, "") for k in ("module", "type", "sentiment", "theme", "evidence")}
        if not all(isinstance(v, str) for v in row.values()):
            raise ValueError("观点字段必须为文字")
        row = {k: v.strip() for k, v in row.items()}
        if row["module"] not in MODULES or row["type"] not in TYPES or row["sentiment"] not in SENTIMENTS:
            raise ValueError("模块、反馈类型或情感不在支持范围内")
        if not row["theme"] or len(row["theme"]) > 120:
            raise ValueError("请填写不超过 120 字的具体主题")
        if not row["evidence"] or row["evidence"] not in source_text:
            raise ValueError("证据必须是当前来源原文中的连续片段")
        sig = packed(row)
        if sig not in seen:
            result.append(row)
            seen.add(sig)
    return result


class Repository:
    def __init__(self, path: Path):
        self.db = Database(path)

    @staticmethod
    def _source(row: sqlite3.Row, batch_ids: list[str] | None = None) -> dict[str, Any]:
        d = dict(row)
        d["keywords"] = json.loads(d["keywords"])
        d["metadata"] = json.loads(d["metadata"])
        d["manual"] = bool(d["manual"])
        d["has_analysis"] = bool(d["analysis_fingerprint"] or d["manual"])
        d["batch_ids"] = batch_ids or []
        d["content_types"] = json.loads(d.get("content_types", "[]"))
        d["review_reasons"] = json.loads(d.get("review_reasons", "[]"))
        d["eligible_for_insights"] = (
            d.get("routing_schema") != RULE_VERSION
            and d.get("analysis_schema") != RULE_VERSION
            or (
                d["brand_relevance"] == "related"
                and bool(set(d["content_types"]) & {"product_feedback", "product_question"})
            )
        )
        return d

    def sources(self) -> list[dict[str, Any]]:
        with self.db.connect() as c:
            links: dict[str, list[str]] = {}
            for row in c.execute("SELECT * FROM batch_sources"):
                links.setdefault(row["source_key"], []).append(row["batch_id"])
            return [
                self._source(r, links.get(r["key"], []))
                for r in c.execute("SELECT * FROM sources ORDER BY first_seen DESC,key")
            ]

    def source(self, key: str) -> dict[str, Any]:
        with self.db.connect() as c:
            row = c.execute("SELECT * FROM sources WHERE key=?", (key,)).fetchone()
            if row is None:
                raise KeyError("找不到这条来源")
            ids = [r[0] for r in c.execute("SELECT batch_id FROM batch_sources WHERE source_key=?", (key,))]
            return self._source(row, ids)

    @staticmethod
    def _opinion(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        for field in ("implicit_target", "implicit_opinion", "context_used", "needs_review"):
            value[field] = bool(value[field])
        return value

    def opinions(self) -> list[dict[str, Any]]:
        with self.db.connect() as c:
            return [self._opinion(r) for r in c.execute("SELECT * FROM opinions ORDER BY rowid")]

    def batches(self) -> list[dict[str, Any]]:
        with self.db.connect() as c:
            return [
                dict(
                    id=r["id"],
                    filename=r["filename"],
                    created_at=r["created_at"],
                    site=r["site"],
                    **json.loads(r["counts"]),
                    errors=json.loads(r["errors"]),
                )
                for r in c.execute("SELECT * FROM batches ORDER BY created_at DESC,rowid DESC")
            ]

    def import_files(self, files: list[dict[str, str]], site: str) -> dict[str, Any]:
        if site not in ("rednote", "xiaohongshu"):
            raise ValueError("请选择来源站点")
        if (
            not files
            or len(files) > 20
            or sum(len(f.get("content", "").encode()) for f in files) > 30_000_000
        ):
            raise ValueError("单次支持 1–20 个文件，总计不超过 30 MB")
        totals = dict(new=0, updated=0, duplicate=0, invalid=0)
        ids = []
        for file in files:
            bid, current = uuid.uuid4().hex, now()
            counts, errors = dict(new=0, updated=0, duplicate=0, invalid=0), []
            with self.db.connect() as c:
                c.execute("BEGIN IMMEDIATE")
                c.execute(
                    "INSERT INTO batches VALUES (?,?,?,?,?,?)",
                    (bid, Path(file["name"]).name, current, site, "{}", "[]"),
                )
                for line_no, line in enumerate(file["content"].lstrip("\ufeff").splitlines(), 1):
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                        if not isinstance(data, dict):
                            raise ValueError("记录必须是对象")
                        kind = "comment" if "comment_id" in data else "note"
                        raw_id = data.get("comment_id" if kind == "comment" else "note_id")
                        note_id = data.get("note_id")
                        if (
                            not isinstance(raw_id, (str, int))
                            or isinstance(raw_id, bool)
                            or not str(raw_id).strip()
                        ):
                            raise ValueError("缺少内容编号")
                        if not isinstance(note_id, (str, int)) or not str(note_id).strip():
                            raise ValueError("缺少所属笔记编号")
                        title = data.get("title", "") if kind == "note" else ""
                        body = data.get("desc", "") if kind == "note" else data.get("content", "")
                        if not isinstance(title, str) or not isinstance(body, str):
                            raise ValueError("正文或标题必须是文字")
                        text = title + "\n\n" + body if title and title != body else body or title
                        if not clean(text):
                            raise ValueError("正文为空")
                        if len(text) > 200_000:
                            raise ValueError("单条内容超过 20 万字，无法导入")
                        external_id = str(raw_id).strip()
                        key, fp = f"xhs:{kind}:{external_id}", digest(clean(text))
                        old = c.execute("SELECT * FROM sources WHERE key=?", (key,)).fetchone()
                        keyword = data.get("source_keyword", "")
                        kws = [keyword] if isinstance(keyword, str) and keyword else []
                        collected = timestamp(data.get("last_modify_ts"))
                        published = timestamp(data.get("time" if kind == "note" else "create_time"))
                        metadata = {
                            k: data[k]
                            for k in ("liked_count", "like_count", "comment_count", "sub_comment_count")
                            if k in data and isinstance(data[k], (int, str))
                        }
                        parent = str(data.get("parent_comment_id") or "")
                        if old:
                            kws = sorted(set(json.loads(old["keywords"]) + kws))
                            # Out-of-order snapshots must not roll a newer body backwards.
                            stale = bool(
                                collected and old["collected_at"] and collected < old["collected_at"]
                            )
                            updated = fp != old["fingerprint"] and not stale
                            counts["updated" if updated else "duplicate"] += 1
                            if updated:
                                self._history(
                                    c,
                                    key,
                                    "content_updated",
                                    {"source": self._source(old), "opinions": self._opinions(c, key)},
                                )
                                c.execute(
                                    "UPDATE sources SET title=?,text=?,fingerprint=?,version=version+1,status=?,review_status=?,error=? WHERE key=?",
                                    (
                                        title,
                                        text,
                                        fp,
                                        "pending",
                                        "needs_review",
                                        "正文已更新，请复核已有结果",
                                        key,
                                    ),
                                )
                            c.execute(
                                "UPDATE sources SET keywords=?,last_seen=? WHERE key=?",
                                (packed(kws), current, key),
                            )
                            if not stale:
                                c.execute(
                                    "UPDATE sources SET site=?,collected_at=COALESCE(?,collected_at),published_at=COALESCE(?,published_at),metadata=?,parent_id=? WHERE key=?",
                                    (site, collected, published, packed(metadata), parent, key),
                                )
                                if parent != old["parent_id"]:
                                    self._mark_context(c, key)
                        else:
                            similar = c.execute(
                                "SELECT key FROM sources WHERE fingerprint=? LIMIT 1", (fp,)
                            ).fetchone()
                            c.execute(
                                """INSERT INTO sources (key,kind,external_id,note_id,parent_id,title,text,site,keywords,published_at,first_seen,last_seen,collected_at,fingerprint,duplicate_of,metadata)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (
                                    key,
                                    kind,
                                    external_id,
                                    str(note_id),
                                    parent,
                                    title,
                                    text,
                                    site,
                                    packed(kws),
                                    published,
                                    current,
                                    current,
                                    collected,
                                    fp,
                                    similar[0] if similar else None,
                                    packed(metadata),
                                ),
                            )
                            counts["new"] += 1
                        if old is None or (old and updated):
                            if kind == "note":
                                dependents = c.execute(
                                    "SELECT key FROM sources WHERE kind='comment' AND note_id=?",
                                    (str(note_id),),
                                ).fetchall()
                            else:
                                dependents = c.execute(
                                    "SELECT key FROM sources WHERE kind='comment' AND note_id=? AND parent_id=?",
                                    (str(note_id), external_id),
                                ).fetchall()
                            for dependent in dependents:
                                self._mark_context(c, dependent[0])
                        c.execute("INSERT OR IGNORE INTO batch_sources VALUES (?,?)", (bid, key))
                    except (ValueError, TypeError, OverflowError) as exc:
                        counts["invalid"] += 1
                        errors.append(
                            {
                                "line": line_no,
                                "message": str(exc)
                                if not isinstance(exc, json.JSONDecodeError)
                                else "JSON 格式错误",
                            }
                        )
                c.execute(
                    "UPDATE batches SET counts=?,errors=? WHERE id=?",
                    (packed({**counts, "count": sum(counts.values())}), packed(errors), bid),
                )
            for k in totals:
                totals[k] += counts[k]
            ids.append(bid)
        return {"batches": [b for b in self.batches() if b["id"] in ids], "totals": totals}

    @staticmethod
    def _mark_context(c: sqlite3.Connection, key: str) -> None:
        c.execute(
            "UPDATE sources SET review_status='needs_review',error='关联上下文已变化，请人工复核；不会自动重新调用 AI' WHERE key=? AND (manual=1 OR analysis_fingerprint IS NOT NULL)",
            (key,),
        )

    @staticmethod
    def _opinions(c: sqlite3.Connection, key: str) -> list[dict[str, Any]]:
        return [
            Repository._opinion(r) for r in c.execute("SELECT * FROM opinions WHERE source_key=?", (key,))
        ]

    @staticmethod
    def _history(c: sqlite3.Connection, key: str, event: str, snapshot: object) -> None:
        c.execute(
            "INSERT INTO history(source_key,created_at,event,snapshot) VALUES (?,?,?,?)",
            (key, now(), event, packed(snapshot)),
        )

    @staticmethod
    def _replace(c: sqlite3.Connection, key: str, items: list[dict[str, Any]], origin: str) -> None:
        c.execute("DELETE FROM opinions WHERE source_key=?", (key,))
        for item in items:
            record = dict(item, id=uuid.uuid4().hex, source_key=key, origin=origin)
            fields = ["id", "source_key", "module", "type", "sentiment", "theme", "evidence", "origin"]
            if item.get("schema_version") == RULE_VERSION:
                fields += [
                    "submodule",
                    "aspect_category",
                    "target_text",
                    "opinion_text",
                    "feedback_type",
                    "sentiment_code",
                    "implicit_target",
                    "implicit_opinion",
                    "context_used",
                    "needs_review",
                    "schema_version",
                ]
            c.execute(
                f"INSERT INTO opinions ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                [record[f] for f in fields],
            )

    @staticmethod
    def _routing(
        c, key: str, result: dict, *, filter_reason: str = "", analysis_schema: str = RULE_VERSION
    ) -> None:
        c.execute(
            "UPDATE sources SET brand_relevance=?,product_scope=?,content_types=?,review_reasons=?,analysis_schema=?,routing_schema=?,filter_reason=? WHERE key=?",
            (
                result["brand_relevance"],
                result["product_scope"],
                packed(result["content_types"]),
                packed(result["review_reasons"]),
                analysis_schema,
                RULE_VERSION,
                filter_reason,
                key,
            ),
        )

    def review(self, key: str, value: dict[str, Any]) -> None:
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM sources WHERE key=?", (key,)).fetchone()
            if row is None:
                raise KeyError("找不到这条来源")
            if value.get("relevance") not in RELEVANCE or value.get("review_status") not in REVIEWS:
                raise ValueError("相关性或复核状态不正确")
            items = validate_opinions(value.get("opinions", []), row["text"])
            if value["relevance"] == "irrelevant" and items:
                raise ValueError("不相关内容不能同时保留有效观点，请先移除观点")
            self._history(
                c, key, "manual_review", {"source": self._source(row), "opinions": self._opinions(c, key)}
            )
            has_routing = any(
                k in value for k in ("brand_relevance", "product_scope", "content_types", "review_reasons")
            )
            all_v2 = all(o.get("schema_version") == RULE_VERSION for o in items)
            if row["analysis_schema"] == RULE_VERSION and not all_v2:
                raise ValueError("新版结果不能降级为旧版观点，请补全二级功能与原文对象")
            if (
                has_routing
                or row["analysis_schema"] == RULE_VERSION
                or value.get("analysis_schema") == RULE_VERSION
            ):
                old = self._source(row)
                result = validate_absa(
                    {
                        "brand_relevance": value.get("brand_relevance", old["brand_relevance"]),
                        "product_scope": value.get("product_scope", old["product_scope"]),
                        "content_types": value.get("content_types", old["content_types"]),
                        "review_reasons": value.get("review_reasons", old["review_reasons"]),
                        "opinions": [canonical_opinion(o) for o in items] if all_v2 else [],
                    },
                    row["text"],
                )
                if items and (
                    result["brand_relevance"] == "unrelated"
                    or not set(result["content_types"]) & {"product_feedback", "product_question"}
                ):
                    raise ValueError("无关或无产品反馈的内容不能保留观点")
                if result["brand_relevance"] == "uncertain" and any(
                    o.get("schema_version") == RULE_VERSION and not o["needs_review"] for o in items
                ):
                    raise ValueError("品牌不确定的观点需要复核")
                self._routing(c, key, result, analysis_schema=RULE_VERSION if all_v2 else "legacy")
                value = dict(value, relevance=BRAND_LEGACY[result["brand_relevance"]])
                if (
                    result["brand_relevance"] == "uncertain"
                    or result["review_reasons"]
                    or any(o.get("needs_review") for o in items)
                ) and value["review_status"] == "reviewed":
                    raise ValueError("仍有不确定标记或复核原因，请先修正后再标为已复核")
            self._replace(c, key, items, "manual")
            c.execute(
                "UPDATE sources SET manual=1,status=?,relevance=?,review_status=?,error=?,analysis_text=text,analysis_version=version WHERE key=?",
                ("done", value["relevance"], value["review_status"], "", key),
            )

    def save_ai(self, key: str, result: dict[str, Any], version: int, fingerprint: str) -> bool:
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM sources WHERE key=?", (key,)).fetchone()
            if row is None:
                return False
            self._history(
                c,
                key,
                "ai_result",
                {"result": result, "version": version, "analysis_fingerprint": fingerprint},
            )
            if row["manual"] or row["version"] != version:
                return False
            is_v2 = "brand_relevance" in result
            if is_v2:
                result = validate_absa(result, row["text"])
                items = [storage_opinion(o) for o in result["opinions"]]
                relevance = BRAND_LEGACY[result["brand_relevance"]]
                self._routing(c, key, result)
            else:
                items = validate_opinions(result.get("opinions"), row["text"])
                relevance = str(result.get("relevance", ""))
            if relevance not in ("relevant", "irrelevant", "uncertain"):
                raise ValueError("AI 相关性字段无效")
            if relevance != "relevant" and items and not is_v2:
                raise ValueError("非明确相关的记录不得输出有效观点")
            self._replace(c, key, items, "ai")
            needs_review = (
                relevance == "uncertain"
                or bool(result.get("review_reasons"))
                or any(
                    o["module"] in ("待确认", "无法判断")
                    or o["sentiment"] == "无法判断"
                    or o.get("needs_review")
                    for o in items
                )
            )
            c.execute(
                "UPDATE sources SET status=?,relevance=?,review_status=?,error=?,analysis_fingerprint=?,analysis_text=text,analysis_version=version WHERE key=?",
                ("done", relevance, "needs_review" if needs_review else "unreviewed", "", fingerprint, key),
            )
            c.execute("INSERT OR REPLACE INTO cache VALUES (?,?)", (fingerprint, packed(result)))
            return True

    def save_skip(self, key: str, version: int, reason: str, result: dict) -> bool:
        fingerprint = digest({"rule": RULE_VERSION, "skip": reason, "key": key, "version": version})
        saved = self.save_ai(key, result, version, fingerprint)
        if saved:
            with self.db.connect() as c:
                c.execute(
                    "UPDATE sources SET status='skipped',filter_reason=? WHERE key=? AND version=? AND manual=0",
                    (reason, key, version),
                )
        return saved

    def set_status(self, key: str, status: str, error: str = "") -> None:
        with self.db.connect() as c:
            c.execute("UPDATE sources SET status=?,error=? WHERE key=? AND manual=0", (status, error, key))

    def detail(self, key: str) -> dict[str, Any]:
        source = self.source(key)
        context = []
        if source["kind"] == "comment":
            for parent in [f"xhs:note:{source['note_id']}", f"xhs:comment:{source['parent_id']}"]:
                if parent == key:
                    continue
                try:
                    s = self.source(parent)
                    if s["note_id"] == source["note_id"]:
                        context.append({"kind": s["kind"], "title": s["title"], "text": s["text"]})
                except KeyError:
                    pass
        with self.db.connect() as c:
            history = [
                {**dict(r), "snapshot": json.loads(r["snapshot"])}
                for r in c.execute(
                    "SELECT created_at,event,snapshot FROM history WHERE source_key=? ORDER BY id DESC LIMIT 30",
                    (key,),
                )
            ]
            opinions = self._opinions(c, key)
        host = "www.rednote.com" if source["site"] == "rednote" else "www.xiaohongshu.com"
        from urllib.parse import quote

        return {
            "source": source,
            "opinions": opinions,
            "context": context,
            "history": history,
            "url": f"https://{host}/explore/{quote(source['note_id'], safe='')}",
        }

    def delete_batch(self, bid: str) -> dict[str, int]:
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            keys = [r[0] for r in c.execute("SELECT source_key FROM batch_sources WHERE batch_id=?", (bid,))]
            c.execute("DELETE FROM batches WHERE id=?", (bid,))
            deleted = 0
            for key in keys:
                if c.execute("SELECT 1 FROM batch_sources WHERE source_key=?", (key,)).fetchone() is None:
                    source = c.execute("SELECT * FROM sources WHERE key=?", (key,)).fetchone()
                    if source["kind"] == "note":
                        dependents = c.execute(
                            "SELECT key FROM sources WHERE kind='comment' AND note_id=?", (source["note_id"],)
                        ).fetchall()
                    else:
                        dependents = c.execute(
                            "SELECT key FROM sources WHERE kind='comment' AND note_id=? AND parent_id=?",
                            (source["note_id"], source["external_id"]),
                        ).fetchall()
                    for dependent in dependents:
                        self._mark_context(c, dependent[0])
                    c.execute("DELETE FROM sources WHERE key=?", (key,))
                    deleted += 1
            if deleted:
                # Cache may include evidence from old revisions, including private original text.
                # Clearing reusable cache never removes remaining sources' effective opinions.
                c.execute("DELETE FROM cache")
            return {"deleted_sources": deleted, "retained_sources": len(keys) - deleted}

    def filtered_opinions(self, filters: dict[str, str]) -> list[dict[str, Any]]:
        sources = {s["key"]: s for s in self.sources()}
        result = []
        for opinion in self.opinions():
            s = sources[opinion["source_key"]]
            if (
                not s["eligible_for_insights"]
                or s["relevance"] != "relevant"
                or not (s["status"] == "done" or s["manual"] or s["has_analysis"])
            ):
                continue
            if filters.get("submodule") and opinion.get("submodule") != filters["submodule"]:
                continue
            if filters.get("content_type") and filters["content_type"] not in s["content_types"]:
                continue
            if filters.get("scope") and s["product_scope"] != filters["scope"]:
                continue
            if filters.get("schema") and s["analysis_schema"] != filters["schema"]:
                continue
            if filters.get("needs_review") in ("true", "false") and bool(opinion.get("needs_review")) != (
                filters["needs_review"] == "true"
            ):
                continue
            if filters.get("kind") and s["kind"] != filters["kind"]:
                continue
            module = filters.get("module")
            if module == "focus" and opinion["module"] not in MODULES[:4]:
                continue
            if module and module != "focus" and opinion["module"] != module:
                continue
            if any(filters.get(k) and opinion[k] != filters[k] for k in ("type", "sentiment", "theme")):
                continue
            if filters.get("review") and s["review_status"] != filters["review"]:
                continue
            if filters.get("batch") and filters["batch"] not in s["batch_ids"]:
                continue
            day = (s["published_at"] or "")[:10]
            if filters.get("from") and (not day or day < filters["from"]):
                continue
            if filters.get("to") and (not day or day > filters["to"]):
                continue
            q = filters.get("q", "").casefold()
            if q and q not in (s["title"] + " " + s["text"] + " " + opinion["theme"]).casefold():
                continue
            result.append({**opinion, "source": s})
        return result

    def export_csv(self, filters: dict[str, str]) -> str:
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(
            [
                "来源编号",
                "来源类型",
                "发布时间",
                "首次导入时间",
                "原文",
                "模块",
                "反馈类型",
                "情感",
                "主题",
                "证据",
                "复核状态",
                "原文链接",
                "二级功能",
                "标准类别",
                "原文对象",
                "评价表达",
                "隐含对象",
                "隐含评价",
                "使用上下文",
                "观点需复核",
                "反馈类型代码",
                "情感代码",
                "分析版本",
                "品牌相关性",
                "研究范围",
                "内容类别",
                "复核原因",
            ]
        )

        def safe(value: object) -> str:
            t = "" if value is None else str(value)
            return "'" + t if t.lstrip().startswith(("=", "+", "-", "@")) or t.startswith(("\t", "\r")) else t

        for o in self.filtered_opinions(filters):
            s = o["source"]
            writer.writerow(
                [
                    safe(v)
                    for v in [
                        s["external_id"],
                        "笔记" if s["kind"] == "note" else "评论",
                        s["published_at"],
                        s["first_seen"],
                        s["analysis_text"] or s["text"],
                        o["module"],
                        o["type"],
                        o["sentiment"],
                        o["theme"],
                        o["evidence"],
                        s["review_status"],
                        self.detail(s["key"])["url"],
                        o.get("submodule"),
                        o.get("aspect_category"),
                        o.get("target_text"),
                        o.get("opinion_text"),
                        bool(o.get("implicit_target")),
                        bool(o.get("implicit_opinion")),
                        bool(o.get("context_used")),
                        bool(o.get("needs_review")),
                        o.get("feedback_type"),
                        o.get("sentiment_code"),
                        o.get("schema_version", "legacy"),
                        s["brand_relevance"],
                        s["product_scope"],
                        " / ".join(s["content_types"]),
                        " / ".join(s["review_reasons"]),
                    ]
                ]
            )
        return "\ufeff" + stream.getvalue()
