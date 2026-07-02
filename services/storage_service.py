"""
services/storage_service.py — image storage abstraction
=========================================================
Uploads civic issue photos to object storage and returns a public URL.
Falls back gracefully when no storage is configured — images remain
as base64 data-URLs (existing behavior, zero breakage).

Provider selection (checked at module import, in priority order):
  1. Cloudflare R2 (R2_ACCOUNT_ID + R2_ACCESS_KEY + R2_SECRET_KEY + R2_BUCKET_NAME)
  2. AWS S3        (AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY + S3_BUCKET_NAME)
  3. Local disk    (LOCAL_STORAGE_PATH set, or always available as last resort in dev)
  4. Passthrough   (no env vars — returns the data-URL unchanged, as before Phase 5)

Why Cloudflare R2:
  - Free 10GB storage + 10M Class B operations/month
  - S3-compatible API — boto3 works with R2 endpoint
  - No egress fees (unlike S3)
  - Serves images via public R2.dev subdomain with no extra CDN setup

Phase 5 — Storage Service + Object Storage.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import time
import uuid
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
#  PROVIDER DETECTION (evaluated once at import time)
# ─────────────────────────────────────────────────────────────────────────────

def _detect_provider() -> str:
    """Return 'r2' | 's3' | 'local' | 'passthrough'."""
    if (os.environ.get('R2_ACCOUNT_ID') and
        os.environ.get('R2_ACCESS_KEY') and
        os.environ.get('R2_SECRET_KEY') and
        os.environ.get('R2_BUCKET_NAME')):
        return 'r2'
    if (os.environ.get('AWS_ACCESS_KEY_ID') and
        os.environ.get('AWS_SECRET_ACCESS_KEY') and
        os.environ.get('S3_BUCKET_NAME')):
        return 's3'
    if os.environ.get('LOCAL_STORAGE_PATH') or os.environ.get('FLASK_DEBUG') == '1':
        return 'local'
    return 'passthrough'


_PROVIDER = _detect_provider()
print(f'[storage_service] provider={_PROVIDER}')


def is_configured() -> bool:
    """Return True if actual object storage is available (not passthrough)."""
    return _PROVIDER in ('r2', 's3', 'local')


def provider_name() -> str:
    return _PROVIDER


# ─────────────────────────────────────────────────────────────────────────────
#  PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

def upload_image(
    image_b64:  str,
    mime:       str   = 'image/jpeg',
    issue_id:   Optional[int] = None,
) -> str:
    """
    Upload an image and return a URL suitable for storing in the database.

    If storage is not configured (passthrough mode), returns the original
    data-URL unchanged — existing behavior, zero breakage.

    Args:
        image_b64: raw base64-encoded image bytes (no data: prefix)
        mime:      MIME type string e.g. 'image/jpeg'
        issue_id:  used to construct a deterministic filename (optional)

    Returns:
        str — either a https:// URL (r2/s3/local) or the original data-URL
    """
    if not image_b64:
        return ''

    if _PROVIDER == 'passthrough':
        # Return data-URL as before Phase 5 — no breakage
        return f'data:{mime};base64,{image_b64}'

    # Generate a stable filename based on content hash + timestamp
    content_hash = hashlib.sha256(image_b64.encode()).hexdigest()[:16]
    ext          = _mime_to_ext(mime)
    filename     = f'issues/{issue_id or "x"}_{content_hash}{ext}'

    try:
        if _PROVIDER == 'r2':
            return _upload_r2(image_b64, mime, filename)
        if _PROVIDER == 's3':
            return _upload_s3(image_b64, mime, filename)
        if _PROVIDER == 'local':
            return _upload_local(image_b64, mime, filename)
    except Exception as exc:
        print(f'[storage_service] upload failed ({_PROVIDER}): {exc} — falling back to data-URL')
        return f'data:{mime};base64,{image_b64}'

    return f'data:{mime};base64,{image_b64}'


def delete_image(url: str) -> bool:
    """
    Delete an image by URL.
    No-op for data-URLs (passthrough) and local dev files.
    Returns True if deletion succeeded or was skipped cleanly.
    """
    if not url or url.startswith('data:'):
        return True   # nothing to delete

    try:
        if _PROVIDER == 'r2':
            return _delete_r2(url)
        if _PROVIDER == 's3':
            return _delete_s3(url)
        if _PROVIDER == 'local':
            return _delete_local(url)
    except Exception as exc:
        print(f'[storage_service] delete failed: {exc}')
    return False


# ─────────────────────────────────────────────────────────────────────────────
#  CLOUDFLARE R2  (S3-compatible, boto3)
# ─────────────────────────────────────────────────────────────────────────────

def _get_r2_client():
    """Build a boto3 S3 client pointed at Cloudflare R2."""
    try:
        import boto3
    except ImportError:
        raise RuntimeError(
            'boto3 is required for R2 storage. '
            'Install it: pip install boto3==1.34.0'
        )
    account_id   = os.environ['R2_ACCOUNT_ID']
    access_key   = os.environ['R2_ACCESS_KEY']
    secret_key   = os.environ['R2_SECRET_KEY']
    endpoint_url = f'https://{account_id}.r2.cloudflarestorage.com'

    return boto3.client(
        's3',
        endpoint_url          = endpoint_url,
        aws_access_key_id     = access_key,
        aws_secret_access_key = secret_key,
        region_name           = 'auto',
    )


def _upload_r2(image_b64: str, mime: str, filename: str) -> str:
    client      = _get_r2_client()
    bucket      = os.environ['R2_BUCKET_NAME']
    public_base = os.environ.get('R2_PUBLIC_URL', '').rstrip('/')
    raw_bytes   = base64.b64decode(image_b64)

    client.put_object(
        Bucket      = bucket,
        Key         = filename,
        Body        = raw_bytes,
        ContentType = mime,
        CacheControl = 'public, max-age=31536000',   # 1 year — images are immutable
    )

    if public_base:
        return f'{public_base}/{filename}'
    # Fallback: construct R2.dev public URL
    account_id = os.environ['R2_ACCOUNT_ID']
    return f'https://pub-{account_id}.r2.dev/{filename}'


def _delete_r2(url: str) -> bool:
    # Extract key from URL — everything after the bucket/public domain
    client = _get_r2_client()
    bucket = os.environ['R2_BUCKET_NAME']
    # Key is the path component: issues/123_abc.jpg
    key = url.split('/', 3)[-1] if '/' in url else url
    client.delete_object(Bucket=bucket, Key=key)
    return True


# ─────────────────────────────────────────────────────────────────────────────
#  AWS S3
# ─────────────────────────────────────────────────────────────────────────────

def _get_s3_client():
    try:
        import boto3
    except ImportError:
        raise RuntimeError('boto3 is required for S3 storage: pip install boto3==1.34.0')
    return boto3.client(
        's3',
        aws_access_key_id     = os.environ['AWS_ACCESS_KEY_ID'],
        aws_secret_access_key = os.environ['AWS_SECRET_ACCESS_KEY'],
        region_name           = os.environ.get('AWS_REGION', 'ap-south-1'),
    )


def _upload_s3(image_b64: str, mime: str, filename: str) -> str:
    client    = _get_s3_client()
    bucket    = os.environ['S3_BUCKET_NAME']
    region    = os.environ.get('AWS_REGION', 'ap-south-1')
    raw_bytes = base64.b64decode(image_b64)

    client.put_object(
        Bucket       = bucket,
        Key          = filename,
        Body         = raw_bytes,
        ContentType  = mime,
        CacheControl = 'public, max-age=31536000',
        ACL          = 'public-read',
    )
    return f'https://{bucket}.s3.{region}.amazonaws.com/{filename}'


def _delete_s3(url: str) -> bool:
    client = _get_s3_client()
    bucket = os.environ['S3_BUCKET_NAME']
    key    = url.split('/', 3)[-1] if '/' in url else url
    client.delete_object(Bucket=bucket, Key=key)
    return True


# ─────────────────────────────────────────────────────────────────────────────
#  LOCAL FILESYSTEM  (dev fallback)
# ─────────────────────────────────────────────────────────────────────────────

def _local_storage_root() -> str:
    return os.environ.get('LOCAL_STORAGE_PATH', 'static/uploads')


def _upload_local(image_b64: str, mime: str, filename: str) -> str:
    root      = _local_storage_root()
    full_path = os.path.join(root, filename)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)

    raw_bytes = base64.b64decode(image_b64)
    with open(full_path, 'wb') as f:
        f.write(raw_bytes)

    # Return a relative URL — Flask will serve /static/uploads/...
    base_url = os.environ.get('AREAPULSE_URL', '').rstrip('/')
    return f'{base_url}/static/uploads/{filename}'


def _delete_local(url: str) -> bool:
    root     = _local_storage_root()
    # Extract relative path from URL
    marker   = '/static/uploads/'
    idx      = url.find(marker)
    if idx == -1:
        return False
    rel_path  = url[idx + len(marker):]
    full_path = os.path.join(root, rel_path)
    if os.path.exists(full_path):
        os.remove(full_path)
    return True


# ─────────────────────────────────────────────────────────────────────────────
#  UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

def _mime_to_ext(mime: str) -> str:
    return {
        'image/jpeg': '.jpg',
        'image/jpg':  '.jpg',
        'image/png':  '.png',
        'image/webp': '.webp',
        'image/heic': '.heic',
        'image/gif':  '.gif',
    }.get((mime or 'image/jpeg').lower(), '.jpg')


def is_object_url(image_field: str) -> bool:
    """
    Return True if the stored image field is an object storage URL
    (not a legacy base64 data-URL).
    Useful for conditional rendering — new rows have URLs, old rows have data-URLs.
    """
    if not image_field:
        return False
    return image_field.startswith('http://') or image_field.startswith('https://')
