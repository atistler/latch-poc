"""Exercise resume decisions with fake services and OpenStack, without a VM."""
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('jq'), 'resume checks require jq')
class ResumeTests(unittest.TestCase):
    def resume(self, state, agent_mode='retry', namespace='ready', domain='running'):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            binary = work / 'bin'
            binary.mkdir()
            (work / 'guest').mkdir()
            (work / 'guest/restart-configure.sh').write_text('exit 0\n')
            common = work / 'common.sh'
            common.write_text(f'set -euo pipefail\nBASE="{work}"\nos_auth() {{ :; }}\n')
            script = (ROOT / 'guest/resume.sh').read_text().replace(
                'source /home/stack/latch/guest/common.sh', f'source "{common}"')
            # Advance Bash's clock without waiting for a real 90-second grace.
            script = script.replace('sleep 2', 'SECONDS=$((SECONDS + 30))')
            script = script.replace('i<180', 'i<8')
            (work / 'resume.sh').write_text(script)
            (work / 'state').write_text(state)
            (work / 'calls').touch()
            commands = {
                'systemctl': '''#!/bin/sh
echo "systemctl $*" >> "$RESUME_TEST_STATE/calls"
if [ "$1" = list-unit-files ]; then
    for unit in n-cpu neutron-rpc-server q-l3 q-dhcp q-agt; do
        echo "devstack@$unit.service enabled enabled"
    done
fi
if [ "$1" = restart ] && [ "$2" = devstack@q-agt.service ]; then
    touch "$RESUME_TEST_STATE/recovered"
fi
''',
                'timeout': '#!/bin/sh\nshift\nexec "$@"\n',
                'sleep': '#!/bin/sh\nexit 0\n',
                'ip': '''#!/bin/sh
if [ "$RESUME_TEST_NAMESPACE" = ready ] || [ -f "$RESUME_TEST_STATE/recovered" ]; then
    echo "qrouter-router-id (id: 0)"
fi
''',
                'virsh': '''#!/bin/sh
echo "virsh $*" >> "$RESUME_TEST_STATE/calls"
if [ "$RESUME_TEST_DOMAIN" = running ] || [ -f "$RESUME_TEST_STATE/started" ]; then
    echo running
else
    echo "shut off"
fi
''',
                'openstack': '''#!/usr/bin/env python3
import json, os, pathlib, sys
work = pathlib.Path(os.environ['RESUME_TEST_STATE'])
args = sys.argv[1:]
with (work / 'calls').open('a') as stream:
    stream.write(' '.join(args) + '\\n')
if args[:2] == ['server', 'list']:
    print(json.dumps([] if (work / 'state').read_text() == 'ABSENT' else [{'Name': 'latch-vm'}]))
elif args[:3] == ['network', 'agent', 'list']:
    # First poll sees a stale L3 heartbeat; recovery must wait for a live one.
    first = not (work / 'polled').exists()
    (work / 'polled').touch()
    mode = os.environ['RESUME_TEST_AGENTS']
    alive = mode == 'healthy' or (mode == 'retry' and not first) or (mode == 'recover' and (work / 'recovered').exists())
    print(json.dumps([{'Agent Type': name, 'Alive': alive}
        for name in ['L3 agent', 'Open vSwitch agent', 'DHCP agent']]))
elif args[:3] == ['compute', 'service', 'list']:
    print(json.dumps([{'State': 'up', 'Status': 'enabled'}]))
elif args[:2] == ['router', 'list']:
    print(json.dumps([] if (work / 'state').read_text() == 'ABSENT' else [{'Name': 'latch-router', 'ID': 'router-id'}]))
elif args[:2] == ['server', 'show']:
    if args[-1] == 'OS-EXT-SRV-ATTR:instance_name':
        print('instance-00000001')
    else:
        # Nova can initially report ACTIVE while libvirt is shut off, then reconcile.
        if os.environ['RESUME_TEST_DOMAIN'] == 'reconcile' and not (work / 'started').exists():
            seen = (work / 'status-seen').exists()
            (work / 'status-seen').touch()
            print('SHUTOFF' if seen else 'ACTIVE')
        else:
            print((work / 'state').read_text())
elif args[:2] == ['server', 'start']:
    (work / 'state').write_text('ACTIVE')
    (work / 'started').touch()
else:
    sys.exit('Unexpected OpenStack command: ' + repr(args))
''',
            }
            # Avoid invoking a version-manager shim for every fake CLI poll.
            # A shell wrapper also handles interpreter paths containing spaces.
            commands['python3'] = f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n'
            for name, content in commands.items():
                path = binary / name
                path.write_text(content)
                path.chmod(0o755)
            env = dict(os.environ, PATH=f'{binary}:{os.environ["PATH"]}',
                       RESUME_TEST_STATE=str(work), RESUME_TEST_AGENTS=agent_mode,
                       RESUME_TEST_NAMESPACE=namespace, RESUME_TEST_DOMAIN=domain)
            result = subprocess.run(['/bin/bash', str(work / 'resume.sh')],
                                    env=env, capture_output=True, text=True, timeout=10)
            return result, (work / 'calls').read_text().splitlines()

    def test_waits_for_agents_then_starts_shutoff_tenant(self):
        result, calls = self.resume('SHUTOFF')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls.count('network agent list -f json'), 2)
        self.assertIn('server start latch-vm', calls)

    def test_running_tenant_is_not_restarted(self):
        result, calls = self.resume('ACTIVE')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('server start latch-vm', calls)

    def test_unprovisioned_cloud_is_ready_without_a_tenant(self):
        result, calls = self.resume('ABSENT')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(call.startswith('server show') for call in calls))

    def test_error_tenant_fails_recovery(self):
        result, calls = self.resume('ERROR')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Tenant is in ERROR state', result.stderr)
        self.assertNotIn('server start latch-vm', calls)

    def test_stale_active_agents_trigger_one_ordered_rpc_recovery(self):
        result, calls = self.resume('SHUTOFF', 'recover')
        self.assertEqual(result.returncode, 0, result.stderr)
        restarts = [call for call in calls if call.startswith('systemctl restart')]
        self.assertEqual(restarts, [f'systemctl restart devstack@{unit}.service'
                                   for unit in ['neutron-rpc-server', 'q-l3', 'q-dhcp', 'q-agt']])
        self.assertGreaterEqual(calls.count('network agent list -f json'), 4)

    def test_healthy_agents_do_not_trigger_rpc_recovery(self):
        result, calls = self.resume('ACTIVE', 'healthy')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(call.startswith('systemctl restart') for call in calls))

    def test_permanently_stale_agents_fail_after_one_recovery(self):
        result, calls = self.resume('ACTIVE', 'stale')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls.count('systemctl restart devstack@neutron-rpc-server.service'), 1)
        self.assertIn('did not recover', result.stderr)

    def test_live_heartbeats_without_router_namespace_trigger_recovery(self):
        result, calls = self.resume('ACTIVE', 'healthy', namespace='missing')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls.count('systemctl restart devstack@neutron-rpc-server.service'), 1)

    def test_stale_nova_active_waits_for_reconciliation_then_starts_once(self):
        result, calls = self.resume('ACTIVE', 'healthy', domain='reconcile')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls.count('server start latch-vm'), 1)
        self.assertGreaterEqual(calls.count('virsh domstate instance-00000001'), 2)

    def test_nova_active_with_stopped_domain_fails(self):
        result, calls = self.resume('ACTIVE', 'healthy', domain='stopped')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('libvirt domain must be running', result.stderr)
        self.assertNotIn('server start latch-vm', calls)


if __name__ == '__main__':
    unittest.main()
