class ExecutionDomainError(Exception):
    """Stable, user-safe domain error exposed by the Stage 3A API."""

    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class InvalidRequestError(ExecutionDomainError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, 400)


class AuthenticationError(ExecutionDomainError):
    def __init__(
        self,
        code: str = "invalid_terminal_credential",
        message: str = "Terminal credential is invalid",
    ) -> None:
        super().__init__(code, message, 401)


class NotFoundError(ExecutionDomainError):
    def __init__(self, resource: str) -> None:
        message = f"{resource.replace('_', ' ').title()} not found"
        super().__init__(f"{resource}_not_found", message, 404)


class ConflictError(ExecutionDomainError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, 409)
