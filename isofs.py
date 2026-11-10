"""isofs.py - read-only file lookup inside ISO 9660 images (Joliet long names when present). Stdlib only.
SPDX-License-Identifier: AGPL-3.0-or-later

Used to boot Windows PE straight from an ISO in the library: the server hands iPXE/wimboot the four boot files from
inside the ISO (bootmgr, BCD, boot.sdi, boot.wim) as plain HTTP downloads - fast and reliable, unlike emulating the
whole ISO as a disk (sanboot), which Windows' boot manager reads in tiny blocks and often stalls on.
Only lookups: an (offset, size) inside the image. Nothing is extracted to disk, nothing is written.
"""
import os
import struct
import threading

SECTOR = 2048
_CACHE = {}
_LOCK = threading.Lock()


def _read(f, lba, n):
    f.seek(lba * SECTOR)
    return f.read(n)


def _volumes(f):
    """(root_lba, root_size, joliet) for the Joliet SVD if present, else the primary volume descriptor."""
    pvd = None
    for i in range(16, 64):
        d = _read(f, i, SECTOR)
        if len(d) < SECTOR or d[1:6] != b"CD001":
            break
        t = d[0]
        root = d[156:156 + 34]
        lba, size = struct.unpack_from("<I", root, 2)[0], struct.unpack_from("<I", root, 10)[0]
        if t == 2 and d[88:91] in (b"%/@", b"%/C", b"%/E"):          # Joliet: UCS-2 long names
            return lba, size, True
        if t == 1 and pvd is None:
            pvd = (lba, size, False)
        if t == 255:
            break
    if not pvd:
        raise ValueError("not an ISO 9660 image")
    return pvd


