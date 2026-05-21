import pytest
from unittest.mock import AsyncMock, patch
from httpx import Response as HttpxResponse, TimeoutException, ConnectError, HTTPStatusError
from src.http_client import AsyncHTTPClient, Response


@pytest.fixture
def mock_response():
    """Create a mock httpx response."""
    return AsyncMock(
        status_code=200,
        headers={"content-type": "application/json"},
        read=AsyncMock(return_value=b'{"key": "value"}'),
        url="http://test.com/api"
    )


@pytest.fixture
def mock_response_500():
    """Create a mock httpx response with 500 status."""
    return AsyncMock(
        status_code=500,
        headers={"content-type": "application/json"},
        read=AsyncMock(return_value=b'{"error": "internal server error"}'),
        url="http://test.com/api"
    )


@pytest.fixture
def mock_response_404():
    """Create a mock httpx response with 404 status."""
    return AsyncMock(
        status_code=404,
        headers={"content-type": "application/json"},
        read=AsyncMock(return_value=b'{"error": "not found"}'),
        url="http://test.com/api"
    )


@pytest.fixture
def mock_response_timeout():
    """Create a mock httpx response with timeout error."""
    return AsyncMock(
        status_code=504,
        headers={"content-type": "application/json"},
        read=AsyncMock(return_value=b'{"error": "timeout"}'),
        url="http://test.com/api"
    )


@pytest.fixture
def mock_response_connection_error():
    """Create a mock httpx response with connection error."""
    return AsyncMock(
        status_code=0,
        headers={},
        read=AsyncMock(return_value=b''),
        url="http://test.com/api"
    )


@pytest.fixture
def mock_response_503():
    """Create a mock httpx response with 503 status."""
    return AsyncMock(
        status_code=503,
        headers={"content-type": "application/json"},
        read=AsyncMock(return_value=b'{"error": "service unavailable"}'),
        url="http://test.com/api"
    )


class TestAsyncHTTPClient:
    """Tests for AsyncHTTPClient class."""
    
    @pytest.mark.asyncio
    async def test_get_success(self, mock_response):
        """Test successful GET request."""
        client = AsyncHTTPClient(base_url="http://test.com")
        
        with patch('src.http_client.AsyncClient') as mock_client_class:
            mock_client_instance = AsyncMock()
            mock_client_instance.get = AsyncMock(return_value=mock_response)
            mock_client_class.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_class.return_value.__aexit__ = AsyncMock(return_value=None)
            
            result = await client.get("/api/test")
            
            assert result.status_code == 200
            assert result.data == b'{"key": "value"}'
            assert result.url == "http://test.com/api/test"
    
    @pytest.mark.asyncio
    async def test_post_success(self, mock_response):
        """Test successful POST request."""
        client = AsyncHTTPClient(base_url="http://test.com")
        
        with patch('src.http_client.AsyncClient') as mock_client_class:
            mock_client_instance = AsyncMock()
            mock_client_instance.post = AsyncMock(return_value=mock_response)
            mock_client_class.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_class.return_value.__aexit__ = AsyncMock(return_value=None)
            
            result = await client.post("/api/test", json={"key": "value"})
            
            assert result.status_code == 200
            assert result.data == b'{"key": "value"}'
    
    @pytest.mark.asyncio
    async def test_put_success(self, mock_response):
        """Test successful PUT request."""
        client = AsyncHTTPClient(base_url="http://test.com")
        
        with patch('src.http_client.AsyncClient') as mock_client_class:
            mock_client_instance = AsyncMock()
            mock_client_instance.put = AsyncMock(return_value=mock_response)
            mock_client_class.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_class.return_value.__aexit__ = AsyncMock(return_value=None)
            
            result = await client.put("/api/test", json={"key": "value"})
            
            assert result.status_code == 200
            assert result.data == b'{"key": "value"}'
    
    @pytest.mark.asyncio
    async def test_delete_success(self, mock_response):
        """Test successful DELETE request."""
        client = AsyncHTTPClient(base_url="http://test.com")
        
        with patch('src.http_client.AsyncClient') as mock_client_class:
            mock_client_instance = AsyncMock()
            mock_client_instance.delete = AsyncMock(return_value=mock_response)
            mock_client_class.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_class.return_value.__aexit__ = AsyncMock(return_value=None)
            
            result = await client.delete("/api/test")
            
            assert result.status_code == 200
            assert result.data == b'{"key": "value"}'
    
    @pytest.mark.asyncio
    async def test_get_with_params(self, mock_response):
        """Test GET request with query parameters."""
        client = AsyncHTTPClient(base_url="http://test.com")
        
        with patch('src.http_client.AsyncClient') as mock_client_class:
            mock_client_instance = AsyncMock()
            mock_client_instance.get = AsyncMock(return_value=mock_response)
            mock_client_class.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_class.return_value.__aexit__ = AsyncMock(return_value=None)
            
            result = await client.get("/api/test", params={"key": "value"})
            
            assert result.status_code == 200
    
    @pytest.mark.asyncio
    async def test_close_client(self):
        """Test client close method."""
        client = AsyncHTTPClient(base_url="http://test.com")
        
        with patch('src.http_client.AsyncClient') as mock_client_class:
            mock_client_instance = AsyncMock()
            mock_client_instance.aclose = AsyncMock()
            mock_client_class.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
            
            await client.close()
            
            mock_client_instance.aclose.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_get_with_headers(self, mock_response):
        """Test GET request with custom headers."""
        client = AsyncHTTPClient(
            base_url="http://test.com",
            headers={"Authorization": "Bearer token123"}
        )
        
        with patch('src.http_client.AsyncClient') as mock_client_class:
            mock_client_instance = AsyncMock()
            mock_client_instance.get = AsyncMock(return_value=mock_response)
            mock_client_class.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_class.return_value.__aexit__ = AsyncMock(return_value=None)
            
            result = await client.get("/api/test")
            
            assert result.status_code == 200
    
    @pytest.mark.asyncio
    async def test_post_with_data(self, mock_response):
        """Test POST request with data parameter."""
        client = AsyncHTTPClient(base_url="http://test.com")
        
        with patch('src.http_client.AsyncClient') as mock_client_class:
            mock_client_instance = AsyncMock()
            mock_client_instance.post = AsyncMock(return_value=mock_response)
            mock_client_class.return_value.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_class.return_value.__aexit__ = AsyncMock(return_value=None)
            
            result = await client.post("/api/test", data={"key": "value"})
            
            assert result.status_code == 200


