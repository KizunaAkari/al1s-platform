from datetime import UTC, datetime, timedelta

import pytest

from al1s.notifications.review_policy import ReviewPolicy, ReviewState

NOW = datetime(2026, 9, 14, tzinfo=UTC)


@pytest.mark.parametrize("state", [ReviewState.HOLD, ReviewState.PENDING, ReviewState.APPROVED])
def test_exact_deadline_cannot_continue(state):
    policy = ReviewPolicy(state, NOW, pending_at=NOW, approved_at=NOW)
    boundary = NOW + timedelta(days=7)
    assert not policy.expired(boundary - timedelta(microseconds=1))
    assert policy.expired(boundary)
    assert not policy.can_approve(boundary)
    assert not policy.can_send(boundary)
    assert not policy.can_delete()


def test_stage_deadlines_have_separate_origins():
    pending = NOW + timedelta(days=6)
    approved = pending + timedelta(days=6)
    policy = ReviewPolicy(ReviewState.APPROVED, NOW, pending, approved)
    assert policy.deadline() == approved + timedelta(days=7)


@pytest.mark.parametrize("state", [
    ReviewState.REJECTED, ReviewState.EXPIRED, ReviewState.COMPLETED,
])
def test_final_cannot_send_and_summary_has_separate_retention(state):
    policy = ReviewPolicy(state, NOW, finalized_at=NOW + timedelta(days=3))
    assert policy.can_delete()
    assert not policy.can_send(NOW)
    assert not policy.summary_expired(NOW + timedelta(days=30))
    assert policy.summary_expired(NOW + timedelta(days=33))
