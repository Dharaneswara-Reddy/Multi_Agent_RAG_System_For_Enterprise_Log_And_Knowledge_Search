#!/usr/bin/env bash
# Fetch real production log samples from LogHub (logpai/loghub, CC-BY licensed).
#
# These are used two ways:
#   1. as an overlay in the corpus, so retrieval and parsing face genuinely
#      inconsistent real-world formats rather than only the synthetic Meridian one
#   2. as the parser stress-test suite (tests/test_parsers.py), which asserts
#      100% parse coverage across all nine formats
#
# The synthetic Meridian corpus stays the evaluable backbone — it is the only
# part with a verifiable ground truth for the golden set.

set -euo pipefail

DEST="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/data/logs/external"
BASE="https://raw.githubusercontent.com/logpai/loghub/master"

FILES=(
  "HDFS/HDFS_2k.log"
  "OpenStack/OpenStack_2k.log"
  "Apache/Apache_2k.log"
  "Zookeeper/Zookeeper_2k.log"
  "Spark/Spark_2k.log"
  "Hadoop/Hadoop_2k.log"
  "Linux/Linux_2k.log"
  "Mac/Mac_2k.log"
  "Thunderbird/Thunderbird_2k.log"
)

mkdir -p "$DEST"
ok=0
for path in "${FILES[@]}"; do
  name="$(basename "$path")"
  if [ -s "$DEST/$name" ]; then
    echo "  skip   $name (already present)"
    ok=$((ok + 1))
    continue
  fi
  if curl -sfL --max-time 60 "$BASE/$path" -o "$DEST/$name"; then
    echo "  ok     $name ($(wc -l < "$DEST/$name") lines)"
    ok=$((ok + 1))
  else
    rm -f "$DEST/$name"
    echo "  FAILED $path" >&2
  fi
done

echo
echo "$ok/${#FILES[@]} log sources available in $DEST"
[ "$ok" -gt 0 ] || exit 1
