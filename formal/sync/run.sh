#!/usr/bin/env bash
# Model-check the sync models with TLC and compare each result with the expected one.
#
#   formal/sync/run.sh [config ...]     default: every *.cfg here
#
# Sync*.cfg check Sync.tla, Cloud*.cfg check CloudInbox.tla. SyncMC and CloudMC must pass. Every other
# config must violate the one property it declares (`Expect: Name violated` in the .cfg). A different
# property, a deadlock, a TLC error or a parse error is a failure.
# Needs Java 11+ (`java` on PATH, or JAVA_HOME), curl, and sha256sum or shasum. macOS has no sha256sum;
# shasum -a 256 is used there. Downloads, then checks by sha256: tla2tools.jar v1.8.0 and two scripts of
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
if command -v sha256sum >/dev/null 2>&1; then
  SHA=sha256sum
elif command -v shasum >/dev/null 2>&1; then
  SHA=shasum
else
  echo "no sha256sum or shasum: install coreutils, or use macOS (shasum is already there)" >&2
  exit 2
fi
# sha256 of $1, or empty and non-zero when the file is missing or unreadable.
sha256_of() {
  if [ "$SHA" = sha256sum ]; then
    sha256sum "$1" 2>/dev/null | awk '{print $1}'
  else
    shasum -a 256 "$1" 2>/dev/null | awk '{print $1}'
  fi
}
for entry in "${FILES[@]}"; do
  read -r name url sum <<<"$entry"
  got=""
  got=$(sha256_of "$CACHE/$name") || true
  if [ "$got" != "$sum" ]; then
    curl -fsSL "$url" -o "$CACHE/$name.tmp" || { echo "download failed: $url" >&2; exit 2; }
    got=$(sha256_of "$CACHE/$name.tmp") || true
    [ "$got" = "$sum" ] || { echo "sha256 mismatch: $url" >&2; rm -f "$CACHE/$name.tmp"; exit 2; }
    mv "$CACHE/$name.tmp" "$CACHE/$name"
  fi
done
chmod +x "$CACHE/tlc.sh"

cd "$HERE"
[ $# -gt 0 ] && configs=("$@") || configs=(*.cfg)
fails=0
printf '%-26s %-18s %-10s %-9s %-6s %-6s %s\n' config expect result states depth time violated
for cfg in "${configs[@]}"; do
  cfg="$(basename "$cfg" .cfg)"
  case "$cfg" in Sync*) spec=Sync.tla ;; Cloud*) spec=CloudInbox.tla ;; *) echo "unknown config: $cfg" >&2; exit 2 ;; esac
  case "$cfg" in
    SyncMC|CloudMC) expect=pass; want="" ;;
    *)
      expect=fail
      want=$(grep -oE 'Expect: [A-Za-z0-9_]+ violated' "$cfg.cfg" | awk 'NR==1 {print $2}')
      if [ -z "$want" ]; then
        echo "$cfg: failing config does not declare an expected property (Expect: Name violated)" >&2
        fails=$((fails + 1))
        continue
      fi
      expect="$want"
      ;;
  esac
  PROOF_SKILLS_TOOLS="$CACHE" TLA2TOOLS="$CACHE/tla2tools.jar" "$CACHE/tlc.sh" "$spec" --config "$cfg.cfg" > /dev/null 2>&1
  rc=$?
  log=".tlc-out/$cfg/tlc.log"
  [ -f "$log" ] || { echo "$cfg: no TLC log (exit $rc)" >&2; fails=$((fails + 1)); continue; }
  states=$(grep -oE '^[0-9]+ states generated, [0-9]+ distinct' "$log" | tail -1 | grep -oE '[0-9]+ distinct' | cut -d' ' -f1)
  depth=$(grep -oE 'depth of the complete state graph search is [0-9]+' "$log" | grep -oE '[0-9]+$')
  took=$(sed -nE 's/^Finished in ([0-9a-z ]+) at .*/\1/p' "$log")
  # TLC: "Invariant N is violated", "Temporal property N was violated", "Action property N is violated".
  what=$(grep -oE '(Invariant|Temporal property|Action property|Property) [A-Za-z0-9_]+ (is|was) violated' "$log" | head -1 | awk '{print $(NF-2)}')
  case $rc in
    0) got=pass ;;
    12|13) got=fail ;;
    11) got=deadlock ;;
    *) got="error($rc)" ;;
  esac
  if [ -z "$want" ]; then
    [ "$got" = pass ] || fails=$((fails + 1))
  elif [ "$got" != fail ] || [ "$what" != "$want" ]; then
    fails=$((fails + 1))
  fi
  printf '%-26s %-18s %-10s %-9s %-6s %-6s %s\n' "$cfg" "$expect" "$got" "${states:-?}" "${depth:-?}" "${took:-?}" "${what:--}"
done
[ "$fails" = 0 ] && echo "OK: every config gave the expected result" || echo "FAILED: $fails config(s) did not give the expected result"
exit $(( fails > 0 ))
