class AppError(Exception):
    def __init__(self, message: str, error_code: str | None = None):
        self.message = message
        self.error_code = error_code
        super().__init__(message)

class NotFoundError(AppError):
    """Raised when the requested resource is not found."""


class AlreadyExistsError(AppError):
    """Raised when the resource already exists."""


class InvalidDataError(AppError):
    """Raised when the provided data is invalid."""


class UnauthorizedError(AppError):
    """Raised when the user is not authenticated."""


class PermissionDeniedError(AppError):
    """Raised when the user is not allowed to perform the action."""