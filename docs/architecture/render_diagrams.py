#!/usr/bin/env python3
"""Render editable, self-contained infrastructure infographics (standard library only).

Run: python3 docs/architecture/render_diagrams.py
Optional PNG: rsvg-convert -w 1800 network-architecture.svg -o preview.png
All coordinates, labels and paths live here; no external images/fonts are loaded.
"""
from html import escape
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
NAVY = '#102b63'
BLUE = '#087ff5'
ORANGE = '#e85527'
PURPLE = '#8039d9'
MUTED = '#455b78'


class Diagram:
    def __init__(self, title, subtitle, height=1290):
        self.height = height
        self.parts = [f'''<svg xmlns="http://www.w3.org/2000/svg" width="1800" height="{height}" viewBox="0 0 1800 {height}" role="img" aria-labelledby="title desc">
<title id="title">{escape(title)}</title><desc id="desc">{escape(subtitle)}</desc>
<defs><linearGradient id="blueBand" x2="0" y2="1"><stop stop-color="#f4fbff"/><stop offset="1" stop-color="#eaf6ff"/></linearGradient><linearGradient id="purpleBand" x2="0" y2="1"><stop stop-color="#fbf9ff"/><stop offset="1" stop-color="#f0eaff"/></linearGradient><linearGradient id="greenBand" x2="0" y2="1"><stop stop-color="#f3fff9"/><stop offset="1" stop-color="#e8faf1"/></linearGradient>
<filter id="shadow" x="-10%" y="-15%" width="120%" height="140%"><feDropShadow dx="0" dy="2" stdDeviation="3" flood-color="#102b63" flood-opacity="0.09"/></filter>''']
        for key, color in [('blue', BLUE), ('orange', ORANGE), ('purple', PURPLE)]:
            self.parts.append(f'<marker id="{key}Arrow" markerWidth="10" markerHeight="10" refX="8" refY="5" orient="auto" markerUnits="userSpaceOnUse"><path d="M0 0L9 5L0 10Z" fill="{color}"/></marker>')
        self.parts.append('</defs><rect width="1800" height="100%" fill="white"/>')
        self.text(900, 49, title, 36, weight=700, anchor='middle')
        self.text(900, 84, subtitle, 19, anchor='middle', fill=MUTED)

    def rect(self, x, y, w, h, fill='white', stroke='#b8c9e2', radius=12, dash=None, shadow=False):
        extra = f' stroke-dasharray="{dash}"' if dash else ''
        if shadow:
            extra += ' filter="url(#shadow)"'
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}" stroke="{stroke}" stroke-width="1.4"{extra}/>')

    def text(self, x, y, content, size=18, weight=400, fill=NAVY, anchor='start', line=25):
        lines = content.split('\n')
        self.parts.append(f'<text x="{x}" y="{y}" font-family="Arial, Helvetica, sans-serif" font-size="{size}" font-weight="{weight}" fill="{fill}" text-anchor="{anchor}">')
        for i, value in enumerate(lines):
            self.parts.append(f'<tspan x="{x}" dy="{0 if i == 0 else line}">{escape(value)}</tspan>')
        self.parts.append('</text>')

    def band(self, y, h, n, title, desc, color):
        fills = {'blue': ('url(#blueBand)', '#90caff'), 'purple': ('url(#purpleBand)', '#c3a3f1'), 'green': ('url(#greenBand)', '#88d6b2')}
        self.rect(18, y, 1764, h, *fills[color])
        self.parts.append(f'<circle cx="53" cy="{y + 40}" r="19" fill="{NAVY}"/>')
        self.text(53, y + 47, str(n), 23, 700, 'white', 'middle')
        self.text(84, y + 47, title, 25, 700, line=30)
        self.text(42, y + 102, desc, 18, fill=MUTED, line=27)

    def icon(self, x, y, kind):
        self.rect(x, y, 34, 34, NAVY, NAVY, 7)
        paths = {
            'user': '<circle cx="17" cy="11" r="5"/><path d="M7 28v-5a10 10 0 0 1 20 0v5Z"/>',
            'server': '<path d="M8 6h18v22H8Z" fill="none" stroke="white" stroke-width="2"/><path d="M11 12h12M11 18h12M11 24h7" fill="none" stroke="white" stroke-width="2"/>',
            'key': '<circle cx="12" cy="12" r="6" fill="none" stroke="white" stroke-width="3"/><path d="M16 16l11 11m-5-5l4-4m-1 7l4-4" fill="none" stroke="white" stroke-width="3"/>',
            'api': '<path d="M12 9l-7 8 7 8m10-16l7 8-7 8M20 6l-6 22" fill="none" stroke="white" stroke-width="2.5"/>',
            'shield': '<path d="M17 5L6 9v9c0 5 7 10 11 12 4-2 11-7 11-12V9Z" fill="none" stroke="white" stroke-width="2"/><path d="M11 17l4 4 8-9" fill="none" stroke="white" stroke-width="2.5"/>',
            'network': '<path d="M17 5v24M5 17h24m-5-5l5 5-5 5M12 10l5-5 5 5M10 12l-5 5 5 5M12 24l5 5 5-5" fill="none" stroke="white" stroke-width="2"/>',
            'store': '<ellipse cx="17" cy="9" rx="11" ry="4"/><path d="M6 9v16c0 6 22 6 22 0V9M6 17c0 6 22 6 22 0" fill="none" stroke="white" stroke-width="2"/>',
            'clock': '<circle cx="17" cy="17" r="11" fill="none" stroke="white" stroke-width="2"/><path d="M17 9v9l6 4" fill="none" stroke="white" stroke-width="2"/>',
        }
        self.parts.append(f'<g transform="translate({x},{y})" fill="white">{paths.get(kind, paths["server"])}</g>')

    def card(self, x, y, w, h, title, body, icon='server', color=NAVY, title_size=21):
        self.rect(x, y, w, h, 'white', color, shadow=True)
        self.icon(x + 15, y + 16, icon)
        self.text(x + 62, y + 39, title, title_size, 700, line=25)
        title_lines = len(title.split('\n'))
        self.text(x + 18, y + 76 + 25 * (title_lines - 1), body, 17, fill=MUTED, line=24)

    def arrow(self, pts, color='blue', label=None, lx=None, ly=None, size=17):
        colors = {'blue': BLUE, 'orange': ORANGE, 'purple': PURPLE}
        c = colors[color]
        dash = ' stroke-dasharray="8 6"' if color == 'orange' else (' stroke-dasharray="4 5"' if color == 'purple' else '')
        self.parts.append(f'<polyline points="{" ".join(f"{x},{y}" for x,y in pts)}" fill="none" stroke="{c}" stroke-width="{3 if color == "blue" else 2.4}" marker-end="url(#{color}Arrow)"{dash}/>')
        if label:
            self.text(lx, ly, label, size, 600, c, 'middle', line=22)

    def footer(self, y, cards):
        for x, w, title, body, icon, color in cards:
            self.card(x, y, w, 124, title, body, icon, color, 20)
        ly = y + 165
        self.arrow([(180, ly - 5), (245, ly - 5)], 'orange')
        self.text(260, ly, 'Control / grant lifecycle', 17)
        self.arrow([(705, ly - 5), (770, ly - 5)], 'purple')
        self.text(785, ly, 'Trust / credentials / AAA', 17)
        self.arrow([(1250, ly - 5), (1315, ly - 5)], 'blue')
        self.text(1330, ly, 'SSH session traffic', 17)

    def save(self, name):
        self.parts.append('</svg>')
        content = '\n'.join(self.parts) + '\n'
        ET.fromstring(content)
        (ROOT / name).write_text(content)


