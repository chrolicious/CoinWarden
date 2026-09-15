import re
import time
import requests

from core.config import Config

REQUEST_TIMEOUT = 30
MAX_RETRIES = 3


class BattleNetClient:
    def __init__(self, config: Config):
        self.config = config
        self._token = None
        self._token_expires_at = 0

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expires_at:
            return self._token

        resp = requests.post(
            f"https://{self.config.region}.battle.net/oauth/token",
            data={"grant_type": "client_credentials"},
            auth=(self.config.client_id, self.config.client_secret),
            timeout=REQUEST_TIMEOUT,
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

        for attempt in range(MAX_RETRIES):
            resp = requests.get(
                f"https://{self.config.region}.api.blizzard.com{path}",
                params=params,
                headers={"Authorization": f"Bearer {self._get_token()}"},
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code == 404:
                return None
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return resp.json()

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
