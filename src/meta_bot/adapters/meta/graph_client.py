"""Meta Graph API HTTP client adapter with rate limiting, retries, and error mapping."""

import re
from typing import Any

import httpx
import structlog
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from meta_bot.config import Settings, get_settings
from meta_bot.errors import (
    MetaRateLimitError,
    MetaTransientError,
    map_meta_error,
)
from meta_bot.services.ports import GraphApi

logger = structlog.get_logger(__name__)

# Only retry transient (5xx, timeouts) and rate limit (429) errors.
# Meta permanent, auth, permission, and window policy errors must NEVER be retried.
_RETRYABLE_EXCEPTIONS = (MetaTransientError, MetaRateLimitError)


def _redact_path(path: str) -> str:
    """Mask numeric IDs in Graph API paths for secure logging."""
    return re.sub(r"/(?:\d{6,})", "/[ID]", path)


class GraphClient(GraphApi):
    """Asynchronous client for interacting with Meta Graph API.

    Enforces API versioning, token authorization headers, exponential backoff with jitter
    on transient failures, and comprehensive error mapping.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=15),
            timeout=httpx.Timeout(timeout=10.0, connect=5.0),
        )

    async def close(self) -> None:
        """Close underlying HTTP client resources."""
        if self._owns_client:
            await self._client.aclose()

    def _build_url(self, path: str) -> str:
        """Construct fully-qualified Graph API URL with pinned version prefix."""
        clean_path = path.lstrip("/")
        version = self._settings.GRAPH_VERSION.strip("/")
        base_url = self._settings.GRAPH_BASE_URL.rstrip("/")
        if clean_path.startswith(f"{version}/"):
            return f"{base_url}/{clean_path}"
        return f"{base_url}/{version}/{clean_path}"

    def _get_headers(self, access_token: str | None) -> dict[str, str]:
        """Generate request headers using Bearer token authorization."""
        headers = {
            "Accept": "application/json",
            "User-Agent": "MetaBot/0.1.0",
        }
        token = access_token
        if not token and self._settings.PAGE_ACCESS_TOKEN:
            token = self._settings.PAGE_ACCESS_TOKEN.get_secret_value()

        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _execute_request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        form_data: dict[str, Any] | None = None,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        """Execute HTTP request with tenacity retries and error parsing."""
        url = self._build_url(path)
        headers = self._get_headers(access_token)
        redacted_path = _redact_path(path)

        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type(_RETRYABLE_EXCEPTIONS),
            stop=stop_after_attempt(3),
            wait=wait_exponential_jitter(initial=1.0, max=8.0),
            reraise=True,
        ):
            with attempt:
                try:
                    response = await self._client.request(
                        method=method,
                        url=url,
                        params=params,
                        json=json_data,
                        data=form_data,
                        headers=headers,
                    )
                except (
                    httpx.ConnectError,
                    httpx.TimeoutException,
                    httpx.NetworkError,
                ) as exc:
                    logger.warning(
                        "Graph API request network failure",
                        method=method,
                        path=redacted_path,
                        attempt=attempt.retry_state.attempt_number,
                    )
                    raise MetaTransientError(
                        status_code=503,
                        message=f"Network error communicating with Meta: {exc}",
                    ) from exc

                retry_after_header = response.headers.get("Retry-After")
                retry_after: float | None = None
                if retry_after_header:
                    try:
                        retry_after = float(retry_after_header)
                    except ValueError:
                        retry_after = None

                # Attempt parsing JSON response
                try:
                    resp_json = response.json()
                except (ValueError, UnicodeDecodeError):
                    resp_json = None

                # Extract fbtrace_id if present
                fbtrace_id: str | None = None
                if isinstance(resp_json, dict) and isinstance(
                    resp_json.get("error"), dict
                ):
                    fbtrace_id = resp_json["error"].get("fbtrace_id")

                logger.info(
                    "Graph API request completed",
                    method=method,
                    path=redacted_path,
                    status_code=response.status_code,
                    fbtrace_id=fbtrace_id,
                )

                if response.is_error or (
                    isinstance(resp_json, dict) and "error" in resp_json
                ):
                    mapped_err = map_meta_error(
                        resp_json if isinstance(resp_json, dict) else None,
                        status_code=response.status_code,
                        retry_after=retry_after,
                    )
                    raise mapped_err

                if resp_json is None:
                    return {}
                return resp_json  # type: ignore[no-any-return]

        raise MetaTransientError(status_code=500, message="Retries exhausted")

    async def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        """Perform a GET request to the Graph API."""
        return await self._execute_request(
            method="GET",
            path=path,
            params=params,
            access_token=access_token,
        )

    async def post(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        """Perform a POST request to the Graph API."""
        if self._settings.DEMO_MODE:
            logger.info(
                "DEMO_MODE active: skipping Graph API POST mutation",
                path=_redact_path(path),
            )
            return {"demo_mode": True, "success": True}

        return await self._execute_request(
            method="POST",
            path=path,
            json_data=json,
            form_data=data,
            access_token=access_token,
        )

    async def debug_token(self, input_token: str) -> dict[str, Any]:
        """Inspect and validate an access token using app-level authentication."""
        app_id = self._settings.APP_ID
        app_secret = self._settings.APP_SECRET.get_secret_value()
        app_token = f"{app_id}|{app_secret}"

        # Meta endpoint: GET /debug_token?input_token={input_token} with app_token
        params = {"input_token": input_token}
        return await self.get(
            path="debug_token",
            params=params,
            access_token=app_token,
        )

    async def get_page_instagram_account(self, page_id: str) -> dict[str, Any]:
        """Query Facebook Page for linked Instagram professional account."""
        # Meta endpoint: GET /{page_id}?fields=instagram_business_account
        params = {"fields": "instagram_business_account"}
        return await self.get(path=page_id, params=params)
