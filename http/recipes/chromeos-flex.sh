# recipe: ChromeOS Flex - Google's official mass-deployment disk image, written raw like Google's own WDS script does
# SPDX-License-Identifier: AGPL-3.0-or-later
# Source: https://dl.google.com/chromeos-flex/images/latest-massdeploy.bin.zip (full, current stable) and
#         https://dl.google.com/chromeos-flex/images/optimized-mass-deploy-beta.zip (Google's 1 GB optimized image, LTS).
# Fetched once over HTTPS, zip CRC checked, repacked as zstd for streaming; the checksum below is of Google's unpacked .bin.
# Params: variant=full|lts (default: full when the disk holds the 31 GB image, else lts)
RECIPE_NAME="ChromeOS Flex"
RECIPE_DESC="Google's ChromeOS for PCs and Macs: fast, low-maintenance, managed from the Google Admin console. Official mass-deploy image."
RECIPE_NOTE="UEFI only. The whole disk is replaced. First boot shows Google's setup (network, account or enterprise enrollment). No SSH: manage it from the Google Admin console. Compact (LTS) edition: finishes installing itself on first boot, about 3-5 min with a dark screen - do not power off."
RECIPE_MIN_GB=16
RECIPE_VARIANTS="full lts"
wget -qO /root/tk/image-lib.sh "$SRV/recipes/image.sh" && . /root/tk/image-lib.sh
RECIPE_NAME="ChromeOS Flex"
RECIPE_METHOD=image
RECIPE_EFI_LABEL="ChromeOS Flex|UEFI OS|BOOT"

FLEX_FULL_IMG=chromeos-flex.bin.zst;     FLEX_FULL_RAW=639bb27446a352bc3dc5948b48452ee15219cf1966fec5317e26141afce53043; FLEX_FULL_BYTES=31138512896
FLEX_LTS_IMG=chromeos-flex-lts.bin.zst;  FLEX_LTS_RAW=35f3970007f699b9d16d1aa8f60ff70bb779ca3af63c83ef3f25093458778dd9;   FLEX_LTS_BYTES=1025524224
IMAGE=$FLEX_FULL_IMG; SHA256=$FLEX_FULL_RAW

flex_pick() {
  [ "$BOOT_MODE" = uefi ] || { echo "ChromeOS Flex needs a UEFI machine (this one boots in legacy BIOS mode)"; return 1; }
  have=$(( $(cat /sys/class/block/${DISK#/dev/}/size) * 512 ))
  v=${VARIANT:-auto}
  [ "$v" = auto ] && { [ "$have" -ge "$FLEX_FULL_BYTES" ] && v=full || v=lts; }
  case "$v" in
    full) IMAGE=$FLEX_FULL_IMG; RAW_SHA256=$FLEX_FULL_RAW; RAW_BYTES=$FLEX_FULL_BYTES ;;
    lts)  IMAGE=$FLEX_LTS_IMG;  RAW_SHA256=$FLEX_LTS_RAW;  RAW_BYTES=$FLEX_LTS_BYTES ;;
    *) echo "variant= must be full or lts"; return 1 ;;
  esac
  echo "ChromeOS Flex image: $v ($((RAW_BYTES/1000000)) MB onto a $((have/1000000000)) GB disk)"
}
recipe_deploy() { flex_pick && image_write; }
