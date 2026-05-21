import asyncio
import logging
from typing import Callable, Optional, Dict, Any, Awaitable
from httpx import Request, Response


logger = logging.getLogger(__name__)


class Middleware:
    """Base class for HTTP middleware."""
    
    async def __call__(self, request: Request) -> Response:
        raise NotImplementedError


class LoggingMiddleware(Middleware):
    """Middleware that logs HTTP requests and responses."""
    
    def __init__(self, level: int = logging.INFO):
        self.level = level
    
    async def __call__(self, request: Request) -> Response:
        method = request.method
        url = str(request.url)
        logger.log(self.level, f"{method} {url}")
        
        # Execute the next middleware or handler
        response = await self._process(request)
        
        logger.log(self.level, f"{method} {url} -> {response.status_code}")
        return response
    
    async def _process(self, request: Request) -> Response:
        """Process the request through the chain."""
        raise NotImplementedError


class HeaderMiddleware(Middleware):
    """Middleware that adds/removes headers from requests."""
    
    def __init__(
        self,
        add_headers: Optional[Dict[str, str]] = None,
        remove_headers: Optional[Dict[str, str]] = None
    ):
        self.add_headers = add_headers or {}
        self.remove_headers = remove_headers or {}
    
    async def __call__(self, request: Request) -> Response:
        # Remove headers
        for header in self.remove_headers:
            request.headers.pop(header, None)
        
        # Add headers
        for key, value in self.add_headers.items():
            request.headers[key] = value
        
        # Execute the next middleware or handler
        response = await self._process(request)
        
        return response
    
    async def _process(self, request: Request) -> Response:
        """Process the request through the chain."""
        raise NotImplementedError


class MiddlewareChain:
    """Chain of middleware that processes requests."""
    
    def __init__(self, *middlewares: Middleware):
        self.middlewares = middlewares
    
    async def __call__(self, request: Request) -> Response:
        """Process request through all middleware in order."""
        for middleware in self.middlewares:
            request = await middleware(request)
        return request
    
    def add(self, middleware: Middleware) -> 'MiddlewareChain':
        """Add middleware to the chain."""
        self.middlewares = (*self.middlewares, middleware)
        return self
    
    def remove(self, middleware: Middleware) -> 'MiddlewareChain':
        """Remove middleware from the chain."""
        self.middlewares = tuple(m for m in self.middlewares if m is not middleware)
        return self
