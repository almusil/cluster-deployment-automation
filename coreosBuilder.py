import os
import sys
import json
import shutil
from typing import Optional
import glob
from git.repo import Repo
from logger import logger
import host


def ensure_fcos_exists(dst: str = "/root/iso/fedora-coreos.iso") -> None:
    logger.info("ensuring that fcos exists")
    builder = CoreosBuilder("/tmp/build")
    if os.path.exists(dst):
        builder.ensure_ign_embedded(dst)
    else:
        logger.info(f"fcos missing from {dst}, building it")
        builder.build(dst)


"""
The purpose of coreos builder is to build an image with "kernel-modules-extra" which would contain
the rshim module. The rshim module is important for communicating with the BlueField-2 DPU.

Firstly 2 repositories are needed "coreos-assembler" and "fedora-coreos-config".

In the working directory, we will construct the custom yaml containing the "kernel-modules-extra"
package. This custom yaml file will be reference in the "fedora-coreos-base.yaml" file in the
"include" section. This is currently below the "shared-el9.yaml" file. TODO: This would subject
to change, we should find a way of adding custom yaml without knowing the layout of the
"fedora-coreos-base.yaml" file.

Afterwards, it is also required to add "kernel-modules-extra" with the correct version "evra" into
the "manifest-lock.x86_64.json" file. We simply inherit the "evra" version from the "kernel" package
for "kernel-modules-extra". Example:
  "kernel": {
      "evra": "6.1.8-200.fc37.x86_64"
  },
  ...
  "kernel-modules-extra": {
      "evra": "6.1.8-200.fc37.x86_64"
  },

Next we will run the core-assembler pod from "quay.io/coreos-assembler/coreos-assembler:latest"
The explanation of the options used for podman is described here:
https://github.com/coreos/coreos-assembler/blob/main/docs/building-fcos.md
In the pod, we will init the git repo "https://github.com/coreos/fedora-coreos-config" that
contains our explicit changes. Following the "building-fcos" guide, we will subsequently
run "fetch" and "build".

An ignition file must be created to give SSH access from the local host to the coreos image
{
  "ignition": {"version": "3.3.0"},
  "passwd": {
    "users": [{
      "name": "core",
      "sshAuthorizedKeys": ["ssh-rsa <your key>"]
    }]
  }
}
Documentation on this is available here: https://coreos.github.io/ignition/examples/#add-users

The "coreos-installer" package provides coreos-installer which is used to embed the ignition file into
the iso.
  coreos-installer iso ignition embed -i <ignition file location> -o <final iso location> <original iso location>

The "final iso location" is the image we will use to live boot our machines.
"""


