# ABSA 离线评估与人工标注

`evaluation.py` 只读取本地 SQLite / JSONL，不加载应用 Repository、不读取模型配置，也不发起模型请求。准备样本不会初始化或迁移数据库，不会使用历史 AI 结果填充答案。

已从当前数据库生成 **80 条待人工确认的标注草稿**，保存在忽略版本管理的 `data/evaluation/annotation-draft.jsonl`。本次划分为 dev 48 条、holdout 32 条，所有 `human_confirmed=false`，所有期望标签和人工评估项均为 `null`。这不是金标集，因此目前不能据此声称模型准确率或产品可用率。文件权限为 0600，所在目录为 0700。真实原文不得复制到本文件、测试夹具或版本管理中。

## 准备草稿

在项目目录执行：

```bash
.venv/bin/python evaluation.py prepare --count 80
```

默认数据库为 `data/feedback.sqlite3`；`--db` 可指定其他本地 SQLite 文件。默认输出如已存在会拒绝覆盖，保护正在标注的内容。需要新草稿时指定新文件：

```bash
.venv/bin/python evaluation.py prepare --count 80 --output data/evaluation/annotation-draft-v2.jsonl
```

采样数量限制为 1–80；不足时返回所有可用来源并报告实际数量。只读 SQLite 连接使用 `mode=ro` 和 `query_only`。不查询观点表，不读取密钥。草稿使用来源的稳定 `key` 作为 `id`；每条包含：

- 当前来源原文及 SHA-256、来源种类、笔记和父评论标识。
- 母笔记的标题及最多 2,000 字原文，供判断品牌/产品语境。该语境不能充当当前评论的证据，不包含父评论推理。
- `expected` 中的品牌相关性、产品范围、内容类型及观点，初始全部为 `null`。
- `assessment.valid_feedback`、`assessment.is_noise`、`assessment.prediction_useful`，初始全部为 `null`。
- `slices`：短文本（去空白后不超过 12 字）、疑似纯符号噪声、历史失败、笔记/评论、历史过滤、人工修改、重复来源。它们是采样和分层提示，不是人工事实标签。

按笔记 ID 及规范化站点组成 `group_id`，固定哈希分配 dev / holdout（目标比例 70% / 30%，小样本实际比例可能不同）。同一笔记下的笔记和所有评论始终处于同一组。新增来源不会改变现有组的划分；采样会从失败、疑似噪声、历史过滤、短文本、笔记和其余来源等桶轮流抽取，便于覆盖少见错误，不代表线上自然分布。

## 人工标注规则

在私有目录内复制草稿为 `gold.jsonl` 后逐条阅读并标注，保留 `id`、`site`、`note_id`、`group_id` 和 `split`。先使用 dev 改进规则和提示词，holdout 留到方案确定后最终检验。评估器会检查整个输入的组隔离，即使只请求评估其中一个 split。

1. 先标注 `expected.brand_relevance`（`related` / `unrelated` / `uncertain`）、`product_scope`（`cockpit` / `other` / `mixed` / `unknown` / `not_applicable`）和 `content_types` 数组。
2. `content_types` 使用 `product_feedback`、`product_question`、`promotion`、`delivery_chat`、`chitchat`、`other`、`uncertain`。
3. `expected.opinions` 填写原始 ABSA 字段：`module`、`submodule`、`target_text`、`opinion_text`、`evidence`、`feedback_type`、`sentiment`、`implicit_target`、`context_used`、`needs_review`。模块和子模块应遵循正式词表。情感使用英文 `positive` / `negative` / `neutral` / `uncertain`；反馈类型使用 `fault` / `complaint` / `suggestion` / `question` / `praise` / `comparison` / `other`。
4. 证据必须是当前 `source_text` 的连续片段，显式目标和观点须包含在证据中。隐式目标可为 `null`，同时设置 `implicit_target=true`；隐式观点可为 `null`，同时补充 `implicit_opinion=true`。空字符串不能代替隐式标记。
5. 已确认“没有任何观点”填 `opinions=[]`；尚未标注填 `opinions=null`。二者含义不同。整个观点列表一旦填写，应完整标注其中每一项，不支持列表内部分字段未知。
6. `assessment.valid_feedback` 是人工判断的有效产品反馈/问题；`assessment.is_noise` 是人工判断应被过滤的内容。拿不准时保留 `null`。这两个字段不从历史 AI 或采样标签自动生成。
7. 完成人工核验后设置 `human_confirmed=true`。允许路由字段或整份观点列表保留未知，但相应指标不计入分母，必须查看各指标的覆盖数量。

`assessment.prediction_useful` 专用于人工查看**本次待评估预测**后判断结果能否直接支持产品分析，填 `true` / `false` / `null`。它不是金标 ABSA 结构的一部分；每次换模型输出，应清空并重新核验这个字段。评估仅统计有对应预测记录且该人工字段明确为布尔值的来源；人工明确评为不可用的失败或无效输出也计入分母。JSON 解析成功、未失败或自动标签一致不会被冒充为产品可用。

## 输入预测与执行

