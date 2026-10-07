# FY 用户反馈洞察工具

从座舱产品反馈中拆分多观点，核对原文证据，进行人工复核与功能看板汇总。公开版本不包含真实数据或 API 密钥。

![合成演示看板](../assets/feedback-dashboard.jpg)

## 启动

推荐 Python 3.12。在仓库根目录运行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
.venv/bin/python run.py
```

浏览器打开 http://127.0.0.1:18765 。无模型配置时也能导入和人工整理。只有配置自己的 HTTPS 模型服务并主动确认分析后，才会发出外部请求。

## 无 API 演示

```sh
.venv/bin/python demo_seed.py
.venv/bin/python -m uvicorn demo_server:app --host 127.0.0.1 --port 18766
```

打开 http://127.0.0.1:18766 。演示使用独立的 `data/demo.sqlite3`，服务不加载 `.env`。文本和标注均为固定合成示例，不能作为模型评测结果。种子脚本只在目标数据库不存在时写入，不覆盖已有数据。

## 核心能力

JSONL 导入、稳定编号去重、多观点结构、品牌/范围/内容分流、原文证据校验、人工修正、模块与子功能看板、CSV 导出、费用预估与预算账本、有限格式修复与异常记录。

## 验证与限制

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
node --check static/app.js
node tests/frontend.test.cjs
```

测试使用合成数据和模拟服务，不调用付费模型。真实模型语义质量需人工标准集与独立留出集验收。母笔记只用于判断品牌，不借用观点，不推断父子评论含义。

[ABSA 规则](absa-v2.md) · [离线评估方法](absa-evaluation.md) · [完整产品案例](../README.md)
