# recipe: openSUSE Leap 15.6 - official Minimal-VM Cloud image, repacked raw+zstd on the server, cloud-init first boot
# SPDX-License-Identifier: AGPL-3.0-or-later
# Source: https://download.opensuse.org/distribution/leap/15.6/appliances/openSUSE-Leap-15.6-Minimal-VM.x86_64-15.6.0-Cloud-Build19.146.qcow2
# Repacked once by linux/build-cloud-image.sh (vendor qcow2 SHA-256 verified -> raw -> zstd -19); RAW_SHA256 below is
# the SHA-256 of the unpacked raw image, checked again on the machine before first boot.
RECIPE_NAME="openSUSE Leap 15.6"
RECIPE_DESC="Stable, enterprise-grade Linux from openSUSE - binary-compatible with SUSE Linux Enterprise. Official Minimal-VM Cloud image. Widely used across Europe."
RECIPE_NOTE="UEFI machines. DHCP networking. SSH as 'opensuse' (passwordless sudo) and as root, with your key. The root filesystem grows to fill the disk on first boot. Manage with zypper."
RECIPE_MIN_GB=10
wget -qO /root/tk/image-lib.sh "$SRV/recipes/image.sh" && . /root/tk/image-lib.sh
RECIPE_NAME="openSUSE Leap 15.6"
RECIPE_METHOD=image
RECIPE_CHECK_PORT=22                  # first boot confirmed when SSH answers
RECIPE_EFI_LABEL="UEFI OS|BOOT|opensuse|openSUSE|Leap"
IMAGE=opensuse-leap.bin.zst
RAW_SHA256=7de97e978fd67faf397e4c4bf39887e1845a8b442a6c38e8afe5e09e8dea8673
RAW_BYTES=879755264
SHA256=$RAW_SHA256
CLOUD_USER=opensuse

recipe_deploy() { image_write && cloudinit_seed; }
