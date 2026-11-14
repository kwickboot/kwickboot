#!/bin/sh
# make-release.sh - pack a KwickBoot release bundle from a tested server tree (run on the build box, not the server).
#   sh make-release.sh <tree> <version> [outdir]
#   <tree> = a copy of a working /opt/pxe-toolkit (code + tftp boot files + http/kbcore RAM OS)
#   KB_RELEASE_PROFILE=compact (default): amd64 Core, without the optional RICH desktop pack.
#   KB_RELEASE_PROFILE=full: amd64 + shipping arm64/i386 Cores and the RICH desktop pack.
#   KB_ARCHES="arm64 i386 ..." overrides the profile's extra-architecture list.
#   KB_OMIT_RUNTIME=1 is for installer-ISO embedding only: the ISO keeps one authoritative Core/rescue copy and
#   restores those directories before first boot instead of storing them twice.
# Ships: the server code, web console, scripts, recipes, boot loaders and the KwickBoot Core RAM OS.
# Never ships: site data (database, machines, logs), images/ISOs, keys (http/authorized_keys, data/*.key), the
# Alpine fallback, caches, internal notes. Output: kwickboot-<version>.tar.gz + .sha256
set -eu
TREE=${1:?tree}; VER=${2:?version}; OUT=${3:-$PWD}
PROFILE=${KB_RELEASE_PROFILE:-compact}
case "$VER" in *[!0-9A-Za-z.-]*) echo "bad version"; exit 1 ;; esac
case "$PROFILE" in compact|full) ;; *) echo "bad KB_RELEASE_PROFILE: $PROFILE (use compact or full)"; exit 1 ;; esac
N=kwickboot-$VER
ST=$(mktemp -d); trap 'rm -rf "$ST"' EXIT
D=$ST/$N; mkdir -p "$D"
cd "$TREE"
for f in *.py README.md LICENSE; do [ -f "$f" ] && cp "$f" "$D/"; done
# the console: only the page and the pinned vendor libraries (never ui/admin.json, the v1 admin password hash)
mkdir -p "$D/ui"; cp ui/index.html "$D/ui/"; cp -r ui/vendor "$D/ui/"; [ -d ui/i18n ] && cp -r ui/i18n "$D/ui/"   # + translations
cp -r linux "$D/"
mkdir -p "$D/http" "$D/tftp" "$D/tftp/alpine"
# http/http = the UEFI HTTP Boot loaders (grubnetx64 / grubaa64 / grubia32) - not in git, staged on the build tree
for d in scripts recipes tools windows kbcore http; do [ -d "http/$d" ] && cp -r "http/$d" "$D/http/"; done

# Most installations serve x86-64 PCs and use the browser console from an admin workstation. Shipping every CPU
# architecture and the optional local RICH WebKit desktop made the normal server download ~787 MB. Compact is the
# practical default: the full PXE/deployment server, amd64 Core and rescue tools, without optional offline packs.
# Full remains the air-gapped mixed-architecture edition. Experimental architectures are included only when explicitly
# requested through KB_ARCHES, so a build box merely having them cannot silently inflate a release.
if [ -n "${KB_ARCHES-}" ]; then
  EXTRA_ARCHES=$KB_ARCHES
elif [ "$PROFILE" = full ]; then
  EXTRA_ARCHES="arm64 i386"
else
  EXTRA_ARCHES=""
fi
for a in $EXTRA_ARCHES; do
  d="http/kbcore-$a"
  [ -d "$d" ] || { echo "FATAL: requested architecture Core missing: $d"; exit 1; }
  cp -r "$d" "$D/http/"
done
if [ "$PROFILE" = compact ] || [ "${KB_NO_DESKTOP:-0}" = 1 ]; then
  rm -f "$D"/http/kbcore*/packs/desktop.sqfs
fi
# OS library tools (Rescue & Wipe, ~35 MB) are part of standalone bundles; an ISO already has the same bootable tools.
if [ "${KB_OMIT_RUNTIME:-0}" != 1 ] && [ -d http/images/tools ]; then
  mkdir -p "$D/http/images"; cp -r http/images/tools "$D/http/images/"
