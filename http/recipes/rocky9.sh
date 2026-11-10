# recipe: Rocky Linux 9 - official GenericCloud image, repacked raw+zstd on the server, cloud-init first boot
# SPDX-License-Identifier: AGPL-3.0-or-later
# Source: https://download.rockylinux.org/pub/rocky/9/images/x86_64/Rocky-9-GenericCloud-Base.latest.x86_64.qcow2
# Repacked once by linux/build-cloud-image.sh (vendor qcow2 SHA-256 verified, converted to raw, zstd); RAW_SHA256 below
# is the SHA-256 of the unpacked raw image, checked again on the machine before first boot.
RECIPE_NAME="Rocky Linux 9"
RECIPE_DESC="Enterprise Linux, RHEL-compatible: 10-year lifecycle, SELinux, dnf. Official GenericCloud image from rockylinux.org."
RECIPE_NOTE="UEFI machines. DHCP networking. SSH as 'rocky' (passwordless sudo) and as root, with your key. The root filesystem grows to fill the disk on first boot. Manage with dnf."
RECIPE_MIN_GB=10
wget -qO /root/tk/image-lib.sh "$SRV/recipes/image.sh" && . /root/tk/image-lib.sh
RECIPE_NAME="Rocky Linux 9"
RECIPE_METHOD=image
RECIPE_CHECK_PORT=22                  # first boot confirmed when SSH answers
RECIPE_EFI_LABEL="UEFI OS|BOOT|Rocky|rocky"
IMAGE=rocky9.bin.zst
RAW_SHA256=4afc3094acfc5d4773ae137e2efca4b38a6d6d778b85e8b15442c1ca166f6c42
RAW_BYTES=10737418240
SHA256=$RAW_SHA256
CLOUD_USER=rocky

recipe_deploy() { image_write && cloudinit_seed; }
