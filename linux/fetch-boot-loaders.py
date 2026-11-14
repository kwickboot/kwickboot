#!/usr/bin/env python3
"""fetch-boot-loaders.py <out-dir> [--suite trixie]  - KwickBoot's signed UEFI network-boot GRUB, reproducibly.
SPDX-License-Identifier: AGPL-3.0-or-later

For each UEFI arch (x64, ARM64, IA32) stages, from the Debian archive:
  <out>/tftp/debian-installer/<arch>/grub<sfx>.efi        signed network GRUB (grub-efi-<arch>-signed: grubnet<sfx>.efi.signed)
  <out>/tftp/debian-installer/<arch>/grub/<platform>/     its modules + .lst files (grub-efi-<arch>-bin, SAME version)
  <out>/http/http/grub<sfx>.efi                           the same GRUB for UEFI HTTP Boot (not x64: own image, build-http-grub.sh)
  <out>/LOADERS.txt                                       versions + sha256 of everything staged
Every download is verified: InRelease against the Debian archive keyring (gpgv), Packages by the Release sha256, each .deb by
the Packages sha256. The newest version across <suite>, <suite>-updates and <suite>-security wins.
Why: GRUB reads the boot server from a proxyDHCP offer only from 2.12-9 on (Debian #935192) - an older GRUB (the bookworm IA32
one) lands at a grub> prompt behind KwickBoot's proxyDHCP; and security updates (deb13uN) must reach the loaders PCs boot.
The signed shims (shim-signed 16.x, Microsoft UEFI CA 2011 + 2023) are not touched here.
"""
import hashlib, io, lzma, os, re, shutil, subprocess, sys, tempfile, urllib.request

ARCHES = [  # kwickboot dir, Debian arch, grub platform, file suffix, also HTTP-boot copy
    ("amd64", "amd64", "x86_64-efi", "x64", False),
    ("arm64", "arm64", "arm64-efi", "aa64", True),
    ("i386", "i386", "i386-efi", "ia32", True),
]
MIRRORS = [("deb.debian.org/debian", "{s}"), ("deb.debian.org/debian", "{s}-updates"), ("security.debian.org/debian-security", "{s}-security")]
KEYRINGS = ["/usr/share/keyrings/debian-archive-keyring.gpg", "/usr/share/keyrings/debian-archive-keyring.pgp"]


