"""Read public SSH CA material without OpenBao credentials."""
import asyncio
import base64
import hashlib
from urllib.parse import urlsplit

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.serialization import load_ssh_public_key
import httpx


class CAUnavailable(Exception):
    """The public CA cannot be safely delivered."""


class OpenBaoClient:
    def __init__(self, address, transport=None):
        parsed = urlsplit(address)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('OPENBAO_ADDR must be an HTTP(S) address without credentials')
        self.url = address.rstrip('/') + '/v1/ssh-client-signer/public_key'
        self.transport = transport

    async def public_ca(self):
        try:
            # A wall-clock budget covers the full request, including slow streams.
            async with asyncio.timeout(2):
                async with httpx.AsyncClient(transport=self.transport,
                                            timeout=httpx.Timeout(1, connect=0.5),
                                            trust_env=False) as client:
                    async with client.stream('GET', self.url) as response:
                        response.raise_for_status()
                        chunks = bytearray()
                        async for chunk in response.aiter_bytes():
                            chunks.extend(chunk)
                            if len(chunks) > 8192:
                                raise ValueError('Oversized CA response')
            key = bytes(chunks).decode('ascii').strip()
            if not key or '\n' in key or '\r' in key:
                raise ValueError('Expected a single SSH public key')
            load_ssh_public_key(key.encode('ascii'))
            wire = base64.b64decode(key.split()[1], validate=True)
            fingerprint = 'SHA256:' + base64.b64encode(hashlib.sha256(wire).digest()).decode().rstrip('=')
            return key, fingerprint
        except (httpx.HTTPError, TimeoutError, ValueError, UnicodeError, IndexError, UnsupportedAlgorithm) as error:
            raise CAUnavailable('SSH CA unavailable') from error
