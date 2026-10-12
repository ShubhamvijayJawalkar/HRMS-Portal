"""ClamAV INSTREAM client for scanning uploaded documents before persistence."""

from __future__ import annotations

import os
import socket
import struct


class ScannerError(Exception):
    """The malware scanner is missing, unreachable, or returned an error."""


class MalwareDetected(Exception):
    """ClamAV identified malware in an uploaded document."""


def scan_bytes(content: bytes) -> None:
    """Scan bytes with a configured clamd daemon; refuse if it cannot be used."""
    host = os.getenv('CLAMD_HOST')
    if not host:
        raise ScannerError('CLAMD_HOST is not configured')

    try:
        port = int(os.getenv('CLAMD_PORT', '3310'))
        timeout = float(os.getenv('CLAMD_TIMEOUT_SECONDS', '15'))
        with socket.create_connection((host, port), timeout=timeout) as connection:
            connection.settimeout(timeout)
            connection.sendall(b'zINSTREAM\0')
            for offset in range(0, len(content), 64 * 1024):
                chunk = content[offset:offset + 64 * 1024]
                connection.sendall(struct.pack('!I', len(chunk)) + chunk)
            connection.sendall(struct.pack('!I', 0))
            response = bytearray()
            while len(response) < 4096:
                part = connection.recv(1024)
                if not part:
                    break
                response.extend(part)
                if b'\0' in part or b'\n' in part:
                    break
    except (OSError, ValueError, TimeoutError) as exc:
        raise ScannerError('ClamAV scan could not be completed') from exc

    result = bytes(response).decode('utf-8', errors='replace').strip('\0\r\n ')
    if result.endswith(' FOUND'):
        raise MalwareDetected(result.rsplit(': ', 1)[-1].removesuffix(' FOUND'))
    if result.endswith(' OK'):
        return
    raise ScannerError('ClamAV returned an invalid scan response')