def get(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read()


def vkey(v):   # Debian version ordering, good enough for grub2 versions (epoch:upstream-revision, ~ sorts first)
    parts = re.split(r"(\d+)", v)
    return [(0, int(p)) if p.isdigit() else (1, p.replace("~", "\0")) for p in parts]


def packages_index(host, suite, debarch):
    """Verified {package: [stanza, ...]} of one suite/arch, or {} if the suite has no such index."""
    try:
        inrel = get(f"https://{host}/dists/{suite}/InRelease")
    except Exception:
        return {}
    kr = next((k for k in KEYRINGS if os.path.exists(k)), None)
    if not kr:
        sys.exit("debian-archive-keyring is not installed (apt install debian-archive-keyring)")
    with tempfile.NamedTemporaryFile() as t:
        t.write(inrel); t.flush()
        r = subprocess.run(["gpgv", "--keyring", kr, t.name], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"{host}/{suite}: InRelease signature NOT valid:\n{r.stderr}")
    rel = inrel.decode(errors="replace")
    path = f"main/binary-{debarch}/Packages.xz"
    m = re.search(r"^ ([0-9a-f]{64})\s+\d+\s+" + re.escape(path) + r"$", rel.split("SHA256:", 1)[1], re.M)
    if not m:
        return {}
    raw = get(f"https://{host}/dists/{suite}/{path}")
    if hashlib.sha256(raw).hexdigest() != m.group(1):
        sys.exit(f"{host}/{suite}/{path}: sha256 mismatch")
    idx = {}
    for st in lzma.decompress(raw).decode(errors="replace").split("\n\n"):
        f = dict(re.findall(r"^([A-Za-z0-9-]+): (.*)$", st, re.M))
        if f.get("Package"):
            idx.setdefault(f["Package"], []).append(dict(f, _host=host, _suite=suite))
    return idx


def newest(pkgs, name, version=None):
    c = [p for p in pkgs.get(name, []) if version is None or p["Version"] == version]
    return max(c, key=lambda p: vkey(p["Version"])) if c else None


def fetch_deb(p, dest):
    raw = get(f"https://{p['_host']}/{p['Filename']}")
    if hashlib.sha256(raw).hexdigest() != p["SHA256"]:
        sys.exit(f"{p['Filename']}: sha256 mismatch")
    with tempfile.NamedTemporaryFile(suffix=".deb") as t:
        t.write(raw); t.flush()
        subprocess.run(["dpkg-deb", "-x", t.name, dest], check=True)
    return p["SHA256"]


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    out = os.path.abspath(sys.argv[1])
    suite = sys.argv[sys.argv.index("--suite") + 1] if "--suite" in sys.argv else "trixie"
    report = []
    for kdir, debarch, plat, sfx, http_copy in ARCHES:
        pkgs = {}
        for host, s in MIRRORS:
            for k, v in packages_index(host, s.format(s=suite), debarch).items():
                pkgs.setdefault(k, []).extend(v)
        signed = newest(pkgs, f"grub-efi-{debarch if debarch != 'i386' else 'ia32'}-signed")
        if not signed:
            sys.exit(f"{suite}/{debarch}: no signed grub package")
        # grub-efi-<arch>-signed's version is "1+<grub version>"; its modules come from grub-efi-<arch>-bin of that version
        gver = re.sub(r"^(\d+\.\d+)\+", r"\1-", re.sub(r"^1\+", "", signed["Version"]))   # 1+2.12+9+deb13u4 -> 2.12-9+deb13u4
        binname = f"grub-efi-{debarch if debarch != 'i386' else 'ia32'}-bin"
        m = re.search(r"Built-Using: .*?grub2 \(= ([^)]+)\)", "\n".join(f"{k}: {v}" for k, v in signed.items()))
        gver = m.group(1) if m else gver
        binp = newest(pkgs, binname, gver)
        if not binp:
            sys.exit(f"{suite}/{debarch}: {binname} {gver} (matching {signed['Package']} {signed['Version']}) not found")
        with tempfile.TemporaryDirectory() as x:
            h1 = fetch_deb(signed, x)
            h2 = fetch_deb(binp, x)
            src = os.path.join(x, "usr/lib/grub", plat + "-signed", f"grubnet{sfx}.efi.signed")
            if not os.path.isfile(src):
                sys.exit(f"{signed['Package']}: {src} missing")
            d = os.path.join(out, "tftp/debian-installer", kdir)
            os.makedirs(os.path.join(d, "grub", plat), exist_ok=True)
            shutil.copy2(src, os.path.join(d, f"grub{sfx}.efi"))
            modsrc = os.path.join(x, "usr/lib/grub", plat)
            n = 0
            for f in sorted(os.listdir(modsrc)):
                if f.endswith((".mod", ".lst", ".img")):
                    shutil.copy2(os.path.join(modsrc, f), os.path.join(d, "grub", plat, f)); n += 1
            if http_copy:
                os.makedirs(os.path.join(out, "http/http"), exist_ok=True)
                shutil.copy2(src, os.path.join(out, "http/http", f"grub{sfx}.efi"))
            sha = hashlib.sha256(open(src, "rb").read()).hexdigest()
        report.append(f"{kdir}: {signed['Package']} {signed['Version']} ({signed['_suite']}, deb sha256 {h1[:16]}) + "
                      f"{binp['Package']} {binp['Version']} ({n} modules, deb sha256 {h2[:16]}); grub{sfx}.efi sha256 {sha}")
        print(report[-1])
    open(os.path.join(out, "LOADERS.txt"), "w").write("\n".join(report) + "\n")


if __name__ == "__main__":
    main()
