"""Guard failure types."""


class GuardViolation(Exception):
    """Raised when an invariant guard rejects a deliberate or accidental violation."""


class StaleLeaseError(GuardViolation):
    """Raised when a lease-fenced task state update affects zero rows."""
