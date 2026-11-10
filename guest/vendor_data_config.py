#!/usr/bin/env python3
"""Edit only Nova vendor-data settings; retain unrelated oslo INI verbatim."""
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

SETTINGS = {
    # Config-drive generation also enumerates legacy vendor_data.json. Keep its
    # provider enabled with no JSON file, so it yields {} and carries no CA.
    'vendordata_providers': 'StaticJSON,DynamicJSON',
    'vendordata_dynamic_targets': 'cloud-init@http://127.0.0.1:8300/v1/vendor-data',
    'vendordata_dynamic_connect_timeout': '3',
    'vendordata_dynamic_read_timeout': '5',
    'vendordata_dynamic_failure_fatal': 'true',
}
MANAGED = {*SETTINGS, 'vendordata_jsonfile_path'}
SECTION = re.compile(r'^\s*\[([^]]+)\]\s*(?:[#;].*)?$')
OPTION = re.compile(r'^\s*([\w.-]+)\s*[=:]')


def configure_text(text):
    output = []
    in_api = False
    inserted = False
    skip_continuation = False

    def insert_settings():
        if output and not output[-1].endswith('\n'):
            output[-1] += '\n'
        output.extend(f'{key} = {value}\n' for key, value in SETTINGS.items())

    for line in text.splitlines(keepends=True):
        section = SECTION.match(line.rstrip('\r\n'))
        if section:
            if in_api and not inserted:
                insert_settings()
                inserted = True
            in_api = section.group(1).lower() == 'api'
            skip_continuation = False
            output.append(line)
            continue
        option = OPTION.match(line)
        if in_api and option and option.group(1).lower() in MANAGED:
            skip_continuation = True
            continue
        if (skip_continuation and line[:1].isspace() and line.strip()
                and not option and not line.lstrip().startswith(('#', ';'))):
            continue
        skip_continuation = False
        output.append(line)
    if not inserted:
        if not in_api:
            if output and not output[-1].endswith('\n'):
                output[-1] += '\n'
            output.append('[api]\n')
        insert_settings()
    return ''.join(output)


def configure_file(path, backup):
    path, backup = Path(path), Path(backup)
    before = path.read_text()
    after = configure_text(before)
    if before == after:
        return False
    backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup.parent.chmod(0o700)
    try:
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(before)
            stream.flush()
            os.fsync(stream.fileno())
    original = path.stat()
    descriptor, pending = tempfile.mkstemp(prefix='.latch-nova-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(after)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), stat.S_IMODE(original.st_mode))
            if (os.getuid(), os.getgid()) != (original.st_uid, original.st_gid):
                os.fchown(stream.fileno(), original.st_uid, original.st_gid)
        os.replace(pending, path)
    finally:
        if os.path.exists(pending):
            os.unlink(pending)
    return True


if __name__ == '__main__':
    sys.exit(10 if configure_file(sys.argv[1], sys.argv[2]) else 0)
