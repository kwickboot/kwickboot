#!/usr/bin/env python3
# gen-console-i18n.py <out-dir> - emit shell env files (<lang>.env) of the kb-console label strings, one per language whose
# script the console bitmap font can render (Latin + Cyrillic). kb-console sources /usr/share/kb-i18n/$lang.env and draws
# each label via ${C_model:-Model} etc. (English fallback). CJK / Arabic / Thai / Devanagari are deliberately skipped - the
# console font cannot render them; those users get the localised UI from the framebuffer GUI (kbgui) or the browser tier.
# Source of truth: http/local/i18n.js (same as the GUI chrome). SPDX-License-Identifier: AGPL-3.0-or-later
import sys, os, re
OUT = sys.argv[1] if len(sys.argv) > 1 else "."
os.makedirs(OUT, exist_ok=True)
HERE = os.path.dirname(os.path.abspath(__file__))
I18N_JS = os.path.join(HERE, "..", "http", "local", "i18n.js")

# console label var -> i18n key. Missing key -> the English default stays (kb-console supplies it).
KEYS = {
 "subtitle": "subtitle", "machine": "thisMachine", "model": "model", "serial": "serial",
 "firmware": "firmware", "cpu": "cpu", "memory": "memory", "disks": "disks",
 "network": "network", "address": "address", "status": "status", "ready": "ready",
}
# languages the console font (CyrAsia-Terminus: Latin + Cyrillic) can render; others are skipped on purpose.
LATIN_CYRILLIC = {"en","es","fr","de","pt","it","nl","pl","ru","uk","tr","cs","sv"}

PAIR = re.compile(r'(\w+)\s*:\s*"((?:[^"\\]|\\.)*)"')
def load_i18n(path):
    langs = {}
    try: text = open(path, encoding="utf-8").read()
    except OSError: return langs
    for ln in text.splitlines():
        m = re.match(r'\s*([a-z]{2}):\{', ln)
        if not m: continue
        d = langs.setdefault(m.group(1), {})
        for k, v in PAIR.findall(ln):
            d.setdefault(k, v)
    return langs

def shq(s):                                        # single-quote for /bin/sh
    return "'" + s.replace("'", "'\\''") + "'"

I18N = load_i18n(I18N_JS)
written = []
for lang in sorted(LATIN_CYRILLIC):
    L = I18N.get(lang)
    if not L: continue
    lines = ["# kb-console labels for %s (auto-generated from i18n.js)" % lang]
    for var, key in KEYS.items():
        val = L.get(key)
        if val: lines.append("C_%s=%s" % (var, shq(val)))
    open(os.path.join(OUT, lang + ".env"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
    written.append(lang)
print("kb-console i18n:", " ".join(written), "(%d langs)" % len(written))
