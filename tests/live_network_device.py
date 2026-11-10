"""Opt-in real network AAA checks. Run ./lab network-verify after network-up.

Exercises Warpgate SSH password injection into FreeRtr, LDAP/TACACS command
policy, revoked identities, short grants and active-session expiry. Temporarily
stops optional LDAP/TACACS services to prove authentication fails closed, and
restarts them in finally blocks. No passwords or bearer tokens are printed.
"""
import base64
import json
import os
import re
import select
from pathlib import Path
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import device_integration as device
import lab
import network_integration as network


def forwarding_nat_rules(rules):
    """Ignore only Docker's loopback DNS translation, never forwarded NAT."""
    docker_dns = re.compile(
        r'-A DOCKER_(?:OUTPUT -d 127\.0\.0\.11/32 -p (?:tcp|udp) '
        r'-m (?:tcp|udp) --dport 53 -j DNAT --to-destination 127\.0\.0\.11:\d+'
        r'|POSTROUTING -s 127\.0\.0\.11/32 -p (?:tcp|udp) '
        r'-m (?:tcp|udp) --sport \d+ -j SNAT --to-source :53)')
    return [line for line in rules.splitlines()
            if any(target in line for target in ('-j MASQUERADE', '-j SNAT', '-j DNAT'))
            and not docker_dns.fullmatch(line)]


