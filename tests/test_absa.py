import copy

import pytest

from absa import EXAMPLES, noise_reason, validate_absa


def test_business_examples_have_valid_evidence():
    for example in EXAMPLES:
        validate_absa(example['result'], example['source_text'])


def test_fabricated_span_and_wrong_taxonomy_rejected():
    for change in [{'target_text': '车机导航'}, {'opinion_text': '非常差'}, {'submodule': '不存在'}, {'evidence': '地图总是乱带路'}, {'context_used': True}]:
        value = copy.deepcopy(EXAMPLES[0]['result'])
        value['opinions'][0].update(change)
        with pytest.raises(ValueError):
            validate_absa(value, EXAMPLES[0]['source_text'])


def test_implicit_target_not_fabricated():
    value = copy.deepcopy(EXAMPLES[1]['result'])
    assert validate_absa(value, EXAMPLES[1]['source_text'])['opinions'][0]['target_text'] is None
    value['opinions'][0]['target_text'] = '车机'
    with pytest.raises(ValueError):
        validate_absa(value, EXAMPLES[1]['source_text'])


def test_noise_keeps_short_faults_and_context_dependent_replies():
    for text in ['死机', '没声音', '不跟车', '我也是', '你好', '网慢', 'http://test.com 导航很差']:
        assert not noise_reason(text)
    for text in ['[赞R][偷笑R]', '👍🙋🏻‍♀️', '。。。', 'https://example.com', '']:
        assert noise_reason(text)


def test_unrelated_or_no_feedback_cannot_carry_opinions():
    for change in [{'brand_relevance': 'unrelated'}, {'content_types': ['chitchat']}, {'product_scope': 'other'}]:
        value = copy.deepcopy(EXAMPLES[0]['result'])
        value.update(change)
        with pytest.raises(ValueError):
            validate_absa(value, EXAMPLES[0]['source_text'])
