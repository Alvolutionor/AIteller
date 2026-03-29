# src/utils/retry.py
import logging
from aiohttp import ClientError
from tenacity import (
    retry, stop_after_attempt, wait_exponential,
    retry_if_exception_type, before_sleep_log,
)

logger = logging.getLogger(__name__)

def async_retry(max_attempts: int = 3, min_wait: float = 1, max_wait: float = 30):
    """Decorator for async functions with exponential backoff retry."""
    return retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
        retry=retry_if_exception_type((ConnectionError, TimeoutError, ClientError, OSError)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