预测 JSONL 每行包含同一稳定 `id` 及原始 ABSA 结果。既支持平铺的 `id` + ABSA 字段，也支持以下包装。此处是合成示例：

```json
{"id":"synthetic-example","status":"succeeded","prediction":{"brand_relevance":"related","product_scope":"cockpit","content_types":["product_feedback"],"opinions":[{"module":"车机","submodule":"系统流畅度","target_text":"车机","opinion_text":"卡","evidence":"车机卡","feedback_type":"complaint","sentiment":"negative","implicit_target":false,"context_used":false,"needs_review":false}]}}
```

调用失败仍保留记录，例如 `{"id":"synthetic-example","status":"failed"}`。可选 `split` 必须与金标相符。`filtered=true` 表示流程明确过滤该来源，需与完整路由结果一起提供。除 `succeeded` / `success` 外的状态按失败处理；省略 `status` 默认成功并校验内容。成功记录缺字段、使用中文情感、布尔值为字符串或证据不忠实，会被计为无效预测。

```bash
.venv/bin/python evaluation.py evaluate \
  --gold data/evaluation/gold.jsonl \
  --predictions data/evaluation/predictions.jsonl \
  --split holdout
```

结果输出至标准输出；可以用 `umask 077` 后重定向至私有评估目录。评估不会自动请求模型、补齐缺失预测或触发费用。重复金标 ID、重复预测 ID、同笔记跨集合以及预测 split 错位会报错。额外预测 ID 只报告数量，不纳入本次分母。

## 指标口径

- `counts` 报告所选集合的总数、人工确认数、排除的未确认数以及缺失/失败/无效预测数。每个 `slices` 分层单独报告同样的主要指标和 `source_count`；分层相互重叠，不能相加。
- **四元组 F1**：每条来源中对 `(target_text, (module, submodule), opinion_text, sentiment)` 做精确集合匹配，跨来源累加 TP/FP/FN。原文跨度不去空格或改写，隐式 `null` 是有效值，同一来源的重复四元组去重。漏掉一个来源的预测，也会漏掉该来源全部已标注四元组。证据、反馈类型和隐式标记本身不属于四元组，另有字段指标及结构验证。
- **字段 F1**：对目标、观点、证据、反馈类型、情感、模块、子模块各自做每来源的投影集合匹配。这是独立字段集合指标，不把观点强行按数组顺序配对，也不声称为词元级跨度 F1。子模块的联合正确性另见 `(module, submodule)` 指标。
- **模块/子模块多标签**：每来源标签集合，分别报告微平均 F1 和每来源 F1 的宏平均；子模块使用 `(module, submodule)`，避免同名子模块跨模块混淆。双空集合不提供人为的满分，会计入 `empty_empty_sources`，不加入宏平均。
- **相关性召回**：人工 `related` 来源中预测 `related` 的比例。相关性准确率仅使用人工明确 `related` / `unrelated` 的来源，`uncertain` / `null` 不在分母。
- **有效反馈召回**：人工 `valid_feedback=true` 来源中，被预测为品牌相关、内容类型包含产品反馈或产品问题且没有被过滤的比例。这是路由保留能力；观点抽取完整性由四元组指标评价。
- **噪声精确率**：被过滤的来源中，人工已判断噪声属性且 `is_noise=true` 的比例；未知人工标签不进入分母。噪声召回为人工噪声中被过滤的比例。完整成功预测中 `brand_relevance=unrelated`，或无观点且纯推广/交付闲聊/闲聊、没有产品反馈/问题/不确定内容类型，也视为过滤。
- **短评误过滤率**：人工有效且 `short=true` 的来源中被明确过滤的比例。失败、缺失和无效预测不会伪装成“正确过滤”，它们由缺失统计及召回指标体现。
- **产品可用率**：仅来自上述明确人工判断，单独列出分子和分母，不能用技术成功率替代。

所有比例展示 `value`、`numerator`、`denominator`；集合指标展示 TP/FP/FN 和 `known_sources`。分母为零返回 JSON `null`。当存在已标注正例而全漏预测时 F1 为 0、precision 可为 `null`，不会因跳过失败样本而抬高成绩。`opinions=null` 排除观点指标，明确 `[]` 则会处罚多抽出的观点。`product_scope=unknown` 不计入范围准确率；其他明确枚举值（包括内容类型或情感中的 `uncertain`）作为真实类别评价。

评估器验证基础结构、英文枚举和文本忠实性；完整词表约束由主 ABSA 模块负责。拼错模块/子模块会按错误标签计入 FP/FN，不能用于替代线上 schema 验证。

## 验证

```bash
.venv/bin/python -m pytest tests/test_evaluation.py -q
.venv/bin/python -m ruff check evaluation.py tests/test_evaluation.py
.venv/bin/python -m mypy evaluation.py
```

测试只用临时 SQLite 和合成文本，覆盖缺失/失败预测、无效输出、未知标签、空集合、多标签、人工可用率、重复 ID、集合泄漏、只读导出、权限和符号链接拒绝。不包含真实原文，也没有付费请求。
