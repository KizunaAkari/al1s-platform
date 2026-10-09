import hashlib

from al1s.execution.package_builder import canonical_package_text


def test_wire_text_preserves_the_existing_python_hash_contract() -> None:
    body = {"whole": 1.0, "small": 1e-6, "large": 1e20, "title": "咖啡厅"}
    text = canonical_package_text(body)
    assert text == '{"large":1e+20,"small":1e-06,"title":"咖啡厅","whole":1.0}'
    assert hashlib.sha256(text.encode()).hexdigest() == hashlib.sha256(
        b'{"large":1e+20,"small":1e-06,"title":"\xe5\x92\x96\xe5\x95\xa1\xe5\x8e\x85","whole":1.0}'
    ).hexdigest()