class TestResponseDataclass:
    """Tests for Response dataclass."""
    
    def test_response_creation(self):
        """Test Response dataclass creation."""
        response = Response(
            status_code=200,
            headers={"content-type": "application/json"},
            data=b'{"key": "value"}',
            url="http://test.com/api"
        )
        
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        assert response.data == b'{"key": "value"}'
        assert response.url == "http://test.com/api"
    
    def test_response_headers_dict(self):
        """Test Response headers as dict."""
        response = Response(
            status_code=200,
            headers={"content-type": "application/json", "x-custom": "header"},
            data=b'{}',
            url="http://test.com/api"
        )
        
        assert "content-type" in response.headers
        assert "x-custom" in response.headers


class TestRetryAsync:
    """Tests for retry_async function."""
    
    @pytest.mark.asyncio
    async def test_retry_success_on_first_try(self):
        """Test retry succeeds on first attempt."""
        async def success_func():
            return "success"
        
        result = await retry_async(success_func)
        assert result == "success"
    
    @pytest.mark.asyncio
    async def test_retry_exhausted(self, mock_response_500):
        """Test retry exhausted after max retries."""
        async def failing_func():
            raise HTTPStatusError(500, request=None, response=mock_response_500)
        
        with pytest.raises(Exception) as exc_info:
            await retry_async(failing_func, max_retries=2)
        
        assert "Failed after 2 retries" in str(exc_info.value)
    
    @pytest.mark.asyncio
    async def test_retry_with_timeout(self, mock_response_timeout):
        """Test retry with timeout error."""
        async def failing_func():
            raise TimeoutException("Connection timed out")
        
        with pytest.raises(Exception) as exc_info:
            await retry_async(failing_func, max_retries=2)
        
        assert "Failed after 2 retries" in str(exc_info.value)
    
    @pytest.mark.asyncio
    async def test_retry_with_connection_error(self, mock_response_connection_error):
        """Test retry with connection error."""
        async def failing_func():
            raise ConnectError("Connection refused")
        
        with pytest.raises(Exception) as exc_info:
            await retry_async(failing_func, max_retries=2)
        
        assert "Failed after 2 retries" in str(exc_info.value)


class TestMiddleware:
    """Tests for middleware classes."""
    
    @pytest.mark.asyncio
    async def test_logging_middleware(self):
        """Test logging middleware."""
        from src.middleware import LoggingMiddleware
        
        middleware = LoggingMiddleware(level=logging.INFO)
        
        # Test that middleware can be instantiated
        assert middleware is not None
    
    @pytest.mark.asyncio
    async def test_header_middleware(self):
        """Test header middleware."""
        from src.middleware import HeaderMiddleware
        
        middleware = HeaderMiddleware(
            add_headers={"X-Custom": "header"},
            remove_headers={"X-Remove": None}
        )
        
        # Test that middleware can be instantiated
        assert middleware is not None
    
    @pytest.mark.asyncio
    async def test_middleware_chain(self):
        """Test middleware chain."""
        from src.middleware import MiddlewareChain, LoggingMiddleware, HeaderMiddleware
        
        chain = MiddlewareChain()
        
        # Test that chain can be created
        assert chain is not None
        
        # Test adding middleware
        chain.add(LoggingMiddleware())
        chain.add(HeaderMiddleware())
        
        assert len(chain.middlewares) == 2
