class DomainError(Exception):
    """A failure the caller can act on; its message is shown to agents and users as is."""


class NotFoundError(DomainError):
    pass


class ConflictError(DomainError):
    pass


class ValidationError(DomainError):
    pass


class ConfirmationNeeded(ValidationError):
    """The write is allowed only with an explicit confirmation (raising live above read)."""
