import os
import re
import sys
import time
from logger import logger
from typing import Optional, Tuple

import host
from clustersConfig import NodeConfig


class VirBridge:
    """
    Wrapper on top of the libvirt virtual bridge.

    It can be running locally or remote.
    """

    hostconn: host.Host

    def __init__(self, h: host.Host):
        self.hostconn = h

    def setup_dhcp_entry(self, cfg: NodeConfig) -> None:
        if cfg.ip is None:
            logger.error_and_exit(f"Missing IP for node {cfg.name}")
        ip = cfg.ip
        mac = cfg.mac
        name = cfg.name
        # If adding a worker node fails, one might want to retry w/o tearing down
        # the whole cluster. In that case, the DHCP entry might already be present,
        # with wrong mac -> remove it

        cmd = "virsh net-dumpxml default"
        ret = self.hostconn.run_or_die(cmd)
        if f"'{name}'" in ret.out:
            logger.info(f"{name} already configured as static DHCP entry - removing before adding back with proper configuration")
            host_xml = f"<host name='{name}'/>"
            cmd = f"virsh net-update default delete ip-dhcp-host \"{host_xml}\" --live --config"
            self.hostconn.run_or_die(cmd)

        cmd = "virsh net-dhcp-leases default"
        ret = self.hostconn.run(cmd)
        # Look for "{name} " in the output. The space is intended to differentiate between "bm-worker-2 " and e.g. "bm-worker-20"
        if f"{name} " in ret.out:
            logger.error(f"Error: {name} found in dhcp leases")
            logger.error("To fix this, run")
            logger.error("\tvirsh net-destroy default")
            logger.error("\tRemove wrong entries from /var/lib/libvirt/dnsmasq/virbr0.status")
            logger.error("\tvirsh net-start default")
            logger.error("\tsystemctl restart libvirt")
            sys.exit(-1)

        host_xml = f"<host mac='{mac}' name='{name}' ip='{ip}'/>"
        logger.info(f"Creating static DHCP entry for VM {name}, ip {ip} mac {mac}")
        cmd = f"virsh net-update default add ip-dhcp-host \"{host_xml}\" --live --config"
        self.hostconn.run_or_die(cmd)

    def _ensure_started(self, api_network: str, bridge_xml: str) -> None:
        cmd = "virsh net-destroy default"
        self.hostconn.run(cmd)  # ignore return code - it might fail if net was not started

        cmd = "virsh net-undefine default"
        ret = self.hostconn.run(cmd)
        if ret.returncode != 0 and "Network not found" not in ret.err:
            logger.error_and_exit(str(ret))

        # Fix cases where virsh net-start fails with error "... interface virbr0: File exists"
        cmd = "ip link delete virbr0"
        self.hostconn.run(cmd)  # ignore return code - it might fail if virbr did not exist

        cmd = f"virsh net-define {bridge_xml}"
        self.hostconn.run_or_die(cmd)

        # set interface down before starting bridge as otherwise bridge start might fail if interface
        # already got an IP address in same network as bridge
        self.hostconn.run(f"ip link set {api_network} down")

        cmd = "virsh net-start default"
        self.hostconn.run_or_die(cmd)

        self.hostconn.run(f"ip link set {api_network} up")

    def limit_dhcp_range(self, old_range: str, new_range: str) -> None:
        # restrict dynamic dhcp range: we use static dhcp ip addresses; however, those addresses might have been used
        # through the dynamic dhcp by any systems such as systems ready to be installed.
        cmd = "virsh net-dumpxml default"
        ret = self.hostconn.run(cmd)
        if f"range start='{old_range}'" in ret.out:
            host_xml = f"<range start='{old_range}' end='192.168.122.254'/>"
            cmd = f"virsh net-update default delete ip-dhcp-range \"{host_xml}\" --live --config"
            r = self.hostconn.run(cmd)
            logger.debug(r.err if r.err else r.out)

            host_xml = f"<range start='{new_range}' end='192.168.122.254'/>"
            cmd = f"virsh net-update default add ip-dhcp-range \"{host_xml}\" --live --config"
            r = self.hostconn.run(cmd)
            logger.debug(r.err if r.err else r.out)

    def _network_xml(self, ip: str, dhcp_range: Optional[Tuple[str, str]] = None) -> str:
        if dhcp_range is None:
            dhcp_part = ""
        else:
            dhcp_part = f"""<dhcp>
                <range start='{dhcp_range[0]}' end='{dhcp_range[1]}'/>
                </dhcp>"""

        return f"""
                <network>
                <name>default</name>
                <forward mode='nat'/>
                <bridge name='virbr0' stp='off' delay='0'/>
                <ip address='{ip}' netmask='255.255.0.0'>
                    {dhcp_part}
                </ip>
                </network>"""

    def _restart(self) -> None:
        self.hostconn.run_or_die("systemctl restart libvirtd")

    def _ensure_run_as_root(self) -> None:
        qemu_conf = self.hostconn.read_file("/etc/libvirt/qemu.conf")
        if re.search('\nuser = "root"', qemu_conf) and re.search('\nuser = "root"', qemu_conf):
            return
        self.hostconn.run("sed -e 's/#\\(user\\|group\\) = \".*\"$/\\1 = \"root\"/' -i /etc/libvirt/qemu.conf")
        self._restart()

    def configure(self, api_network: str) -> None:
        hostname = self.hostconn.hostname()
        cmd = "systemctl enable libvirtd --now"
        self.hostconn.run_or_die(cmd)

        self._ensure_run_as_root()

        # stp must be disabled or it might conflict with default configuration of some physical switches
        # 'bridge' section of network 'default' can't be updated => destroy and recreate
        # check that default exists and contains stp=off
        cmd = "virsh net-dumpxml default"
        ret = self.hostconn.run(cmd)

        if "stp='off'" not in ret.out or "range start='192.168.122.2'" in ret.out:
            logger.info("Destoying and recreating bridge")
            logger.info(f"creating default-net.xml on {hostname}")
            if hostname == "localhost":
                contents = self._network_xml('192.168.122.1', ('192.168.122.129', '192.168.122.254'))
            else:
                contents = self._network_xml('192.168.123.250')

            bridge_xml = os.path.join("/tmp", 'vir_bridge.xml')
            self.hostconn.write(bridge_xml, contents)
            # Not sure why/whether this is needed. But we saw failures w/o it.
            # Without this, net-undefine within ensure_bridge_is_started fails as libvirtd fails to restart
            # We need to investigate how to remove the sleep to speed up
            time.sleep(5)
            self._ensure_started(api_network, bridge_xml)

            self.limit_dhcp_range("192.168.122.2", "192.168.122.129")

            self._restart()

            # Not sure why/whether this is needed. But we saw failures w/o it.
            # We need to investigate how to remove the sleep to speed up
            time.sleep(5)
