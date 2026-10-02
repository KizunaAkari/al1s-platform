import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from al1s.notifications.lexicon import TextNormalizer, parse_lexicon
from al1s.notifications.lexicon_service import LexiconService


def test_restart_reads_saved_version_and_checks_hash():
    value = parse_lexicon(json.dumps({"words": ["test"]}).encode(), "a" * 40,
                          "MIT License", TextNormalizer())
    repository = MagicMock()
    repository.active.return_value = value
    service = LexiconService(repository)
    assert service.match("TEST")[1] == ("test",)
    repository.active.return_value = SimpleNamespace(
        commit=value.commit, sha256="bad", raw=value.raw,
        license_text=value.license_text, normalization_version=value.normalization_version,
    )
    with pytest.raises(ValueError, match="hash"):
        service.current()


def test_download_failure_never_activates():
    repository = MagicMock()
    with (
        patch("al1s.notifications.lexicon_service.fetch_lexicon", side_effect=TimeoutError),
        pytest.raises(TimeoutError),
    ):
        LexiconService(repository).update()
    repository.activate.assert_not_called()


def test_missing_lexicon_holds_instead_of_allowing():
    repository = MagicMock()
    repository.active.return_value = None
    assert LexiconService(repository).match("text") is None
