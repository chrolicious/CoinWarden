import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


class Config:
    client_id = os.environ["BNET_CLIENT_ID"]
    client_secret = os.environ["BNET_CLIENT_SECRET"]
    region = os.environ.get("BNET_REGION", "eu")
    realm_slugs = [
        slug.strip()
        for slug in os.environ["WOW_REALM_SLUGS"].split(",")
        if slug.strip()
    ]

    r2_account_id = os.environ.get("R2_ACCOUNT_ID")
    r2_access_key_id = os.environ.get("R2_ACCESS_KEY_ID")
    r2_secret_access_key = os.environ.get("R2_SECRET_ACCESS_KEY")
    r2_bucket = os.environ.get("R2_BUCKET", "coinwarden")

    data_dir = Path(os.environ.get("COINWARDEN_DATA_DIR", "data"))
    hourly_retention_days = int(os.environ.get("COINWARDEN_HOURLY_RETENTION_DAYS", "3"))
    daily_retention_days = int(os.environ.get("COINWARDEN_DAILY_RETENTION_DAYS", "60"))

    @property
    def r2_configured(self) -> bool:
        return bool(self.r2_account_id and self.r2_access_key_id and self.r2_secret_access_key)
