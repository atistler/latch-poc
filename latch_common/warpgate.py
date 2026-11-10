"""Warpgate admin API client shared by CLI and service processes."""
import http.cookiejar
import json
from pathlib import Path
import ssl
import urllib.error
import urllib.request


class Api:
    """Cookie authenticated API with the gateway's locally exported TLS pin."""
    def __init__(self, address, state, *, control_plane_host=None):
        self.address = address.rstrip('/')
        self.control_plane_host = control_plane_host
        state = Path(state)
        self.password_file = state / 'warpgate/admin-password'
        context = ssl.create_default_context(cafile=str(state / 'warpgate/tls.certificate.pem'))
        # Setup's SAN is localhost; forwarders may expose it as 127.0.0.1.
        # The exact self-signed certificate remains the only trusted issuer.
        context.check_hostname = False
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.request('POST', '/auth/login', {
            'username': 'admin',
            'password': self.password_file.read_text().strip(),
        }, admin=False)

    def request(self, method, path, data=None, *, admin=True, raw=False, _retry=True):
        prefix = '/@warpgate/admin/api' if admin else '/@warpgate/api'
        body = None if data is None else json.dumps(data).encode()
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}
        if getattr(self, 'control_plane_host', None):
            # Service DNS locates the connection; Warpgate selects its admin
            # context using the configured control-plane HTTP host.
            headers['Host'] = self.control_plane_host
        request = urllib.request.Request(self.address + prefix + path, data=body,
            method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=30) as response:
                content = response.read()
        except urllib.error.HTTPError as error:
            if error.code == 401 and admin and _retry:
                self.request('POST', '/auth/login', {'username': 'admin',
                    'password': self.password_file.read_text().strip()}, admin=False)
                return self.request(method, path, data, admin=admin, raw=raw, _retry=False)
            # Server responses may contain secrets; report only the operation/status.
            raise RuntimeError(f'Warpgate {method} {path}: HTTP {error.code}') from None
        return content if raw else (json.loads(content) if content else None)
