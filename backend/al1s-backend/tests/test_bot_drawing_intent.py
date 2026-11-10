import json

import pytest

from al1s.bot_ai import drawing_inference
from al1s.bot_ai.drawing_inference import decode_interpretation, decode_wish, explicit_prompt, split_negative_marker


def response(mode, keys=()):
    return json.dumps({"mode": mode, "lora_keys": list(keys)}, ensure_ascii=False)


def test_direct_prompt_keeps_weights_and_moves_explicit_negative_conditions():
    intent = decode_interpretation(response("direct", ["角色A"]),
        "角色A, (red hair:1.25), 不要 模糊；排除 水印", ["角色A"])
    assert intent.mode == "direct"
    assert "(red hair:1.25)" in intent.positive and "角色A" in intent.positive
    assert "模糊" in intent.negative and "水印" in intent.negative
    assert "不要" not in intent.positive and intent.lora_keys == ("角色A",)


def test_explicit_positive_and_negative_fields_do_not_need_llm():
    intent = explicit_prompt("正面: (cat:1.2), sitting\n负面: blurry, watermark", [])
    assert intent.mode == "direct" and intent.positive == "(cat:1.2), sitting"
    assert intent.negative == "blurry, watermark"
    assert explicit_prompt("你觉得这张图怎么样？", []) is None


@pytest.mark.parametrize('marker', ['【负面】', '【负向】', '【NP】', '【np】'])
def test_bracket_negative_markers_keep_unlabeled_text_positive(marker):
    positive, negative = split_negative_marker(f'一只猫 {marker} 水印, 模糊')
    assert (positive, negative) == ('一只猫', '水印, 模糊')
    intent = decode_interpretation(response('wish'), f'一只猫 {marker} 水印, 模糊', [])
    assert intent.negative == '水印, 模糊'


def test_negative_markers_merge_and_preserve_legacy_and_ambiguous_natural_language():
    assert split_negative_marker('猫【负面】：水印【NP】模糊') == ('猫', '水印, 模糊')
    direct = explicit_prompt('正面：cat【负向】blur', [], allow_english=True)
    assert (direct.positive, direct.negative) == ('cat', 'blur')
    tags = drawing_inference.explicit_tag_prompt('1girl, blue hair, sitting【NP】blur', [])
    assert (tags.positive, tags.negative) == ('1girl, blue hair, sitting', 'blur')
    intent = decode_interpretation(response('wish'), '画一只不要耳朵的猫【负面】水印', [])
    assert intent.negative == '水印'


def test_english_prompt_fields_and_unambiguous_tag_lists_are_direct():
    result = explicit_prompt("prompt: (cat:1.2), sitting\nnegative: blurry, watermark", [], allow_english=True)
    assert result is not None and result.mode == "direct" and result.positive == "(cat:1.2), sitting"
    assert result.negative == "blurry, watermark"
    tags = "(masterpiece:1.2), 1girl, blue hair"
    assert drawing_inference.explicit_tag_prompt(tags, []).positive == tags
    assert drawing_inference.explicit_tag_prompt("hello, good, morning", []) is None
    assert drawing_inference.explicit_tag_prompt("1girl, blue hair, what do you think?", []) is None


def test_only_user_mentioned_aliases_are_accepted_and_negative_lora_does_not_route():
    with pytest.raises(ValueError, match="drawing_intent_invalid"):
        decode_interpretation(response("wish", ["角色A"]), "画一只猫", ["角色A"])
    result = decode_interpretation(response("direct", []), "一只猫，不要角色A", ["角色A"])
    assert result.lora_keys == () and "角色A" in result.negative
    with pytest.raises(ValueError, match="drawing_intent_invalid"):
        decode_interpretation(response("direct", ["角色A"]), "一只猫，不要角色A", ["角色A"])


def test_a_plain_character_name_is_not_treated_as_an_unrequested_lora():
    user_text = "帮我画一张蔚蓝档案角色未花坐在花园里的图片，阳光温暖"
    intent = decode_interpretation(response("wish", ["未花"]), user_text, [])
    assert intent.mode == "wish" and intent.lora_keys == ()
    explicit = decode_interpretation(response("wish", ["未花"]), "使用 LoRA 未花 画一张图", [])
    assert explicit.lora_keys == ("未花",)


@pytest.mark.parametrize("raw", ["not-json", '{"mode":"direct","lora_keys":[],"extra":1}',
    '{"mode":"direct","lora_keys":["unknown"]}', '{"mode":"magic","lora_keys":[]}'])
def test_invalid_intent_does_not_start_drawing(raw):
    with pytest.raises(ValueError, match="drawing_intent_invalid"):
        decode_interpretation(raw, "画一只猫", ["角色A"])


def test_wish_output_is_bounded_and_cannot_change_workflow():
    positive, negative = decode_wish('{"positive":"一只橘猫","negative":"模糊"}')
    assert (positive, negative) == ("一只橘猫", "模糊")
    with pytest.raises(ValueError, match="drawing_wish_invalid"):
        decode_wish('{"positive":"猫","negative":"","workflow":2}')
    with pytest.raises(ValueError, match="drawing_wish_invalid"):
        decode_wish('{"positive":"<lora:other:1>","negative":""}')


def test_wish_retains_user_negative_condition_and_detects_explicit_lora_even_if_model_omits_it():
    intent = decode_interpretation(response("wish"), "角色A的猫，不要 水印", ["角色A"])
    assert intent.lora_keys == ("角色A",) and intent.negative == "水印"
    positive, negative = decode_wish('{"positive":"橘猫","negative":"blurry"}', intent.negative)
    assert positive == "橘猫" and "blurry" in negative and "水印" in negative


def test_embedded_negative_is_not_guessed_as_the_rest_of_a_natural_sentence():
    intent = decode_interpretation(response("wish"), "画一只不要耳朵的猫", [])
    assert intent.negative == ""
    with pytest.raises(ValueError, match="drawing_negative_unclear"):
        decode_interpretation(response("direct"), "画一只不要耳朵的猫", [])


def test_explicit_unknown_lora_is_retained_for_default_workflow_fallback():
    intent = decode_interpretation(response("direct", ["角色X"]), "一只猫，使用 lora 角色X", [])
    assert intent.lora_keys == ("角色X",) and "lora" not in intent.positive.lower()
    suffix = decode_interpretation(response("direct", ["角色X"]), "一只猫，角色X 的 LoRA", [])
    assert suffix.lora_keys == ("角色X",)


def test_normal_english_words_are_not_mistaken_for_lora_syntax():
    intent = decode_interpretation(response("direct"), "floral background, colorful light", [])
    assert intent.positive == "floral background, colorful light" and intent.lora_keys == ()
