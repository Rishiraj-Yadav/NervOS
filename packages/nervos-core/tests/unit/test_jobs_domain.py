"""C1 durable execution domain vocabulary."""

from nervos_core.domain.jobs import AttemptStatus, JobStatus, RetryDisposition


def test_exact_c1_vocabularies() -> None:
    assert {x.value for x in JobStatus} == {
        "queued",
        "claimed",
        "running",
        "retry_wait",
        "succeeded",
        "failed",
        "cancelled",
    }
    assert {x.value for x in AttemptStatus} == {
        "claimed",
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "expired",
    }
    assert {x.value for x in RetryDisposition} == {"SAFE_TO_RETRY", "DO_NOT_RETRY", "AMBIGUOUS"}
