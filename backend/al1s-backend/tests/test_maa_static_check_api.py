from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from al1s.api.maa_catalog import StaticCheckRequest, check_script_candidate
from al1s.maa.types import QualificationKind, QualificationStatus


def test_check_uses_exact_candidate_and_projects_validation_issues():
    service = Mock()
    receipt_id, script_id, version_id, correlation_id = (uuid4() for _ in range(4))
    service.validate_candidate.return_value = SimpleNamespace(
        receipt_id=receipt_id,
        kind=QualificationKind.STATIC_CHECK,
        status=QualificationStatus.FAILED,
        executor_version="maa-dsl-v2.2",
        created_at=datetime.now(UTC),
        diagnostic={
            "issues": [{"code": "invalid", "pointer": "/steps/0", "message": "invalid step"}]
        },
    )
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(maa_publication=service)),
        state=SimpleNamespace(request_id=str(correlation_id)),
    )
    result = check_script_candidate(
        script_id, StaticCheckRequest(candidate_version_id=version_id), request, "same-request"
    )
    service.validate_candidate.assert_called_once_with(
        script_id=script_id,
        candidate_version_id=version_id,
        idempotency_key="same-request",
        correlation_id=correlation_id,
    )
    assert result.status == "failed" and result.receipt_id == receipt_id
    assert result.issues[0]["pointer"] == "/steps/0"
