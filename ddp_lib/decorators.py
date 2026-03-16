from functools import wraps
import traceback
from loguru import logger

from ddp_lib.exceptions import (
    InvalidDataError, 
    UnauthorizedError, 
    PermissionDeniedError,
    NotFoundError,
    AlreadyExistsError,
)

def catch_exceptions(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            traceback.print_exc()
            return {"status": "fail", "message": str(e)}, 500

    return wrapper


def exception_handler(message=None):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)

            except InvalidDataError as e:
                logger.error(f"Validation error: {e}")
                return _build_error_response(e.message, 400, e.error_code)

            except ValueError as e:
                logger.error(f"Value error: {e}")
                return _build_error_response(str(e), 400)

            except UnauthorizedError as e:
                logger.error(f"Unauthorized error: {e}")
                return _build_error_response(e.message, 401, e.error_code)

            except PermissionDeniedError as e:
                logger.error(f"Permission denied error: {e}")
                return _build_error_response(e.message, 403, e.error_code)

            except NotFoundError as e:
                logger.error(f"Not found error: {e}")
                return _build_error_response(e.message, 404, e.error_code)

            except AlreadyExistsError as e:
                logger.error(f"Already exists error: {e}")
                return _build_error_response(e.message, 409, e.error_code)

            except Exception as e:
                traceback.print_exc()
                logger.error(f"Error in {func.__name__}: {e}")
                return _build_error_response(
                    message or "Erreur interne du serveur",
                    500,
                    "INTERNAL_SERVER_ERROR"
                )

        return wrapper
    return decorator


def _build_error_response(message, status_code, error_code=None):
    response = {
        "status": "error",
        "message": message,
    }
    if error_code:
        response["error_code"] = error_code
    return response, status_code
