import gzip
import json
import time
from pathlib import Path

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from core.config import Config

DB_KEY = "db/coinwarden.sqlite.gz"


class R2Storage:
    def __init__(self, config: Config):
        self.bucket = config.r2_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=f"https://{config.r2_account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=config.r2_access_key_id,
            aws_secret_access_key=config.r2_secret_access_key,
            region_name="auto",
            config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"}),
        )

    def download_db(self, dest: Path) -> bool:
        t0 = time.monotonic()
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=DB_KEY)
        except ClientError as e:
            if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                print("db: no existing database in R2, starting fresh", flush=True)
                return False
            raise
        dest.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(obj["Body"], "rb") as src, open(dest, "wb") as out:
            while chunk := src.read(1 << 20):
                out.write(chunk)
        print(f"db: downloaded {dest.stat().st_size / 1e6:.1f} MB in {time.monotonic() - t0:.2f}s", flush=True)
        return True

    def upload_db(self, src: Path) -> None:
        t0 = time.monotonic()
        gz_path = src.with_suffix(src.suffix + ".gz")
        with open(src, "rb") as f_in, gzip.open(gz_path, "wb", compresslevel=6) as f_out:
            while chunk := f_in.read(1 << 20):
                f_out.write(chunk)
        self.client.upload_file(str(gz_path), self.bucket, DB_KEY)
        print(f"db: uploaded {gz_path.stat().st_size / 1e6:.1f} MB gz in {time.monotonic() - t0:.2f}s", flush=True)

    def put_json(self, key: str, payload, cache_seconds: int = 300) -> None:
        body = gzip.compress(json.dumps(payload, separators=(",", ":")).encode(), compresslevel=6)
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
            ContentEncoding="gzip",
            CacheControl=f"public, max-age={cache_seconds}",
        )


class LocalStorage:
    """Drop-in for R2Storage when no R2 credentials are present: keeps the DB
    where it is and writes the published JSON under data_dir/public."""

    def __init__(self, config: Config):
        self.root = config.data_dir / "public"

    def download_db(self, dest: Path) -> bool:
        return dest.exists()

    def upload_db(self, src: Path) -> None:
        print("db: local mode, nothing uploaded", flush=True)

    def put_json(self, key: str, payload, cache_seconds: int = 300) -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, separators=(",", ":")))


def get_storage(config: Config):
    if config.r2_configured:
        return R2Storage(config)
    print("storage: R2 not configured, using local mode", flush=True)
    return LocalStorage(config)
