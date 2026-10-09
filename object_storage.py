"""
Object storage abstraction for FR-PAY-07 (payslips) and FR-DOC-02 (documents).

Uses boto3 against an S3-compatible backend (MinIO for dev/CI, AWS S3 for prod).
Configuration via environment variables:
  S3_ENDPOINT_URL       - endpoint (e.g. http://localhost:9000 for MinIO, None for AWS)
  S3_ACCESS_KEY_ID      - access key
  S3_SECRET_ACCESS_KEY  - secret key
  S3_BUCKET             - bucket name (default: hrms)
  S3_REGION             - region (default: us-east-1)
  S3_USE_SSL            - use HTTPS for presigned URLs (default: true unless endpoint is localhost)
  S3_PRESIGNED_EXPIRY   - presigned URL TTL seconds (default: 3600 = 1 hour)
"""

from __future__ import annotations

import logging
import os
from typing import BinaryIO, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class ObjectStorageError(Exception):
    """Raised when object storage operations fail."""
    pass


class _ObjectStorage:
    """Singleton wrapper around boto3 S3 client."""

    def __init__(self):
        self._client = None
        self._bucket = None
        self._presigned_expiry = None
        self._use_ssl = None
        self._endpoint_url = None
        self._initialized = False

    def _init(self) -> None:
        """Lazy initialization of the S3 client."""
        if self._initialized:
            return

        endpoint_url = os.getenv('S3_ENDPOINT_URL')
        access_key = os.getenv('S3_ACCESS_KEY_ID')
        secret_key = os.getenv('S3_SECRET_ACCESS_KEY')
        bucket = os.getenv('S3_BUCKET', 'hrms')
        region = os.getenv('S3_REGION', 'us-east-1')
        presigned_expiry = int(os.getenv('S3_PRESIGNED_EXPIRY', '3600'))
        use_ssl_env = os.getenv('S3_USE_SSL')

        if not access_key or not secret_key:
            raise ObjectStorageError(
                "S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY must be set"
            )

        # Determine SSL and endpoint.
        # In test mode (with moto), don't use a custom endpoint_url - moto
        # intercepts standard AWS endpoints. Check for test indicators.
        in_test_mode = (
            os.getenv('FLASK_ENV') == 'test'
            or os.getenv('PYTEST_CURRENT_TEST') is not None
            or 'test' in os.getenv('_', '')
        )
        if in_test_mode:
            endpoint_url = None
            use_ssl = False
        else:
            # Production/dev with MinIO or AWS
            if use_ssl_env is not None:
                use_ssl = use_ssl_env.lower() == 'true'
            elif endpoint_url and ('localhost' in endpoint_url or '127.0.0.1' in endpoint_url):
                use_ssl = False
            else:
                use_ssl = True

        self._client = boto3.client(
            's3',
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            use_ssl=use_ssl,
        )
        self._bucket = bucket
        self._presigned_expiry = presigned_expiry
        self._use_ssl = use_ssl
        self._endpoint_url = endpoint_url
        self._initialized = True

        # Ensure bucket exists
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        """Create bucket if it doesn't exist."""
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError as exc:
            code = exc.response.get('Error', {}).get('Code')
            if code == '404':
                try:
                    if self._use_ssl or not self._endpoint_url:
                        self._client.create_bucket(Bucket=self._bucket)
                    else:
                        # MinIO needs LocationConstraint for non-us-east-1
                        region = os.getenv('S3_REGION', 'us-east-1')
                        if region == 'us-east-1':
                            self._client.create_bucket(Bucket=self._bucket)
                        else:
                            self._client.create_bucket(
                                Bucket=self._bucket,
                                CreateBucketConfiguration={'LocationConstraint': region}
                            )
                    logger.info("Created object storage bucket: %s", self._bucket)
                except ClientError as create_exc:
                    raise ObjectStorageError(
                        f"Failed to create bucket {self._bucket}: {create_exc}"
                    )
            elif code == '403':
                raise ObjectStorageError(f"Access denied to bucket {self._bucket}")
            else:
                raise ObjectStorageError(f"Bucket check failed: {exc}")

    def upload_bytes(
        self,
        key: str,
        data: bytes,
        content_type: str = 'application/octet-stream',
        metadata: Optional[dict] = None,
    ) -> str:
        """Upload bytes to object storage. Returns the object key."""
        self._init()
        try:
            extra_args = {'ContentType': content_type}
            if metadata:
                extra_args['Metadata'] = {k: str(v) for k, v in metadata.items()}
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                **extra_args,
            )
            logger.debug("Uploaded object: s3://%s/%s (%d bytes)", self._bucket, key, len(data))
            return key
        except ClientError as exc:
            raise ObjectStorageError(f"Upload failed: {exc}")

    def upload_fileobj(
        self,
        key: str,
        fileobj: BinaryIO,
        content_type: str = 'application/octet-stream',
        metadata: Optional[dict] = None,
    ) -> str:
        """Upload a file-like object to object storage. Returns the object key."""
        self._init()
        try:
            extra_args = {'ContentType': content_type}
            if metadata:
                extra_args['Metadata'] = {k: str(v) for k, v in metadata.items()}
            self._client.upload_fileobj(
                Fileobj=fileobj,
                Bucket=self._bucket,
                Key=key,
                ExtraArgs=extra_args,
            )
            logger.debug("Uploaded fileobj: s3://%s/%s", self._bucket, key)
            return key
        except ClientError as exc:
            raise ObjectStorageError(f"Upload failed: {exc}")

    def generate_presigned_url(
        self,
        key: str,
        expires_in: Optional[int] = None,
        response_content_disposition: Optional[str] = None,
    ) -> str:
        """Generate a presigned URL for GET access to an object."""
        self._init()
        params = {'Bucket': self._bucket, 'Key': key}
        if response_content_disposition:
            params['ResponseContentDisposition'] = response_content_disposition
        try:
            url = self._client.generate_presigned_url(
                'get_object',
                Params=params,
                ExpiresIn=expires_in or self._presigned_expiry,
            )
            logger.debug("Generated presigned URL for s3://%s/%s", self._bucket, key)
            return url
        except ClientError as exc:
            raise ObjectStorageError(f"Presigned URL generation failed: {exc}")

    def delete_object(self, key: str) -> bool:
        """Delete an object. Returns True if deleted, False if not found."""
        self._init()
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
            logger.debug("Deleted object: s3://%s/%s", self._bucket, key)
            return True
        except ClientError as exc:
            code = exc.response.get('Error', {}).get('Code')
            if code == 'NoSuchKey':
                return False
            raise ObjectStorageError(f"Delete failed: {exc}")

    def object_exists(self, key: str) -> bool:
        """Check if an object exists."""
        self._init()
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError as exc:
            code = exc.response.get('Error', {}).get('Code')
            if code == '404':
                return False
            raise ObjectStorageError(f"Head object failed: {exc}")

    def get_object_bytes(self, key: str) -> bytes:
        """Download an object as bytes."""
        self._init()
        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=key)
            return resp['Body'].read()
        except ClientError as exc:
            code = exc.response.get('Error', {}).get('Code')
            if code == 'NoSuchKey':
                raise ObjectStorageError(f"Object not found: {key}")
            raise ObjectStorageError(f"Download failed: {exc}")

    @property
    def bucket(self) -> str:
        self._init()
        return self._bucket

    @property
    def is_configured(self) -> bool:
        """Check if object storage is configured (credentials present)."""
        return bool(os.getenv('S3_ACCESS_KEY_ID') and os.getenv('S3_SECRET_ACCESS_KEY'))


