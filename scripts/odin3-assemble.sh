#!/usr/bin/env bash
#
# Assemble the build tree: an upstream checkout with our work laid on top.
#
# Two mechanisms, deliberately kept apart:
#
#   overlay/       files we ADD, at the path they take in the tree. A path that
#                  already exists upstream is refused rather than overwritten -
#                  overwriting silently reverts whatever upstream changed in that
#                  file, with nothing to notice it.
#
#   tree-patches/  changes to a file upstream OWNS, applied in name order. Must
#                  apply cleanly; if upstream moved the code the patch stops the
#                  build instead of quietly restoring our older copy.
#
# Run from the root of the upstream checkout:
#   bash /path/to/odin3/scripts/odin3-assemble.sh /path/to/odin3
set -euo pipefail

SRC=${1:?usage: odin3-assemble.sh <odin3 checkout>}
[ -d "$SRC/tree-patches" ] || { echo "::error::$SRC is not an odin3 checkout"; exit 1; }
[ -f Containerfile ] && [ -d packages ] || { echo "::error::run from the root of an armada checkout"; exit 1; }

# ------------------------------------------------------------------- added
added=0
collisions=0
while IFS= read -r -d '' f; do
  rel=${f#"$SRC/overlay/"}
  if [ -e "$rel" ]; then
    echo "::error file=$rel::we would overwrite a file upstream already has."
    echo "         If upstream adopted this change, delete ours. If the name"
    echo "         clashes, rename ours. To modify it, use tree-patches/."
    collisions=$((collisions + 1))
  fi
done < <(find "$SRC/overlay" -type f ! -name .keep -print0)

if [ "$collisions" -gt 0 ]; then
  echo "::error::$collisions file(s) collide with upstream - refusing to assemble"
  exit 1
fi

while IFS= read -r -d '' f; do
  rel=${f#"$SRC/overlay/"}
  mkdir -p "$(dirname "$rel")"
  cp -p "$f" "$rel"
  echo "  added   $rel"
  added=$((added + 1))
done < <(find "$SRC/overlay" -type f ! -name .keep -print0 | sort -z)

# ---------------------------------------------------------- tree-patches
applied=0
shopt -s nullglob
for p in "$SRC"/tree-patches/*.patch; do
  # --3way would let a patch apply against moved context by guessing; that is
  # exactly the silent drift this is meant to catch, so plain apply only.
  if git apply --whitespace=nowarn "$p"; then
    echo "  patched $(basename "$p")"
    applied=$((applied + 1))
  else
    echo "::error file=$(basename "$p")::patch no longer applies - upstream changed"
    echo "         the file it edits. Rebase the patch, or drop it if upstream"
    echo "         has fixed the same thing."
    exit 1
  fi
done
shopt -u nullglob

if [ "$added" -eq 0 ] && [ "$applied" -eq 0 ]; then
  echo "::error::nothing was assembled - no files added and no tree-patches."
  echo "         A build from this tree would be stock Armada published as ours."
  exit 1
fi

# An image whose policy does not trust our own repository installs fine and then
# rejects every update after it. Refuse to produce one.
if ! python3 -c '
import json, sys
docker = json.load(open("system_files/etc/containers/policy.json"))["transports"]["docker"]
sys.exit(0 if docker.get("ghcr.io/allolive/armada") else 1)
'; then
  echo "::error file=system_files/etc/containers/policy.json::the image would not trust"
  echo "         ghcr.io/allolive/armada, so a device running it could never verify"
  echo "         the next update. Add the signing tree-patch first (see README)."
  exit 1
fi

echo "assembled: $added file(s) added, $applied patch(es) applied"
