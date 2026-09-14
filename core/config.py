import os
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
    turso_database_url = os.environ["TURSO_DATABASE_URL"]
    turso_auth_token = os.environ["TURSO_AUTH_TOKEN"]
