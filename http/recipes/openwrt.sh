# recipe: OpenWrt 24.10 (x86-64, UEFI) - official disk image, checksum verified
RECIPE_NAME="OpenWrt 24.10.8 (router / firewall)"
RECIPE_DESC="Router and firewall OS. Official x86-64 UEFI image, 14 MB."
RECIPE_NOTE="After install the LAN port is 192.168.1.1 (OpenWrt default); best on machines with 2+ network ports. Web UI (LuCI) on the LAN port."
RECIPE_MIN_GB=1
wget -qO /root/tk/image-lib.sh "$SRV/recipes/image.sh" && . /root/tk/image-lib.sh
RECIPE_NAME="OpenWrt 24.10.8 (router / firewall)"
RECIPE_METHOD=image
RECIPE_EFI_LABEL="UEFI OS|BOOT|OpenWrt"
IMAGE=https://downloads.openwrt.org/releases/24.10.8/targets/x86/64/openwrt-24.10.8-x86-64-generic-ext4-combined-efi.img.gz
SHA256=1abb90f522f40990ba6507a936dace792668514025d2319d3e6173ec8d6dada3
