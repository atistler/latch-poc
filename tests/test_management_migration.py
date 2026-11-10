"""Management migration must reject unknown attachments and overlapping routes."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('migration', Path(__file__).resolve().parents[1] / 'guest/management-migrate.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class MigrationSafety(unittest.TestCase):
    def container(self):
        return {'Id': 'bao-container', 'Image': 'image-local', 'State': {'Running': True},
                'Config': {'Labels': {'com.docker.compose.project': 'latch-poc',
                                      'com.docker.compose.service': 'openbao'}},
                'Mounts': [{'Type': 'volume', 'Destination': '/opt/latch/bao', 'Name': 'original-volume'}]}

    def network(self, subnet):
        return {'IPAM': {'Config': [{'Subnet': subnet, 'Gateway': subnet.rsplit('.', 1)[0] + '.254'}]},
                'Containers': {'bao-container': {}}}

    def test_default_route_is_allowed_but_conflicting_subnets_are_not(self):
        module.check_routes([{'dst': 'default'}, {'dst': '10.1.1.0/24'}])
        for route in ['10.0.0.0/8', '10.1.3.0/24', '10.1.3.8/32']:
            with self.subTest(route=route), self.assertRaisesRegex(RuntimeError, 'overlap'):
                module.check_routes([{'dst': route}])

    def test_unknown_container_is_rejected(self):
        good = {'Config': {'Labels': {'com.docker.compose.project': 'latch-poc', 'com.docker.compose.service': 'openbao'}}}
        module.check_container(good)
        for labels in [{}, {'com.docker.compose.project': 'other', 'com.docker.compose.service': 'openbao'},
                       {'com.docker.compose.project': 'latch-poc', 'com.docker.compose.service': 'db'}]:
            with self.assertRaisesRegex(RuntimeError, 'Unexpected'):
                module.check_container({'Config': {'Labels': labels}})

    def test_prepare_backs_up_before_removing_and_verify_checks_ca_and_volume(self):
        calls = []
        def command(*args, **kwargs):
            calls.append(args)
            if args[:2] == ('ip', '-j'):
                return '[]'
            if args[:2] == ('docker', 'inspect'):
                return json.dumps([self.container()])
            if args[:2] == ('docker', 'compose'):
                return json.dumps({'services': {'openbao': {}}, 'networks': {'management': {'ipam': {'config': []}}}})
            return ''

        def archive(args, **kwargs):
            calls.append(tuple(args))
            kwargs['stdout'].write(b'cold-backup')

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(module, 'ROOT', root), patch.object(module, 'PENDING', root / 'pending.json'), \
                    patch.object(module, 'run', side_effect=command), \
                    patch.object(module.subprocess, 'run', side_effect=archive), \
                    patch.object(module, 'public_ca', return_value='ssh-ed25519 original'), \
                    patch.object(module, 'inspect_network', return_value=self.network(module.OLD)):
                module.prepare()
                record = json.loads((root / 'pending.json').read_text())
                backup = Path(record['backup'])
                self.assertEqual((backup / 'openbao-volume.tar').read_bytes(), b'cold-backup')
                self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
                for path in backup.iterdir():
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                rollback = json.loads((backup / 'rollback-compose.json').read_text())
                self.assertEqual(rollback['networks']['management']['ipam']['config'][0]['subnet'], module.OLD)
                stop = next(i for i, args in enumerate(calls) if args[:2] == ('docker', 'stop'))
                cold = next(i for i, args in enumerate(calls) if args[:2] == ('docker', 'run'))
                remove = next(i for i, args in enumerate(calls) if args[:2] == ('docker', 'rm'))
                image_check = calls.index(('docker', 'image', 'inspect', 'latch-poc-openbao:latest'))
                self.assertLess(image_check, stop)
                self.assertIn('latch-poc-openbao:latest', calls[cold])
                self.assertIn('--pull=never', calls[cold])
                self.assertLess(stop, cold)
                self.assertLess(cold, remove)
                self.assertFalse(any('-v' in args for args in calls if args[:2] == ('docker', 'rm')))
                before = len(calls)
                module.prepare()
                self.assertEqual(len(calls), before)
                with patch.object(module, 'inspect_network', return_value=self.network(module.NEW)):
                    module.verify()
                self.assertFalse((root / 'pending.json').exists())
                self.assertTrue((backup / 'verified.json').exists())

    def test_verify_rejects_changed_ca_and_keeps_pending_record(self):
        with tempfile.TemporaryDirectory() as directory:
            pending = Path(directory) / 'pending.json'
            pending.write_text(json.dumps({'backup': directory, 'volume': 'original-volume',
                                           'public_ca': 'ssh-ed25519 original'}))
            with patch.object(module, 'PENDING', pending), \
                    patch.object(module, 'inspect_network', return_value=self.network(module.NEW)), \
                    patch.object(module, 'run', return_value=json.dumps([self.container()])), \
                    patch.object(module, 'public_ca', return_value='ssh-ed25519 replacement'):
                with self.assertRaisesRegex(RuntimeError, 'CA changed'):
                    module.verify()
                self.assertTrue(pending.exists())

    def test_cold_backup_failure_restarts_original_and_does_not_remove_it(self):
        calls = []
        def command(*args, **kwargs):
            calls.append(args)
            if args[:2] == ('ip', '-j'):
                return '[]'
            if args[:2] == ('docker', 'inspect'):
                return json.dumps([self.container()])
            if args[:2] == ('docker', 'compose'):
                return json.dumps({'services': {'openbao': {}}, 'networks': {'management': {'ipam': {}}}})
            return ''

        def direct_command(args, **kwargs):
            calls.append(tuple(args))
            if args[:2] == ['docker', 'run']:
                raise RuntimeError('archive failed')

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(module, 'ROOT', root), patch.object(module, 'PENDING', root / 'pending.json'), \
                    patch.object(module, 'run', side_effect=command), \
                    patch.object(module.subprocess, 'run', side_effect=direct_command), \
                    patch.object(module, 'public_ca', return_value='ssh-ed25519 original'), \
                    patch.object(module, 'inspect_network', return_value=self.network(module.OLD)):
                with self.assertRaisesRegex(RuntimeError, 'archive failed'):
                    module.prepare()
                self.assertIn(('docker', 'start', 'bao-container'), calls)
                self.assertFalse(any(args[:2] == ('docker', 'rm') for args in calls))
                self.assertFalse((root / 'pending.json').exists())


if __name__ == '__main__':
    unittest.main()
