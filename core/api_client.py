import time
import requests

from core.config import Config


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
        )
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expires_at = time.time() + payload["expires_in"] - 60
        return self._token

    def _get(self, path: str, namespace: str, extra_params: dict | None = None) -> dict:
        params = {"namespace": namespace, "locale": "en_US"}
        if extra_params:
            params.update(extra_params)

        resp = requests.get(
            f"https://{self.config.region}.api.blizzard.com{path}",
            params=params,
            headers={"Authorization": f"Bearer {self._get_token()}"},
        )
        resp.raise_for_status()
        return resp.json()

    def get_connected_realm_id(self, realm_slug: str) -> int:
        data = self._get(
            f"/data/wow/realm/{realm_slug}",
            namespace=f"dynamic-{self.config.region}",
        )
        return data["connected_realm"]["id"]

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
