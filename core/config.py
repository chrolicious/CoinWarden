import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    client_id = os.environ["BNET_CLIENT_ID"]
    client_secret = os.environ["BNET_CLIENT_SECRET"]
    region = os.environ.get("BNET_REGION", "eu")
    realm_slug = os.environ["WOW_REALM_SLUG"]
