"""OpenStack adapter contract: project scoping and narrowly-owned reachability."""
import importlib.util
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

AVAILABLE = all(importlib.util.find_spec(name) for name in ('httpx', 'cryptography'))
if AVAILABLE:
    import httpx
    from latch_api.openstack import OpenStackNetwork
    from latch_api.device_access import AccessError

PROJECT = '11111111-1111-4111-8111-111111111111'
INSTANCE = '22222222-2222-4222-8222-222222222222'
PORT = '33333333-3333-4333-8333-333333333333'
FIP = '44444444-4444-4444-8444-444444444444'
GROUP = '55555555-5555-4555-8555-555555555555'
RULE = '66666666-6666-4666-8666-666666666666'
FORWARDING = '77777777-7777-4777-8777-777777777777'


@unittest.skipUnless(AVAILABLE, 'Install latch_api/requirements.txt')
class OpenStackContract(unittest.TestCase):
    def setUp(self):
        self.server = SimpleNamespace(server_id=INSTANCE, project_id=PROJECT, port_id=PORT,
                                  floating_ip_id=FIP, security_group_id=GROUP,
                                  host='100.64.10.120', port=22001, internal_ip='192.168.20.15')
        self.requests = []
        self.forwardings = []
        self.rules = [{'id': RULE, 'direction': 'ingress', 'ethertype': 'IPv4', 'protocol': 'tcp',
                       'port_range_min': 22, 'port_range_max': 22,
                       'remote_ip_prefix': '100.64.10.10/32', 'description': ''}]
        self.nova_server = {'id': INSTANCE, 'tenant_id': PROJECT, 'status': 'ACTIVE'}
        self.port = {'project_id': PROJECT, 'device_id': INSTANCE, 'port_security_enabled': True,
                     'security_groups': [GROUP], 'fixed_ips': [{'ip_address': self.server.internal_ip}]}
        self.fip = {'project_id': PROJECT, 'floating_ip_address': self.server.host, 'port_id': None}
        self.post_fail = False
        self.delete_fail = False
        self.redirect_auth = False
        self.resource_unauthorized = 0
        self.config = {'auth_url': 'http://identity/v3', 'application_credential_id': 'credential-id',
                       'application_credential_secret': 'private-secret', 'region_name': 'RegionOne'}
        self.adapter = OpenStackNetwork(self.config, transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        import json
        self.requests.append(request)
        path, method = request.url.path, request.method
        if path == '/v3/auth/tokens':
            if self.redirect_auth:
                return httpx.Response(307, headers={'location': 'http://attacker/collect'})
            self.assertEqual(json.loads(request.content)['auth']['identity']['methods'], ['application_credential'])
            return httpx.Response(201, headers={'X-Subject-Token': 'scoped-token'}, json={'token': {
                'project': {'id': PROJECT},
                'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                'catalog': [{'type': kind, 'endpoints': [{'url': 'http://' + kind,
                                'interface': 'public', 'region': 'RegionOne'}]}
                            for kind in ('compute', 'network')]}})
        self.assertEqual(request.headers['X-Auth-Token'], 'scoped-token')
        if self.resource_unauthorized:
            self.resource_unauthorized -= 1
            return httpx.Response(401)
        if path == '/servers/' + INSTANCE:
            return httpx.Response(200, json={'server': self.nova_server})
        if path == '/v2.0/ports/' + PORT:
            return httpx.Response(200, json={'port': self.port})
        if path == '/v2.0/floatingips/' + FIP:
            return httpx.Response(200, json={'floatingip': self.fip})
        if path == '/v2.0/security-groups/' + GROUP:
            return httpx.Response(200, json={'security_group': {
                'project_id': PROJECT, 'security_group_rules': self.rules}})
        if path == '/v2.0/security-group-rules' and method == 'POST':
            rule = json.loads(request.content)['security_group_rule']
            rule['id'] = RULE
            self.rules.append(rule)
            return httpx.Response(201, json={'security_group_rule': rule})
        if path == '/v2.0/security-group-rules/' + RULE and method == 'DELETE':
            self.rules = [r for r in self.rules if r['id'] != RULE]
            return httpx.Response(204)
        if path == '/v2.0/floatingips/' + FIP + '/port_forwardings':
            if method == 'GET':
                return httpx.Response(200, json={'port_forwardings': self.forwardings})
            if method == 'POST':
                forwarding = json.loads(request.content)['port_forwarding']
                forwarding['id'] = FORWARDING
                self.forwardings.append(forwarding)
                if self.post_fail:
                    return httpx.Response(503, text='private-backend-details')
                return httpx.Response(201, json={'port_forwarding': forwarding})
        if path == '/v2.0/floatingips/' + FIP + '/port_forwardings/' + FORWARDING and method == 'DELETE':
            if self.delete_fail:
                return httpx.Response(503)
            self.forwardings = [r for r in self.forwardings if r['id'] != FORWARDING]
            return httpx.Response(204)
        raise AssertionError('Unexpected request: ' + method + ' ' + str(request.url))

    def test_scoped_authentication_and_idempotent_forwarding(self):
        self.adapter.ensure(self.server)
        self.adapter.ensure(self.server)
        self.assertEqual(len(self.forwardings), 1)
        self.assertEqual(self.forwardings[0]['description'], 'latch-server:' + INSTANCE)
        self.assertEqual(self.forwardings[0]['internal_port'], 22)
        self.assertEqual(sum(r.url.path == '/v3/auth/tokens' for r in self.requests), 1)
        self.adapter.remove(self.server)
        self.adapter.remove(self.server)
        self.assertEqual(self.forwardings, [])
        self.assertEqual(len(self.rules), 1, 'Bootstrap-owned SG rule must remain')

    def test_foreign_project_credential_denied_before_resource_requests(self):
        self.server.project_id = '88888888-8888-4888-8888-888888888888'
        with self.assertRaises(AccessError) as result:
            self.adapter.ensure(self.server)
        self.assertEqual(result.exception.status_code, 403)
        self.assertEqual(len(self.requests), 1)

    def test_server_port_and_fip_inventory_mismatch_fail_closed(self):
        for target, key, value in [(self.nova_server, 'tenant_id', 'another-project'),
                                   (self.port, 'device_id', 'another-instance'),
                                   (self.port, 'port_security_enabled', False),
                                   (self.fip, 'port_id', PORT)]:
            with self.subTest(key=key):
                previous = target[key]
                target[key] = value
                with self.assertRaises(AccessError):
                    self.adapter.ensure(self.server)
                self.assertFalse(self.forwardings)
                target[key] = previous

    def test_broader_ssh_ingress_is_rejected(self):
        self.rules[0]['remote_ip_prefix'] = '0.0.0.0/0'
        with self.assertRaisesRegex(AccessError, 'only the gateway'):
            self.adapter.ensure(self.server)
        self.assertFalse(self.forwardings)

    def test_gateway_rule_must_only_allow_tcp_22(self):
        self.rules[0]['port_range_min'] = 1
        self.rules[0]['port_range_max'] = 65535
        with self.assertRaisesRegex(AccessError, 'only the gateway'):
            self.adapter.ensure(self.server)
        self.assertFalse(self.forwardings)

    def test_missing_sg_rule_created_and_only_owned_rule_removed(self):
        self.rules = []
        self.adapter.ensure(self.server)
        self.assertEqual(self.rules[0]['remote_ip_prefix'], '100.64.10.10/32')
        self.adapter.remove(self.server)
        self.assertFalse(self.rules)
        self.assertFalse(self.forwardings)

    def test_legacy_owned_resources_survive_rename_and_remain_revocable(self):
        self.rules = []
        self.adapter.ensure(self.server)
        self.forwardings[0]['description'] = 'latch-vm:' + INSTANCE
        self.rules[0]['description'] = 'latch-vm:' + INSTANCE
        self.adapter.ensure(self.server)
        self.assertEqual(len(self.forwardings), 1)
        self.assertEqual(len(self.rules), 1)
        self.adapter.remove(self.server)
        self.assertFalse(self.forwardings)
        self.assertFalse(self.rules)

    def test_unmarked_conflicting_forwarding_never_adopted_or_deleted(self):
        self.adapter.ensure(self.server)
        self.forwardings[0]['description'] = ''
        before = deepcopy(self.forwardings)
        with self.assertRaises(AccessError) as result:
            self.adapter.ensure(self.server)
        self.assertEqual(result.exception.status_code, 409)
        self.adapter.remove(self.server)
        self.assertEqual(self.forwardings, before)

    def test_foreign_overlapping_port_range_is_rejected(self):
        self.forwardings = [{'id': FORWARDING, 'protocol': 'tcp', 'external_port_range': '22000:22010',
                              'description': 'another-service'}]
        with self.assertRaisesRegex(AccessError, 'already owned'):
            self.adapter.ensure(self.server)

    def test_lost_creation_response_recovers_owned_rule_without_leaking_details(self):
        self.post_fail = True
        with self.assertRaises(AccessError) as result:
            self.adapter.ensure(self.server)
        self.assertNotIn('private-backend', str(result.exception))
        self.assertEqual(len(self.forwardings), 1)
        self.adapter.remove(self.server)
        self.assertFalse(self.forwardings)

    def test_cleanup_failure_reported_and_retry_preserves_bootstrap_rule(self):
        self.adapter.ensure(self.server)
        self.delete_fail = True
        with self.assertRaises(AccessError):
            self.adapter.remove(self.server)
        self.assertEqual(len(self.forwardings), 1)
        self.delete_fail = False
        self.adapter.remove(self.server)
        self.assertFalse(self.forwardings)
        self.assertEqual(len(self.rules), 1)

    def test_modified_owned_rule_is_not_deleted(self):
        self.adapter.ensure(self.server)
        self.forwardings[0]['internal_port'] = 2222
        with self.assertRaisesRegex(AccessError, 'modified'):
            self.adapter.remove(self.server)
        self.assertEqual(len(self.forwardings), 1)

    def test_authentication_redirect_does_not_follow_or_leak_secret(self):
        self.redirect_auth = True
        with self.assertRaisesRegex(AccessError, 'authentication failed'):
            self.adapter.ensure(self.server)
        self.assertEqual(len(self.requests), 1)

    def test_rejected_service_token_refreshes_once(self):
        self.resource_unauthorized = 1
        self.adapter.ensure(self.server)
        self.assertEqual(sum(r.url.path == '/v3/auth/tokens' for r in self.requests), 2)
        self.assertEqual(len(self.forwardings), 1)

    def test_repeated_unauthorized_is_bounded(self):
        self.resource_unauthorized = 10
        with self.assertRaises(AccessError):
            self.adapter.ensure(self.server)
        self.assertEqual(sum(r.url.path == '/v3/auth/tokens' for r in self.requests), 2)
        self.assertFalse(self.forwardings)

    def test_cleanup_does_not_require_a_running_server(self):
        self.adapter.ensure(self.server)
        self.nova_server['status'] = 'ERROR'
        self.requests.clear()
        self.adapter.remove(self.server)
        self.assertFalse(any(r.url.path.startswith('/servers/') for r in self.requests))
        self.assertFalse(self.forwardings)


if __name__ == '__main__':
    unittest.main()
