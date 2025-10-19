"""Async helper for verifying Twitter engagement via the official API."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Optional

import httpx


logger = logging.getLogger(__name__)


class TwitterAPIError(RuntimeError):
    """Raised when the Twitter API request fails."""


@dataclass(slots=True)
class VerificationResult:
    supported: bool
    method: Optional[str]


class TwitterClient:
    """Small wrapper around the Twitter API v2 to verify tweet support."""

    def __init__(self, bearer_token: str, timeout: float = 10.0) -> None:
        if not bearer_token:
            raise ValueError("Twitter bearer token is required for verification")
        self._client = httpx.AsyncClient(
            base_url="https://api.twitter.com/2",
            headers={"Authorization": f"Bearer {bearer_token}"},
            timeout=timeout,
        )
        self._user_cache: Dict[str, str] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def verify_support(self, username: str, tweet_id: str) -> VerificationResult:
        """Check whether the given user liked or reposted a tweet."""

        user_id = await self._get_user_id(username)
        if await self._check_user_list(f"/tweets/{tweet_id}/liking_users", user_id):
            return VerificationResult(True, "like")
        if await self._check_user_list(f"/tweets/{tweet_id}/retweeted_by", user_id):
            return VerificationResult(True, "retweet")
        return VerificationResult(False, None)

    async def _get_user_id(self, username: str) -> str:
        normalised = username.lstrip("@").strip()
        if not normalised:
            raise TwitterAPIError("Укажи корректный ник в X/Twitter")
        cached = self._user_cache.get(normalised.lower())
        if cached:
            return cached

        data = await self._request(
            "GET",
            f"/users/by/username/{normalised}",
            params={"user.fields": "id"},
        )
        try:
            user_id = data["data"]["id"]
        except (KeyError, TypeError):
            raise TwitterAPIError(
                f"Не удалось найти пользователя @{normalised} в Twitter"
            ) from None

        self._user_cache[normalised.lower()] = user_id
        return user_id

    async def _check_user_list(self, endpoint: str, user_id: str) -> bool:
        params: Dict[str, str] = {"max_results": "100", "user.fields": "id"}
        next_token: Optional[str] = None

        while True:
            if next_token:
                params["pagination_token"] = next_token
            response = await self._request("GET", endpoint, params=params)
            users = response.get("data") or []
            for user in users:
                if user.get("id") == user_id:
                    return True

            meta = response.get("meta") or {}
            next_token = meta.get("next_token")
            if not next_token:
                break

        return False

    async def _request(
        self, method: str, endpoint: str, params: Optional[Dict[str, str]] = None
    ) -> Dict:
        try:
            response = await self._client.request(method, endpoint, params=params)
        except httpx.HTTPError as exc:
            raise TwitterAPIError("Не удалось подключиться к Twitter API") from exc

        if response.status_code == 429:
            raise TwitterAPIError(
                "Достигнут лимит запросов Twitter API. Попробуй повторить позже."
            )
        if response.status_code >= 400:
            logger.warning("Twitter API error %s: %s", response.status_code, response.text)
            raise TwitterAPIError("Twitter API вернул ошибку: %s" % response.status_code)

        data = response.json()
        if data.get("errors"):
            logger.warning("Twitter API returned errors: %s", data["errors"])
            raise TwitterAPIError("Twitter API вернул ошибку при проверке поддержки")
        return data


__all__ = ["TwitterClient", "TwitterAPIError", "VerificationResult"]
