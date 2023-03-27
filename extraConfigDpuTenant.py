from k8sClient import K8sClient
import os
import host
import time
from common_patches import apply_common_pathches
from concurrent.futures import Future
from extraConfigSriov import ExtraConfigSriov
from extraConfigSriov import ExtraConfigSriovOvSHWOL
from typing import Dict
import sys
import jinja2


class ExtraConfigDpuTenantMC:
    def __init__(self, cc):
        self._cc = cc

    def run(self, cfg, futures: Dict[str, Future]) -> None:
        [f.result() for (_, f) in futures.items()]
        print("Running post config step")
        tclient = K8sClient("/root/kubeconfig.tenantcluster")
        create_nm_operator(tclient)
        apply_common_pathches(tclient)
        print("Apply DPU tenant mc")
        tclient.oc("create -f manifests/tenant/dputenantmachineconfig.yaml")
        time.sleep(60)
        print("Waiting for mcp to be updated")
        tclient.oc("wait mcp dpu-host --for condition=updated")

        print("Patching mcp setting maxUnavailable to 2")
        tclient.oc("patch mcp dpu-host --type=json -p=\[\{\"op\":\"replace\",\"path\":\"/spec/maxUnavailable\",\"value\":2\}\]")

        print("Labeling nodes")
        for e in self._cc["workers"]:
            cmd = f"label node {e['name']} node-role.kubernetes.io/dpu-host="
            print(tclient.oc(cmd))
        print("Need to deploy sriov network operator")


