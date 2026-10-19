import pytest

from al1s.components.qq_compatibility import assert_qq_source_verified, compatible_qq_sources


def test_only_verified_native_pairs_are_compatible():
    assert compatible_qq_sources("8.3.2") == "8.1.9,8.3.2"
    assert compatible_qq_sources("8.4.0") == "8.4.0"
    with pytest.raises(ValueError, match="unverified"):
        assert_qq_source_verified("8.4.0", "sha256:" + "a" * 64)
    with pytest.raises(ValueError, match="unverified"):
        assert_qq_source_verified("8.3.2", "sha256:" + "a" * 64)


def test_verified_version_is_bound_to_the_reviewed_official_image_index():
    assert_qq_source_verified(
        "8.3.2", "sha256:0b9e20fdda15db223eceff87b0afd51c82dee156ce3c49424f1eb7f417786cdd"
    )
