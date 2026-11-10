"""Negative SSH checks must not pass because another check banned the lab NAT."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, call

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_network_device import Harness, forwarding_nat_rules


class NetworkRoutingNat(unittest.TestCase):
    def test_docker_loopback_dns_is_not_forwarded_nat(self):
        rules = '\n'.join([
            '-A DOCKER_OUTPUT -d 127.0.0.11/32 -p tcp -m tcp --dport 53 -j DNAT --to-destination 127.0.0.11:39847',
            '-A DOCKER_POSTROUTING -s 127.0.0.11/32 -p udp -m udp --sport 41002 -j SNAT --to-source :53',
        ])
        self.assertEqual(forwarding_nat_rules(rules), [])

    def test_device_nat_is_rejected_even_in_a_docker_chain(self):
        for rule in (
            '-A POSTROUTING -s 10.1.4.0/24 -j MASQUERADE',
            '-A DOCKER_POSTROUTING -s 10.1.4.20/32 -j SNAT --to-source 10.1.3.253',
            '-A DOCKER_OUTPUT -d 10.1.4.20/32 -p tcp -m tcp --dport 22 -j DNAT --to-destination 10.1.3.21:49',
        ):
            with self.subTest(rule=rule):
                self.assertEqual(forwarding_nat_rules(rule), [rule])


class NetworkAcceptanceProtection(unittest.TestCase):
    def harness(self, blocked=(), result=(255, 'Permission denied')):
        # Do not initialize the live harness: no state, credentials, gateway
        # connections or subprocesses are needed to verify rejection handling.
        harness = Harness.__new__(Harness)
        harness.login_protection_cleanup = []
        harness.login_protection = Mock(return_value=list(blocked))
        harness.shell = Mock(return_value=result)
        return harness

    def test_preexisting_source_ban_fails_before_probe_without_reset(self):
        for source in ('10.1.1.254', '::ffff:10.1.1.254'):
            with self.subTest(source=source):
                harness = self.harness(blocked=[{'ip_address': source}])
                with self.assertRaisesRegex(RuntimeError, 'already IP-blocked'):
                    harness.denied_login({'grant_id': 'test-grant'})
                harness.shell.assert_not_called()
                harness.login_protection.assert_called_once_with('GET')
                self.assertEqual(harness.login_protection_cleanup, [])

    def test_expected_denial_resets_only_test_source(self):
        # An unrelated blocked address must remain untouched.
        harness = self.harness(blocked=[{'ip_address': '192.0.2.99'}])
        grant = {'grant_id': 'test-grant'}
        harness.denied_login(grant)
        harness.shell.assert_called_once_with(grant, ['show version', 'exit'], timeout=50)
        self.assertEqual(harness.login_protection.call_args_list,
                         [call('GET'), call('DELETE', '10.1.1.254')])
        self.assertEqual(harness.login_protection_cleanup,
                         [{'grant_id': 'test-grant', 'source': '10.1.1.254'}])

    def test_unexpected_access_still_fails_and_resets_probe_history(self):
        for result in ((0, 'connected'), (255, 'freeRouter version'), (255, 'freeRtr v1')):
            with self.subTest(result=result):
                harness = self.harness(result=result)
                with self.assertRaisesRegex(RuntimeError, 'authentication to fail closed'):
                    harness.denied_login({'grant_id': 'test-grant'})
                self.assertEqual(harness.login_protection.call_args_list,
                                 [call('GET'), call('DELETE', '10.1.1.254')])


if __name__ == '__main__':
    unittest.main()
