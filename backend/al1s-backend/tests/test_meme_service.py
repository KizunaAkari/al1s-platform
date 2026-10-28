from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from al1s.meme_workshop.contracts import RenderInputs
from al1s.meme_workshop.service import MemeWorkshopService


def test_random_selection_uses_the_complete_eligible_set_and_handles_no_candidate(monkeypatch):
    repository, assets = MagicMock(), MagicMock()
    service = MemeWorkshopService(repository, assets)
    candidates = [MagicMock(), MagicMock(), MagicMock()]
    repository.random_candidates.return_value = candidates
    choose = MagicMock(return_value=candidates[2])
    monkeypatch.setattr("al1s.meme_workshop.service.random.SystemRandom", lambda: choose)
    choose.choice.return_value = candidates[2]
    supplied = RenderInputs(avatars=[b"avatar"], texts=["hello"])
    assert service._choose("随机表情", supplied) == candidates[2]
    repository.random_candidates.assert_called_once_with(1, 0, 1)
    choose.choice.assert_called_once_with(candidates)
    repository.random_candidates.return_value = []
    assert service._choose("随机表情", RenderInputs()) is None
    choose.choice.assert_called_once()


def test_a_stopped_or_expired_receipt_cannot_render_or_read_assets():
    repository, assets = MagicMock(), MagicMock()
    repository.receipt_available.return_value = False
    service = MemeWorkshopService(repository, assets)
    with pytest.raises(ValueError, match="receipt_unavailable"):
        service.render_command(uuid4(), uuid4(), "摸摸", RenderInputs())
    repository.find_keyword.assert_not_called()
    assets.read_many.assert_not_called()