fi
for d in debian-installer ipxe pxelinux.cfg; do [ -d "tftp/$d" ] && cp -r "tftp/$d" "$D/tftp/"; done
# no-PXE boot media: tiny generic iPXE stick (proxyDHCP broadcast, no baked IP) for clients that can't/won't PXE-boot
[ -d usb/no-pxe ] && { mkdir -p "$D/no-pxe"; cp -r usb/no-pxe/. "$D/no-pxe/"; [ -f usb/boot-stick.md ] && cp usb/boot-stick.md "$D/no-pxe/README.md"; }
for f in tftp/pxelinux.0 tftp/ldlinux.c32 tftp/splash.png tftp/version.info; do [ -f "$f" ] && cp "$f" "$D/tftp/"; done
# never ship site state, secrets or build leftovers
rm -rf "$D/linux/truenas" "$D/ui/vendor/node_modules" "$D/linux/e2e-test.sh"   # e2e-test: wired to our own lab
find "$D" -name '__pycache__' -prune -exec rm -rf {} + ; find "$D" -name '*.pyc' -delete
# Windows BCD registry-hive transaction journals (regenerated when the hive is opened) - never ship them
find "$D/linux/windows" -type f \( -name '*.LOG' -o -name '*.LOG1' -o -name '*.LOG2' -o -name '*.regtrans-ms' -o -name '*.TM.blf' \) -delete 2>/dev/null || true
find "$D" \( -name '*.key' -o -name 'authorized_keys' -o -name '*.pem' -o -name 'config.json' -o -name '*.db' -o -name 'admin.json' \) -print -delete
rm -f "$D/tftp/debian-installer/amd64/grub/grub.cfg" "$D/tftp/debian-installer/arm64/grub/grub.cfg" "$D/tftp/debian-installer/i386/grub/grub.cfg"   # rendered at start from grub.cfg.tmpl
cp "$D/linux/install.sh" "$D/install.sh"
if [ "${KB_OMIT_RUNTIME:-0}" = 1 ]; then
  rm -rf "$D"/http/kbcore "$D"/http/kbcore-*
  printf '%s\n' iso > "$D/RUNTIME_SOURCE"
fi
# sanity: a complete server tree has ~31 root modules. Refuse to ship an obviously-incomplete bundle -
# guards the 9-Nov defect where an incomplete BUILD tree packed a server missing sso/saml/mfa/health/...
# (pxeserver.py/webui.py import those at top level, so the installed server ImportError-crashed on boot).
nmod=$(ls "$D"/*.py 2>/dev/null | wc -l)
[ "$nmod" -ge 24 ] || { echo "FATAL: only $nmod root *.py in the bundle (<24) - source tree looks incomplete; not packing"; exit 1; }
# every local module an entrypoint imports must be packed (catches a dropped module even above the floor)
miss=""; for ep in pxeserver.py webui.py kbctl.py; do [ -f "$D/$ep" ] || continue
  for m in $(grep -hoE '^(import|from) [a-z_][a-z0-9_]+' "$D/$ep" | awk '{print $2}' | sort -u); do
    [ -f "$TREE/$m.py" ] && [ ! -f "$D/$m.py" ] && miss="$miss $m"; done; done
[ -z "$miss" ] || { echo "FATAL: entrypoint imports present in tree but not packed:$miss"; exit 1; }
# the network-boot loaders: without them the server answers PXE but no PC can boot (2026.11.11 shipped like that - the
# build tree had none and the copy loops above skip what is missing). They are not in git: stage them first
# (linux/fetch-boot-loaders.py <tree> for the signed GRUBs + iPXE; the Microsoft-signed shims bootnet*.efi from shim-signed).
need="tftp/ipxe/undionly.kpxe tftp/ipxe/ipxe.efi tftp/debian-installer/amd64/bootnetx64.efi tftp/debian-installer/amd64/grubx64.efi http/http/grubnetx64.efi"
for a in $EXTRA_ARCHES; do case $a in
  arm64) need="$need tftp/debian-installer/arm64/bootnetaa64.efi tftp/debian-installer/arm64/grubaa64.efi http/http/grubaa64.efi" ;;
  i386)  need="$need tftp/debian-installer/i386/bootnetia32.efi tftp/debian-installer/i386/grubia32.efi http/http/grubia32.efi" ;;
esac; done
lmiss=""; for f in $need; do [ -s "$D/$f" ] || lmiss="$lmiss $f"; done
[ -z "$lmiss" ] || { echo "FATAL: network-boot loaders missing from the bundle:$lmiss - stage them on the build tree; not packing"; exit 1; }
# nothing site-specific may ship: fail the build on our own hostnames, user names or home paths
if grep -rIlE --exclude=make-release.sh 'REPLACE_WITH_PRIVATE_BUILD_HOST|REPLACE_WITH_PRIVATE_PRODUCTION_HOST|C:.Users' "$D" ; then echo "site-specific text found (above) - not packed"; exit 1; fi
echo "$VER" > "$D/VERSION"
printf '%s\n' "$PROFILE" > "$D/RELEASE_PROFILE"
( cd "$D" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS )
mkdir -p "$OUT"
tar -C "$ST" -czf "$OUT/$N.tar.gz" "$N"
( cd "$OUT" && sha256sum "$N.tar.gz" > "$N.tar.gz.sha256" )
echo "$OUT/$N.tar.gz  profile=$PROFILE  $(du -h "$OUT/$N.tar.gz" | cut -f1)  $(cut -d' ' -f1 "$OUT/$N.tar.gz.sha256")"
