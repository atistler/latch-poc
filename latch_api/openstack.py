"""Project-scoped Nova/Neutron adapter for Latch-owned SSH reachability.

The grant coordinator serializes ensure/remove calls per instance. Inventory,
endpoint overrides and application credentials are operator-controlled inputs.
"""
from datetime import datetime, timedelta, timezone
import ipaddress
import threading
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from latch_api.device_access import AccessError


class OpenStackNetwork:
    def __init__(self, config, *, transport=None):
        self.auth_url = config['auth_url'].rstrip('/')
        if not self.auth_url.endswith('/v3'):
            raise ValueError('OpenStack auth_url must end with /v3')
        self.credential = {'id': config['application_credential_id'],
                           'secret': config['application_credential_secret']}
        self.region = config.get('region_name', 'RegionOne')
        self.interface = config.get('interface', 'public')
        self.overrides = config.get('endpoint_overrides', {})
        self.gateway = str(ipaddress.IPv4Address(config.get('gateway_ip', '100.64.10.10'))) + '/32'
        self._validate_url(self.auth_url)
        for endpoint in self.overrides.values():
            self._validate_url(endpoint)
        self.client = httpx.Client(transport=transport, timeout=10, follow_redirects=False,
                                   trust_env=False, verify=config.get('ca_file', True))
        self.token = None
        self.expires = datetime.min.replace(tzinfo=timezone.utc)
        self.project_id = None
        self.endpoints = {}
        self.lock = threading.RLock()

    @staticmethod
    def _validate_url(value):
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('OpenStack endpoints must be HTTP(S) URLs without embedded credentials')
        if parsed.query or parsed.fragment:
            raise ValueError('OpenStack endpoints cannot contain a query or fragment')

    def _authenticate(self):
        try:
            response = self.client.post(self.auth_url + '/auth/tokens', json={
                'auth': {'identity': {'methods': ['application_credential'],
                                     'application_credential': self.credential}}})
            response.raise_for_status()
            result = response.json()['token']
            token = response.headers['X-Subject-Token']
            project = str(UUID(result['project']['id']))
            expires = datetime.fromisoformat(result['expires_at'].replace('Z', '+00:00'))
            endpoints = {}
            for kind in ('compute', 'network'):
                matches = [e['url'] for service in result['catalog'] if service['type'] == kind
                           for e in service['endpoints']
                           if e['interface'] == self.interface and e.get('region') == self.region]
                if kind in self.overrides:
                    endpoint = self.overrides[kind]
                elif len(matches) == 1:
                    endpoint = matches[0]
                else:
                    raise ValueError('Ambiguous service catalog')
                self._validate_url(endpoint)
                endpoints[kind] = endpoint.rstrip('/')
            if self.project_id is not None and self.project_id != project:
                raise ValueError('Application credential changed projects')
            self.token, self.project_id, self.expires, self.endpoints = token, project, expires, endpoints
        except Exception:
            raise AccessError('OpenStack authentication failed') from None

    def _request(self, method, kind, path, payload=None, *, missing_ok=False, _retry=True):
        with self.lock:
            if not self.token or self.expires <= datetime.now(timezone.utc) + timedelta(seconds=30):
                self._authenticate()
            endpoint, token = self.endpoints[kind], self.token
        # Neutron catalogs normally advertise a bare endpoint; some include v2.0.
        if kind == 'network' and not endpoint.endswith('/v2.0'):
            endpoint += '/v2.0'
        try:
            response = self.client.request(method, endpoint + path, json=payload,
                                           headers={'X-Auth-Token': token})
            if response.status_code == 401 and _retry:
                with self.lock:
                    self._authenticate()
                return self._request(method, kind, path, payload, missing_ok=missing_ok, _retry=False)
            if missing_ok and response.status_code == 404:
                return None
            response.raise_for_status()
            if 300 <= response.status_code < 400:
                raise ValueError('Redirect rejected')
            return response.json() if response.content else {}
        except Exception:
            raise AccessError('OpenStack resource operation failed') from None

    def _project(self, project_id):
        with self.lock:
            if not self.token or self.expires <= datetime.now(timezone.utc) + timedelta(seconds=30):
                self._authenticate()
            if str(UUID(project_id)) != self.project_id:
                raise AccessError('Server project does not match service credential', 403)

    @staticmethod
    def _marker(server):
        return 'latch-server:' + str(UUID(server.server_id))

    @classmethod
    def _owned_marker(cls, server, description):
        # Existing forwarding/security rules remain owned after renaming.
        return description in (cls._marker(server), 'latch-vm:' + str(UUID(server.server_id)))

    @staticmethod
    def _uuid(value):
        return str(UUID(value))

    def _fip(self, server, *, missing_ok=False):
        self._project(server.project_id)
        result = self._request('GET', 'network', '/floatingips/' + self._uuid(server.floating_ip_id),
                               missing_ok=missing_ok)
        if result is None:
            return None
        fip = result['floatingip']
        if (fip['project_id'] != server.project_id or fip['floating_ip_address'] != server.host
                or fip.get('port_id')):
            raise AccessError('Floating IP does not match isolated server inventory', 409)
        return fip

    def _verify(self, server):
        self._project(server.project_id)
        instance = self._request('GET', 'compute', '/servers/' + self._uuid(server.server_id))['server']
        if instance['tenant_id'] != server.project_id or instance.get('status') != 'ACTIVE':
            raise AccessError('Server project or running state does not match inventory', 409)
        port = self._request('GET', 'network', '/ports/' + self._uuid(server.port_id))['port']
        if (port['project_id'] != server.project_id or port['device_id'] != server.server_id
                or not port.get('port_security_enabled', False)
                or not any(ip['ip_address'] == server.internal_ip for ip in port['fixed_ips'])
                or server.security_group_id not in port['security_groups']):
            raise AccessError('Server network port does not match trusted inventory', 409)
        self._fip(server)
        allowed = False
        for group_id in port['security_groups']:
            group = self._request('GET', 'network', '/security-groups/' + self._uuid(group_id))['security_group']
            if group['project_id'] != server.project_id:
                raise AccessError('Server security group belongs to another project', 409)
            for rule in group['security_group_rules']:
                if not self._permits_ssh(rule):
                    continue
                if not self._gateway_only(rule):
                    raise AccessError('Server SSH security groups must allow only the gateway /32', 409)
                allowed = True
        return allowed

    @staticmethod
    def _permits_ssh(rule):
        if rule['direction'] != 'ingress' or rule.get('protocol') not in (None, 'tcp', '6', 6):
            return False
        low, high = rule.get('port_range_min'), rule.get('port_range_max')
        return low is None or high is None or low <= 22 <= high

    def _gateway_only(self, rule):
        return (rule.get('ethertype') == 'IPv4' and rule.get('remote_ip_prefix') == self.gateway
                and rule.get('protocol') in ('tcp', '6', 6)
                and rule.get('port_range_min') == 22 and rule.get('port_range_max') == 22
                and not rule.get('remote_group_id') and not rule.get('remote_address_group_id'))

    @staticmethod
    def _forwarding_path(server):
        return '/floatingips/' + str(UUID(server.floating_ip_id)) + '/port_forwardings'

    def _matches(self, server, rule):
        return (self._owned_marker(server, rule.get('description')) and rule.get('protocol') == 'tcp'
                and rule.get('external_port') == server.port and rule.get('internal_port') == 22
                and rule.get('internal_port_id') == server.port_id
                and rule.get('internal_ip_address') == server.internal_ip)

    def ensure(self, server):
        """Open only the inventory forwarding; compatible preexisting SG rules remain owned by bootstrap."""
        allowed = self._verify(server)
        marker = self._marker(server)
        if not allowed:
            self._request('POST', 'network', '/security-group-rules', {'security_group_rule': {
                'security_group_id': server.security_group_id, 'direction': 'ingress',
                'ethertype': 'IPv4', 'protocol': 'tcp', 'port_range_min': 22,
                'port_range_max': 22, 'remote_ip_prefix': self.gateway, 'description': marker}})
        path = self._forwarding_path(server)
        rules = self._request('GET', 'network', path)['port_forwardings']
        owned = [rule for rule in rules if self._owned_marker(server, rule.get('description'))]
        if any(not self._matches(server, rule) for rule in owned):
            raise AccessError('Latch forwarding ownership conflicts with inventory', 409)
        conflicts = [rule for rule in rules if rule.get('protocol') == 'tcp'
                     and self._includes_external_port(rule, server.port) and not self._matches(server, rule)]
        if conflicts:
            raise AccessError('Server forwarding port is already owned by another rule', 409)
        if not owned:
            result = self._request('POST', 'network', path, {'port_forwarding': {
                'protocol': 'tcp', 'external_port': server.port, 'internal_port': 22,
                'internal_port_id': server.port_id, 'internal_ip_address': server.internal_ip,
                'description': marker}})
            if not self._matches(server, result['port_forwarding']):
                raise AccessError('OpenStack did not preserve forwarding ownership')

    @staticmethod
    def _includes_external_port(rule, port):
        if rule.get('external_port') == port:
            return True
        value = rule.get('external_port_range')
        if value:
            low, _, high = str(value).partition(':')
            return int(low) <= port <= int(high or low)
        return False

    def remove(self, server):
        """Remove only matching Latch resources, even if the server has been deleted."""
        if self._fip(server, missing_ok=True) is not None:
            path = self._forwarding_path(server)
            for rule in self._request('GET', 'network', path)['port_forwardings']:
                if not self._owned_marker(server, rule.get('description')):
                    continue
                if not self._matches(server, rule):
                    raise AccessError('Refusing cleanup of a modified Latch forwarding', 409)
                self._request('DELETE', 'network', path + '/' + self._uuid(rule['id']), missing_ok=True)
        group = self._request('GET', 'network', '/security-groups/' + self._uuid(server.security_group_id),
                              missing_ok=True)
        if group is None:
            return
        if group['security_group']['project_id'] != server.project_id:
            raise AccessError('Refusing cleanup of a foreign security group', 409)
        for rule in group['security_group']['security_group_rules']:
            if self._owned_marker(server, rule.get('description')):
                if not self._gateway_only(rule) or rule.get('port_range_min') != 22 or rule.get('port_range_max') != 22:
                    raise AccessError('Refusing cleanup of a modified Latch security rule', 409)
                self._request('DELETE', 'network', '/security-group-rules/' + self._uuid(rule['id']), missing_ok=True)
