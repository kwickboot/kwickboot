# recipe: Home Assistant OS (x86-64, UEFI) - official disk image, checksum verified
# SPDX-License-Identifier: AGPL-3.0-or-later
RECIPE_NAME="Home Assistant OS 18.4 (smart home)"
RECIPE_DESC="Home automation hub: lights, sensors, cameras, energy. Official x86-64 image from home-assistant.io, 539 MB."
RECIPE_NOTE="Needs 2 GB RAM and 32 GB disk. After boot, open http://homeassistant.local:8123 (or the machine's IP) - first visit creates the owner account."
RECIPE_MIN_GB=32
wget -qO /root/tk/image-lib.sh "$SRV/recipes/image.sh" && . /root/tk/image-lib.sh
RECIPE_NAME="Home Assistant OS 18.4 (smart home)"
RECIPE_METHOD=image
RECIPE_CHECK_PORT=8123                # first boot confirmed when the Home Assistant web UI answers
RECIPE_EFI_LABEL="UEFI OS|BOOT|hassos|Home Assistant"
IMAGE=https://github.com/home-assistant/operating-system/releases/download/18.4/haos_generic-x86-64-18.4.img.xz
SHA256=6923ed7407511dc37119a00199575411d93f144fe83f276e7b595fe04acb561c
