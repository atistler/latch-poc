"""Opt-in live acceptance for Latch-managed OpenStack Server grants.

Run after ./lab provision:
  .state/latch-api-venv/bin/python tests/live_server_gateway.py

Requires no existing Server grants, so the last-grant network assertion cannot
interrupt another user's session. Creates only its own grants and cleans them
in finally. Uses actual certificate-backed SSH and decoded session recordings.
No API token, OpenStack credential or private key is printed.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import lab
import server_integration as servers
import warpgate


class Harness:
    def __init__(self):
        self.options = lab.settings()
        self.api = lab.gateway_api()
        self.grants = []
        self.processes = []
        self.checks = []
        self.recordings = []
        self.probe_history_cleanup = []
        self.api_stopped = False
        inventory = json.loads((lab.STATE / 'vm/inventory.json').read_text())
        if len(inventory.get('servers', inventory.get('vms', {}))) != 1:
            raise RuntimeError('The VM acceptance test requires the single-VM lab inventory')
        self.instance = next(iter(inventory.get('servers', inventory.get('vms', {}))))

    def api_status(self, payload, authentication='valid'):
        # Read the actual bearer only in the container's memory. Return HTTP
        # status and any unexpectedly-created grant ID: no credentials escape.
        script = '''import json,pathlib,sys,urllib.error,urllib.request
p=json.load(sys.stdin)
headers={'Content-Type':'application/json'}
if p['authentication']=='valid':
 headers['Authorization']='Bearer '+pathlib.Path('/run/server-api/api-token').read_text().strip()
elif p['authentication']=='invalid':
 headers['Authorization']='Bearer deliberately-invalid-acceptance-test-token'
request=urllib.request.Request('http://127.0.0.1:8320/v1/server-grants',method='POST',headers=headers,data=json.dumps(p['payload']).encode())
try:
 with urllib.request.urlopen(request,timeout=10) as response:
  status=response.status
  grant_id=json.load(response).get('grant_id') if status==201 else None
except urllib.error.HTTPError as error: status=error.code
print(json.dumps({'status':status,'grant_id':grant_id if status==201 else None}))
'''
        result = subprocess.run(servers.compose(['exec', '-T', '--user', '10001:10001', 'server-api',
                                           'python', '-c', script], self.options),
                                input=json.dumps({'payload': payload, 'authentication': authentication}),
                                capture_output=True, text=True, check=True, timeout=30)
        response = json.loads(result.stdout)
        if response.get('grant_id'):
            self.grants.append({'grant_id': response['grant_id']})
        return response['status']

    def rejection_checks(self):
        payload = {'server_id': self.instance,
                   'public_key': (lab.STATE / 'gateway_ed25519.pub').read_text().strip(),
                   'ttl_seconds': 30}
        cases = [('missing bearer', payload, 'missing', 401),
                 ('incorrect bearer', payload, 'invalid', 401),
                 ('unauthorized inventory', {**payload, 'server_id': str(uuid.uuid4())}, 'valid', 403),
                 ('caller destination override', {**payload, 'host': '127.0.0.1'}, 'valid', 422),
                 ('excessive lifetime', {**payload, 'ttl_seconds': 301}, 'valid', 422)]
        before = self.inspect()
        for label, body, authentication, expected in cases:
            if self.api_status(body, authentication) != expected:
                raise RuntimeError('Server API did not reject ' + label + ' with HTTP ' + str(expected))
        if self.inspect() != before:
            raise RuntimeError('Rejected Server API requests changed grant or forwarding state')
        self.checks.append('live API rejects missing/wrong credentials, unauthorized inventory and destination/TTL overrides')
        print('PASS: API authentication, inventory authorization and request limits reject unsafe requests')

    def service(self, operation):
        subprocess.run(servers.compose([operation, 'server-api'], self.options),
                       capture_output=True, check=True, timeout=60)
        self.api_stopped = operation == 'stop'

    def ready(self, grant):
        deadline = time.monotonic() + 45
        while True:
            try:
                return servers.request(self.options, 'GET', '/v1/server-grants/' + grant['grant_id'])
            except (subprocess.CalledProcessError, RuntimeError):
                if time.monotonic() >= deadline:
                    raise RuntimeError('Server API did not recover after its restart') from None
                time.sleep(.5)

    def inspect(self):
        # Fetch state through the trusted Lima channel. The service credential
        # stays inside the API container; the test sees only counts and IDs.
        script = '''import json,pathlib,sqlite3,sys
from latch_api.openstack import OpenStackNetwork
from latch_api.server_access import Server
instance=json.load(sys.stdin)['server_id']
directory=pathlib.Path('/run/server-api')
inventory=json.loads((directory/'inventory.json').read_text())
machine=Server(server_id=instance,**inventory.get('servers', inventory.get('vms', {}))[instance])
network=OpenStackNetwork(json.loads((directory/'openstack.json').read_text()))
network._project(machine.project_id)
rules=network._request('GET','network',network._forwarding_path(machine))['port_forwardings']
with sqlite3.connect('/data/grants.sqlite') as db:
 count=db.execute("SELECT COUNT(*) FROM server_grants WHERE state<>'revoked' AND server_id=?",(instance,)).fetchone()[0]
owned=[r['id'] for r in rules if r.get('description')==network._marker(machine)]
print(json.dumps({'owned_forwarding_ids':owned,'nonrevoked_grants':count}))
'''
        result = subprocess.run(servers.compose(['exec', '-T', '--user', '10001:10001', 'server-api',
                                           'python', '-c', script], self.options),
                                input=json.dumps({'server_id': self.instance}),
                                capture_output=True, text=True, check=True, timeout=60)
        return json.loads(result.stdout)

    def issue(self, ttl=180):
        grant = servers.issue(lab.STATE, self.options, self.instance, ttl_seconds=ttl)
        if grant['state'] != 'active':
            raise RuntimeError('Server API did not return an active grant')
        self.grants.append(grant)
        return grant

    def revoke(self, grant):
        result = servers.request(self.options, 'DELETE', '/v1/server-grants/' + grant['grant_id'])
        if result['state'] != 'revoked':
            raise RuntimeError('Server grant revocation remains incomplete: ' + result['state'])
        return result

    def open(self, grant):
        # Neutron commits its API rule before the L3 agent applies the dataplane.
        # Probe from the authorized gateway source before starting the SSH test.
        inventory = json.loads((lab.STATE / 'vm/inventory.json').read_text())
        server = inventory.get('servers', inventory.get('vms', {}))[grant['server_id']]
        deadline = time.monotonic() + 15
        while True:
            result = lab.worker('nc', '-z', '-w', '1', server['host'], str(server['port']),
                                check=False, capture_output=True, text=True)
            if result.returncode == 0:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('Neutron forwarding did not become reachable from the gateway')
            time.sleep(.5)
        marker = 'LATCH_VM_' + uuid.uuid4().hex
        command = ('printf "%s\\n" ' + marker + '; printf "SOURCE=%s\\n" "$SSH_CONNECTION"; '
                   'while :; do sleep 1; done')
        process = subprocess.Popen(servers.ssh_command(lab.STATE, self.options, grant, command, tty=True),
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT)
        self.processes.append(process)
        output = bytearray()
        deadline = time.monotonic() + 45
        while marker.encode() not in output or b'SOURCE=100.64.10.10 ' not in output:
            if process.poll() is not None:
                raise RuntimeError('Server SSH exited before execution evidence: ' + output.decode(errors='replace')[-2000:])
            if time.monotonic() >= deadline:
                raise RuntimeError('Timed out waiting for certificate-backed VM SSH output')
            ready, _, _ = select.select([process.stdout], [], [], .5)
            if ready:
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError('Server SSH closed before execution evidence: ' + output.decode(errors='replace')[-2000:])
                output.extend(chunk)
        return process, marker

    @staticmethod
    def closed(process, timeout=20):
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError('Revoked VM SSH session remained connected') from None

    def sessions(self, grant):
        from urllib.parse import urlencode
        return self.api.request('GET', '/sessions?' + urlencode({
            'username': grant['ssh_username'].split(':', 1)[0],
            'active_only': 'true', 'limit': 100}))['items']

    def recording(self, grant, marker):
        name = grant['ssh_username'].split(':', 1)[0]
        deadline = time.monotonic() + 20
        while True:
            try:
                evidence = warpgate.verify_recording(self.api, marker, username=name, target_name=name)
                self.recordings.append({'grant_id': grant['grant_id'], **evidence})
                return
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.5)

    def login_protection(self, method, source=None):
        script = '''import json,pathlib,sys
from latch_common.warpgate import Api
p=json.load(sys.stdin)
api=Api('https://warpgate:8888',pathlib.Path('/run/server-api'),control_plane_host='localhost')
print(json.dumps(api.request(p['method'],'/login-protection/blocked-ips',{'ip':p['source']} if p['source'] else None)))
'''
        result = subprocess.run(servers.compose(['exec', '-T', '--user', '10001:10001', 'server-api',
                                           'python', '-c', script], self.options),
                                input=json.dumps({'method': method, 'source': source}),
                                capture_output=True, text=True, check=True, timeout=60)
        return json.loads(result.stdout)

    def denied(self, grant):
        # Reject a preexisting ban as evidence: it could hide incorrect grant
        # admission. Clear only this deliberately-negative test's source history.
        source = '10.1.1.254'
        blocked = self.login_protection('GET')
        if any(item['ip_address'] in (source, '::ffff:' + source) for item in blocked):
            raise RuntimeError('Gateway test source was already blocked; grant denial cannot be verified')
        marker = 'LATCH_UNEXPECTED_ADMISSION_' + uuid.uuid4().hex
        try:
            result = subprocess.run(servers.ssh_command(lab.STATE, self.options, grant,
                                                   'printf "%s\\n" ' + marker),
                                    stdin=subprocess.DEVNULL, capture_output=True, timeout=45)
            if result.returncode == 0 or marker.encode() in result.stdout:
                raise RuntimeError('Revoked Server grant unexpectedly admitted an SSH connection')
        finally:
            self.login_protection('DELETE', source)
            self.probe_history_cleanup.append({'grant_id': grant['grant_id'], 'source': source})

    def reconnect(self, grant, *, tty=False, label='surviving grant'):
        marker = 'LATCH_RECONNECT_' + uuid.uuid4().hex
        result = subprocess.run(servers.ssh_command(lab.STATE, self.options, grant,
                                'printf "%s\\n" ' + marker, tty=tty),
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=45)
        if result.returncode or marker.encode() not in result.stdout:
            # Only our fixed marker command runs. SSH diagnostics contain no
            # bearer, private key material or injected certificate credentials.
            diagnostic = result.stderr.decode(errors='replace')[-2000:]
            expires = datetime.fromisoformat(grant['expires_at'].replace('Z', '+00:00')).timestamp()
            remaining = round(expires - time.time(), 1)
            raise RuntimeError(f'{label} reconnect failed (exit {result.returncode}, '
                               f'PTY={tty}, grant_seconds_remaining={remaining}): {diagnostic!r}')

    def diagnose_reconnect(self):
        initial = self.inspect()
        if initial['nonrevoked_grants'] or initial['owned_forwarding_ids']:
            raise RuntimeError('Revoke existing Server grants before reconnect diagnostics')
        first, second = self.issue(), self.issue()
        self.open(first)
        self.open(second)
        for tty in (False, True):
            self.reconnect(second, tty=tty, label='before restart/revocation')
            print('PASS: initial reconnect PTY=' + str(tty))
        self.service('restart')
        self.ready(second)
        for tty in (False, True):
            self.reconnect(second, tty=tty, label='after API restart')
            print('PASS: reconnect after API restart PTY=' + str(tty))
        self.revoke(first)
        for tty in (False, True):
            self.reconnect(second, tty=tty, label='after other grant revocation')
            print('PASS: reconnect after other grant revocation PTY=' + str(tty))

    def run(self):
        initial = self.inspect()
        if initial['nonrevoked_grants'] or initial['owned_forwarding_ids']:
            raise RuntimeError('Close/revoke existing Server grants before the live acceptance test')
        self.rejection_checks()
        first, second = self.issue(), self.issue()
        shared = self.inspect()
        if len(shared['owned_forwarding_ids']) != 1 or shared['nonrevoked_grants'] != 2:
            raise RuntimeError('Concurrent grants did not share exactly one owned Neutron forwarding')
        self.checks.append('two independent grants share one owned Neutron forwarding')
        first_session, first_marker = self.open(first)
        second_session, second_marker = self.open(second)
        self.checks.append('both grants execute SSH through gateway provider source 100.64.10.10')
        print('PASS: two certificate-backed VM sessions share one Neutron forwarding')

        self.service('restart')
        if any(self.ready(grant)['state'] != 'active' for grant in (first, second)):
            raise RuntimeError('API restart lost an active durable grant')
        if first_session.poll() is not None or second_session.poll() is not None or self.inspect() != shared:
            raise RuntimeError('API restart disrupted an existing session or shared forwarding')
        self.checks.append('API restart preserves both durable grants, live sessions and shared forwarding')
        print('PASS: API restart preserves durable grants and established SSH sessions')

        self.revoke(first)
        self.closed(first_session)
        if self.sessions(first):
            raise RuntimeError('Warpgate reports an active revoked VM session')
        if second_session.poll() is not None:
            raise RuntimeError('Revoking the first grant disconnected the second grant')
        after_first = self.inspect()
        if after_first['owned_forwarding_ids'] != shared['owned_forwarding_ids']:
            raise RuntimeError('Revoking one grant altered the other grant network lease')
        self.denied(first)
        self.reconnect(second)
        self.recording(first, first_marker)
        self.checks.append('revoking first grant closes only its sessions and denies reconnect')
        self.checks.append('second grant retains its session and admits a new connection')
        print('PASS: independent revocation closes the first grant and preserves the second')

        self.revoke(second)
        self.closed(second_session)
        if self.inspect()['owned_forwarding_ids']:
            raise RuntimeError('Last grant revocation left its Neutron forwarding open')
        self.recording(second, second_marker)
        self.checks.append('last-grant revocation removes owned Neutron forwarding')
        self.checks.append('decoded SSH recordings contain real output from both grants')
        print('PASS: final revocation removes the network path; both sessions have replayable recordings')

        self.expiry()
        self.expiry(api_downtime=True)

    def expiry(self, *, api_downtime=False):
        expiring = self.issue(ttl=30)
        process, marker = self.open(expiring)
        expires = datetime.fromisoformat(expiring['expires_at'].replace('Z', '+00:00')).timestamp()
        if api_downtime:
            self.service('stop')
        while time.time() < expires - 1:
            if process.poll() is not None:
                raise RuntimeError('VM SSH session closed before its grant expired')
            time.sleep(.25)
        if api_downtime:
            while time.time() <= expires + 1:
                time.sleep(.25)
            self.service('start')
            self.ready(expiring)
        self.closed(process, timeout=20)
        deadline = time.monotonic() + 20
        while True:
            status = servers.request(self.options, 'GET', '/v1/server-grants/' + expiring['grant_id'])
            if status['state'] == 'revoked':
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('Server grant expiry cleanup did not complete')
            time.sleep(.5)
        if self.sessions(expiring) or self.inspect()['owned_forwarding_ids']:
            raise RuntimeError('Server grant expiry left a live session or forwarding')
        self.denied(expiring)
        self.recording(expiring, marker)
        self.checks.append('timed expiry closes active SSH, removes forwarding and denies reconnect')
        if api_downtime:
            self.checks.append('startup reconciliation removes an expired durable lease after API downtime')
            print('PASS: startup reconciles expiry during API downtime and closes SSH/network access')
        else:
            print('PASS: periodic expiry closes active SSH, removes forwarding and denies reconnect')

    def cleanup(self):
        failed = False
        if self.api_stopped:
            self.service('start')
            if self.grants:
                self.ready(self.grants[-1])
        for grant in self.grants:
            try:
                self.revoke(grant)
            except Exception:
                failed = True
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
        if failed:
            raise RuntimeError('VM acceptance cleanup incomplete; inspect the test grant IDs')


def main():
    sys.stdout.reconfigure(line_buffering=True)
    report = lab.STATE / 'server-verification.json'
    report.unlink(missing_ok=True)
    harness = Harness()
    try:
        if sys.argv[1:] == ['--diagnose-reconnect']:
            harness.diagnose_reconnect()
        elif sys.argv[1:]:
            raise RuntimeError('Unknown live-test argument')
        else:
            harness.run()
    finally:
        harness.cleanup()
    if sys.argv[1:]:
        return
    report.write_text(json.dumps({'tested_at': datetime.now(timezone.utc).isoformat(),
                                 'passed': True, 'checks': harness.checks,
                                 'recordings': harness.recordings,
                                 'test_login_protection_cleanup': harness.probe_history_cleanup}, indent=2) + '\n')
    report.chmod(0o600)
    print('Evidence: ' + str(report))


if __name__ == '__main__':
    main()
