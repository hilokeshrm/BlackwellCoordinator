#!/bin/sh
# Downloads the pinned adapter on first start (weights persist in the jev_models volume).
set -eu
case "${JEV_CHECKPOINT:-9b}" in
  9b|9B) REPO=ZefanCai/Open-Jev-9B; REV=47e966881e489511c0c7f5633a9e1960a676a551 ;;
  2b|2B) REPO=ZefanCai/Open-Jev-2B; REV=0c7aa498b1627be8da4acf34c863ff0ee0a92785 ;;
  *) echo "JEV_CHECKPOINT must be 9b or 2b" >&2; exit 1 ;;
esac
DIR="/models/$(basename "$REPO")-$REV"
if [ ! -f "$DIR/.complete" ]; then
  hf download "$REPO" --revision "$REV" --local-dir "$DIR"
  touch "$DIR/.complete"
fi
CKPT=$(dirname "$(find "$DIR" -name temperature.json | head -1)")
exec python -m jev.server --checkpoint "$CKPT" --device cuda:0 \
  --max-length "${JEV_MAX_LENGTH:-4096}" --batch-size "${JEV_BATCH_SIZE:-16}" \
  --host 0.0.0.0 --port 8791
