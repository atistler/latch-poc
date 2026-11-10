"""Persisted reader identities must migrate without resetting directory data."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ReaderIdentityMigration(unittest.TestCase):
    def migrate(self, entries, reader_bind_status=0, search_status=0):
        source = (ROOT / 'containers/ldap.sh').read_text()
        migration = 'migrate_reader_identity() {' + source.split('migrate_reader_identity() {', 1)[1]
        migration = migration.split('\nmigrate_reader_identity\n', 1)[0]
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / 'operations'
            script = '''set -euo pipefail
admin=()
ldapsearch() { printf '%s\\n' "$DIRECTORY_ENTRIES"; return "$SEARCH_STATUS"; }
ldapmodrdn() { printf '%s\\n' "$*" >> "$OPERATION_LOG"; }
ldapwhoami() {
    [[ "$2" == '-H' && "$3" == 'ldaps://openldap:636' ]] || return 90
    return "$READER_BIND_STATUS"
}
'''
            result = subprocess.run(['bash', '-c', script + migration + '\nmigrate_reader_identity\n'],
                                    capture_output=True, text=True,
                                    env={**os.environ, 'DIRECTORY_ENTRIES': entries,
                                         'READER_BIND_STATUS': str(reader_bind_status),
                                         'SEARCH_STATUS': str(search_status),
                                         'OPERATION_LOG': str(log)})
            return result, log.read_text() if log.exists() else ''

    def test_legacy_identity_renamed_without_password_replacement(self):
        result, operations = self.migrate('dn: cn=ise,ou=services,dc=latch,dc=lab\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(operations, '-r cn=ise,ou=services,dc=latch,dc=lab cn=reader\n')

    def test_current_identity_does_not_trigger_rename(self):
        result, operations = self.migrate('dn: cn=reader,ou=services,dc=latch,dc=lab\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(operations, '')

    def test_ambiguous_or_missing_identities_fail_without_mutation(self):
        for entries in ('', 'dn: cn=reader,ou=services,dc=latch,dc=lab\n\n'
                            'dn: cn=ise,ou=services,dc=latch,dc=lab\n'):
            with self.subTest(entries=entries):
                result, operations = self.migrate(entries)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(operations, '')

    def test_directory_search_failure_does_not_rename(self):
        result, operations = self.migrate('', search_status=49)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(operations, '')

    def test_incorrect_persisted_reader_password_fails_readiness(self):
        result, operations = self.migrate('dn: cn=reader,ou=services,dc=latch,dc=lab\n',
                                          reader_bind_status=49)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(operations, '')


class RoleGroupMigration(unittest.TestCase):
    def migrate(self, existing_roles, search_status=0):
        source = (ROOT / 'containers/ldap.sh').read_text()
        migration = 'ensure_role_groups() {' + source.split('ensure_role_groups() {', 1)[1]
        migration = migration.split('\nmigrate_reader_identity\n', 1)[0]
        entries = '\n'.join('dn: cn=' + role + ',ou=groups,dc=latch,dc=lab'
                            for role in existing_roles)
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / 'additions'
            script = '''set -euo pipefail
admin=()
ldapsearch() { printf '%s\\n' "$DIRECTORY_ENTRIES"; return "$SEARCH_STATUS"; }
ldapadd() { cat >> "$OPERATION_LOG"; }
'''
            result = subprocess.run(['bash', '-c', script + migration + '\nensure_role_groups\n'],
                                    capture_output=True, text=True,
                                    env={**os.environ, 'DIRECTORY_ENTRIES': entries,
                                         'SEARCH_STATUS': str(search_status),
                                         'OPERATION_LOG': str(log)})
            return result, log.read_text() if log.exists() else ''

    def test_existing_directory_only_adds_missing_admin_group(self):
        result, additions = self.migrate(('operator', 'readonly'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(additions, 'dn: cn=admin,ou=groups,dc=latch,dc=lab\n'
                         'objectClass: groupOfNames\ncn: admin\n'
                         'member: cn=sentinel,ou=services,dc=latch,dc=lab\n')

    def test_existing_admin_and_memberships_are_untouched(self):
        result, additions = self.migrate(('admin', 'operator', 'readonly', 'custom'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(additions, '')

    def test_group_search_failure_never_creates_groups(self):
        result, additions = self.migrate((), search_status=49)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(additions, '')


class OpenBaoIdentityMigration(unittest.TestCase):
    def configure(self, directory):
        source = (ROOT / 'containers/openbao-ldap.sh').read_text().split('\nbao server ', 1)[0]
        source = source.replace('/opt/latch/bao', str(directory))
        return subprocess.run(['bash', '-c', source], capture_output=True, text=True)

    def test_existing_raft_identity_survives_service_rename(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            (data / 'init.json').write_text('{}')
            (data / 'server.hcl').write_text('storage "raft" {\n  node_id = "existing-raft-member"\n}\n')
            result = self.configure(data)
            self.assertEqual(result.returncode, 0, result.stderr)
            actual = (data / 'server.hcl').read_text()
            self.assertIn('node_id = "existing-raft-member"', actual)
            self.assertIn('api_addr = "http://ldap-openbao:8200"', actual)

    def test_fresh_storage_gets_generic_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            result = self.configure(data)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('node_id = "latch-ldap-bao"', (data / 'server.hcl').read_text())

    def test_initialized_storage_without_config_refuses_identity_reset(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            (data / 'init.json').write_text('{}')
            result = self.configure(data)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((data / 'server.hcl').exists())


if __name__ == '__main__':
    unittest.main()
