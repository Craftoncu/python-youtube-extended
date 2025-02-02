"""The YouTube API."""
from __future__ import annotations

import asyncio
from dataclasses import field
from logging import getLogger
from typing import TYPE_CHECKING, Any, TypeVar

import async_timeout
from aiohttp import ClientError, ClientResponse, ClientSession

from youtubeaio.helper import (
    build_url,
    first,
)
from youtubeaio.models import (
    YouTubeChannel,
    YouTubePlaylistItem,
    YouTubeSubscription,
    YouTubeVideo,
    SuperChatEvent,
)
from youtubeaio.types import (
    AuthScope,
    ForbiddenError,
    MissingScopeError,
    UnauthorizedError,
    YouTubeAPIError,
    YouTubeBackendError,
    YouTubeResourceNotFoundError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Coroutine

    from typing_extensions import Self

__all__ = [
    "YouTube",
]
T = TypeVar("T")


class YouTube:
    """YouTube API client."""

    base_url: str = "https://youtube.googleapis.com/youtube/v3/"
    _close_session: bool = False

    logger = getLogger(__name__)

    _user_auth_token: str | None = None
    _user_auth_refresh_token: str | None = None
    _user_auth_scopes: list[AuthScope] = field(default_factory=list)
    _has_user_auth = False

    def __init__(
        self,
        app_id: str | None = None,
        app_secret: str | None = None,
        session: ClientSession | None = None,
        session_timeout: int = 10,
        auto_refresh_auth: bool | None = None,
    ) -> None:
        """Initialize YouTube object."""
        self.session = session
        self.session_timeout = session_timeout
        self.app_id = app_id
        self.app_secret = app_secret
        if auto_refresh_auth is None:
            self.auto_refresh_auth = app_id is not None and app_secret is not None
        else:
            self.auto_refresh_auth = auto_refresh_auth
        self._r_lookup: dict[
            str,
            Callable[
                [ClientSession, str, dict[str, Any] | None],
                Coroutine[Any, Any, ClientResponse],
            ],
        ] = {
            "get": self._api_get_request,
        }

    async def _check_request_return(self, response: ClientResponse) -> ClientResponse:
        if response.status == 500:
            msg = "Internal Server Error"
            raise YouTubeBackendError(msg)
        if response.status == 400:
            msg = (await response.json()).get("message")
            raise YouTubeAPIError(
                "Bad Request" + ("" if msg is None else f" - {msg!s}"),
            )
        if response.status == 404:
            raise YouTubeResourceNotFoundError
        if response.status == 401:
            raise UnauthorizedError
        if response.status == 403:
            response_json = await response.json()
            error_message = response_json["error"]["errors"][0]["message"]
            raise ForbiddenError(error_message)
        if 400 <= response.status < 500:
            try:
                response.raise_for_status()
            except ClientError as exc:
                raise YouTubeAPIError from exc
        return response

    async def _api_get_request(
        self,
        session: ClientSession,
        url: str,
        data: dict[str, Any] | None = None,
    ) -> ClientResponse:
        """Make GET request with authorization."""
        headers = {"Authorization": f"Bearer {self._user_auth_token}"}
        self.logger.debug("making GET request to %s", url)
        response = await session.get(url, headers=headers, json=data)
        return await self._check_request_return(response)

    async def _build_generator(
        self,
        req: str,
        url: str,
        url_params: dict[str, Any],
        return_type: T,
        body_data: dict[str, Any] | None = None,
        split_lists: bool = False,
    ) -> AsyncGenerator[T, None]:
        method = self._r_lookup.get(req.lower(), self._api_get_request)
        _after = url_params.get("nextPageToken")
        _first = True
        if not self.session:
            self.session = ClientSession()
            self._close_session = True
        try:
            while _first or _after is not None:
                url_params["pageToken"] = _after
                _url = build_url(
                    self.base_url + url,
                    url_params,
                    remove_none=True,
                    split_lists=split_lists,
                )
                async with async_timeout.timeout(self.session_timeout):
                    response = await method(self.session, _url, body_data)
                if response.content_type != "application/json":
                    msg = "Unexpected response type"
                    raise YouTubeAPIError(msg)
                data = await response.json()
                for entry in data.get("items", []):
                    yield return_type(**entry)  # type: ignore[operator]
                _after = data.get("nextPageToken")
                _first = False
        except asyncio.TimeoutError as exc:
            msg = "Timeout occurred"
            raise YouTubeBackendError(msg) from exc

    async def set_user_authentication(
        self,
        token: str,
        scopes: list[AuthScope],
        refresh_token: str | None = None,
    ) -> None:
        """Set a user token to be used.

        :param token: the generated user token
        :param scopes: List of Authorization Scopes that the given user token has
        :param refresh_token: The generated refresh token, has to be provided if
        :attr:`auto_refresh_auth` is True |default| :code:`None`
        :raises ValueError: if :attr:`auto_refresh_auth` is True but refresh_token is
        not set
        :raises ~youtubeaio.types.MissingScopeException: if given token is
         missing one of the required scopes
        :raises ~youtubeaio.types.InvalidTokenException: if the given token
        is invalid or for a different client id
        """
        if refresh_token is None and self.auto_refresh_auth:
            msg = "refresh_token has to be provided when auto_refresh_auth is True"
            raise ValueError(msg)
        if not scopes:
            msg = "scope was not provided"
            raise MissingScopeError(msg)

        self._user_auth_token = token
        self._user_auth_refresh_token = refresh_token
        self._user_auth_scopes = scopes
        self._has_user_auth = True

    def get_user_auth_token(self) -> str | None:
        """Return the current user auth token, None if no user Authentication is set.

        :return: current user auth token
        """
        return self._user_auth_token

    async def get_videos(
        self,
        video_ids: list[str],
    ) -> AsyncGenerator[YouTubeVideo, None]:
        """Get videos by id."""
        if not video_ids:
            msg = "at least one video id has to be set"
            raise ValueError(msg)
        param = {
            "part": "snippet",
            "id": ",".join(video_ids),
        }
        async for item in self._build_generator(
            "GET",
            "videos",
            param,
            YouTubeVideo,
        ):
            yield item  # type: ignore[misc]

    async def get_video(self, video_id: str) -> YouTubeVideo | None:
        """Get a single video."""
        return await first(self.get_videos([video_id]))

    async def _get_channels(
        self,
        param: dict[str, Any],
    ) -> AsyncGenerator[YouTubeChannel, None]:
        """Get channels."""
        async for item in self._build_generator(
            "GET",
            "channels",
            param,
            YouTubeChannel,
        ):
            yield item  # type: ignore[misc]

    async def get_user_channels(self) -> AsyncGenerator[YouTubeChannel, None]:
        """Return channels owned by the authenticated user."""
        param = {
            "part": "snippet",
            "mine": "true",
        }
        async for item in self._get_channels(param):
            yield item

    async def get_channels(
        self,
        channel_ids: list[str],
    ) -> AsyncGenerator[YouTubeChannel, None]:
        """Return list of channels."""
        param = {
            "part": "snippet,contentDetails,statistics",
            "id": ",".join(channel_ids),
        }
        async for item in self._get_channels(param):
            yield item

    async def get_user_subscriptions(
        self,
    ) -> AsyncGenerator[YouTubeSubscription, None]:
        """Get subscriptions for authenticated user."""
        param = {
            "part": "snippet",
            "mine": "true",
        }
        async for item in self._build_generator(
            "GET",
            "subscriptions",
            param,
            YouTubeSubscription,
        ):
            yield item  # type: ignore[misc]

    async def get_super_chats_events(
        self,
    ) -> AsyncGenerator[SuperChatEvent, None]:
        """Get super-chats for authenticated channel."""
        param = {
            "part": "snippet",
            "mine": "true",
        }
        async for item in self._build_generator(
            "GET",
            "superChatEvents",
            param,
            SuperChatEvent,
        ):
            yield item  # type: ignore[misc]

    async def get_playlist_items(
        self,
        playlist_id: str,
        max_results: int = 50,
    ) -> AsyncGenerator[YouTubePlaylistItem, None]:
        """Get playlist by id."""
        param = {
            "part": "snippet,contentDetails",
            "playlistId": playlist_id,
            "maxResults": max_results,
        }
        async for item in self._build_generator(
            "GET",
            "playlistItems",
            param,
            YouTubePlaylistItem,
        ):
            yield item  # type: ignore[misc]

    async def close(self) -> None:
        """Close open client session."""
        if self.session and self._close_session:
            await self.session.close()

    async def __aenter__(self) -> Self:
        """Async enter.

        Returns
        -------
            The YouTube object.
        """
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        """Async exit.

        Args:
        ----
            _exc_info: Exec type.
        """
        await self.close()
