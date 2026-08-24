"""Polygon.io Flat Files S3 access tools."""
import os
import json
from pathlib import Path
from typing import Optional, Dict, Any, List
from datetime import datetime, date
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# Available prefixes (asset classes)
ASSET_CLASSES = {
    "us_stocks_sip": "US Stocks (SIP)",
    "us_options_opra": "US Options (OPRA)",
    "us_indices": "US Indices",
    "global_forex": "Global Forex",
    "global_crypto": "Global Crypto",
}

# Data types within each asset class
DATA_TYPES = {
    "trades_v1": "Trades",
    "quotes_v1": "Quotes",
    "minute_aggs_v1": "Minute Aggregates",
    "day_aggs_v1": "Day Aggregates",
}


def _require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"{name} not configured in environment")
    return value


def get_s3_bucket() -> str:
    """Return the configured flat files S3 bucket name."""
    return _require_env("POLYGON_S3_BUCKET")


def get_s3_client():
    """Initialize and return S3 client for Polygon flat files."""
    access_key = _require_env("POLYGON_S3_ACCESS_KEY")
    secret_key = _require_env("POLYGON_S3_SECRET_KEY")
    endpoint = _require_env("POLYGON_S3_ENDPOINT")

    session = boto3.Session(
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
    )

    return session.client(
        's3',
        endpoint_url=endpoint,
        config=Config(signature_version='s3v4'),
    )


def get_cache_dir() -> Path:
    """Get the cache directory path, creating it if necessary."""
    cache_dir = Path(os.environ.get("POLYGON_FLATFILES_CACHE_DIR", "/app/.cache/flatfiles"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def get_cached_file_path(s3_key: str) -> Path:
    """Get the local cache path for an S3 key."""
    cache_dir = get_cache_dir()
    # Replace slashes with underscores to create a flat cache structure
    safe_filename = s3_key.replace("/", "_")
    return cache_dir / safe_filename


def is_file_cached(s3_key: str) -> bool:
    """Check if a file is already cached locally."""
    cache_path = get_cached_file_path(s3_key)
    return cache_path.exists()


def get_cache_info(s3_key: str) -> Optional[Dict[str, Any]]:
    """Get information about a cached file."""
    cache_path = get_cached_file_path(s3_key)
    if not cache_path.exists():
        return None

    stat = cache_path.stat()
    return {
        "path": str(cache_path),
        "size_bytes": stat.st_size,
        "size_mb": round(stat.st_size / 1024 / 1024, 2),
        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
    }


def list_prefixes(prefix: str = "") -> List[str]:
    """List available prefixes (directories) in S3."""
    s3 = get_s3_client()
    bucket = get_s3_bucket()

    paginator = s3.get_paginator('list_objects_v2')
    prefixes = set()

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter='/'):
        if 'CommonPrefixes' in page:
            for cp in page['CommonPrefixes']:
                prefixes.add(cp['Prefix'])

    return sorted(list(prefixes))


def list_files(prefix: str, max_results: int = 100) -> List[Dict[str, Any]]:
    """List files in a specific prefix with caching info."""
    s3 = get_s3_client()
    bucket = get_s3_bucket()

    paginator = s3.get_paginator('list_objects_v2')
    files = []

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, PaginationConfig={'MaxItems': max_results}):
        if 'Contents' in page:
            for obj in page['Contents']:
                key = obj['Key']
                file_info = {
                    "key": key,
                    "size_bytes": obj['Size'],
                    "size_mb": round(obj['Size'] / 1024 / 1024, 2),
                    "last_modified": obj['LastModified'].isoformat(),
                    "cached": is_file_cached(key),
                }

                # Add cache info if file is cached
                if file_info['cached']:
                    cache_info = get_cache_info(key)
                    file_info['cache_path'] = cache_info['path']
                    file_info['cache_size_mb'] = cache_info['size_mb']

                files.append(file_info)

    return files


def download_file(s3_key: str, force: bool = False) -> Dict[str, Any]:
    """Download a flat file from S3 to cache, or return cached version."""
    cache_path = get_cached_file_path(s3_key)

    # Check if already cached
    if cache_path.exists() and not force:
        cache_info = get_cache_info(s3_key)
        return {
            "status": "cached",
            "message": "File already cached locally",
            "path": str(cache_path),
            "size_mb": cache_info['size_mb'],
        }

    # Download from S3
    s3 = get_s3_client()
    bucket = get_s3_bucket()

    try:
        # Get file size first
        response = s3.head_object(Bucket=bucket, Key=s3_key)
        file_size_mb = round(response['ContentLength'] / 1024 / 1024, 2)

        # Download file
        s3.download_file(bucket, s3_key, str(cache_path))

        return {
            "status": "downloaded",
            "message": f"Downloaded {file_size_mb}MB to cache",
            "path": str(cache_path),
            "size_mb": file_size_mb,
        }
    except ClientError as e:
        return {
            "status": "error",
            "message": f"Download failed: {str(e)}",
        }


def get_file_info(s3_key: str) -> Dict[str, Any]:
    """Get metadata about a specific S3 file."""
    s3 = get_s3_client()
    bucket = get_s3_bucket()

    try:
        response = s3.head_object(Bucket=bucket, Key=s3_key)

        info = {
            "key": s3_key,
            "size_bytes": response['ContentLength'],
            "size_mb": round(response['ContentLength'] / 1024 / 1024, 2),
            "last_modified": response['LastModified'].isoformat(),
            "content_type": response.get('ContentType', 'unknown'),
            "cached": is_file_cached(s3_key),
        }

        # Add cache info if cached
        if info['cached']:
            cache_info = get_cache_info(s3_key)
            info['cache_path'] = cache_info['path']
            info['cache_size_mb'] = cache_info['size_mb']

        return info
    except ClientError as e:
        return {
            "error": str(e),
            "key": s3_key,
        }


def list_available_dates(asset_class: str, data_type: str, year: Optional[int] = None) -> List[str]:
    """List available dates for a specific asset class and data type."""
    prefix = f"{asset_class}/{data_type}/"

    if year:
        prefix += f"{year}/"

    s3 = get_s3_client()
    bucket = get_s3_bucket()
    paginator = s3.get_paginator('list_objects_v2')

    dates = set()

    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        if 'Contents' in page:
            for obj in page['Contents']:
                key = obj['Key']
                # Extract date from filename (e.g., 2024-03-04.csv.gz)
                filename = key.split('/')[-1]
                if filename.endswith('.csv.gz'):
                    date_str = filename.replace('.csv.gz', '')
                    if len(date_str) == 10:  # YYYY-MM-DD format
                        dates.add(date_str)

    return sorted(list(dates))


def clear_cache(asset_class: Optional[str] = None) -> Dict[str, Any]:
    """Clear cached files, optionally filtered by asset class."""
    cache_dir = get_cache_dir()
    deleted_count = 0
    freed_bytes = 0

    for cache_file in cache_dir.iterdir():
        if cache_file.is_file():
            # If asset_class specified, only delete files from that class
            if asset_class and not cache_file.name.startswith(asset_class):
                continue

            freed_bytes += cache_file.stat().st_size
            cache_file.unlink()
            deleted_count += 1

    return {
        "deleted_files": deleted_count,
        "freed_mb": round(freed_bytes / 1024 / 1024, 2),
    }
