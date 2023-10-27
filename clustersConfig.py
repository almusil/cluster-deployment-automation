from os import path, getcwd
import os
import io
import sys
import re
from typing import Optional
import jinja2
from yaml import safe_load
import host
from logger import logger
import common
from clusterInfo import ClusterInfo
from clusterInfo import load_all_cluster_info


# Run the hostname command and only take the first part. For example
# "my-host.test.redhat.com" would return "my-host" here.
# This is only required if we are using the Google sheets integration
# to match the node name syntax in the spreadsheet.
def current_host() -> str:
    lh = host.LocalHost()
    return lh.run("hostname").out.strip().split(".")[0]


class ClustersConfig:
    def __init__(self, yaml_path: str):
        self._clusters: Optional[ClusterInfo] = None
        self._current_host = current_host()
        self._load_full_config(yaml_path)

        # Some config may be left out from the yaml. Try to provide defaults.
        for cc in self.fullConfig["clusters"]:
            if "masters" not in cc:
                cc["masters"] = []
            if "workers" not in cc:
                cc["workers"] = []
            if "kubeconfig" not in cc:
                cc["kubeconfig"] = path.join(getcwd(), f'kubeconfig.{cc["name"]}')
            if "preconfig" not in cc:
                cc["preconfig"] = ""
            if "postconfig" not in cc:
                cc["postconfig"] = ""
            if "version" not in cc:
                cc["version"] = "4.13.0-ec.3"
            if "external_port" not in cc:
                cc["external_port"] = "auto"
            if "network_api_port" not in cc:
                cc["network_api_port"] = "auto"
            if "proxy" not in cc:
                cc["proxy"] = None

            if "hosts" not in cc:
                cc["hosts"] = []

            # creates hosts entries for each referenced node name
            all_nodes = cc["masters"] + cc["workers"]
            for n in all_nodes:
                if "disk_size" not in n:
                    n["disk_size"] = 48
                if "preallocated" not in n:
                    n["preallocated"] = True
                if "os_variant" not in n:
                    n["os_variant"] = "rhel8.6"

            node_names = set(x["name"] for x in cc["hosts"])
            for h in all_nodes:
                if h["node"] not in node_names:
                    cc["hosts"].append({"name": h["node"]})
                    node_names.add(h["node"])

            # Set default value for optional parameters for workers.
            for node in all_nodes:
                if "bmc_ip" not in node:
                    node["bmc_ip"] = None
                if "bmc_user" not in node:
                    node["bmc_user"] = "root"
                if "bmc_password" not in node:
                    node["bmc_password"] = "calvin"
                if "image_path" not in node:
                    base_path = f'/home/{cc["name"]}_guests_images'
                    qemu_img_name = f'{node["name"]}.qcow2'
                    node["image_path"] = os.path.join(base_path, qemu_img_name)
            for host_config in cc["hosts"]:
                if "network_api_port" not in host_config:
                    host_config["network_api_port"] = cc["network_api_port"]
                if "username" not in host_config:
                    host_config["username"] = "core"
                if "password" not in host_config:
                    host_config["password"] = None
                if "pre_installed" not in host_config:
                    host_config["pre_installed"] = "True"

    def _load_full_config(self, yaml_path: str) -> None:
        if not path.exists(yaml_path):
            logger.error(f"could not find config in path: '{yaml_path}'")
            sys.exit(1)

        with open(yaml_path, 'r') as f:
            contents = f.read()
            # load it twice, so that self-reference becomes possible
            self.fullConfig = safe_load(io.StringIO(contents))["clusters"][0]
            contents = self._apply_jinja(contents)
            self.fullConfig = safe_load(io.StringIO(contents))["clusters"][0]

    def autodetect_external_port(self) -> None:
        detected = common.route_to_port(host.LocalHost(), "default")
        self.__setitem__("external_port", detected)

    def prepare_external_port(self) -> None:
        if self.__getitem__("external_port") == "auto":
            self.autodetect_external_port()

    def validate_external_port(self) -> bool:
        extif = self.__getitem__("external_port")
        return host.LocalHost().port_exists(extif)

    def _apply_jinja(self, contents: str) -> str:
        def worker_number(a):
            self._ensure_clusters_loaded()
            assert self._clusters is not None
            name = self._clusters.workers[a]
            return re.sub("[^0-9]", "", name)

        def worker_name(a):
            self._ensure_clusters_loaded()
            assert self._clusters is not None
            return self._clusters.workers[a]

        def api_network():
            self._ensure_clusters_loaded()
            assert self._clusters is not None
            return self._clusters.network_api_port

        format_string = contents

        template = jinja2.Template(format_string)
        template.globals['worker_number'] = worker_number
        template.globals['worker_name'] = worker_name
        template.globals['api_network'] = api_network

        kwargs = {}
        kwargs["cluster_name"] = self.fullConfig["name"]

        t = template.render(**kwargs)
        return t

    def _ensure_clusters_loaded(self) -> None:
        if self._clusters is not None:
            return
        all_cluster_info = load_all_cluster_info()
        ch = current_host()
        if ch in all_cluster_info:
            self._clusters = all_cluster_info[ch]
        else:
            sys.exit(-1)

    def __getitem__(self, key):
        return self.fullConfig[key]

    def __setitem__(self, key, value) -> None:
        self.fullConfig[key] = value

    def all_nodes(self) -> list:
        return self["masters"] + self["workers"]

    def all_hosts(self) -> list:
        return self["hosts"]

    def all_vms(self) -> list:
        return [x for x in self.all_nodes() if x["type"] == "vm"]

    def worker_vms(self) -> list:
        return [x for x in self["workers"] if x["type"] == "vm"]

    def master_vms(self) -> list:
        return [x for x in self["masters"] if x["type"] == "vm"]

    def local_vms(self) -> list:
        return [x for x in self.all_vms() if x["node"] == "localhost"]

    def is_sno(self) -> bool:
        return len(self["masters"]) == 1 and len(self["workers"]) == 0


def main() -> None:
    pass


if __name__ == "__main__":
    main()
