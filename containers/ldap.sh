#!/bin/bash
set -euo pipefail
umask 077
mkdir -p /var/lib/ldap /etc/latch-tls /etc/latch-ca
for secret in ldap-admin-password ldap-openbao-password ldap-reader-password; do
    [[ -s /run/device-secrets/$secret ]] || { echo "Missing device bootstrap secret: $secret" >&2; exit 1; }
    # Secret writers may append a text newline; LDAP bind consumers strip it.
    tr -d '\r\n' < /run/device-secrets/"$secret" > /run/"$secret"
done
if [[ ! -s /etc/latch-tls/ca.pem ]]; then
    openssl req -x509 -newkey rsa:3072 -nodes -days 3650 -subj '/CN=Latch POC LDAP CA' -keyout /etc/latch-tls/ca.key -out /etc/latch-tls/ca.pem 2>/dev/null
fi
if [[ ! -s /etc/latch-tls/server.pem ]]; then
    openssl req -new -newkey rsa:3072 -nodes -subj '/CN=openldap' -keyout /etc/latch-tls/server.key -out /etc/latch-tls/server.csr 2>/dev/null
    printf 'subjectAltName=DNS:openldap,DNS:localhost\nextendedKeyUsage=serverAuth\n' > /etc/latch-tls/extensions
    openssl x509 -req -in /etc/latch-tls/server.csr -CA /etc/latch-tls/ca.pem -CAkey /etc/latch-tls/ca.key -CAcreateserial -days 825 -extfile /etc/latch-tls/extensions -out /etc/latch-tls/server.pem 2>/dev/null
fi
cp /etc/latch-tls/ca.pem /etc/latch-ca/ca.pem
chmod 644 /etc/latch-ca/ca.pem
admin_hash=$(slappasswd -T /run/ldap-admin-password)
cat > /etc/ldap/slapd.conf <<EOF
include /etc/ldap/schema/core.schema
include /etc/ldap/schema/cosine.schema
include /etc/ldap/schema/inetorgperson.schema
pidfile /run/slapd/slapd.pid
argsfile /run/slapd/slapd.args
modulepath /usr/lib/ldap
moduleload back_mdb
moduleload ppolicy
TLSCACertificateFile /etc/latch-tls/ca.pem
TLSCertificateFile /etc/latch-tls/server.pem
TLSCertificateKeyFile /etc/latch-tls/server.key
TLSProtocolMin 3.3
security simple_bind=128
database mdb
maxsize 1073741824
suffix "dc=latch,dc=lab"
rootdn "cn=admin,dc=latch,dc=lab"
rootpw $admin_hash
directory /var/lib/ldap
index objectClass eq
index uid,cn eq
access to dn.subtree="ou=users,dc=latch,dc=lab" attrs=userPassword
  by dn.exact="cn=openbao,ou=services,dc=latch,dc=lab" write
  by anonymous auth
  by * none
access to attrs=userPassword
  by anonymous auth
  by * none
access to dn.subtree="ou=users,dc=latch,dc=lab"
  by dn.exact="cn=openbao,ou=services,dc=latch,dc=lab" write
  by dn.exact="cn=reader,ou=services,dc=latch,dc=lab" read
  by * none
access to dn.regex="^cn=(admin|operator|readonly),ou=groups,dc=latch,dc=lab$" attrs=member
  by dn.exact="cn=openbao,ou=services,dc=latch,dc=lab" write
  by dn.exact="cn=reader,ou=services,dc=latch,dc=lab" read
  by * none
access to *
  by dn.exact="cn=reader,ou=services,dc=latch,dc=lab" read
  by dn.exact="cn=openbao,ou=services,dc=latch,dc=lab" read
  by * none
overlay ppolicy
ppolicy_hash_cleartext
EOF
mkdir -p /run/slapd
if [[ ! -f /var/lib/ldap/data.mdb ]]; then
    bao_hash=$(slappasswd -T /run/ldap-openbao-password)
    reader_hash=$(slappasswd -T /run/ldap-reader-password)
    cat > /run/slapd/bootstrap.ldif <<EOF
dn: dc=latch,dc=lab
objectClass: dcObject
objectClass: organization
dc: latch
o: Latch POC

dn: ou=services,dc=latch,dc=lab
objectClass: organizationalUnit
ou: services

dn: ou=users,dc=latch,dc=lab
objectClass: organizationalUnit
ou: users

