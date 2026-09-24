class CTGovError(Exception):
    """The API rejected a request or kept failing after retries."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class CTGovTimeout(CTGovError):
    pass
