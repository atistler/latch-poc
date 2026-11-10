"""Opt-in Warpgate control-plane contract smoke test; makes no SSH connections.

Run from repo root with the API dependencies installed:
  .state/latch-api-venv/bin/python tests/live_device_gateway.py

Creates and removes a unique temporary SSH target/user/role and test host pin.
No device or LDAP server is contacted. Existing VM targets are untouched.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
import lab
from latch_api.device_access import Device, LDAPLease, WarpgateGateway


def main():
    api = lab.gateway_api()
    gateway = WarpgateGateway(api)
    name = 'latch-device-' + uuid.uuid4().hex
    # RFC5737 TEST-NET-1 is deliberately unused: no connection is attempted.
    host, port = '192.0.2.254', 65022
    public_key = (lab.STATE / 'gateway_ed25519.pub').read_text().strip()
    device = Device(host, port, public_key)
    expires = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat().replace('+00:00', 'Z')
    before_pins = {pin['id'] for pin in api.request('GET', '/ssh/known-hosts')}
    try:
        gateway.provision(name, device,
                          LDAPLease('control-plane-only', 'dummy-ldap-user', 'dummy-control-plane-only', 120),
                          public_key, expires)
        target = next(t for t in api.request('GET', '/targets') if t['name'] == name)
        user = next(u for u in api.request('GET', '/users') if u['username'] == name)
        role = next(r for r in api.request('GET', '/roles') if r['name'] == name)
        assert target['options']['kind'] == 'Ssh'
        assert target['options']['auth']['kind'] == 'Password'
        assert target['options']['username'] == 'dummy-ldap-user'
        assert target['options']['host'] == host and target['options']['port'] == port
        assignment = next(r for r in api.request('GET', '/users/' + user['id'] + '/roles') if r['id'] == role['id'])
        actual_expiry = datetime.fromisoformat(assignment['expires_at'].replace('Z', '+00:00'))
        assert actual_expiry == datetime.fromisoformat(expires.replace('Z', '+00:00'))
        assert assignment['is_active'] and not assignment['is_expired']
        assert any(r['id'] == role['id'] for r in api.request('GET', '/targets/' + target['id'] + '/roles'))
        print('PASS: true SSH target, password auth, isolated user/role, expiring role assignment')
    finally:
        gateway.cleanup(name)
        for pin in api.request('GET', '/ssh/known-hosts'):
            if pin['id'] not in before_pins and pin['host'] == host and pin['port'] == port:
                api.request('DELETE', '/ssh/known-hosts/' + pin['id'])
    for path, field in (('/targets', 'name'), ('/users', 'username'), ('/roles', 'name')):
        assert all(obj[field] != name for obj in api.request('GET', path))
    print('PASS: temporary target/user/role removed; active VM configuration untouched')
    print('No SSH connections or LDAP operations were tested.')


if __name__ == '__main__':
    main()
