#!/usr/bin/env python3
"""Fail-closed LDAP MAVIS backend; protocol constants come from upstream mavis.h.

No passwords are cached or logged. INFO queries recheck account existence and the
single group role. AUTH additionally performs a fresh bind as the user's DN.
"""
import json
from pathlib import Path
import re
import socket
import sys

import ldap
from ldap.filter import escape_filter_chars

BASE = "dc=latch,dc=lab"
ROLES = {"readonly", "operator", "admin"}


def connect():
    connection = ldap.initialize("ldaps://openldap:636")
    connection.set_option(ldap.OPT_NETWORK_TIMEOUT, 2)
    connection.set_option(ldap.OPT_TIMEOUT, 2)
    connection.set_option(ldap.OPT_REFERRALS, 0)
    connection.set_option(ldap.OPT_X_TLS_CACERTFILE, "/etc/latch-tls/ca.pem")
    connection.set_option(ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_DEMAND)
    connection.set_option(ldap.OPT_X_TLS_NEWCTX, 0)
    return connection


def reader():
    connection = connect()
    try:
        password = Path("/run/device-secrets/ldap-reader-password").read_text().strip()
        if not password:
            raise ValueError("empty service credential")
        connection.simple_bind_s("cn=reader,ou=services," + BASE, password)
        return connection
    except Exception:
        connection.unbind_s()
        raise


def check(username, password=None):
    if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,128}", username):
        raise ValueError("invalid username")
    connection = reader()
    try:
        users = connection.search_s("ou=users," + BASE, ldap.SCOPE_ONELEVEL,
                                    "(&(objectClass=inetOrgPerson)(uid=" + escape_filter_chars(username) + "))", ["uid"])
        if len(users) != 1 or not users[0][0]:
            raise ValueError("unknown or ambiguous account")
        user_dn = users[0][0]
        groups = connection.search_s("ou=groups," + BASE, ldap.SCOPE_ONELEVEL,
                                     "(&(objectClass=groupOfNames)(member=" + escape_filter_chars(user_dn) + "))", ["cn"])
        roles = [value.decode() for dn, attrs in groups if dn for value in attrs.get("cn", []) if value.decode() in ROLES]
        if len(roles) != 1:
            raise ValueError("missing or ambiguous role")
        role = roles[0]
    finally:
        connection.unbind_s()
    if password is not None:
        if not isinstance(password, str) or not password or "\x00" in password:
            raise ValueError("invalid password")
        connection = connect()
        try:
            connection.simple_bind_s(user_dn, password)
        finally:
            connection.unbind_s()
    return role


def mavis():
    attributes = {}
    for line in sys.stdin:
        if not line.startswith("="):
            key, separator, value = line.rstrip("\n").partition(" ")
            if separator and key.isdecimal():
                attributes[int(key)] = value
            continue
        # Never trust inbound role/profile/result attributes.
        for key in (6, 47):
            attributes.pop(key, None)
        attributes[6] = "NAK"
        try:
            if attributes.get(0) != "TACPLUS" or attributes.get(49) not in {"AUTH", "INFO"}:
                raise ValueError("unsupported request")
            password = attributes.get(8, "") if attributes[49] == "AUTH" else None
            role = check(attributes.get(4), password)
            attributes[47] = '"' + role + '"'
            attributes[6] = "ACK"
        except Exception:
            # LDAP failures, deletion, invalid passwords and ambiguous roles deny.
            pass
        for key, value in sorted(attributes.items()):
            print(f"{key} {value}")
        print("=0", flush=True)
        attributes = {}


if __name__ == "__main__":
    if "--health" in sys.argv:
        try:
            reader().unbind_s()
            with socket.create_connection(("127.0.0.1", 49), timeout=1):
                pass
        except Exception:
            sys.exit(1)
    elif "--check" in sys.argv:
        try:
            request = json.load(sys.stdin)
            role = check(request["username"], request["password"])
            print(json.dumps({"authenticated": True, "role": role}))
        except Exception:
            print(json.dumps({"authenticated": False, "role": None}))
            sys.exit(1)
    else:
        mavis()
