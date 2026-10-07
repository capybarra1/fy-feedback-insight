"""Run only against a disposable localhost database; never invokes a provider."""

import json
import os
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

URL = os.environ.get("FIREFLY_TEST_URL", "http://127.0.0.1:8766")


def main():
    parsed = urlparse(URL)
    assert parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}, "Disposable localhost server required"
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, accept_downloads=True, reduced_motion="reduce")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        start_attempts = []
        page.route("**/api/analysis/start", lambda route: (start_attempts.append(route.request.url), route.abort()))
        page.goto(URL, wait_until="networkidle")
        initial = page.request.get(URL + "/api/state").json()
        assert not initial["sources"] and not initial["batches"] and not initial["ledger"], "Use a fresh disposable database"
        assert not initial["settings"]["has_api_key"], "Test server must not load any provider key"
        expect(page.locator("#connection-status")).to_have_text("本地已连接")
        page.locator('[data-page="imports"]').click()
        expect(page.locator("#import-site")).to_have_value("rednote")
        synthetic = [
            {"note_id": "browser-note", "desc": "lumo 的座舱体验"},
            {
                "note_id": "browser-note",
                "comment_id": "browser-comment",
                "content": "lumo 经常听不懂，但外观很好看。联系13812345678",
            },
            {"note_id": "browser-empty-note", "desc": ""},
            {"note_id": "browser-note", "comment_id": "browser-empty-comment", "content": ""},
        ]
        page.locator("#file-input").set_input_files(
            {
                "name": "browser-fixture.jsonl",
                "mimeType": "application/json",
                "buffer": "\n".join(json.dumps(r, ensure_ascii=False) for r in synthetic).encode(),
            }
        )
        page.locator("#import-button").click()
        expect(page.locator("#import-result")).to_contain_text("新增 2")
        expect(page.locator("#import-result")).to_contain_text("异常 2")
        page.locator("#file-input").set_input_files(
            {
                "name": "browser-fixture.jsonl",
                "mimeType": "application/json",
                "buffer": "\n".join(json.dumps(r, ensure_ascii=False) for r in synthetic).encode(),
            }
        )
        page.locator("#import-button").click()
        expect(page.locator("#import-result")).to_contain_text("重复 2")
        page.locator("#estimate-button").click()
        expect(page.locator("#start-analysis-button")).to_be_disabled()
        expect(page.locator("#estimate-panel")).to_contain_text("API 密钥")
        page.locator('[data-page="dashboard"]').click()
        expect(page.locator("#comment-count")).to_have_text("1")
        expect(page.locator("#note-count")).to_have_text("1")
        page.locator("#sources-tab").click()
        page.locator("#filter-q").fill("lumo 经常听不懂")
        page.locator('[data-source="xhs:comment:browser-comment"]').click()
        expect(page.locator("#source-dialog")).to_be_visible()
        page.locator("#review-relevance").select_option("relevant")
        expect(page.locator("#review-brand")).to_have_value("related")
        page.locator("#review-scope").select_option("cockpit")
        page.locator('[data-content-type="product_feedback"]').check()
        page.locator("#review-status").select_option("reviewed")
        page.locator("#add-opinion-button").click()
        editor = page.locator('[data-editor="0"]')
        editor.locator('[data-field="module"]').select_option("语音助手（lumo）")
        editor.locator('[data-field="submodule"]').select_option("语义理解")
        editor.locator('[data-field="feedback_type"]').select_option("fault")
        editor.locator('[data-field="sentiment"]').select_option("负面")
        editor.locator('[data-field="implicit_target"]').uncheck()
        editor.locator('[data-field="target_text"]').fill("lumo")
        editor.locator('[data-field="implicit_opinion"]').uncheck()
        editor.locator('[data-field="opinion_text"]').fill("经常听不懂")
        editor.locator('[data-field="needs_review"]').uncheck()
        expect(editor.locator('[data-field="context_used"]')).to_be_disabled()
        editor.locator('[data-field="theme"]').fill("")
        editor.locator('[data-field="evidence"]').fill("lumo 经常听不懂")
        with page.expect_response(lambda response: response.url.endswith("/review") and response.request.method == "PUT") as review_response:
            page.locator("#save-review-button").click()
        assert review_response.value.ok, review_response.value.text()
        expect(page.locator("#toast")).to_contain_text("人工核对结果已保存")
        expect(editor.locator('[data-field="theme"]')).to_have_value("语义理解")
        # An explicit manual theme survives while the canonical submodule stays fixed.
        editor.locator('[data-field="theme"]').fill("语音理解不准确")
        editor.locator('[data-field="implicit_target"]').check()
        expect(editor.locator('[data-field="target_text"]')).to_be_disabled()
        with page.expect_response(lambda response: response.url.endswith("/review") and response.request.method == "PUT") as review_response:
            page.locator("#save-review-button").click()
        assert review_response.value.ok, review_response.value.text()
        expect(editor.locator('[data-field="theme"]')).to_have_value("语音理解不准确")
        expect(page.locator("#save-review-button")).to_be_enabled()
        saved = page.request.get(URL + "/api/sources/xhs%3Acomment%3Abrowser-comment").json()
        assert saved["source"]["analysis_schema"] == "absa-v2"
        assert saved["source"]["brand_relevance"] == "related"
        assert saved["source"]["product_scope"] == "cockpit"
        result = saved["opinions"][0]
        assert result["theme"] == "语音理解不准确" and result["submodule"] == "语义理解"
        assert result["target_text"] is None and result["implicit_target"] is True
        assert result["opinion_text"] == "经常听不懂" and result["context_used"] is False
        assert result["sentiment_code"] == "negative" and result["feedback_type"] == "fault"
        assert result["needs_review"] is False
        page.locator("#close-dialog").click()
        page.locator("#reset-filters").click()
        page.locator("#opinions-tab").click()
        expect(page.locator("#filtered-opinion-count")).to_have_text("1")
        page.locator("#filter-module").select_option("语音助手（lumo）")
        page.locator("#filter-submodule").select_option("语义理解")
        page.locator("#filter-schema").select_option("absa-v2")
        page.locator("#filter-content_type").select_option("product_feedback")
        expect(page.locator("#results-list")).to_contain_text("语音理解不准确")
        expect(page.locator("#results-list")).to_contain_text("隐含对象 · null")
        expect(page.locator("#timeline-list")).to_contain_text("日期未知")
        with page.expect_download() as info:
            page.locator("#export-button").click()
        download = info.value
        exported = Path(download.path()).read_text(encoding="utf-8-sig")
        assert all(value in exported for value in ["语音理解不准确", "browser-comment", "语义理解", "absa-v2", "经常听不懂"])
        page.screenshot(path="/tmp/firefly-browser-dashboard.png", full_page=True, animations="disabled")
        page.locator('[data-page="settings"]').click()
        page.locator("#setting-monthly_budget").fill("20")
        page.locator(".advanced > summary").click()
        page.locator("#setting-concurrency").select_option("2")
        page.locator("#setting-reasoning_effort").select_option("low")
        page.locator("#setting-format_retries").select_option("1")
        page.locator('#settings-form button[type="submit"]').click()
        expect(page.locator("#settings-feedback")).to_have_text("设置已保存")
        page.reload(wait_until="networkidle")
        expect(page.locator("#setting-monthly_budget")).to_have_value("20")
        expect(page.locator("#setting-concurrency")).to_have_value("2")
        expect(page.locator("#setting-reasoning_effort")).to_have_value("low")
        expect(page.locator("#setting-format_retries")).to_have_value("1")
        page.locator('[data-page="dashboard"]').click()
        expect(page.locator("#filtered-opinion-count")).to_have_text("1")
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.locator("#page-dashboard").evaluate("el => getComputedStyle(el).opacity") == "1", "Dashboard must be fully visible before capture"
        page.screenshot(path="/tmp/firefly-browser-mobile.png", full_page=True, animations="disabled")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "mobile overflow"
        assert not errors, errors
        assert not start_attempts, "Smoke test must never start model analysis"
        final = page.request.get(URL + "/api/state").json()
        assert final["ledger"] == [] and not final["job"]["running"]
        browser.close()
        print(
            "Browser smoke passed: synthetic import/dedup, v2 routing and explicit/null spans, default/custom theme, filters, CSV, settings persistence, mobile, no JS errors; no provider requests."
        )


if __name__ == "__main__":
    main()
