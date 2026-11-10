#!/bin/sh
# KwickBoot deploy gate: syntax-check a staged set of files; prints ALL-CHECKS-OK only if every check passes.
#   sh kb-gate.sh /tmp/kbchk
cd "${1:-/tmp/kbchk}" || exit 1
for f in *; do sed -i 's/\r$//' "$f"; done
for f in *.py; do [ -e "$f" ] || continue; python3 -m py_compile "$f" || { echo "FAIL $f"; exit 1; }; done
for f in *.sh; do [ -e "$f" ] || continue; sh -n "$f" || { echo "FAIL $f"; exit 1; }; done
if [ -e index.html ]; then
  python3 - <<'EOF' || exit 1
import re
s = open("index.html", encoding="utf-8").read()
open("ui.js", "w", encoding="utf-8").write("\n".join(re.findall(r"<script>(.*?)</script>", s, re.S)))
EOF
  node --check ui.js || { echo "FAIL index.html (JS)"; exit 1; }
fi
echo ALL-CHECKS-OK
