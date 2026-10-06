#!/usr/bin/env python3
"""Seal one native worker's checkpoint before its assignment deadline."""
import argparse
import json
from pathlib import Path
import time
import shutil

from watch import atomic_json


def complete(worker):
    assignment = json.loads((worker / "native-assignment.json").read_text())
    if (worker / "native-closed.json").exists() or time.time() >= assignment["deadline_unix"]:
        raise ValueError("Native worker is already closed or past its deadline")
    result = json.loads((worker / "research-result.json").read_text())
    evidence = worker / "evidence"
    if any(p.is_symlink() for p in evidence.rglob("*")):
        raise ValueError("Native evidence must not contain symlinks")
    sealed = worker / "native-sealed"
    sealed.mkdir(mode=0o700)
    shutil.copytree(evidence, sealed / "evidence")
    atomic_json(sealed / "research-result.json", result)
    finished = time.time()
    if (worker / "native-closed.json").exists() or finished >= assignment["deadline_unix"]:
        raise ValueError("Native worker closed while sealing its result")
    atomic_json(worker / "native-complete.json", {"job_id": assignment["job_id"], "finished_unix": finished})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("worker", type=Path)
    complete(parser.parse_args().worker)
