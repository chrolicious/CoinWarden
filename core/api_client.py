import re
import time
import requests

from core.config import Config

REQUEST_TIMEOUT = 30
MAX_RETRIES = 6
BACKOFF_CAP_SECONDS = 30


def _retry_delay(attempt: int, resp: requests.Response) -> float:
    """Exponential backoff (1, 2, 4, 8, 16, 30 capped), but Retry-After wins
    when the server sends one - Blizzard's 429s usually do."""
    retry_after = resp.headers.get("Retry-After")
    if retry_after is not None:
        try:
            return max(float(retry_after), 0)
        except ValueError:
            pass
    return min(2 ** attempt, BACKOFF_CAP_SECONDS)


def _request_with_retry(method: str, url: str, **kwargs) -> requests.Response:
    """Shared retry path for every outbound call: retries 429 and 5xx (both
    Battle.net's API and its OAuth endpoint return these transiently) with
    backoff, logs each retry so a run that recovers still leaves a trace of
    what it recovered from, and raises on the final attempt's response."""
    resp = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as e:
            if attempt == MAX_RETRIES - 1:
                raise
            delay = min(2 ** attempt, BACKOFF_CAP_SECONDS)
            print(f"  {method} {url} failed ({e.__class__.__name__}), "
                  f"retry {attempt + 1}/{MAX_RETRIES} in {delay:.0f}s", flush=True)
            time.sleep(delay)
            continue

        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == MAX_RETRIES - 1:
                break
            delay = _retry_delay(attempt, resp)
            print(f"  {method} {url} -> {resp.status_code}, "
                  f"retry {attempt + 1}/{MAX_RETRIES} in {delay:.0f}s", flush=True)
            time.sleep(delay)
            continue
        return resp

    return resp


class BattleNetClient:
    def __init__(self, config: Config):
        self.config = config
        self._token = None
        self._token_expires_at = 0

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires_at:
            return self._token

        resp = _request_with_retry(
            "POST",
            f"https://{self.config.region}.battle.net/oauth/token",
            data={"grant_type": "client_credentials"},
            auth=(self.config.client_id, self.config.client_secret),
        )
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expires_at = time.time() + payload["expires_in"] - 60
        return self._token

    def _get(self, path: str, namespace: str, extra_params: dict | None = None) -> dict | None:
        """Returns None on 404 (item removed from game), raises on other failures."""
        params = {"namespace": namespace, "locale": "en_US"}
        if extra_params:
            params.update(extra_params)

        resp = _request_with_retry(
            "GET",
            f"https://{self.config.region}.api.blizzard.com{path}",
            params=params,
            headers={"Authorization": f"Bearer {self._get_token()}"},
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def get_connected_realm_id(self, realm_slug: str) -> int:
        data = self._get(
            f"/data/wow/realm/{realm_slug}",
            namespace=f"dynamic-{self.config.region}",
        )
        href = data["connected_realm"]["href"]
        match = re.search(r"/connected-realm/(\d+)", href)
        return int(match.group(1))

    def get_auctions_for_connected_realm(self, connected_realm_id: int) -> dict:
        """Non-commodity items (armor, weapons, mounts, pets, recipes) — connected-realm locked."""
        return self._get(
            f"/data/wow/connected-realm/{connected_realm_id}/auctions",
            namespace=f"dynamic-{self.config.region}",
        )

    def get_commodities(self) -> dict:
        """Stackable commodities — region-wide, not connected-realm locked."""
        return self._get(
            "/data/wow/auctions/commodities",
            namespace=f"dynamic-{self.config.region}",
        )

    def get_item(self, item_id: int) -> dict | None:
        return self._get(
            f"/data/wow/item/{item_id}",
            namespace=f"static-{self.config.region}",
        )

    def get_item_icon_url(self, item_id: int) -> str | None:
        data = self._get(
            f"/data/wow/media/item/{item_id}",
            namespace=f"static-{self.config.region}",
        )
        if not data:
            return None
        for asset in data.get("assets", []):
            if asset.get("key") == "icon":
                return asset.get("value")
        return None
