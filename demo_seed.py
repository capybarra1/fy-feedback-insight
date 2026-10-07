"""Seed a separate demo database with synthetic, predetermined examples; no API calls."""
import copy
import json
from pathlib import Path
from absa import EXAMPLES, RULE_VERSION
from repository import Repository

def seed() -> None:
    target = Path(__file__).parent / 'data/demo.sqlite3'
    if target.exists():
        print('Demo database already exists; preserving it.')
        return
    repo = Repository(target)
    records = []
    for i, example in enumerate(EXAMPLES):
        records.append({'note_id': f'portfolio-demo-{i:02}', 'desc': example['source_text'], 'title': '合成演示样例（预设标注）'})
    repo.import_files([{'name': 'synthetic-demo.jsonl', 'content': '\n'.join(json.dumps(r, ensure_ascii=False) for r in records)}], 'rednote')
    for i, example in enumerate(EXAMPLES):
        key = f'xhs:note:portfolio-demo-{i:02}'
        result = copy.deepcopy(example['result'])
        repo.review(key, dict(result, relevance='relevant' if result['brand_relevance']=='related' else 'irrelevant', review_status='unreviewed', analysis_schema=RULE_VERSION))
    print(f'Seeded {len(records)} synthetic examples. These are not model outputs or human gold labels.')

if __name__ == '__main__':
    seed()