class Harness:
    def __init__(self):
        self.options = lab.settings()
        self.api = lab.gateway_api()
        self.grants = []
        self.evidence = []
        self.checks = []
        self.login_protection_cleanup = []
        self.public_key = (lab.STATE / 'gateway_ed25519.pub').read_text().strip()

    def issue(self, role='readonly', ttl=180):
        grant = device.request(self.options, 'POST', '/v1/device-grants', {
            'device_id': 'freertr', 'role': role, 'public_key': self.public_key, 'ttl_seconds': ttl})
        self.grants.append(grant)
        return grant

    def revoke(self, grant):
        return device.request(self.options, 'DELETE', '/v1/device-grants/' + grant['grant_id'])

    def command(self, grant):
        # The gateway key is already trusted by the existing foundation; never
        # accept a new host key merely to make this validation pass.
        known = str(lab.STATE / 'warpgate_known_hosts').replace('"', '\\"')
        return ['ssh', '-tt', '-p', str(self.options.get('LAB_WORKER_PORT', '9202')),
                '-i', str(lab.STATE / 'gateway_ed25519'), '-o', 'IdentityAgent=none',
                '-o', 'IdentitiesOnly=yes', '-o', 'PasswordAuthentication=no',
                '-o', 'KbdInteractiveAuthentication=no', '-o', 'BatchMode=yes',
                '-o', 'ConnectTimeout=15', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'HostKeyAlias=latch-warpgate', '-o', f'UserKnownHostsFile="{known}"',
                grant['ssh_username'] + '@127.0.0.1']

    def shell(self, grant, commands, timeout=40):
        # Keep stdin open while the remote shell runs. communicate(input=...)
        # sends EOF immediately, which can close the gateway before the target
        # finishes opening its interactive channel.
        process = subprocess.Popen(self.command(grant), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        output = ''
        deadline = time.monotonic() + timeout
        try:
            output += self.read_prompt(process, deadline)
            for command in commands:
                if process.poll() is not None:
                    break
                self.send(process, command)
                output += self.read_prompt(process, deadline)
            if process.poll() is None:
                process.stdin.close()
                process.stdin = None
            tail, _ = process.communicate(timeout=max(.1, deadline - time.monotonic()))
            return process.returncode, output + tail
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=10)

    @staticmethod
    def read_prompt(process, deadline):
        output = bytearray()
        while True:
            rendered = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', output.decode(errors='replace'))
            if re.search(r'latch-router(?:\([^\r\n)]*\))?#\s*$', rendered):
                return output.decode(errors='replace')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError('Timed out waiting for the live router CLI prompt')
            readable, _, _ = select.select([process.stdout], [], [], min(.5, remaining))
            if readable:
                data = os.read(process.stdout.fileno(), 65536)
                if not data:
                    process.wait(timeout=max(.1, remaining))
                    return output.decode(errors='replace')
                output.extend(data)

    def denied_login(self, grant):
        # Lima publishes gateway SSH through this control-network NAT address.
        # Expected negative tests must reach the actual admission/device checks,
        # rather than accidentally passing because an earlier probe banned it.
        source = '10.1.1.254'
        blocked = self.login_protection('GET')
        if any(item['ip_address'] in (source, '::ffff:' + source) for item in blocked):
            raise RuntimeError('Test SSH source is already IP-blocked; investigate before testing rejection')
        try:
            status, output = self.shell(grant, ['show version', 'exit'], timeout=50)
            if status == 0 or 'freeRouter' in output or 'freeRtr v' in output:
                raise RuntimeError('Expected network device authentication to fail closed')
        finally:
            # Preserve the runtime ban policy. Remove only failure history from
            # this deliberately negative probe on the exclusively used lab NAT
            # source, through the existing trusted management connection.
            self.login_protection('DELETE', source)
            self.login_protection_cleanup.append({'grant_id': grant['grant_id'], 'source': source})

    def login_protection(self, method, source=None):
        script = '''import json,pathlib,sys
from latch_common.warpgate import Api
p=json.load(sys.stdin)
api=Api('https://warpgate:8888',pathlib.Path('/run/device-api'),control_plane_host='localhost')
result=api.request(p['method'],'/login-protection/blocked-ips',{'ip':p['source']} if p['source'] else None)
print(json.dumps(result))
'''
        result = subprocess.run(network.compose(['exec', '-T', '--user', '10001:10001', 'device-api',
                                                 'python', '-c', script], self.options),
                                input=json.dumps({'method': method, 'source': source}), text=True,
                                capture_output=True, check=True, timeout=60)
        return json.loads(result.stdout)

    def target(self, grant):
        name = grant['ssh_username'].split(':', 1)[0]
        return next(t for t in self.api.request('GET', '/targets') if t['name'] == name)

    def sessions(self, grant, active=False):
        name = grant['ssh_username'].split(':', 1)[0]
        query = {'username': name, 'limit': 100}
        if active:
            query['active_only'] = 'true'
        return self.api.request('GET', '/sessions?' + urllib.parse.urlencode(query))['items']

    def recording(self, grant, marker):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            for session in self.sessions(grant):
                for recording in self.api.request('GET', '/sessions/' + session['id'] + '/recordings'):
                    data = self.api.request('GET', '/recordings/' + recording['id'] + '/data', raw=True)
                    output = bytearray()
                    for line in data.splitlines():
                        event = json.loads(line)
                        if 'data' in event and event.get('stream', 'Output') in ('Output', 'Error'):
                            output.extend(base64.b64decode(event['data']))
                    if marker.encode() in output:
                        self.evidence.append({'grant_id': grant['grant_id'], 'session_id': session['id'],
                                              'recording_id': recording['id']})
                        return
            time.sleep(1)
        raise RuntimeError('No replayable Warpgate recording contains actual router output')

    @staticmethod
    def send(process, command):
        process.stdin.write(command + '\n')
        process.stdin.flush()

    @staticmethod
    def read_until(process, markers, timeout):
        deadline = time.monotonic() + timeout
        output = bytearray()
        while not any(marker.encode() in output for marker in markers):
            if time.monotonic() > deadline or process.poll() is not None:
                raise RuntimeError('Live router session did not produce expected protocol output')
            readable, _, _ = select.select([process.stdout], [], [], .5)
            if readable:
                output.extend(os.read(process.stdout.fileno(), 65536))
        return output.decode(errors='replace')

    def stop(self, service):
        subprocess.run(network.compose(['stop', service], self.options), check=True,
                       stdout=subprocess.DEVNULL)

    def start(self, service):
        subprocess.run(network.compose(['up', '-d', '--no-deps', '--wait', '--wait-timeout', '120', service],
                                       self.options), check=True, stdout=subprocess.DEVNULL)

    def revoke_ldap_only(self, grant):
        script = '''import json,pathlib,sqlite3,sys,urllib.request
p=json.load(sys.stdin)
db=sqlite3.connect('/data/grants.sqlite')
lease=db.execute('SELECT lease_id FROM grants WHERE id=?',(p['grant_id'],)).fetchone()[0]
token=pathlib.Path('/run/device-api/device-bao-token').read_text().strip()
r=urllib.request.Request('http://ldap-openbao:8200/v1/sys/leases/revoke',method='PUT',data=json.dumps({'lease_id':lease}).encode(),headers={'X-Vault-Token':token,'Content-Type':'application/json'})
with urllib.request.urlopen(r,timeout=20) as response: response.read()
'''
        subprocess.run(network.compose(['exec', '-T', '--user', '10001:10001', 'device-api',
                                        'python', '-c', script], self.options),
                       input=json.dumps({'grant_id': grant['grant_id']}), text=True,
                       capture_output=True, check=True)

    def routed_network(self):
        """Prove separate attachments, explicit routes and restricted live forwarding."""
        services = ('warpgate', 'freertr', 'tacacs', 'device-router')
        ids = []
        for service in services:
            result = subprocess.run(network.compose(['ps', '-q', service], self.options),
                                    capture_output=True, text=True, check=True)
            if len(result.stdout.split()) != 1:
                raise RuntimeError('Routed-network test requires one running ' + service)
            ids.append(result.stdout.strip())
        result = subprocess.run(network.lima.shell('sudo', 'docker', 'inspect', *ids),
                                capture_output=True, text=True, check=True)
        actual = {item['Config']['Labels']['com.docker.compose.service']: {
            name.removeprefix('latch-poc_'): endpoint['IPAddress']
            for name, endpoint in item['NetworkSettings']['Networks'].items()}
            for item in json.loads(result.stdout)}
        expected = {
            'warpgate': {'control': '10.1.1.2', 'management': '10.1.3.10', 'provider': '100.64.10.10'},
            'freertr': {'device_management': '10.1.4.20'},
            'tacacs': {'management': '10.1.3.21'},
            'device-router': {'management': '10.1.3.253', 'device_management': '10.1.4.1'},
        }
        if actual != expected:
            raise RuntimeError('Live network attachments differ from the routed topology: ' + repr(actual))

        def execute(service, *args):
            return subprocess.run(network.compose(['exec', '-T', service, *args], self.options),
                                  capture_output=True, text=True, check=True).stdout

        for service, destination, gateway, source in (
                ('warpgate', '10.1.4.20', '10.1.3.253', '10.1.3.10'),
                ('freertr', '10.1.3.21', '10.1.4.1', '10.1.4.20'),
                ('tacacs', '10.1.4.20', '10.1.3.253', '10.1.3.21')):
            route = json.loads(execute(service, 'ip', '-j', 'route', 'get', destination))[0]
            if route.get('gateway') != gateway or route.get('prefsrc') != source:
                raise RuntimeError(service + ' does not use the explicit device router: ' + repr(route))
        for service in ('warpgate', 'freertr'):
            if json.loads(execute(service, 'ip', '-j', 'route', 'show', 'default')):
                raise RuntimeError(service + ' unexpectedly has a default route')

        def tcp(service, address, port, allowed):
            if service == 'freertr':
                command = ['nc', '-z', '-w', '3', address, str(port)]
            else:
                # Every non-freeRouter service used here includes Python 3.
                command = ['python3', '-c',
                           'import socket,sys; s=socket.create_connection((sys.argv[1],int(sys.argv[2])),3); s.close()',
                           address, str(port)]
            result = subprocess.run(network.compose(['exec', '-T', service, *command], self.options),
                                    capture_output=True, text=True)
            if result.returncode not in (0, 1):
                raise RuntimeError('TCP probe failed to execute in ' + service)
            if (result.returncode == 0) != allowed:
                raise RuntimeError(f'Unexpected routed TCP result: {service} -> {address}:{port}; allowed={allowed}')

        # Probe real listeners first so a closed service cannot pass a deny check.
        tcp('warpgate', '10.1.4.20', 22, True)
        tcp('freertr', '10.1.3.21', 49, True)
        ldap = execute('tacacs', 'python3', '-c',
                       'import socket; print(socket.gethostbyname("openldap"))').strip()
        tcp('tacacs', ldap, 636, True)
        tcp('tacacs', '10.1.4.20', 22, False)
        tcp('freertr', ldap, 636, False)
        nat_rules = execute('device-router', 'iptables', '-t', 'nat', '-S')
        if forwarding_nat_rules(nat_rules):
            raise RuntimeError('Device routing must preserve source addresses without NAT')
        if '-P FORWARD DROP' not in execute('device-router', 'iptables', '-S', 'FORWARD'):
            raise RuntimeError('Device routing does not have a default-deny forwarding policy')
        self.checks.append('separate device subnet, explicit routes, allowed SSH/TACACS and denied cross-network access without NAT')
        print('PASS: routed device subnet, gateway SSH, device TACACS+, default-deny forwarding and source preservation')

    def roles(self):
        for role in ('readonly', 'operator', 'admin'):
            grant = self.issue(role)
            _, output = self.shell(grant, ['show version', 'show running-config', 'exit'])
            marker = 'freeRouter' if 'freeRouter' in output else 'freeRtr v'
            if marker not in output or 'hostname latch-router' not in output or 'not authorized' in output:
                raise RuntimeError(role + ' could not read version and running configuration')
            self.recording(grant, marker)
            if role == 'readonly':
                _, denied = self.shell(grant, ['configure terminal', 'write terminal', 'sleep 1',
                                                'show version | redirect /tmp/latch-denied', 'exit'])
                if denied.count('not authorized to execute that') != 4:
                    raise RuntimeError('Readonly configuration/save/admin/file-write policy failed')
                # Ping uses the router stack, which has no data-plane interfaces
                # in this management-only fixture. Verify admission separately
                # from reachability; telnet below proves an actual connection.
                _, diagnostic = self.shell(grant, ['ping 127.0.0.1 vrf management repeat 1 timeout 10', 'exit'])
                if 'not authorized' in diagnostic or 'pinging 127.0.0.1' not in diagnostic:
                    raise RuntimeError('Readonly ping was not admitted')
                subprocess.run(network.compose(['exec', '-d', 'freertr', 'sh', '-c',
                    "timeout 60 sh -c \"(sleep 1; printf 'latch-readonly-telnet-proof\\r\\n'; sleep 2) | nc -l -p 2323 -q 1\""], self.options),
                    check=True, capture_output=True, text=True)
                _, connected = self.shell(grant, ['telnet 127.0.0.1 2323 proxy management-host', 'exit'])
                if 'latch-readonly-telnet-proof' not in connected or 'not authorized' in connected:
                    raise RuntimeError('Readonly telnet did not reach the real TCP listener')
            else:
                description = 'latch-' + role + '-arbitrary-config'
                try:
                    _, configured = self.shell(grant, ['configure terminal', 'interface loopback99',
                        'description ' + description, 'end', 'show interfaces description', 'write memory', 'exit'])
                    if ('saving configuration' not in configured or '% success' not in configured
                            or 'not authorized' in configured or not re.search(r'loopback99[^\r\n]*' + description, configured)):
                        raise RuntimeError(role + ' arbitrary configuration/save did not execute')
                finally:
                    _, restored = self.shell(grant, ['configure terminal', 'interface loopback99',
                                                    'no description', 'end', 'write memory', 'exit'])
                    if 'not authorized' in restored or '% success' not in restored:
                        raise RuntimeError('Router configuration cleanup was denied')
                _, privileged = self.shell(grant, ['sleep 1', 'exit'])
                if role == 'operator' and 'not authorized to execute that' not in privileged:
                    raise RuntimeError('Operator admin-only exec command was not denied')
                if role == 'admin' and ('not authorized' in privileged or 'bad command' in privileged):
                    raise RuntimeError('Admin unrestricted exec command was denied')
            self.revoke(grant)
            self.denied_login(grant)
            self.checks.append(role + ': SSH injection, role permissions, recording, revoke')
            print('PASS ' + role + ': real SSH, role permissions, recording and revoke')

    def run(self):
        self.routed_network()
        self.roles()
        revoking = self.issue()
        process = subprocess.Popen(self.command(revoking), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            self.read_prompt(process, time.monotonic() + 20)
            self.send(process, 'show version')
            self.read_until(process, ('freeRouter', 'freeRtr v'), 20)
            if process.poll() is not None:
                raise RuntimeError('SSH session closed before explicit revocation')
            self.revoke(revoking)
            deadline = time.monotonic() + 15
            while process.poll() is None:
                if time.monotonic() > deadline:
                    raise RuntimeError('Live SSH session remained open after explicit revocation')
                time.sleep(.25)
            while self.sessions(revoking, active=True):
                if time.monotonic() > deadline:
                    raise RuntimeError('Gateway still reports an active explicitly revoked session')
                time.sleep(.25)
            self.denied_login(revoking)
            self.checks.append('explicit revoke closes active SSH and rejects reconnect')
            print('PASS: explicit revoke closes an established SSH session and rejects reconnect')
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=10)

        pinned = self.issue()
        _, baseline = self.shell(pinned, ['show version', 'exit'])
        if 'freeRouter' not in baseline and 'freeRtr v' not in baseline:
            raise RuntimeError('Host-key test baseline did not execute a real router command')
        pins = [pin for pin in self.api.request('GET', '/ssh/known-hosts')
                if pin['host'] == 'freertr' and pin['port'] == 22]
        if len(pins) != 1:
            raise RuntimeError('Host-key test requires exactly one existing router pin')
        original = {key: pins[0][key] for key in ('host', 'port', 'key_type', 'key_base64')}
        different_kind, different_key = self.public_key.split()[:2]
        if (different_kind, different_key) == (original['key_type'], original['key_base64']):
            raise RuntimeError('Host-key test requires a different public key')
        try:
            self.api.request('DELETE', '/ssh/known-hosts/' + pins[0]['id'])
            self.api.request('POST', '/ssh/known-hosts',
                             {**original, 'key_type': different_kind, 'key_base64': different_key})
            self.denied_login(pinned)
        finally:
            for pin in self.api.request('GET', '/ssh/known-hosts'):
                if pin['host'] == 'freertr' and pin['port'] == 22:
                    self.api.request('DELETE', '/ssh/known-hosts/' + pin['id'])
            self.api.request('POST', '/ssh/known-hosts', original)
        _, restored = self.shell(pinned, ['show version', 'exit'])
        if 'freeRouter' not in restored and 'freeRtr v' not in restored:
            raise RuntimeError('Restoring router host pin did not restore SSH access')
        self.revoke(pinned)
        self.checks.append('mismatched router host key rejects SSH; restored pin restores access')
        print('PASS: mismatched router host key rejected; restored pin restores SSH')

        wrong = self.issue()
        target = self.target(wrong)
        target['options']['auth'] = {'kind': 'Password', 'password': 'deliberately-invalid-network-test'}
        allowed = ('name', 'description', 'require_approval', 'ticket_requests_disabled',
                   'ticket_require_approval', 'options')
        self.api.request('PUT', '/targets/' + target['id'], {k: target[k] for k in allowed if k in target})
        self.denied_login(wrong)
        self.revoke(wrong)
        self.checks.append('wrong LDAP password rejected')
        print('PASS: wrong LDAP password rejected through real SSH/TACACS')

        revoked = self.issue()
        self.revoke_ldap_only(revoked)
        self.denied_login(revoked)
        self.revoke(revoked)
        self.checks.append('revoked LDAP identity rejected with existing gateway grant')
        print('PASS: revoked LDAP identity rejected while gateway grant still exists')

        for service in ('tacacs', 'openldap'):
            grant = self.issue()
            established = subprocess.Popen(self.command(grant), stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                self.read_prompt(established, time.monotonic() + 20)
                self.send(established, 'show version')
                self.read_until(established, ('freeRouter', 'freeRtr v'), 20)
                self.stop(service)
                self.send(established, 'show version')
                self.read_until(established, ('not authorized to execute that',), 40)
                self.denied_login(grant)
            finally:
                self.start(service)
                self.revoke(grant)
                if established.poll() is None:
                    established.terminate()
                established.communicate(timeout=10)
            print('PASS: ' + service + ' outage denies established commands and new login; restored')
            self.checks.append(service + ' outage denies existing commands and new login; restored')

        expiring = self.issue(ttl=30)
        process = subprocess.Popen(self.command(expiring), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            self.read_prompt(process, time.monotonic() + 15)
            process.stdin.write('show version\n')
            process.stdin.flush()
            deadline = time.monotonic() + 15
            output = bytearray()
            while b'freeRouter' not in output and b'freeRtr v' not in output:
                if time.monotonic() > deadline or process.poll() is not None:
                    raise RuntimeError('Expiring SSH session never executed a router command')
                readable, _, _ = select.select([process.stdout], [], [], .5)
                if readable:
                    output.extend(os.read(process.stdout.fileno(), 65536))
            expires = datetime.fromisoformat(expiring['expires_at'].replace('Z', '+00:00')).timestamp()
            # A disconnect before expiry is a failure, not cleanup evidence.
            while time.time() < expires - 1:
                if process.poll() is not None:
                    raise RuntimeError('SSH session closed before the grant expired')
                time.sleep(.25)
            deadline = time.monotonic() + 15
            while process.poll() is None:
                if time.monotonic() > deadline:
                    raise RuntimeError('SSH session remained open after grant expiry')
                time.sleep(.5)
            if self.sessions(expiring, active=True):
                raise RuntimeError('Gateway still reports an active expired session')
            deadline = time.monotonic() + 15
            while True:
                result = device.request(self.options, 'GET', '/v1/device-grants/' + expiring['grant_id'])
                if result['state'] == 'revoked':
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError('Expired grant cleanup did not finish')
                time.sleep(.5)
            self.denied_login(expiring)
            self.checks.append('grant expiry closes active SSH and rejects reconnect')
            print('PASS: short grant expires, live SSH session closes, reconnect denied')
        finally:
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=10)

    def cleanup(self):
        failed = False
        for grant in self.grants:
            try:
                self.revoke(grant)
            except Exception:
                failed = True
        if failed:
            raise RuntimeError('Network verification grant cleanup incomplete; inspect device API grants')


def main():
    sys.stdout.reconfigure(line_buffering=True)
    report = lab.STATE / 'network-verification.json'
    report.unlink(missing_ok=True)
    harness = Harness()
    try:
        harness.run()
    finally:
        harness.cleanup()
    report.write_text(json.dumps({'recordings': harness.evidence, 'checks': harness.checks,
                                 'test_login_protection_cleanup': harness.login_protection_cleanup,
                                 'tested_at': datetime.now(timezone.utc).isoformat(),
                                 'passed': True}, indent=2) + '\n')
    report.chmod(0o600)
    print('Evidence: ' + str(report))


if __name__ == '__main__':
    main()
