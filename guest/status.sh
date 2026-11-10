#!/bin/bash
source /home/stack/latch/guest/common.sh
if [[ -f "$DATA/install-complete" ]]; then
    echo 'Installation completed.'
    systemctl --no-pager --full status latch-network
    os_auth
    openstack network agent list
    openstack server list
else
    systemctl --no-pager --full status latch-install || true
    echo 'Installation has not completed. Follow ./lab logs'
    exit 1
fi