def networks():
    d = Diagram('Latch — Server access and network architecture', 'Implemented OpenStack path · Warpgate SSH gateway · OpenBao certificates · Temporary Neutron forwarding')
    d.band(112, 245, 1, 'Request access', 'Control plane\n\nIssue a grant separately.\nConnect with its UUID.\nTTL: 1–300 seconds.', 'blue')
    d.card(350, 174, 300, 145, 'Engineer / CLI', './server-grant\n./server-connect --grant UUID\nLaptop private key stays local.', 'user')
    d.card(750, 160, 430, 175, 'Latch server API', 'Authorize trusted server inventory.\nCreate per-grant Warpgate admission.\nPersist grant / cleanup state in SQLite.\nReconcile revoke and expiry.', 'api')
    d.card(1280, 160, 470, 175, 'Nova + Neutron APIs', 'Validate instance, project and port ownership.\nEnsure restricted floating-IP forwarding.\nConcurrent grants share one network path.\nRemove it after the final grant closes.', 'network')
    d.arrow([(650, 230), (750, 230)], 'orange', 'Request', 700, 211)
    d.arrow([(1180, 230), (1280, 230)], 'orange', 'Validate /\nconfigure', 1230, 202, 16)
    d.band(373, 242, 2, 'Bootstrap trust', 'Provisioning time\n\nInstall public CA trust on\nnew OpenStack instances.\nCA private key stays in Bao.', 'purple')
    d.card(350, 433, 290, 154, 'OpenBao SSH CA', 'Public CA is exported.\nPrivate CA key never leaves\nOpenBao.', 'key', PURPLE)
    d.card(720, 433, 310, 154, 'Latch vendor-data API', 'Read the public CA.\nRender platform SSH policy.\nSeparate from the grant API.', 'api', PURPLE, 20)
    d.card(1110, 433, 285, 154, 'Nova / config drive', 'Dynamic vendor data\n→ cloud-init on first boot.\nReboot does not refresh it.', 'server', PURPLE, 20)
    d.card(1470, 433, 280, 154, 'Server sshd', 'TrustedUserCAKeys\nApproved principal: ubuntu\nNo target agent.', 'shield', PURPLE)
    d.arrow([(640, 507), (720, 507)], 'purple', 'Public CA', 680, 488, 15)
    d.arrow([(1030, 507), (1110, 507)], 'purple', 'Config', 1070, 488, 15)
    d.arrow([(1395, 507), (1470, 507)], 'purple', 'Trust', 1432, 488, 15)
    d.band(631, 438, 3, 'Use the grant', 'Runtime data plane\n\nWarpgate terminates SSH,\nchecks the target host key,\nand records the session.\n\nThe gateway is outside\nthe tenant network.', 'green')
    d.text(355, 684, 'OpenBao signs Warpgate’s target public key; Warpgate injects the short-lived ubuntu certificate.', 19, 600, PURPLE)
    d.rect(350, 725, 665, 309, '#f7fffb', '#6fba9b', dash='8 5')
    d.text(370, 757, 'OPENSTACK EXTERNAL / PROVIDER NETWORK\n100.64.10.0/24 · private', 18, 700)
    d.rect(1030, 725, 720, 309, '#f7fffb', '#6fba9b', dash='8 5')
    d.text(1050, 757, 'OPENSTACK NETWORKING / TENANT INFRASTRUCTURE', 18, 700)
    d.rect(1380, 777, 355, 210, '#ffffff', '#6fba9b', dash='5 4')
    d.text(1400, 801, 'TENANT  ·  192.168.20.0/24', 16, 700)
    d.card(380, 805, 300, 171, 'Warpgate gateway', 'Provider IP: 100.64.10.10\nIngress IP: 10.1.1.2\nManagement IP: 10.1.3.10\nNo direct tenant route.', 'shield')
    d.card(730, 805, 270, 171, 'Floating-IP endpoint', '100.64.10.120:22001\nRoutable from Warpgate.\nExternal/provider-facing.\nLogical endpoint, not an agent.', 'network', title_size=18)
    d.card(1050, 805, 290, 171, 'Neutron routing\n& DNAT', 'L3 agent or OVN\nCurrent POC: L3 agent\nPort forwarding to TCP 22', 'network', title_size=20)
    d.card(1400, 817, 320, 159, 'OpenStack VM', '192.168.20.15:22 · ubuntu\nSG: 100.64.10.10/32 only\nValidates the SSH certificate.\nSource remains gateway .10.', 'server')
    d.arrow([(680, 875), (730, 875)], 'blue', 'SSH', 705, 854, 14)
    d.arrow([(1000, 875), (1050, 875)], 'blue')
    d.arrow([(1340, 875), (1400, 875)], 'blue', 'DNAT', 1370, 854, 14)
    d.text(380, 1006, 'User → Warpgate → routable floating IP → Neutron forwarding → tenant SSH. No full VM-port association.', 17, 600)
    d.footer(1086, [(18, 555, 'Explicit grant lifecycle', 'requested → active → revoking → revoked\nDisconnecting leaves a grant active until revoke / expiry.', 'clock', NAVY), (591, 590, 'Revoke / expiry closes sessions', 'Latch removes admission and closes that grant’s SSH.\nThe final grant releases owned Neutron forwarding.', 'shield', NAVY), (1199, 583, 'Certificate expiry alone is insufficient', 'Certificate expiry blocks new authentication.\nAn API outage can delay active-session termination.', 'key', ORANGE)])
    d.save('network-architecture.svg')


