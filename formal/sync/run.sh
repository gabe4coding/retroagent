#!/usr/bin/env bash
# Model-check the sync models with TLC and compare each result with the expected one.
#
#   formal/sync/run.sh [config ...]     default: every *.cfg here
#
# Sync*.cfg check Sync.tla, Cloud*.cfg check CloudInbox.tla. A config whose name has no suffix (SyncMC, CloudMC)
# must pass. Every other config must fail: it turns a bug on, or it is a sanity check.
# Needs Java 11+ (`java` on PATH, or JAVA_HOME) and curl. Downloads, then checks by sha256: tla2tools.jar v1.8.0 and two scripts of
# opum-ai/proof-skills (MIT) at a fixed commit. Cache: $KB_FORMAL_CACHE or ~/.cache/retroagent-formal.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CACHE="${KB_FORMAL_CACHE:-$HOME/.cache/retroagent-formal}"
PS="https://raw.githubusercontent.com/opum-ai/proof-skills/578749fd8baa6456d89d7a0037c88bc09a89213d/skills/tlaplus-model/scripts"
FILES=(
  "tla2tools.jar https://github.com/tlaplus/tlaplus/releases/download/v1.8.0/tla2tools.jar 5bd7b64db99ae4c6c35f290de5e809c0ee41af43f8699cf696099ffb6452b1e2"
  "tlc.sh $PS/tlc.sh d595e838320123e3947522b8c0e657b1740edbab336ddc8debe47fe5f9e498e5"
  "parse_tlc_trace.py $PS/parse_tlc_trace.py 3431c4b7ee891239141b6e445d323e702be768b1a510f7037c7a42059b71ff60"
)
mkdir -p "$CACHE"
command -v java >/dev/null 2>&1 || { [ -n "${JAVA_HOME:-}" ] && export PATH="$JAVA_HOME/bin:$PATH"; }
command -v java >/dev/null 2>&1 || { echo "no java: install Java 11+ or set JAVA_HOME" >&2; exit 2; }
for entry in "${FILES[@]}"; do
  read -r name url sum <<<"$entry"
  if ! echo "$sum  $CACHE/$name" | sha256sum -c --status 2>/dev/null; then
    curl -fsSL "$url" -o "$CACHE/$name.tmp" || { echo "download failed: $url" >&2; exit 2; }
    echo "$sum  $CACHE/$name.tmp" | sha256sum -c --status || { echo "sha256 mismatch: $url" >&2; rm -f "$CACHE/$name.tmp"; exit 2; }
    mv "$CACHE/$name.tmp" "$CACHE/$name"
  fi
done
chmod +x "$CACHE/tlc.sh"

cd "$HERE"
[ $# -gt 0 ] && configs=("$@") || configs=(*.cfg)
fails=0
printf '%-26s %-6s %-8s %-9s %-6s %-6s %s\n' config expect result states depth time violated
for cfg in "${configs[@]}"; do
  cfg="$(basename "$cfg" .cfg)"
  case "$cfg" in Sync*) spec=Sync.tla ;; Cloud*) spec=CloudInbox.tla ;; *) echo "unknown config: $cfg" >&2; exit 2 ;; esac
  case "$cfg" in SyncMC|CloudMC) expect=pass ;; *) expect=fail ;; esac
  PROOF_SKILLS_TOOLS="$CACHE" TLA2TOOLS="$CACHE/tla2tools.jar" "$CACHE/tlc.sh" "$spec" --config "$cfg.cfg" > /dev/null 2>&1
  rc=$?
  log=".tlc-out/$cfg/tlc.log"
  [ -f "$log" ] || { echo "$cfg: no TLC log (exit $rc)" >&2; fails=$((fails + 1)); continue; }
  states=$(grep -oE '^[0-9]+ states generated, [0-9]+ distinct' "$log" | tail -1 | grep -oE '[0-9]+ distinct' | cut -d' ' -f1)
  depth=$(grep -oE 'depth of the complete state graph search is [0-9]+' "$log" | grep -oE '[0-9]+$')
  took=$(sed -nE 's/^Finished in ([0-9a-z ]+) at .*/\1/p' "$log")
  what=$(grep -oE '(Invariant|Temporal property) [A-Za-z]+ (is|was) violated' "$log" | head -1 | awk '{print $(NF-2)}')
  case $rc in 0) got=pass ;; 12|13|11) got=fail ;; *) got="error($rc)" ;; esac
  [ "$got" = "$expect" ] || fails=$((fails + 1))
  printf '%-26s %-6s %-8s %-9s %-6s %-6s %s\n' "$cfg" "$expect" "$got" "${states:-?}" "${depth:-?}" "${took:-?}" "${what:--}"
done
[ "$fails" = 0 ] && echo "OK: every config gave the expected result" || echo "FAILED: $fails config(s) did not give the expected result"
exit $(( fails > 0 ))
