"""Explicit, budgeted AI analysis. No calls occur during import or app startup."""

import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from statistics import mean
from typing import Any, Callable, Literal
from urllib.parse import urlparse

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field

from absa import RULE_VERSION, SYSTEM, noise_reason, noise_result, validate_absa
from db import Database, now, packed
from provider import ProviderClient, ProviderError, provider_request
from repository import Repository, digest, validate_opinions

MICROS = 1_000_000


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    base_url: str = ""
    model: str = ""
    input_price: float | None = Field(default=None, ge=0, le=100000)
    output_price: float | None = Field(default=None, ge=0, le=100000)
    monthly_budget: float = Field(default=20, ge=0, le=10000)
    max_output_tokens: int = Field(default=2000, ge=200, le=8000)
    max_input_chars: int = Field(default=16000, ge=100, le=100000)
    concurrency: int = Field(default=2, ge=1, le=4)
    reasoning_effort: Literal["low", "high", "max"] = "low"
    format_retries: int = Field(default=1, ge=0, le=1)


class Settings:
    def __init__(self, db: Database, *, env_path: Path | None = None):
        self.db, self.api_key = db, ""
        self.env_path = env_path.resolve() if env_path else None
        self.key_source = "unset"
        if self.env_path and self.env_path.is_file():
            key = dotenv_values(self.env_path, interpolate=False).get("FIREFLY_API_KEY") or ""
            if len(key) > 1000 or any(ord(char) < 32 or ord(char) > 126 for char in key):
                raise ValueError(".env 中的 FIREFLY_API_KEY 格式不正确，请填写单行密钥")
            self.api_key = key.strip()
            self.key_source = "env" if self.api_key else "unset"
        self.lock = threading.RLock()
        with db.connect() as c:
            row = c.execute("SELECT value FROM settings WHERE id=1").fetchone()
        self.config = Config.model_validate_json(row[0]) if row else Config()

    def public(self) -> dict[str, Any]:
        with self.lock:
            return {
                **self.config.model_dump(),
                "has_api_key": bool(self.api_key),
                "key_source": self.key_source,
                "env_file": str(self.env_path) if self.env_path else "",
            }

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {**self.config.model_dump(), "api_key": self.api_key}

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        changes = dict(changes)
        api_key = changes.pop("api_key", None)
        clear_key = changes.pop("clear_key", False)
        if api_key is not None and (not isinstance(api_key, str) or len(api_key) > 1000):
            raise ValueError("API 密钥格式不正确")
        with self.lock:
            config = Config.model_validate({**self.config.model_dump(), **changes})
            if config.base_url:
                url = urlparse(config.base_url)
                if (
                    url.scheme != "https"
                    or not url.hostname
                    or url.username
                    or url.password
                    or url.query
                    or url.fragment
                ):
                    raise ValueError("服务地址需为 HTTPS API 根地址，不含账号、参数或片段")
            config.base_url = config.base_url.rstrip("/")
            config.model = config.model.strip()
            self.config = config
            if clear_key:
                self.api_key = ""
                self.key_source = "unset"
            elif api_key:
                self.api_key = api_key.strip()
                self.key_source = "session" if self.api_key else "unset"
            with self.db.connect() as c:
                c.execute("INSERT OR REPLACE INTO settings VALUES (1,?)", (config.model_dump_json(),))
            return self.public()


def micros(value: float) -> int:
    return int((Decimal(str(value)) * MICROS).to_integral_value(rounding=ROUND_CEILING))


