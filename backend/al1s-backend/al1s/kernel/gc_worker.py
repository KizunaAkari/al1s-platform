from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from al1s.kernel.ports import BlobStore, UnitOfWork
from al1s.kernel.types import FailedGcJob, GcBatchResult, PreparedGcJob


class GcWorker:
    """Deletes S3 objects outside transactions and records bounded retry state."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        blob_store: BlobStore,
        worker_id: str,
        lease_duration: timedelta = timedelta(seconds=30),
        batch_size: int = 50,
        max_attempts: int = 8,
        base_retry_delay: timedelta = timedelta(seconds=5),
        max_retry_delay: timedelta = timedelta(minutes=15),
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not worker_id:
            raise ValueError("worker_id is required")
        if not 1 <= batch_size <= 50:
            raise ValueError("batch_size must be between 1 and 50")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._uow_factory = uow_factory
        self._blob_store = blob_store
        self._worker_id = worker_id
        self._lease_duration = lease_duration
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._base_retry_delay = base_retry_delay
        self._max_retry_delay = max_retry_delay
        self._now = now or (lambda: datetime.now(UTC))

    def run_once(self) -> GcBatchResult:
        claim_time = self._now()
        with self._uow_factory() as uow:
            claimed = uow.gc_jobs.claim_batch(
                worker_id=self._worker_id,
                now=claim_time,
                lease_duration=self._lease_duration,
                limit=self._batch_size,
            )
            prepared = uow.blobs.prepare_deletions(claimed)
            uow.commit()

        if not claimed:
            return GcBatchResult(claimed=0, deleted=0, failed=0, stale=0)

        deleted: list[PreparedGcJob] = []
        prepared_ids = {job.job_id for job in prepared}
        failed = [
            FailedGcJob(
                job_id=job.job_id,
                row_version=job.row_version,
                attempt_count=job.attempt_count,
                error_type="BlobStateConflict",
            )
            for job in claimed
            if job.job_id not in prepared_ids
        ]
        for job in prepared:
            try:
                self._blob_store.delete(job.object_key)
                deleted.append(job)
            except Exception as exc:  # adapter failures are classified without storing messages
                failed.append(
                    FailedGcJob(
                        job_id=job.job_id,
                        row_version=job.job_row_version,
                        attempt_count=job.attempt_count,
                        error_type=type(exc).__name__[:255],
                    )
                )

        outcome_time = self._now()
        claimed_by_id = {job.job_id: job for job in claimed}
        completed_claims = [claimed_by_id[job.job_id] for job in deleted]
        with self._uow_factory() as uow:
            deleted_count = uow.blobs.mark_deleted(deleted, outcome_time)
            completed_count = uow.gc_jobs.mark_completed(
                completed_claims,
                worker_id=self._worker_id,
                completed_at=outcome_time,
            )
            failed_count = uow.gc_jobs.mark_failed(
                failed,
                worker_id=self._worker_id,
                failed_at=outcome_time,
                max_attempts=self._max_attempts,
                base_retry_delay=self._base_retry_delay,
                max_retry_delay=self._max_retry_delay,
            )
            if deleted_count != completed_count:
                uow.rollback()
                completed_count = 0
                deleted_count = 0
                failed_count = 0
            else:
                uow.commit()

        updated = completed_count + failed_count
        return GcBatchResult(
            claimed=len(claimed),
            deleted=completed_count,
            failed=failed_count,
            stale=len(claimed) - updated,
        )
