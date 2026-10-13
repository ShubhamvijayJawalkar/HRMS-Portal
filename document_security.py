"""Shared document upload validation and per-category size policy."""

from __future__ import annotations

import os

import clamav_scanner

MIB = 1024 * 1024
DEFAULT_CATEGORY_LIMIT_BYTES = 20 * MIB
DEFAULT_EXPENSE_RECEIPT_LIMIT_BYTES = 5 * MIB
DOCUMENT_CATEGORY_LIMITS = {
    'address proof': 10 * MIB,
    'bank details': 5 * MIB,
    'certification': 15 * MIB,
    'education': 15 * MIB,
    'id proof': 10 * MIB,
    'identity': 10 * MIB,
    'offer letter': 5 * MIB,
    'other': 20 * MIB,
    'payslip': 5 * MIB,
    'salary slip': 5 * MIB,
}
EXPENSE_RECEIPT_CATEGORY_LIMITS = {
    'travel': 10 * MIB,
    'food': 5 * MIB,
    'office supplies': 10 * MIB,
    'equipment': 20 * MIB,
    'utilities': 5 * MIB,
    'other': 5 * MIB,
}

_CONTENT_TYPES = {
    'pdf': ('application/pdf', b'%PDF-'),
    'jpg': ('image/jpeg', b'\xff\xd8\xff'),
    'jpeg': ('image/jpeg', b'\xff\xd8\xff'),
    'png': ('image/png', b'\x89PNG\r\n\x1a\n'),
}


class DocumentUploadError(Exception):
    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


def category_limit_bytes(category: str | None) -> int:
    key = (category or 'Other').strip().casefold()
    return DOCUMENT_CATEGORY_LIMITS.get(key, DEFAULT_CATEGORY_LIMIT_BYTES)


def expense_receipt_limit_bytes(category: str | None) -> int:
    key = (category or 'Other').strip().casefold()
    return EXPENSE_RECEIPT_CATEGORY_LIMITS.get(key, DEFAULT_EXPENSE_RECEIPT_LIMIT_BYTES)


def validate_document_upload(
    filename: str,
    category: str | None,
    content: bytes,
    declared_type: str | None = None,
    *,
    size_limit_bytes: int | None = None,
) -> str:
    """Validate extension, category cap, content signature, MIME, and malware."""
    extension = os.path.splitext(filename)[1].lstrip('.').lower()
    spec = _CONTENT_TYPES.get(extension)
    if not spec:
        raise DocumentUploadError('Only PDF, JPG, JPEG and PNG documents are accepted', 400)

    if not content:
        raise DocumentUploadError('Document is empty', 400)
    limit = category_limit_bytes(category) if size_limit_bytes is None else size_limit_bytes
    if len(content) > limit:
        raise DocumentUploadError('Document exceeds the size limit for its category', 413)

    content_type, signature = spec
    if not content.startswith(signature):
        raise DocumentUploadError('Document content does not match its extension', 400)
    if declared_type and declared_type.lower() not in (content_type, 'application/octet-stream'):
        raise DocumentUploadError('Document MIME type does not match its content', 400)
    if b'EICAR-STANDARD-ANTIVIRUS-TEST-FILE' in content:
        raise DocumentUploadError('Document failed the malware safety check', 400)

    try:
        clamav_scanner.scan_bytes(content)
    except clamav_scanner.MalwareDetected as exc:
        raise DocumentUploadError('Document failed the malware safety check', 400) from exc
    except clamav_scanner.ScannerError as exc:
        raise DocumentUploadError('Malware scanner unavailable; document was not stored', 503) from exc
    return content_type
