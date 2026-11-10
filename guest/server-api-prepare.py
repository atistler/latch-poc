"""Bootstrap project-scoped Server API credentials; run with sourced OpenStack admin env.

Only resource identifiers are printed. The application credential stays in an
owner-only file inside the Linux host, never in CLI arguments or laptop state.
"""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys


def os_cli(*arguments, env=None):
    result = subprocess.run(['openstack', *arguments, '-f', 'json'], env=env,
                            capture_output=True, text=True)
    if result.returncode:
        detail = result.stderr.strip()
        for name, value in (env or os.environ).items():
            if any(word in name for word in ('PASSWORD', 'SECRET', 'TOKEN')) and value:
                detail = detail.replace(value, '[redacted]')
        raise RuntimeError('OpenStack bootstrap operation failed: ' + ' '.join(arguments[:3]) + ': ' + detail[:1000])
    return json.loads(result.stdout)


def prepare():
    directory = Path('/home/stack/latch/.state/vm')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    server = os_cli('server', 'show', 'latch-vm')
    port = os_cli('port', 'show', 'latch-vm-port')
    group = os_cli('security', 'group', 'show', 'latch-ssh')
    floating_ips = os_cli('floating', 'ip', 'list', '--floating-ip-address', '100.64.10.120')
    if len(floating_ips) != 1 or port['device_id'] != server['id']:
        raise RuntimeError('VM inventory does not match provisioned resources')
    credential_file = directory / 'openstack.json'
    if not credential_file.exists():
        password = secrets.token_urlsafe(40)
        users = [u for u in os_cli('user', 'list', '--domain', 'default')
                 if u.get('Name', u.get('name')) == 'latch-server-api']
        user = users[0] if users else os_cli('user', 'create', '--domain', 'default', 'latch-server-api')
        user_id = user.get('id', user.get('ID'))
        # Passing the password in an HTTP request body avoids process-list disclosure.
        from keystoneauth1 import session
        from keystoneauth1.identity import v3
        import requests
        identity = os.environ['OS_AUTH_URL'].rstrip('/')
        if not identity.endswith('/v3'):
            identity += '/v3'
        admin_auth = v3.Password(auth_url=identity,
                                username=os.environ['OS_USERNAME'], password=os.environ['OS_PASSWORD'],
                                project_name=os.environ['OS_PROJECT_NAME'],
                                user_domain_name=os.environ.get('OS_USER_DOMAIN_NAME', 'Default'),
                                project_domain_name=os.environ.get('OS_PROJECT_DOMAIN_NAME', 'Default'))
        admin = session.Session(auth=admin_auth)
        result = requests.patch(identity + '/users/' + user_id,
                                headers={'X-Auth-Token': admin.get_token()},
                                json={'user': {'password': password}}, timeout=20)
        result.raise_for_status()
        subprocess.run(['openstack', 'role', 'add', '--project', server['project_id'],
                        '--user', user_id, 'member'], check=True, stdout=subprocess.DEVNULL)
        environment = {key: value for key, value in os.environ.items() if not key.startswith('OS_')}
        environment.update(OS_AUTH_URL=identity, OS_AUTH_TYPE='v3password',
                           OS_USERNAME='latch-server-api', OS_PASSWORD=password,
                           OS_USER_DOMAIN_NAME='Default', OS_PROJECT_DOMAIN_NAME='Default',
                           OS_PROJECT_ID=server['project_id'], OS_IDENTITY_API_VERSION='3')
        credential = os_cli('application', 'credential', 'create', 'latch-server-api-' + secrets.token_hex(6),
                            '--role', 'member', env=environment)
        config = {'auth_url': identity, 'application_credential_id': credential.get('id', credential.get('ID')),
                  'application_credential_secret': credential.get('secret', credential.get('Secret')), 'region_name': 'RegionOne'}
        if not config['application_credential_id'] or not config['application_credential_secret']:
            raise RuntimeError('OpenStack returned incomplete application credentials')
        with open(credential_file, 'x', opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
            json.dump(config, stream)
    inventory = {'server_id': server['id'], 'project_id': server['project_id'],
                      'port_id': port['id'], 'floating_ip_id': floating_ips[0]['ID'],
                      'host': '100.64.10.120', 'port': 22001,
                      'internal_ip': '192.168.20.15', 'security_group_id': group['id'],
                 'login': 'ubuntu'}
    if '--retire-legacy' in sys.argv:
        rules = os_cli('floating', 'ip', 'port', 'forwarding', 'list', inventory['floating_ip_id'])
        for rule in rules:
            # Migrate only the exact old lab SSH path; never delete unrelated
            # forwarding rules or rules already owned by the API.
            if (rule['External Port'] == 22001 and rule['Internal Port'] == 22
                    and rule['Internal Port ID'] == inventory['port_id']
                    and rule['Internal IP Address'] == inventory['internal_ip']
                    and rule['Protocol'] == 'tcp' and not rule.get('Description')):
                subprocess.run(['openstack', 'floating', 'ip', 'port', 'forwarding', 'delete',
                                inventory['floating_ip_id'], rule['ID']], check=True,
                               stdout=subprocess.DEVNULL)
    print(json.dumps(inventory))


if __name__ == '__main__':
    try:
        prepare()
    except Exception as error:
        # Upstream SDK exceptions may contain credential material.
        print('Server API inventory/credential bootstrap failed (' + type(error).__name__ + ')', file=sys.stderr)
        if isinstance(error, RuntimeError):
            print(str(error), file=sys.stderr)
        sys.exit(1)