class CoreosBuilder:
    def __init__(self, working_dir: str):
        self._workdir = working_dir

    def build(self, dst: str) -> None:
        logger.info(f"Building FCOS and will store it at {dst}")
        fcos_dir = os.path.join(self._workdir, "fcos")
        lh = host.LocalHost()

        self._clone_if_not_exists("https://github.com/coreos/coreos-assembler.git")
        config_dir = self._clone_if_not_exists("https://github.com/coreos/fedora-coreos-config")

        lh.run(f"git -C {config_dir} submodule update --init --recursive")

        contents = "packages:\n  - kernel-modules-extra\n"
        # contents = contents + "  - python3\n  - libvirt\n  - qemu-img\n  - qemu-kvm\n  - virt-install\n  - netcat\n  - bridge-utils\n  - tcpdump\n"

        custom_yaml = os.path.join(config_dir, 'manifests/custom.yaml')
        logger.info(f"writing {custom_yaml}")
        with open(custom_yaml, 'w') as outfile:
            outfile.write(contents)

        base_cfg = os.path.join(config_dir, "manifests/fedora-coreos-base.yaml")
        with open(base_cfg, 'r') as f:
            contents = f.read()
        to_include = "\n  - custom.yaml"

        include_start = contents.index("include:")
        include_end = contents.index("\n", include_start)
        before = contents[:include_end]
        after = contents[include_end:]
        if not after.startswith(to_include):
            contents = before + to_include + after

        coreos_yaml = os.path.join(config_dir, 'manifests/fedora-coreos.yaml')
        logger.debug(f"modifying {coreos_yaml}")
        with open(coreos_yaml, 'r') as f:
            core_content = f.read()
        old_str = "- python3\n"
        core_content = core_content.replace(old_str, "# python3\n")
        old_str = "- python3-libs\n"
        core_content = core_content.replace(old_str, "# python3-libs\n")
        old_str = "- perl\n"
        core_content = core_content.replace(old_str, "# perl\n")
        old_str = "- perl-interpreter\n"
        core_content = core_content.replace(old_str, "# perl-interpreter\n")
        with open(coreos_yaml, "w") as f:
            f.write(core_content)

        logger.info(os.getcwd())
        manifest_lock = os.path.join(config_dir, "manifest-lock.x86_64.json")
        with open(manifest_lock) as f:
            j = json.load(f)
            j["packages"]["kernel-modules-extra"] = j["packages"]["kernel"]

        with open(manifest_lock, "w") as f:
            f.write(json.dumps(j, indent=4, sort_keys=True))

        with open(base_cfg, "w") as f:
            f.write(contents)

        logger.info(f"Clearing dir {fcos_dir}")
        if os.path.exists(fcos_dir):
            shutil.rmtree(fcos_dir)
        os.makedirs(fcos_dir)
        cur_dir = os.getcwd()
        os.chdir(fcos_dir)

        cmd = f"""
        podman run --rm -ti --userns=host -u root --privileged                \
               -v {fcos_dir}:/srv/ --device /dev/kvm --device /dev/fuse       \
               --tmpfs /tmp -v /var/tmp:/var/tmp --name cosa                  \
               -v {config_dir}:/git:ro                                        \
               quay.io/coreos-assembler/coreos-assembler:latest """

        def build_step(cmd: str, step: str) -> None:
            logger.info(f"Building CoreOS: {step}")
            cmd = cmd + " " + step
            r = lh.run(cmd)
            if r.returncode != 0:
                logger.info("Building CoreOS failed while running:")
                logger.info(cmd)
                logger.info("output was:")
                logger.info(r)
                sys.exit(-1)

        build_step(cmd, "init /git")
        build_step(cmd, "fetch")
        build_step(cmd, "build")
        build_step(cmd, "buildextend-metal")
        build_step(cmd, "buildextend-metal4k")
        build_step(cmd, "buildextend-live")

        embed_src = self._find_iso(fcos_dir)
        if embed_src is None:
            logger.info("Couldn't find iso")
            sys.exit(-1)

        self._embed_ign(embed_src, dst)
        os.chdir(cur_dir)

        # cleanup
        coreos_yaml = os.path.join(config_dir, 'manifests/fedora-coreos.yaml')
        logger.info(f"modifying {coreos_yaml}")
        with open(coreos_yaml, 'r') as f:
            core_content = f.read()
        old_str = "# python3\n"
        core_content = core_content.replace(old_str, "- python3\n")
        old_str = "# python3-libs\n"
        core_content = core_content.replace(old_str, "- python3-libs\n")
        old_str = "# perl\n"
        core_content = core_content.replace(old_str, "- perl\n")
        old_str = "# perl-interpreter\n"
        core_content = core_content.replace(old_str, "- perl-interpreter\n")
        with open(coreos_yaml, "w") as f:
            f.write(core_content)

    def _embed_ign(self, embed_src: str, dst: str) -> None:
        fn_ign = embed_src.replace(".iso", "-embed.ign")

        with open(fn_ign, "w") as f:
            ign = self.create_ignition()
            logger.info(f"Writing ignition to {ign}")
            f.write(ign)

        os.makedirs(os.path.dirname(dst), exist_ok=True)

        cmd = f"coreos-installer iso ignition embed -i {fn_ign} -o {dst} {embed_src}"
        lh = host.LocalHost()
        logger.info(lh.run(cmd))
        logger.info(lh.run(f"chmod a+rw {dst}"))

    def _find_iso(self, fcos_dir: str) -> Optional[str]:
        for root, _, files in os.walk(fcos_dir, topdown=False):
            for name in files:
                if name.endswith(".iso"):
                    return os.path.join(root, name)
        return None

    def _clone_if_not_exists(self, url: str) -> str:
        dest = url.split("/")[-1]
        if dest.endswith(".git"):
            dest = dest[:-4]

        repo_dir = os.path.join(self._workdir, dest)
        if os.path.exists(repo_dir):
            logger.info(f"Repo exists at {repo_dir}, not touching it")
        else:
            logger.info(f"Cloning repo to {repo_dir}")
            Repo.clone_from(url, repo_dir)
        return repo_dir

    def create_ignition(self, public_key_dir: str = "/root/.ssh/") -> str:
        logger.info("Creating ignition")

        keys = []
        for file in glob.glob(f"{public_key_dir}/*.pub"):
            logger.info(f"appending key from {file}")
            with open(file, 'r') as f:
                key = " ".join(f.read().split(" ")[:-1])
            keys.append(key)

        ign = {"ignition": {"version": "3.3.0"}, "passwd": {"users": [{"name": "core", "sshAuthorizedKeys": keys}]}}
        return json.dumps(ign)

    def ensure_ign_embedded(self, dst: str) -> None:
        lh = host.LocalHost()
        r = lh.run(f"coreos-installer iso ignition show {dst}")
        if r.out == self.create_ignition():
            return
        r = lh.run(f"coreos-installer iso ignition remove {dst}")
        shutil.move(dst, dst + ".tmp")
        self._embed_ign(dst + ".tmp", dst)


def main() -> None:
    builder = CoreosBuilder("/tmp/build")
    destination = "/root/iso/fedora-coreos.iso"
    builder.build(destination)


if __name__ == "__main__":
    main()