def deployment():
    d = Diagram('Latch — Local infrastructure', 'Mac laptop → hardware-accelerated ARM64 Linux host → nested OpenStack VM', 1290)
    d.band(112, 235, 1, 'Laptop entry', 'Mac / local CLI\n\nServer grants are separate\nfrom SSH connection.\nKeys remain on the laptop.', 'blue')
    d.card(350, 169, 330, 142, 'Mac laptop', './server-grant\n./server-connect --grant UUID\nOpenSSH + gateway login key.', 'user')
    d.card(800, 153, 430, 174, 'Lima · latch-arm64', 'Ubuntu 24.04 ARM64 · Apple VZ\n6 CPUs · 8 GiB RAM · 80 GiB sparse disk\nSSH loopback 9202 → Warpgate 2222\nUI 9200 → 8888; OpenBao 8200 → 8200.', 'server')
    d.card(1350, 169, 400, 142, 'Private control channel', 'Grant commands use trusted Lima stdin.\nServer API: Linux loopback TCP 8320.\nVendor-data API: loopback TCP 8300.', 'shield')
    d.arrow([(680, 230), (800, 230)], 'blue', 'Local SSH', 740, 209)
    d.arrow([(1230, 230), (1350, 230)], 'orange', 'CLI request', 1290, 209)
    d.band(363, 279, 2, 'Host services', 'Inside the Lima Linux VM\n\nSeparate containers keep\ncredentials and duties apart.\nOpenStack runs on the host.', 'purple')
    d.card(350, 430, 310, 178, 'Warpgate container', 'Ingress: 10.1.1.2\nProvider: 100.64.10.10\nManagement: 10.1.3.10\nTarget key + recording volume.', 'shield')
    d.card(700, 430, 310, 178, 'Server API container', 'Management · TCP 8320\nScoped OpenStack credential\nDurable SQLite grant volume\nNo provider network interface.', 'api')
    d.card(1050, 430, 310, 178, 'OpenBao container', 'Management · TCP 8200\nSSH signer + public CA\nCA key + Raft state persist.\nPrivate CA key stays here.', 'key', PURPLE)
    d.card(1400, 430, 350, 178, 'Vendor-data container', 'Management · TCP 8300\nNova DynamicJSON endpoint\nReads the public CA only.\nNo grant / signing credentials.', 'api', PURPLE)
    d.text(355, 402, 'Gateway ingress: 10.1.1.0/24   ·   Service management: 10.1.3.0/24   ·   Other service IPs are dynamic.', 18, 600)
    d.band(658, 411, 3, 'OpenStack path', 'Controller + network +\ncompute on the Linux host\n\nOVS supplies Layer 2.\nThe L3 agent supplies NAT.\nWarpgate is outside tenant.', 'green')
    d.card(350, 730, 400, 153, 'Native OpenStack services', 'Keystone · Glance · Placement · Nova\nNeutron · MySQL · RabbitMQ\nlibvirt / QEMU · 172.20.2.15/24', 'server')
    d.card(840, 730, 400, 153, 'Provider L2 / OVS uplink', 'Docker latch-provider → veth pair\nOVS br-ex → br-int → qrouter qg-*\nbr-ex: 100.64.10.1/32', 'network')
    d.card(1330, 730, 420, 153, 'Neutron L3 namespace', 'qrouter: provider qg-* → tenant qr-*\n100.64.10.120:22001\nDNAT → 192.168.20.15:22', 'network')
    d.arrow([(750, 801), (840, 801)], 'orange', 'Program', 795, 710, 16)
    d.arrow([(1240, 801), (1330, 801)], 'blue', 'SSH', 1285, 780, 16)
    d.rect(350, 914, 1400, 124, '#f7fffb', '#6fba9b', dash='8 5')
    d.text(374, 947, 'NESTED TENANT VM  ·  192.168.20.15:22', 23, 700)
    d.text(374, 980, 'Ubuntu ARM64 / UEFI  ·  1 vCPU  ·  1 GiB RAM  ·  8 GB disk  ·  OpenSSH trusts the OpenBao public CA.', 18)
    d.text(374, 1007, 'Security group permits SSH only from Warpgate 100.64.10.10/32. No gateway agent runs inside the VM.', 18, fill=MUTED)
    d.arrow([(1540, 883), (1540, 914)], 'blue')
    d.footer(1086, [(18, 555, 'Hardware acceleration', 'Outer Linux VM uses Apple VZ. Tenant uses KVM\nafter a successful probe; otherwise ARM64 QEMU/TCG.', 'server', NAVY), (591, 590, 'Network isolation', 'Gateway has no default or direct tenant route.\nProvider: 100.64.10.0/24 · tenant: 192.168.20.0/24.', 'shield', NAVY), (1199, 583, 'Persistent state', 'Gateway host keys + recordings; OpenBao CA / Raft;\nserver grant database. Stop / start retains these.', 'store', NAVY)])
    d.save('architecture.svg')


