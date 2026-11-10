"""Render operator-controlled platform policy using the shared cloud-init renderer."""
import json
import re

from latch_common.cloud_init import cloud_config

def render(ca, fingerprint, revision):
    if not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', revision):
        raise ValueError('LATCH_PROFILE_REVISION must contain 1–64 letters, digits, dots, underscores or hyphens')
    config = cloud_config(ca)
    config['write_files'].append({
        'path': '/etc/latch/bootstrap.json', 'permissions': '0644',
        'content': json.dumps({'schema_version': 1, 'profile_revision': revision,
                               'ssh_ca_fingerprint': fingerprint}, indent=2) + '\n',
    })
    return '#cloud-config\n' + json.dumps(config, indent=2)