def _entries(f, lba, size, joliet):
    data = _read(f, lba, size)
    i = 0
    while i < len(data):
        ln = data[i]
        if ln == 0:                                      # records never cross sectors: skip to the next one
            i = (i // SECTOR + 1) * SECTOR
            continue
        rec = data[i:i + ln]
        ext, dlen, flags, nlen = struct.unpack_from("<I", rec, 2)[0], struct.unpack_from("<I", rec, 10)[0], rec[25], rec[32]
        raw = rec[33:33 + nlen]
        i += ln
        if raw in (b"\x00", b"\x01"):
            continue
        name = raw.decode("utf-16-be", "replace") if joliet else raw.decode("latin-1")
        name = name.split(";")[0].rstrip(".")
        yield name, ext, dlen, bool(flags & 2)


# ---------------------------------------------------------------- UDF (all Windows media since Windows 8 are UDF;
# their ISO 9660 part is a stub with README.TXT). Subset: physical (type 1) partition maps, short/long allocation
# descriptors - what oscdimg and most mastering tools write. Metadata partitions (UDF 2.50+ type 2 maps) -> not handled.
class _UDF:
    def __init__(self, f):
        self.f = f
        avdp = _read(f, 256, SECTOR)
        if struct.unpack_from("<H", avdp, 0)[0] != 2:
            raise ValueError("no UDF anchor")
        vds_len, vds_loc = struct.unpack_from("<II", avdp, 16)
        self.part_start, self.bs, fsd = None, SECTOR, None
        for i in range(vds_len // SECTOR):
            d = _read(f, vds_loc + i, SECTOR)
            tag = struct.unpack_from("<H", d, 0)[0]
            if tag == 5:                                  # partition descriptor
                self.part_start = struct.unpack_from("<I", d, 188)[0]
            elif tag == 6:                                # logical volume descriptor
                self.bs = struct.unpack_from("<I", d, 212)[0]
                fsd = struct.unpack_from("<I", d, 252)[0]          # long_ad location (lbn) of the file set descriptor
                nmaps = struct.unpack_from("<I", d, 268)[0]
                if nmaps and d[440] != 1:
                    raise ValueError("UDF metadata partition not supported")
            elif tag == 8:
                break
        if self.part_start is None or fsd is None or self.bs != SECTOR:
            raise ValueError("unsupported UDF layout")
        fs = self._blk(fsd, SECTOR)
        if struct.unpack_from("<H", fs, 0)[0] != 256:
            raise ValueError("no UDF file set descriptor")
        self.root = struct.unpack_from("<I", fs, 404)[0]           # root directory ICB (long_ad lbn)

    def _blk(self, lbn, n):
        return _read(self.f, self.part_start + lbn, n)

    def entry(self, lbn):
        """(is_dir, size, [(byte offset, length)]) from a (Extended) File Entry."""
        d = self._blk(lbn, SECTOR)
        tag = struct.unpack_from("<H", d, 0)[0]
        if tag == 261:
            size, lea, lad, base = struct.unpack_from("<Q", d, 56)[0], *struct.unpack_from("<II", d, 168), 176
        elif tag == 266:
            size, lea, lad, base = struct.unpack_from("<Q", d, 56)[0], *struct.unpack_from("<II", d, 208), 216
        else:
            raise ValueError("not a UDF file entry")
        is_dir, kind = d[27] == 4, struct.unpack_from("<H", d, 34)[0] & 7     # icbtag file type, AD type
        ads, out = d[base + lea:base + lea + lad], []
        step = 8 if kind == 0 else 16 if kind == 1 else None
        if step is None:
            return is_dir, size, None                   # embedded or extended ADs: not handled (never seen on boot files)
        for i in range(0, len(ads) - step + 1, step):
            ln, pos = struct.unpack_from("<II", ads, i)
            if ln >> 30 == 3 or (ln & 0x3FFFFFFF) == 0:
                break
            out.append(((self.part_start + pos) * self.bs, ln & 0x3FFFFFFF))
        return is_dir, size, out

    def children(self, lbn):
        is_dir, size, ext = self.entry(lbn)
        if not is_dir or ext is None:
            return
        data = b"".join(_read_at(self.f, o, n) for o, n in ext)[:size]
        i = 0
        while i + 38 <= len(data):
            if struct.unpack_from("<H", data, i)[0] != 257:
                break
            chars, lfi = data[i + 18], data[i + 19]
            icb = struct.unpack_from("<I", data, i + 24)[0]
            liu = struct.unpack_from("<H", data, i + 36)[0]
            raw = data[i + 38 + liu:i + 38 + liu + lfi]
            i += (38 + liu + lfi + 3) & ~3
            if chars & 8 or not raw:                     # parent entry
                continue
            name = raw[1:].decode("utf-16-be", "replace") if raw[0] == 16 else raw[1:].decode("latin-1")
            yield name, icb


def _read_at(f, off, n):
    f.seek(off)
    return f.read(n)


def _lookup_udf(f, parts):
    u = _UDF(f)
    lbn = u.root
    for part in parts:
        lbn = next((icb for name, icb in u.children(lbn) if name.lower() == part.lower()), None)
        if lbn is None:
            return None
    is_dir, size, ext = u.entry(lbn)
    if is_dir or not ext or sum(n for _, n in ext) < size:
        return None
    return size, ext


def lookup(iso_path, inner):
    """(size, [(byte offset, length), ...]) of the file `inner` (e.g. 'sources/boot.wim', case-insensitive) inside the
    ISO, or None. ISO 9660/Joliet first, then UDF."""
    st = os.stat(iso_path)
    key = (iso_path, st.st_size, st.st_mtime, inner.lower())
    with _LOCK:
        if key in _CACHE:
            return _CACHE[key]
    parts = [p for p in inner.replace("\\", "/").split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        return None
    res = None
    with open(iso_path, "rb") as f:
        try:
            lba, size, joliet = _volumes(f)
            for n, part in enumerate(parts):
                hit = next(((e, s, d) for name, e, s, d in _entries(f, lba, size, joliet) if name.lower() == part.lower()), None)
                if not hit:
                    break
                lba, size, is_dir = hit
                if n == len(parts) - 1:
                    res = None if is_dir else (size, [(lba * SECTOR, size)])
                elif not is_dir:
                    break
        except (ValueError, struct.error):
            res = None
        if res is None:
            try:
                res = _lookup_udf(f, parts)
            except (ValueError, struct.error, IndexError):
                res = None
    if res and any(o + n > st.st_size for o, n in res[1]):
        res = None                                       # a damaged image must not make us read past its end
    with _LOCK:
        _CACHE[key] = res
    return res


def read_range(iso_path, found, start, end):
    """Yield the bytes [start, end] (inclusive) of a file located by lookup(), in chunks, across its extents."""
    size, ext = found
    end = min(end, size - 1)
    pos = 0
    with open(iso_path, "rb") as f:
        for off, ln in ext:
            a, b = max(start, pos), min(end, pos + ln - 1)
            if a <= b:
                f.seek(off + (a - pos))
                left = b - a + 1
                while left > 0:
                    chunk = f.read(min(left, 1 << 20))
                    if not chunk:
                        return
                    left -= len(chunk)
                    yield chunk
            pos += ln
            if pos > end:
                return


def listdir(iso_path, inner):
    """Names in a directory of the ISO ('' = root), ISO 9660/Joliet first, then UDF; [] if absent."""
    parts = [p for p in inner.replace("\\", "/").split("/") if p]
    with open(iso_path, "rb") as f:
        try:
            lba, size, joliet = _volumes(f)
            ok = True
            for part in parts:
                hit = next(((e, s) for name, e, s, d in _entries(f, lba, size, joliet) if d and name.lower() == part.lower()), None)
                if not hit:
                    ok = False
                    break
                lba, size = hit
            names = [n for n, _, _, _ in _entries(f, lba, size, joliet)] if ok else []
            if names and names != ["README.TXT"]:          # Windows media: the ISO 9660 part is only a stub
                return names
        except (ValueError, struct.error):
            pass
        try:
            u = _UDF(f)
            lbn = u.root
            for part in parts:
                lbn = next((icb for name, icb in u.children(lbn) if name.lower() == part.lower()), None)
                if lbn is None:
                    return []
            return [n for n, _ in u.children(lbn)]
        except (ValueError, struct.error, IndexError):
            return []


def winpe_files(iso_path, efi=False):
    """The wimboot file set of a Windows PE / Windows Setup ISO, or None if it is not one.
    Returns [(inner path, name)]. wimboot serves each file by name in every directory, so a PE build that keeps its
    image as boot/BOOT.WIM boots like Microsoft's sources/boot.wim. install.wim/esd (Setup's multi-GB payload) is skipped."""
    try:
        bcd = "efi/microsoft/boot/bcd" if efi else "boot/bcd"
        if not lookup(iso_path, bcd) or not lookup(iso_path, "boot/boot.sdi"):
            return None
        wims = [f"{d}/{n}" for d in ("sources", "boot") for n in listdir(iso_path, d)
                if n.lower().endswith(".wim") and not n.lower().startswith("install")]
        if not wims:
            return None
        files = [(bcd, "BCD"), ("boot/boot.sdi", "boot.sdi")] + [(w, w.split("/")[-1].lower()) for w in wims]
        if efi and lookup(iso_path, "efi/boot/bootx64.efi"):
            files.insert(0, ("efi/boot/bootx64.efi", "bootx64.efi"))
        elif not efi and lookup(iso_path, "bootmgr"):
            files.insert(0, ("bootmgr", "bootmgr"))
        return files
    except (OSError, ValueError, struct.error):
        return None
