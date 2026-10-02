import time

import boto3
import structlog
from botocore.config import Config
from botocore.exceptions import ClientError

from al1s.app.config import get_settings
from al1s.app.logging import configure_logging


def ensure_bucket() -> None:
    settings = get_settings()
    client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 0}),
    )
    try:
        client.head_bucket(Bucket=settings.s3_bucket)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code not in {"404", "NoSuchBucket", "NotFound"}:
            raise
        client.create_bucket(Bucket=settings.s3_bucket)
    finally:
        client.close()


def main() -> int:
    configure_logging()
    logger = structlog.get_logger()
    for attempt in range(1, 31):
        try:
            ensure_bucket()
            logger.info("s3_bucket_ready")
            return 0
        except Exception as exc:
            logger.warning("s3_bucket_retry", attempt=attempt, error=type(exc).__name__)
            if attempt == 30:
                raise
            time.sleep(min(2.0, attempt * 0.2))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
