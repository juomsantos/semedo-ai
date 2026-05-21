import asyncio
from typing import Callable, Any, Optional, Dict, Type
from httpx import TimeoutException, ConnectError, HTTPStatusError


class RetryError(Exception):
    """Exception raised when all retries are exhausted."""
    def __init__(self, message: str, last_exception: Optional[Exception] = None):
        self.message = message
        self.last_exception = last_exception
        super().__init__(self.message)


async def retry_async(
    func: Callable[..., Any],
    *args: Any,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    retryable_exceptions: Optional[Type[Exception]] = None,
    **kwargs: Any
) -> Any:
    """
    Execute a coroutine with exponential backoff retry logic.
    
    Args:
        func: Async callable to execute
        *args: Positional arguments for func
        max_retries: Maximum number of retry attempts
        base_delay: Base delay in seconds for exponential backoff
        max_delay: Maximum delay between retries
        retryable_exceptions: Exception types to retry on (default: ConnectError, TimeoutException, HTTPStatusError)
        **kwargs: Keyword arguments for func
    
    Returns:
        Result of the function call
    
    Raises:
        RetryError: When all retries are exhausted
    """
    if retryable_exceptions is None:
        retryable_exceptions = (ConnectError, TimeoutException, HTTPStatusError)
    
    delay = base_delay
    last_exception: Optional[Exception] = None
    
    for attempt in range(max_retries + 1):
        try:
            return await func(*args, **kwargs)
        except retryable_exceptions as e:
            last_exception = e
            if attempt < max_retries:
                await asyncio.sleep(delay)
                delay = min(delay * 2, max_delay)
                continue
    
    raise RetryError(
        f"Failed after {max_retries} retries: {last_exception}",
        last_exception=last_exception
    )