dn: ou=groups,dc=latch,dc=lab
objectClass: organizationalUnit
ou: groups

dn: cn=openbao,ou=services,dc=latch,dc=lab
objectClass: organizationalRole
objectClass: simpleSecurityObject
cn: openbao
userPassword: $bao_hash

dn: cn=reader,ou=services,dc=latch,dc=lab
objectClass: organizationalRole
objectClass: simpleSecurityObject
cn: reader
userPassword: $reader_hash

dn: cn=operator,ou=groups,dc=latch,dc=lab
objectClass: groupOfNames
cn: operator
member: cn=sentinel,ou=services,dc=latch,dc=lab

dn: cn=readonly,ou=groups,dc=latch,dc=lab
objectClass: groupOfNames
cn: readonly
member: cn=sentinel,ou=services,dc=latch,dc=lab

dn: cn=admin,ou=groups,dc=latch,dc=lab
objectClass: groupOfNames
cn: admin
member: cn=sentinel,ou=services,dc=latch,dc=lab
EOF
    slapadd -f /etc/ldap/slapd.conf -l /run/slapd/bootstrap.ldif
    rm /run/slapd/bootstrap.ldif
fi
# Existing databases retain their password hashes and leased users. Rename the
# former service identity over authenticated, CA-verified LDAPS before readiness.
slapd -f /etc/ldap/slapd.conf -h 'ldaps://0.0.0.0:636/' -d 0 &
pid=$!
trap 'kill "$pid" 2>/dev/null || true; wait "$pid" || true' EXIT
trap 'exit 0' TERM INT
export LDAPTLS_CACERT=/etc/latch-tls/ca.pem LDAPTLS_REQCERT=demand
admin=(-x -H ldaps://openldap:636 -D cn=admin,dc=latch,dc=lab -y /run/ldap-admin-password)
ready=false
for attempt in {1..60}; do
    kill -0 "$pid" 2>/dev/null || { echo 'LDAP server exited during bootstrap' >&2; exit 1; }
    if ldapwhoami "${admin[@]}" >/dev/null 2>&1; then
        ready=true
        break
    fi
    sleep 1
done
[[ "$ready" == true ]] || { echo 'LDAP admin bind did not become ready' >&2; exit 1; }

migrate_reader_identity() {
    # This legacy DN is referenced only to migrate persisted directory data.
    local old_dn='cn=ise,ou=services,dc=latch,dc=lab'
    local new_dn='cn=reader,ou=services,dc=latch,dc=lab'
    local entries old_present=false new_present=false
    entries=$(ldapsearch "${admin[@]}" -LLL -o ldif-wrap=no -b ou=services,dc=latch,dc=lab -s one '(objectClass=*)' dn)
    case $'\n'"$entries"$'\n' in *$'\n'"dn: $old_dn"$'\n'*) old_present=true ;; esac
    case $'\n'"$entries"$'\n' in *$'\n'"dn: $new_dn"$'\n'*) new_present=true ;; esac
    if [[ "$old_present" == true && "$new_present" == true ]]; then
        echo 'Ambiguous LDAP reader identities; refusing to change persisted directory' >&2
        return 1
    fi
    if [[ "$old_present" == true ]]; then
        ldapmodrdn "${admin[@]}" -r "$old_dn" cn=reader
    elif [[ "$new_present" != true ]]; then
        echo 'LDAP reader identity is missing; refusing to reset directory' >&2
        return 1
    fi
    ldapwhoami -x -H ldaps://openldap:636 -D "$new_dn" -y /run/ldap-reader-password >/dev/null
}

ensure_role_groups() {
    # Add missing role groups only; existing memberships and credentials survive.
    local role entries dn
    entries=$(ldapsearch "${admin[@]}" -LLL -o ldif-wrap=no -b ou=groups,dc=latch,dc=lab -s one '(objectClass=*)' dn)
    for role in admin operator readonly; do
        dn="cn=$role,ou=groups,dc=latch,dc=lab"
        case $'\n'"$entries"$'\n' in
            *$'\n'"dn: $dn"$'\n'*) continue ;;
        esac
        ldapadd "${admin[@]}" <<EOF
dn: $dn
objectClass: groupOfNames
cn: $role
member: cn=sentinel,ou=services,dc=latch,dc=lab
EOF
    done
}
migrate_reader_identity
ensure_role_groups
wait "$pid"
