# recipe: Fedora 45 - official Cloud Base Generic image, repacked raw+zstd on the server, cloud-init first boot
# SPDX-License-Identifier: AGPL-3.0-or-later
# Source: https://download.fedoraproject.org/pub/fedora/linux/releases/45/Cloud/x86_64/images/Fedora-Cloud-Base-Generic-45-1.4.x86_64.qcow2
# Repacked once by linux/build-cloud-image.sh (vendor qcow2 SHA-256 verified, converted to raw, zstd); RAW_SHA256 below
# is the SHA-256 of the unpacked raw image, checked again on the machine before first boot.
RECIPE_NAME="Fedora 45"
RECIPE_DESC="Leading-edge Linux from the Fedora Project: newest kernel and tooling, the upstream of RHEL. Official Cloud Base image."
RECIPE_NOTE="UEFI machines. DHCP networking. SSH as 'fedora' (passwordless sudo) and as root, with your key. The root filesystem grows to fill the disk on first boot. Manage with dnf."
RECIPE_MIN_GB=10
wget -qO /root/tk/image-lib.sh "$SRV/recipes/image.sh" && . /root/tk/image-lib.sh
RECIPE_NAME="Fedora 45"
RECIPE_METHOD=image
RECIPE_CHECK_PORT=22                  # first boot confirmed when SSH answers
RECIPE_EFI_LABEL="UEFI OS|BOOT|Fedora|fedora"
IMAGE=fedora.bin.zst
RAW_SHA256=08f25fd5401669b02139d11fa06b1f9cba215bb541159f3440bc395d0a4950d5
RAW_BYTES=5368709120
SHA256=$RAW_SHA256
CLOUD_USER=fedora

recipe_deploy() { image_write && cloudinit_seed; }