class Budget:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _totals(c, month: str) -> tuple[int, int]:
        row = c.execute(
            """SELECT COALESCE(SUM(CASE WHEN status='settled' THEN amount_micros ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN status!='settled' THEN reserved_micros ELSE 0 END),0) FROM ledger WHERE month=?""",
            (month,),
        ).fetchone()
        return row[0], row[1]

    def summary(self, limit: float) -> dict[str, Any]:
        month = now()[:7]
        with self.db.connect() as c:
            spent, reserved = self._totals(c, month)
        return {
            "month": month,
            "spent": spent / MICROS,
            "reserved": reserved / MICROS,
            "remaining": max(0, micros(limit) - spent - reserved) / MICROS,
        }

    def reserve(self, key: str, cost: float, limit: float) -> str:
        created = now()
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            spent, reserved = self._totals(c, created[:7])
            amount = micros(cost)
            if spent + reserved + amount > micros(limit):
                raise ValueError("本月剩余预算不足，未发起请求")
            lid = uuid.uuid4().hex
            c.execute(
                "INSERT INTO ledger(id,source_key,created_at,month,status,reserved_micros) VALUES (?,?,?,?,?,?)",
                (lid, key, created, created[:7], "reserved", amount),
            )
        return lid

    def settle(self, lid: str, cost: float) -> None:
        with self.db.connect() as c:
            c.execute(
                "UPDATE ledger SET status=?,amount_micros=?,error=? WHERE id=?",
                ("settled", micros(cost), "", lid),
            )

    def unknown(self, lid: str, error: str) -> None:
        with self.db.connect() as c:
            c.execute("UPDATE ledger SET status=?,error=? WHERE id=?", ("unknown", error, lid))

    def ledger(self) -> list[dict[str, Any]]:
        with self.db.connect() as c:
            return [
                {**dict(r), "amount": r["amount_micros"] / MICROS, "reserved": r["reserved_micros"] / MICROS}
                for r in c.execute("SELECT * FROM ledger ORDER BY created_at DESC,rowid DESC LIMIT 200")
            ]

    def record_attempt(self, lid: str, data: dict) -> None:
        fields = (
            "outcome",
            "finish_reason",
            "duration_ms",
            "prompt_tokens",
            "completion_tokens",
            "retry_index",
            "model",
            "rule_version",
            "json_valid",
        )
        with self.db.connect() as c:
            c.execute(
                "UPDATE ledger SET " + ",".join(f"{f}=?" for f in fields) + " WHERE id=?",
                [data.get(f) for f in fields] + [lid],
            )

    def metrics(self) -> dict:
        with self.db.connect() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM ledger")]
        measured = [r for r in rows if r["outcome"]]
        n = len(measured)
        received = [
            r
            for r in measured
            if r["outcome"] in ("success", "schema_error", "json_error", "truncated", "finish_error")
        ]
        json_measured = [r for r in received if r["json_valid"] is not None]
        durations = sorted(r["duration_ms"] / 1000 for r in measured if r["duration_ms"] is not None)

        def rate(count, total):
            return count / total if total else None

        return {
            "requests": n,
            "legacy_requests": sum(not r["rule_version"] and not r["outcome"] for r in rows),
            "request_success_rate": rate(len(received), n),
            "json_success_rate": rate(sum(bool(r["json_valid"]) for r in json_measured), len(json_measured)),
            "schema_success_rate": rate(sum(r["outcome"] == "success" for r in received), len(received)),
            "truncation_rate": rate(sum(r["outcome"] == "truncated" for r in measured), n),
            "retry_rate": rate(sum((r["retry_index"] or 0) > 0 for r in measured), n),
            "avg_latency_seconds": mean(durations) if durations else None,
            "p95_latency_seconds": durations[min(len(durations) - 1, int(len(durations) * 0.95))]
            if durations
            else None,
            "cost": sum(r["amount_micros"] for r in measured) / MICROS,
        }


def redact(text: str) -> tuple[str, dict[str, str]]:
    replacements: dict[str, str] = {}
    pattern = r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)|(?<!\d)\d{17}[0-9Xx](?!\d)|https?://[^\s<>]+"

    def replace(match: re.Match[str]) -> str:
        marker = f"【隐私字段{len(replacements) + 1}】"
        replacements[marker] = match.group(0)
        return marker

    return re.sub(pattern, replace, text), replacements


