# ruff: noqa: RUF001
import json

import pytest

from al1s.notifications.lexicon import TextNormalizer, parse_lexicon


def test_both_terms_and_messages_are_normalized_without_changing_original():
    normalizer = TextNormalizer()
    raw = json.dumps({"words": ["測試", "ＡＢＣ", "测试"]}, ensure_ascii=False).encode()
    lexicon = parse_lexicon(raw, "a" * 40, "MIT License", normalizer)
    original = "這是測試，abc"
    assert set(lexicon.match(original, normalizer)) == {"测试", "abc"}
    assert original == "這是測試，abc"
    assert len(lexicon.words) == 2


@pytest.mark.parametrize("words", [[], [""], [None], ["a" * 129], "text"])
def test_bad_words_rejected(words):
    with pytest.raises(ValueError):
        parse_lexicon(json.dumps({"words": words}).encode(), "a" * 40,
                      "MIT License", TextNormalizer())


def test_unverified_license_or_commit_rejected():
    for commit, license_text in [("main", "MIT License"), ("a" * 40, "unknown")]:
        with pytest.raises(ValueError):
            parse_lexicon(b'{"words":["test"]}', commit, license_text, TextNormalizer())
