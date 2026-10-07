# Frontend API contract
All timestamps ISO strings; money RMB. JSON for POST/PUT. `GET /api/state` contains `csrf_token`; send `X-Firefly-Token` for all mutations. Errors `{detail: "Chinese message"}`. Never inject source text via raw HTML.

GET /api/state => {csrf_token, sources:[], opinions:[], batches:[], settings:{base_url,model,input_price,output_price,monthly_budget,max_output_tokens,max_input_chars,has_api_key}, budget:{month,spent,reserved,remaining},job:{running,status,total,completed,message}, options:{modules,types,sentiments},ledger:[]}
source={key,kind:"note"|"comment",external_id,note_id,parent_id,title,text,site,keywords:[],published_at,first_seen,last_seen,collected_at,status:"pending"|"done"|"failed"|"unknown"|"skipped",relevance:"unknown"|"relevant"|"irrelevant"|"uncertain",review_status:"unreviewed"|"needs_review"|"reviewed",manual:boolean,error,duplicate_of,batch_ids:[],version}
opinion={id,source_key,module,type,sentiment,theme,evidence,origin:"ai"|"manual"}
batch={id,filename,created_at,site,new,updated,duplicate,invalid,errors:[{line,message}],count}
ledger={id,source_key,created_at,status:"reserved"|"settled"|"unknown",amount,reserved,error}
modules=[智能硬件,辅助驾驶,语音助手（lumo）,车机,其他,待确认]; types=[体验评价,问题反馈,改进建议,咨询疑问,购买意向,对比评价,其他]; sentiments=[正面,负面,中性,无法判断]. 待确认 is uncertainty not a fifth actual module.

POST /api/import {files:[{name,content}],site:"rednote"|"xiaohongshu"} => {batches:[], totals:{new,updated,duplicate,invalid}}
GET /api/sources/{encoded key} => {source,opinions,context:[{kind,text,title}],history:[],url}; history contains original/AI/manual snapshots for display, credentials excluded.
PUT /api/sources/{key}/review {relevance,review_status,opinions:[{module,type,sentiment,theme,evidence}]} => {ok:true}; replace opinions, preserve original history. Add/remove rows to support edits. evidence exact substring of original source text (title+body for note).
DELETE /api/batches/{id} => {deleted_sources,retained_sources}; confirm prompt must explain exclusive records+analysis removed, shared records retained, fee history kept.
PUT /api/settings {base_url,model,input_price,output_price,monthly_budget,max_output_tokens,max_input_chars,api_key?,clear_key?:boolean} => public settings. Prices CNY per million tokens; blank key means retain. Key is held in memory after reading the ignored local .env at startup or accepting a session override. Public settings include key_source and env_file, never the secret. Clearing the session key does not edit the file.
POST /api/analysis/estimate {mode:"pending"|"failed"|"selected"|"legacy",source_keys?:[]} => {count,estimated_cost,can_start,reason,estimate_token,preview:{source_text,source_kind,brand_context},excluded}; default pending excludes manual results; selected also cannot overwrite manual; force reset requires review feature separately (not auto).
POST /api/analysis/start {estimate_token} => {ok,job}; must use returned estimate token from preview before enabling Start. estimates expire when data/config changes.
POST /api/analysis/stop {} => {ok:true}; stops after in-flight request settles.
POST /api/sources/{key}/retry-confirm {} => {ok:true}; only unknown request, after explicit UI warning that uncertain request may have been charged; keeps conservative fee and sets failed to manually retry.
GET /api/export.csv?kind=comment&module=...&sentiment=...&type=...&theme=...&review=...&from=YYYY-MM-DD&to=YYYY-MM-DD&q=...&batch=... => UTF8 BOM CSV, one opinion/row; module=focus includes four modules, empty=all.
Dashboard filter/sort uses same fields: kind default comment; module default focus with visible 其他 and 待确认 buttons. Pending sources not opinions must still be accessible in a source library list. Stats total/status source counts separate from opinions. Module bars use distinct source keys, themes also distinct source keys+note_ids; sentiment by opinion. Review dialog and export exact current filters. Last updated body may retain manual opinions; needs_review shown.

Versioning: source.has_analysis, analysis_text, analysis_version preserve last effective opinion context across body updates. Effective eligibility includes has_analysis. Export original text uses analysis_text; q still searches current source title/text+theme. Context changes flag needs_review without automatic reanalysis.


## ABSA v2 additions (2026-09-29)

Canonical model output is validated by `absa.AnalysisResult`. It uses English sentiment and feedback-type codes. Existing UI fields `sentiment` and `type` remain Chinese; opinion `sentiment_code` and `feedback_type` expose canonical codes. Do not use UI Chinese values as the offline evaluation prediction schema.

- Sources add `brand_relevance`, `product_scope`, `content_types`, `review_reasons`, `filter_reason`, `analysis_schema`, `routing_schema`, `eligible_for_insights`. Routing and opinion-schema upgrades are independent: manual routing of mixed/legacy opinions is saved, while `analysis_schema` remains `legacy`. An empty opinion collection with fully validated new routing may be v2. Raw source library always retains all sources.
- Opinions add `submodule`, `aspect_category`, nullable `target_text`/`opinion_text`, `feedback_type`, `sentiment_code`, boolean `implicit_target`/`implicit_opinion`/`context_used`/`needs_review`, and `schema_version`. V2 requires exact continuous spans and a valid module/submodule pair; phase-one context_used must be false. The theme defaults to submodule; validated manual custom themes persist.
- Options add `submodules` (mapping) and `feedback_types`/`content_types` (code→label mappings). The first four modules stay the focus modules; independent other groups follow, then legacy 其他/待确认. Full taxonomy comes from `absa.TAXONOMY`.
- Manual PUT accepts optional source routing fields and analysis_schema in addition to prior fields. Existing v2 records cannot downgrade to legacy opinions. Uncertain/review-flagged results cannot be marked reviewed until flags and review reasons are resolved. Manual edits remain protected from AI.
- CSV and dashboard filters add `submodule`, `content_type`, `schema`, `needs_review` ("true"/"false"), `scope`. Both respect `eligible_for_insights=false`, including newly routed legacy sources. CSV includes new source and opinion fields, with one row per opinion.
- Settings add concurrency 1–4 (default 2), reasoning_effort low/high/max (default low), format_retries 0–1 (default 1). The exclusive GLM argument is sent only to supported provider/model combinations.
- Estimate covers one attempt sequence per identical fingerprint, including allowed repair. Failed duplicate fingerprints also share the result, not another call. Pure-noise local filtering and valid caches require no API key. Legacy mode excludes manually protected and unknown-billing sources.
- Job includes succeeded/failed/skipped separately. Stop prevents new dispatch; in-flight requests may finish. Never equate completion with all records being valid feedback.
- State adds `analysis_metrics`; ledger adds outcome, finish_reason, json_valid, duration_ms, prompt_tokens, completion_tokens, retry_index, model, rule_version. No raw answers, reasoning text or keys are logged. JSON success denominators exclude unknown parse outcomes; historical requests without diagnostics are counted separately. Technical success is not semantic accuracy.