# Module-level singleton
_storage = _ObjectStorage()


def get_storage() -> _ObjectStorage:
    """Get the object storage singleton."""
    return _storage


def upload_payslip(run_id: int, emp_id: str, pdf_bytes: bytes) -> str:
    """Upload a payslip PDF to object storage. Returns the object key."""
    key = f"payslips/{run_id}/{emp_id}.pdf"
    return _storage.upload_bytes(
        key,
        pdf_bytes,
        content_type='application/pdf',
        metadata={'run_id': str(run_id), 'emp_id': emp_id},
    )


def get_payslip_presigned_url(run_id: int, emp_id: str, expires_in: int = 3600) -> str:
    """Get a presigned URL for a payslip PDF."""
    key = f"payslips/{run_id}/{emp_id}.pdf"
    return _storage.generate_presigned_url(
        key,
        expires_in=expires_in,
        response_content_disposition=f'attachment; filename="payslip_{emp_id}_{run_id}.pdf"',
    )


def upload_document(emp_id: str, doc_id: int, filename: str, content: bytes, content_type: str) -> str:
    """Upload a document to object storage. Returns the object key."""
    key = f"documents/{emp_id}/{doc_id}/{filename}"
    return _storage.upload_bytes(
        key,
        content,
        content_type=content_type,
        metadata={'emp_id': emp_id, 'doc_id': str(doc_id), 'filename': filename},
    )


def get_document_presigned_url(emp_id: str, doc_id: int, filename: str, expires_in: int = 3600) -> str:
    """Get a presigned URL for a document."""
    key = f"documents/{emp_id}/{doc_id}/{filename}"
    return _storage.generate_presigned_url(
        key,
        expires_in=expires_in,
        response_content_disposition=f'attachment; filename="{filename}"',
    )


def delete_document_object(emp_id: str, doc_id: int, filename: str) -> bool:
    """Delete a document from object storage."""
    key = f"documents/{emp_id}/{doc_id}/{filename}"
    return _storage.delete_object(key)
