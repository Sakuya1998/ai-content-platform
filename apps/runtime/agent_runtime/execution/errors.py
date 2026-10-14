"""Execution outcomes shared by workers and infrastructure adapters."""


class JobCancelled(RuntimeError):
    """The durable control state requires this worker to stop."""


class RetryableTaskError(RuntimeError):
    """Request a bounded business retry without MQ redelivery."""


class WaitingReview(dict):
    """A handler checkpoint that requires an explicit job.resume command."""
