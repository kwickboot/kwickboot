# recipe: AlmaLinux 9 - official GenericCloud image, repacked raw+zstd on the server, cloud-init first boot
# SPDX-License-Identifier: AGPL-3.0-or-later
# Source: https://repo.almalinux.org/almalinux/9/cloud/x86_64/images/AlmaLinux-9-GenericCloud-latest.x86_64.qcow2
# Repacked once by linux/build-cloud-image.sh (vendor qcow2 SHA-256 verified, converted to raw, zstd); RAW_SHA256 below
# is the SHA-256 of the unpacked raw image, checked again on the machine before first boot.
RECIPE_NAME="AlmaLinux 9"
RECIPE_DESC="Enterprise Linux, RHEL-compatible: 10-year lifecycle, SELinux, dnf. Official GenericCloud image from almalinux.org."
RECIPE_NOTE="UEFI machines. DHCP networking. SSH as 'almalinux' (passwordless sudo) and as root, with your key. The root filesystem grows to fill the disk on first boot. Manage with dnf."
RECIPE_MIN_GB=10
wget -qO /root/tk/image-lib.sh "$SRV/recipes/image.sh" && . /root/tk/image-lib.sh
RECIPE_NAME="AlmaLinux 9"
RECIPE_METHOD=image
RECIPE_CHECK_PORT=22                  # first boot confirmed when SSH answers
RECIPE_EFI_LABEL="UEFI OS|BOOT|AlmaLinux|almalinux"
IMAGE=almalinux9.bin.zst
RAW_SHA256=568265932c67f7118c9ca3ba4a4e4bcff4c0fb9560a6df32ba2706319bc285a6
RAW_BYTES=10737418240
SHA256=$RAW_SHA256
CLOUD_USER=almalinux

recipe_deploy() { image_write && cloudinit_seed; }
