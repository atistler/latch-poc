"""Cloud-init platform policy shared by vendor data and the static CLI."""


def cloud_config(ca):
    return {
        'users': ['default', {'name': 'latch-other', 'lock_passwd': True, 'shell': '/bin/bash'}],
        'ssh_pwauth': False,
        'disable_root': True,
        'write_files': [
            {'path': '/etc/systemd/timesyncd.conf.d/latch.conf', 'permissions': '0644',
             'content': '[Time]\nNTP=100.64.10.1\nFallbackNTP=\n'
                        'PollIntervalMinSec=16\nPollIntervalMaxSec=32\n'},
            {'path': '/etc/ssh/latch-user-ca.pub', 'permissions': '0644', 'content': ca.strip() + '\n'},
            {'path': '/etc/ssh/sshd_config.d/00-latch-certificates.conf', 'permissions': '0644',
             'content': 'TrustedUserCAKeys /etc/ssh/latch-user-ca.pub\n'
                        'AuthorizedKeysFile none\nAuthorizedKeysCommand none\n'
                        'PasswordAuthentication no\nKbdInteractiveAuthentication no\n'
                        'AuthenticationMethods publickey\nPermitRootLogin no\n'
                        'AllowUsers ubuntu latch-other\n'},
        ],
        'runcmd': [['systemctl', 'enable', '--now', 'systemd-timesyncd'],
                   ['systemctl', 'restart', 'systemd-timesyncd'],
                   ['sshd', '-t'], ['systemctl', 'restart', 'ssh']],
    }