def validate_result(value: object, text: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("relevance") not in ("relevant", "irrelevant", "uncertain"):
        raise ValueError("AI 返回的相关性字段不正确")
    opinions = validate_opinions(value.get("opinions"), text)
    if value["relevance"] != "relevant" and opinions:
        raise ValueError("非明确相关内容不应有有效观点")
    return {"relevance": value["relevance"], "opinions": opinions}


class Analyzer:
    def __init__(self, repo: Repository, settings: Settings, transport: Callable = provider_request):
        self.repo, self.settings, self.transport = repo, settings, transport
        self.budget = Budget(repo.db)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.estimates: dict[str, dict[str, Any]] = {}
        self.job: dict[str, Any] = {
            "running": False,
            "status": "idle",
            "total": 0,
            "completed": 0,
            "message": "等待你开始分析",
        }
        with repo.db.connect() as c:
            stale = c.execute("SELECT source_key FROM ledger WHERE status='reserved'").fetchall()
            c.execute(
                "UPDATE ledger SET status='unknown',error='上次运行中断，用量待核对' WHERE status='reserved'"
            )
            for row in stale:
                c.execute(
                    "UPDATE sources SET status='unknown',error='上次请求中断，请核对费用后重试' WHERE key=? AND manual=0",
                    (row[0],),
                )
            last = c.execute("SELECT value FROM jobs WHERE id='latest'").fetchone()
        if last:
            self.job = json.loads(last[0])
            if self.job.get("running"):
                self.job.update(
                    running=False, status="paused", message="上次任务中断；已完成结果保留，可继续待处理项"
                )

    def state(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.job)

    def _job(self, **changes) -> None:
        with self.lock:
            self.job.update(changes)
            with self.repo.db.connect() as c:
                c.execute("INSERT OR REPLACE INTO jobs VALUES ('latest',?)", (packed(self.job),))

    def _signature(self) -> str:
        return digest(
            {
                "config": self.settings.snapshot(),
                "sources": [
                    (s["key"], s["version"], s["status"], s["manual"], s["text"], s["parent_id"])
                    for s in self.repo.sources()
                ],
            }
        )

    def _item(self, source: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        detail = self.repo.detail(source["key"])
        text, replacements = redact(source["text"])
        brand_context = [redact(c["text"])[0] for c in detail["context"] if c["kind"] == "note"]
        payload = {"source_kind": source["kind"], "source_text": text, "brand_context": brand_context}
        skipped = noise_reason(source["text"])
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": packed(payload)}]
        size = sum(len(m["content"]) for m in messages)
        if size > config["max_input_chars"] and not skipped:
            raise ValueError(f"正文与上下文共 {size} 字，超过当前输入上限；请调整上限后重试")
        # Conservative byte-based token estimate plus chat framing allowance.
        tokens = sum(len(m["content"].encode("utf-8")) for m in messages) + 512
        cost = (
            (tokens + 1024) * (config["input_price"] or 0)
            + config["max_output_tokens"] * (config["output_price"] or 0)
        ) / MICROS
        fingerprint = digest(
            {
                "payload": payload,
                "original_hash": digest(source["text"]),
                "model": config["model"],
                "base_url": config["base_url"],
                "rules": RULE_VERSION,
                "prompt_hash": digest(SYSTEM),
                "reasoning_effort": config["reasoning_effort"],
                "max_output_tokens": config["max_output_tokens"],
            }
        )
        with self.repo.db.connect() as c:
            cache = c.execute("SELECT result FROM cache WHERE fingerprint=?", (fingerprint,)).fetchone()
        return {
            "key": source["key"],
            "version": source["version"],
            "messages": messages,
            "preview": payload,
            "replacements": replacements,
            "fingerprint": fingerprint,
            "cost": 0 if cache or skipped else micros(cost) / MICROS,
            "skip_reason": skipped,
            "cache": json.loads(cache[0]) if cache else None,
        }

    def estimate(self, request: dict[str, Any]) -> dict[str, Any]:
        config = self.settings.snapshot()
        mode = request.get("mode", "pending")
        if mode not in ("pending", "failed", "selected", "legacy"):
            raise ValueError("分析范围不正确")
        keys = set(request.get("source_keys") or [])
        items, excluded = [], []
        for source in self.repo.sources():
            eligible = (
                source["key"] in keys
                if mode == "selected"
                else (source["analysis_schema"] != RULE_VERSION and source["status"] in ("done", "failed"))
                if mode == "legacy"
                else source["status"] == ("failed" if mode == "failed" else "pending")
            )
            if not eligible:
                continue
            if source["manual"] or source["status"] == "unknown":
                excluded.append({"key": source["key"], "reason": "人工结果受保护，或请求状态未知"})
                continue
            try:
                items.append(self._item(source, config))
            except ValueError as exc:
                excluded.append({"key": source["key"], "reason": str(exc)})
        cost = round(
            sum({i["fingerprint"]: i["cost"] for i in items}.values()) * (1 + config["format_retries"]), 6
        )
        paid = any(i["cost"] > 0 or not (i["skip_reason"] or i["cache"]) for i in items)
        reason = ""
        if not items:
            reason = "没有符合条件的待分析内容"
        elif paid and (not config["api_key"] or not config["base_url"] or not config["model"]):
            reason = "请先在设置中填写模型服务、型号与 API 密钥"
        elif paid and (config["input_price"] is None or config["output_price"] is None):
            reason = "请先填写模型输入与输出价格"
        elif cost > self.budget.summary(config["monthly_budget"])["remaining"]:
            reason = "预计费用超过本月剩余额度，请缩小选择范围"
        elif self.state()["running"]:
            reason = "已有分析任务正在运行"
        token = uuid.uuid4().hex
        estimate = {
            "items": items,
            "signature": self._signature(),
            "created": time.monotonic(),
            "can_start": not reason,
            "config": config,
        }
        with self.lock:
            self.estimates = {token: estimate}
        return {
            "count": len(items),
            "noise_count": sum(bool(i["skip_reason"]) for i in items),
            "estimated_cost": cost,
            "can_start": not reason,
            "reason": reason,
            "estimate_token": token,
            "preview": items[0]["preview"] if items else {"source_text": "", "context": []},
            "excluded": excluded,
        }

    def start(self, token: str) -> dict[str, Any]:
        with self.lock:
            estimate = self.estimates.get(token)
            if self.job["running"]:
                raise ValueError("已有任务正在运行")
            if (
                not estimate
                or time.monotonic() - estimate["created"] > 300
                or estimate["signature"] != self._signature()
            ):
                raise ValueError("数据或设置已变化，请重新预估")
            if not estimate["can_start"]:
                raise ValueError("配置或预算不足，请调整后重新预估")
            self.estimates.clear()
            self.stop_event.clear()
            self._job(
                running=True,
                status="running",
                total=len(estimate["items"]),
                completed=0,
                succeeded=0,
                failed=0,
                skipped=0,
                message="正在执行 ABSA 分析",
            )
            self.thread = threading.Thread(
                target=self._run, args=(estimate["items"], estimate["config"]), daemon=True
            )
            self.thread.start()
            return {"ok": True, "job": dict(self.job)}

    def stop(self) -> None:
        self.stop_event.set()
        if self.state()["running"]:
            self._job(message="停止请求已接收，等待当前请求结束")

    def wait(self, timeout: float = 10) -> None:
        if self.thread:
            self.thread.join(timeout)

    def _usage_cost(self, usage: dict[str, Any], config: dict[str, Any]) -> float | None:
        inp, out = usage.get("prompt_tokens"), usage.get("completion_tokens")
        if (
            not isinstance(inp, int)
            or isinstance(inp, bool)
            or not isinstance(out, int)
            or isinstance(out, bool)
            or inp < 0
            or out < 0
        ):
            return None
        return (inp * config["input_price"] + out * config["output_price"]) / MICROS

    def _advance(self, outcome: str) -> None:
        with self.lock:
            self._job(completed=self.job["completed"] + 1, **{outcome: self.job.get(outcome, 0) + 1})

    def _process(
        self,
        item: dict,
        config: dict,
        transport: Callable,
        halted: threading.Event,
        previous_failure: str | None = None,
    ) -> str | None:
        if self.stop_event.is_set() or halted.is_set():
            return None
        try:
            source = self.repo.source(item["key"])
            if source["manual"] or source["version"] != item["version"]:
                self._advance("skipped")
                return None
        except KeyError:
            self._advance("skipped")
            return None
        if previous_failure is not None:
            self.repo.set_status(item["key"], "failed", previous_failure)
            self._advance("failed")
            return previous_failure
        if item["skip_reason"]:
            self.repo.save_skip(item["key"], item["version"], item["skip_reason"], noise_result())
            self._advance("skipped")
            return None
        with self.repo.db.connect() as c:
            cached = c.execute(
                "SELECT result FROM cache WHERE fingerprint=?", (item["fingerprint"],)
            ).fetchone()
        if cached:
            try:
                result = validate_absa(json.loads(cached[0]), source["text"])
                self.repo.save_ai(item["key"], result, item["version"], item["fingerprint"])
                self._advance("succeeded")
                return None
            except (ValueError, TypeError):
                # Do not make an unestimated paid call for a malformed free cache.
                error = "旧缓存未通过 ABSA 校验，请重新预估后重试"
                self.repo.set_status(item["key"], "failed", error)
                with self.repo.db.connect() as c:
                    c.execute("DELETE FROM cache WHERE fingerprint=?", (item["fingerprint"],))
                self._advance("failed")
                return error
        messages = item["messages"]
        for attempt in range(1 + config["format_retries"]):
            if self.stop_event.is_set() or halted.is_set():
                return None
            # A manual correction can happen while other items are in flight.
            try:
                current = self.repo.source(item["key"])
                if current["manual"] or current["version"] != item["version"]:
                    self._advance("skipped")
                    return None
            except KeyError:
                self._advance("skipped")
                return None
            try:
                lid = self.budget.reserve(item["key"], item["cost"], self.settings.public()["monthly_budget"])
            except ValueError as exc:
                halted.set()
                self._job(message=str(exc))
                return None
            started = time.monotonic()
            diagnostic = {"model": config["model"], "rule_version": RULE_VERSION, "retry_index": attempt}
            self.budget.record_attempt(lid, diagnostic)
            cost = None
            try:
                raw, usage = transport(config, messages)
                diagnostic.update(usage.get("_diagnostics") or {})
                diagnostic.setdefault("json_valid", raw is not None)
                for field in ("prompt_tokens", "completion_tokens"):
                    number = usage.get(field)
                    if isinstance(number, int) and not isinstance(number, bool) and number >= 0:
                        diagnostic[field] = number
                cost = self._usage_cost(usage, config)
                if cost is None:
                    self.budget.unknown(lid, "服务未返回可核算用量，保留预估费用")
                else:
                    self.budget.settle(lid, cost)
                if diagnostic.get("finish_reason") == "length":
                    diagnostic["outcome"] = "truncated"
                    raise ValueError("输出达到 token 上限而截断；请降低思考强度或调整输出上限后手动重试")
                if diagnostic.get("finish_reason") not in (None, "stop", "unknown"):
                    diagnostic["outcome"] = "finish_error"
                    raise ValueError("模型没有正常完成文本输出，请检查结束原因")
                if raw is None:
                    diagnostic["outcome"] = "json_error"
                    raise ValueError("模型未返回可解析的 JSON")
                diagnostic["outcome"] = "schema_error"
                result = validate_absa(raw, item["preview"]["source_text"])
                for opinion in result["opinions"]:
                    for field in ("target_text", "opinion_text", "evidence"):
                        if opinion[field] is not None:
                            for marker, original in item["replacements"].items():
                                opinion[field] = opinion[field].replace(marker, original)
                saved = self.repo.save_ai(item["key"], result, item["version"], item["fingerprint"])
                diagnostic["outcome"] = "success"
                self._advance("succeeded" if saved else "skipped")
                return None if saved else "相同输入的请求已完成，但来源已被修改；请重新预估后重试"
            except ProviderError as exc:
                diagnostic.update(exc.diagnostics)
                diagnostic.setdefault("outcome", "transport_error")
                if exc.known_zero:
                    self.budget.settle(lid, 0)
                else:
                    self.budget.unknown(lid, str(exc))
                self.repo.set_status(item["key"], "unknown" if exc.uncertain else "failed", str(exc))
                if exc.retryable and exc.known_zero and attempt < config["format_retries"]:
                    continue
                halted.set()
                self._job(message=str(exc))
                self._advance("failed")
                return None
            except ValueError as exc:
                if cost is None:
                    self.repo.set_status(item["key"], "unknown", str(exc) + "；用量未知，请核对费用")
                    halted.set()
                    self._job(message="分析未通过且用量未知，请核对费用后重试")
                    self._advance("failed")
                    return None
                self.repo.set_status(item["key"], "failed", str(exc))
                # Unknown billing and truncation do not trigger more paid calls automatically.
                if (
                    diagnostic.get("outcome") in ("json_error", "schema_error")
                    and cost is not None
                    and attempt < config["format_retries"]
                ):
                    messages = item["messages"] + [
                        {
                            "role": "user",
                            "content": "上次结果未通过 JSON/字段/证据校验。请严格使用规定的枚举、完整必填字段和当前原文连续证据重新输出，仅返回 JSON，不要解释。",
                        }
                    ]
                    continue
                self._advance("failed")
                return str(exc)
            except Exception:
                diagnostic["outcome"] = "processing_error"
                self.budget.unknown(lid, "请求处理异常，用量待核对")
                self.repo.set_status(item["key"], "unknown", "请求处理异常，请核对用量")
                halted.set()
                self._advance("failed")
                return None
            finally:
                diagnostic["duration_ms"] = round((time.monotonic() - started) * 1000)
                self.budget.record_attempt(lid, diagnostic)
        return None

    def _run(self, items: list[dict[str, Any]], config: dict[str, Any]) -> None:
        halted = threading.Event()
        provider = ProviderClient() if self.transport is provider_request else None
        transport = provider or self.transport
        locks = {item["fingerprint"]: threading.Lock() for item in items}
        failures: dict[str, str] = {}

        def work(item):
            # The estimate covers one attempt sequence per fingerprint, including failures.
            with locks[item["fingerprint"]]:
                error = self._process(item, config, transport, halted, failures.get(item["fingerprint"]))
                if error is not None:
                    failures[item["fingerprint"]] = error

        try:
            with ThreadPoolExecutor(max_workers=config["concurrency"]) as pool:
                list(pool.map(work, items))
        finally:
            if provider:
                provider.close()
            paused = self.stop_event.is_set() or halted.is_set()
            with self.lock:
                counts = f"成功 {self.job.get('succeeded', 0)} · 失败 {self.job.get('failed', 0)} · 跳过 {self.job.get('skipped', 0)}"
                self._job(
                    running=False,
                    status="paused" if paused else "completed",
                    message=(self.job["message"] + "；" if halted.is_set() else "")
                    + counts
                    + ("；剩余内容可手动继续" if paused else "；失败不等于无有效反馈"),
                )

    def retry_confirm(self, key: str) -> None:
        source = self.repo.source(key)
        if self.state()["running"]:
            raise ValueError("请先停止正在运行的任务")
        if source["status"] != "unknown":
            raise ValueError("仅请求结果未知的记录需要此确认")
        self.repo.set_status(key, "failed", "用户已确认可能重复计费，等待手动重试；原费用预留保留")
