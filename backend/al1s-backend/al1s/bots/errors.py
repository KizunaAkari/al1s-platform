class BotDomainError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class BotAuthenticationError(BotDomainError):
    def __init__(self) -> None:
        super().__init__("invalid_bot_credential", "Bot worker authentication failed", 401)
