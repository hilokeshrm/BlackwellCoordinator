"""Throwaway training projects written into pytest temp dirs. They sleep instead of using the GPU."""
import json
from pathlib import Path

FINETUNE_RAW = '''import argparse, json, math, os, random, time

ap = argparse.ArgumentParser()
ap.add_argument("--config", default="config.json")
args = ap.parse_args()
cfg = json.load(open(args.config))
EPOCH_SECONDS = float(os.environ.get("SAMPLE_EPOCH_SECONDS", cfg["epoch_seconds"]))
random.seed(0)
weights = {"lora_a": 0.0}
for epoch in range(cfg["epochs"]):
    time.sleep(EPOCH_SECONDS)
    weights["lora_a"] += cfg["lr"] * random.random()
    loss = 2.5 * math.exp(-0.18 * (epoch + 1))
    print(f"INFO epoch {epoch + 1}/{cfg['epochs']} loss={loss:.4f}", flush=True)
json.dump(weights, open("adapter.json", "w"))
'''

FINETUNE_PATCHED = '''import argparse, json, math, os, random, time
# BLACKWELL:START
from blackwell_sdk import Blackwell
# BLACKWELL:END

ap = argparse.ArgumentParser()
ap.add_argument("--config", default="config.json")
args = ap.parse_args()
cfg = json.load(open(args.config))
EPOCH_SECONDS = float(os.environ.get("SAMPLE_EPOCH_SECONDS", cfg["epoch_seconds"]))
random.seed(0)
weights = {"lora_a": 0.0}
# BLACKWELL:START
bw = Blackwell(total_steps=cfg["epochs"])
start = 0
state = bw.load_json()
if state:
    weights = state["weights"]
    start = int(bw.latest_checkpoint().step)
# BLACKWELL:END
print(f"INFO finetuning from epoch {start}", flush=True)
for epoch in range(start, cfg["epochs"]):
    time.sleep(EPOCH_SECONDS)
    weights["lora_a"] += cfg["lr"] * random.random()
    loss = 2.5 * math.exp(-0.18 * (epoch + 1))
    print(f"INFO epoch {epoch + 1}/{cfg['epochs']} loss={loss:.4f}", flush=True)
    # BLACKWELL:START
    n = epoch + 1
    bw.progress(n, metrics={"loss": loss})
    if bw.checkpoint_due(n, every=5) or bw.should_stop:
        bw.checkpoint_json(n, {"weights": weights})
    if bw.should_stop:
        bw.exit_paused()
    # BLACKWELL:END
json.dump(weights, open("adapter.json", "w"))
# BLACKWELL:START
bw.complete()
# BLACKWELL:END
'''

PRETRAIN_PATCHED = '''import os, sys, time
# BLACKWELL:START
from blackwell_sdk import Blackwell
# BLACKWELL:END

EPOCHS = 100
EPOCH_SECONDS = float(os.environ.get("SAMPLE_EPOCH_SECONDS", 18))


def main():
    state = {"loss": 11.0}
    # BLACKWELL:START
    bw = Blackwell(total_steps=EPOCHS)
    start = 0
    saved = bw.load_json()
    if saved:
        state = saved
        start = int(bw.latest_checkpoint().step)
    # BLACKWELL:END
    print(f"INFO pretraining from epoch {start}", flush=True)
    for epoch in range(start, EPOCHS):
        time.sleep(EPOCH_SECONDS)
        state["loss"] = max(1.8, state["loss"] * 0.975)
        print(f"INFO epoch {epoch + 1}/{EPOCHS} loss={state['loss']:.3f}", flush=True)
        # BLACKWELL:START
        n = epoch + 1
        bw.progress(n, metrics={"loss": state["loss"]})
        bw.checkpoint_json(n, state)
        if bw.should_stop:
            bw.exit_paused()
        # BLACKWELL:END
    # BLACKWELL:START
    bw.complete()
    # BLACKWELL:END
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def make_finetune(dst: Path, patched: bool) -> Path:
    dst.mkdir(parents=True)
    (dst / "train.py").write_text(FINETUNE_PATCHED if patched else FINETUNE_RAW)
    (dst / "config.json").write_text(json.dumps({"epochs": 20, "epoch_seconds": 30, "lr": 0.0002}))
    (dst / "README.md").write_text("Quick LoRA finetune, about 2 hours.\n")
    return dst


def make_pretrain(dst: Path) -> Path:
    dst.mkdir(parents=True)
    (dst / "train.py").write_text(PRETRAIN_PATCHED)
    (dst / "README.md").write_text("Long pretraining run, about 33 hours.\n")
    return dst