def devices():
    d = Diagram('Latch — Network-device access', 'Logical routed topology · Warpgate recording · OpenBao LDAP leases · TACACS+ command authorization', 1290)
    d.band(112, 245, 1, 'Grant a role', 'Control plane\n\nChoose device, role and TTL.\nInventory permissions\nlimit what can be granted.', 'blue')
    d.card(350, 173, 365, 145, 'Engineer / CLI', './lab device-grant --device freertr\n  --role readonly --ttl 300\n./lab device-connect --grant UUID', 'user', title_size=21)
    d.card(830, 159, 430, 175, 'Latch device API', 'Authenticate caller and authorize role.\nPersist grant deadline + cleanup state.\nConfigure per-grant Warpgate target.\nPin router host key; close expired sessions.', 'api')
    d.card(1375, 173, 375, 145, 'Warpgate admission', 'Gateway login uses the laptop key.\nTarget login uses a temporary password.\nNo target agent is required.', 'shield')
    d.arrow([(715, 230), (830, 230)], 'orange', 'Grant', 772, 210)
    d.arrow([(1260, 230), (1375, 230)], 'orange', 'Target / role', 1317, 210, 16)
    d.band(373, 260, 2, 'Issue credentials', 'Temporary identity\n\nSeparate LDAP OpenBao\nfrom the server SSH CA.\nAccount lease ≤ 5 minutes.', 'purple')
    d.card(350, 443, 355, 151, 'LDAP OpenBao', 'Device API requests a dynamic lease.\nCreate / revoke temporary account.\nScoped credentials stay server-side.', 'key', PURPLE)
    d.card(830, 443, 430, 151, 'OpenLDAP', 'Temporary users + role-group membership.\nLDAPS TCP 636.\nOnly managed accounts are deleted.', 'store', PURPLE)
    d.card(1375, 443, 375, 151, 'tac_plus-ng', 'Authenticate via LDAP password bind.\nLook up role groups.\nEnforce per-command TACACS+ policy.', 'shield', PURPLE)
    d.arrow([(705, 511), (830, 511)], 'purple', 'LDAPS', 767, 491)
    d.arrow([(1375, 511), (1260, 511)], 'purple', 'LDAP bind', 1317, 491, 16)
    d.band(649, 420, 3, 'Use the device', 'Runtime data plane\n\nWarpgate records SSH.\nThe router authenticates\nand authorizes via TACACS+.\nNo Neutron forwarding.', 'green')
    d.rect(350, 707, 390, 318, '#f7fffb', '#6fba9b', dash='8 5')
    d.text(374, 742, 'GATEWAY INGRESS NETWORK', 20, 700)
    d.text(374, 767, '10.1.1.0/24 · management: 10.1.3.10', 16)
    d.rect(820, 707, 430, 318, '#f7fffb', '#6fba9b', dash='8 5')
    d.text(844, 742, 'DEVICE MANAGEMENT NETWORK', 19, 700)
    d.text(844, 767, '10.1.4.0/24 · routed subnet', 16)
    d.rect(1330, 707, 420, 318, '#f7fffb', '#6fba9b', dash='8 5')
    d.text(1354, 742, 'SERVICE MANAGEMENT', 19, 700)
    d.text(1354, 767, '10.1.3.0/24 · AAA services live here', 16)
    d.card(380, 791, 320, 180, 'Warpgate gateway', 'Check trusted router host key.\nInject temporary password.\nRecord terminal traffic.\nManagement IP: 10.1.3.10.', 'shield')
    d.card(865, 791, 355, 180, 'freeRouter', '10.1.4.20:22 · routed management\nSeparate subnet from Warpgate.\nCommand requests go to TACACS+.\nNo laptop-published router port.', 'network')
    d.card(1375, 791, 345, 180, 'tac_plus-ng', '10.1.3.21:49 · routed from device\nTACACS+ authentication\nand command authorization.\nLDAP role determines policy.', 'shield')
    d.arrow([(700, 871), (865, 871)], 'blue', 'Routed SSH', 782, 850, 16)
    d.arrow([(1220, 871), (1375, 871)], 'purple', 'Routed TACACS+', 1297, 850, 16)
    d.text(374, 1003, 'Lab device-router: 10.1.3.253 ↔ 10.1.4.1 · SSH / TACACS+ and replies only · no forwarded NAT · partial live acceptance; see validation guide.', 16, 600)
    d.footer(1086, [(18, 555, 'Roles', 'readonly: operational diagnostics and connections.\noperator: configuration + save. admin: all commands.', 'user', NAVY), (591, 590, 'Revoke / expiry', 'Delete the LDAP lease; remove gateway admission.\nExplicitly close existing grant sessions.', 'clock', NAVY), (1199, 583, 'Policy limit', 'Operator configuration can change AAA settings.\nOperator is not a strict security boundary from admin.', 'shield', ORANGE)])
    d.save('device-access.svg')


if __name__ == '__main__':
    networks()
    deployment()
    devices()
    print('Rendered three architecture SVGs.')