class ExtraConfigDpuTenant:
    def __init__(self, cc):
        self._cc = cc

    def render_sriov_node_policy(self, policyname: str, bf_port: str, bf_addr: str, numvfs: int, resourcename: str, outfilename: str):
        with open("./manifests/tenant/SriovNetworkNodePolicy.yaml.j2") as f:
            j2_template = jinja2.Template(f.read())
            rendered = j2_template.render(policyName=policyname, bf_port=bf_port, bf_addr=bf_addr, numVfs=numvfs, resourceName=resourcename)
            print(rendered)

        with open(outfilename, "w") as outFile:
            outFile.write(rendered)

    def run(self, cfg, futures: Dict[str, Future]) -> None:
        [f.result() for (_, f) in futures.items()]
        print("Running post config step")
        tclient = K8sClient("/root/kubeconfig.tenantcluster")
        print("Waiting for mcp dpu-host to become ready")
        tclient.oc("wait mcp dpu-host --for condition=updated --timeout=50m")

        first_worker = self._cc["workers"][0]['name']
        ip = tclient.get_ip(first_worker)
        if ip is None:
            sys.exit(-1)
        rh = host.RemoteHost(ip)
        rh.ssh_connect("core")
        bf = [x for x in rh.run("lspci").out.split("\n") if "BlueField" in x]
        if not bf:
            print(f"Couldn't find BF on {first_worker}")
            sys.exit(-1)
        bf = bf[0].split(" ")[0]

        print(f"BF is at {bf}")

        bf_port = None
        for port in rh.all_ports():
            ret = rh.run(f'ethtool -i {port["ifname"]}')
            if ret.returncode != 0:
                continue

            d = {}
            for e in ret.out.strip().split("\n"):
                key, value = e.split(":", 1)
                d[key] = value
            if d["bus-info"].endswith(bf):
                bf_port = port["ifname"]
        print(bf_port)

        numVfs = 16
        numMgmtVfs = 1
        workloadPolicyName = "policy-mlnx-bf"
        workloadResourceName = "mlnx_bf"
        workloadBfPort = bf_port + f"#{numMgmtVfs}-{numVfs-1}"
        workloadPolicyFile = "/tmp/" + workloadPolicyName + ".yaml"
        mgmtPolicyName = "mgmt-policy-mlnx-bf"
        mgmtResourceName = "mgmtvf"
        mgmtBfPort = bf_port + f"#0-{numMgmtVfs-1}"
        mgmtPolicyFile = "/tmp/" + mgmtPolicyName + ".yaml"

        self.render_sriov_node_policy(workloadPolicyName, workloadBfPort, bf, numVfs, workloadResourceName, workloadPolicyFile)
        self.render_sriov_node_policy(mgmtPolicyName, mgmtBfPort, bf, numVfs, mgmtResourceName, mgmtPolicyFile)

        print("Creating sriov pool config")
        tclient.oc("create -f manifests/tenant/sriov-pool-config.yaml")
        tclient.oc("create -f " + workloadPolicyFile)
        tclient.oc("create -f " + mgmtPolicyFile)
        print("Waiting for mcp to be updated")
        time.sleep(60)
        tclient.oc("wait mcp dpu-host --for condition=updated --timeout=50m")

        print("creating config map to put ovn-k into dpu host mode")
        tclient.oc("create -f manifests/tenant/sriovdpuconfigmap.yaml")
        print("creating mc to disable ovs")
        tclient.oc("create -f manifests/tenant/disable-ovs.yaml")
        print("Waiting for mcp")
        time.sleep(60)
        tclient.oc("wait mcp dpu-host --for condition=updated --timeout=50m")

        print("setting ovn kube node env-override to set management port")
        print(os.getcwd())
        contents = open("manifests/tenant/setenvovnkube.yaml").read()
        for e in cfg["mapping"]:
            a = {}
            a["OVNKUBE_NODE_MGMT_PORT_NETDEV"] = f"{bf_port}v0"
            contents += f"  {e['worker']}: |\n"
            for (k, v) in a.items():
                contents += f"    {k}={v}\n"
        open("/tmp/1.yaml", "w").write(contents)

        print("Running create")
        print(tclient.oc("create -f /tmp/1.yaml"))

        for e in self._cc["workers"]:
            cmd = f"label node {e['name']} network.operator.openshift.io/dpu-host="
            print(tclient.oc(cmd))
            rh = host.RemoteHost(tclient.get_ip(e['name']))
            rh.ssh_connect("core")
            # workaround for https://issues.redhat.com/browse/NHE-335
            print(rh.run("sudo ovs-vsctl del-port br-int ovn-k8s-mp0"))

        print("Final infrastructure cluster configuration")
        iclient = K8sClient("/root/kubeconfig.infracluster")

        # https://issues.redhat.com/browse/NHE-334
        iclient.oc(f"project tenantcluster-dpu")
        print(iclient.oc(f"create secret generic tenant-cluster-1-kubeconf --from-file=config={tclient._kc}"))

        contents = open("manifests/tenant/envoverrides.yaml").read()
        for e in cfg["mapping"]:
            a = {}
            a["TENANT_K8S_NODE"] = e['worker']
            a["DPU_IP"] = iclient.get_ip(e['bf'])
            a["MGMT_IFNAME"] = "eth1"
            contents += f"  {e['bf']}: |\n"
            for (k, v) in a.items():
                contents += f"    {k}={v}\n"
        open("/tmp/envoverrides.yaml", "w").write(contents)

        iclient.oc("create -f /tmp/envoverrides.yaml")
        r = iclient.oc(
            "patch --type merge -p {\"spec\":{\"kubeConfigFile\":\"tenant-cluster-1-kubeconf\"}} OVNKubeConfig ovnkubeconfig-sample -n tenantcluster-dpu")
        print(r)
        print("Creating network attachement definition")
        tclient.oc("create -f manifests/tenant/nad.yaml")

        ec = ExtraConfigSriovOvSHWOL(self._cc)
        ec.ensure_pci_realloc(tclient, "dpu-host")


def create_nm_operator(client: K8sClient):
    print("Apply NMO subscription")
    client.oc("create -f manifests/tenant/nmo-subscription.yaml")


def main():
    pass


if __name__ == "__main__":
    main()
