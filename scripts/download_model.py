#!/usr/bin/env python3
"""Download the pretrained square classifier (ChessLens, MIT License) and verify its checksum.

    python scripts/download_model.py            # to models/square_cnn.onnx (or $MODEL_PATH)
    python scripts/download_model.py PATH
"""

import hashlib
import os
import sys
import urllib.request
from pathlib import Path

# Pinned to a commit so the file can't change underneath us.
URL = (
    "https://raw.githubusercontent.com/zeljko1611/ChessLens/"
    "c4e27c1c8842a407500b074e1442a9d050ad9e6c/models/square_cnn.onnx"
)
SHA256 = "a7fb6e04cb840f1c309203593ab652fbb9e1ba410d968b61f3936baba5f1ca07"
DEFAULT = Path(__file__).resolve().parent.parent / "models" / "square_cnn.onnx"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    dest = Path(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MODEL_PATH", DEFAULT))
    if dest.is_file() and sha256(dest) == SHA256:
        print(f"{dest} is already up to date")
        return 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    print(f"downloading {URL}")
    try:
        with urllib.request.urlopen(URL, timeout=60) as r, open(part, "wb") as f:
            while chunk := r.read(1 << 16):
                f.write(chunk)
        if sha256(part) != SHA256:
            print("checksum mismatch; not installing the file", file=sys.stderr)
            return 1
        os.replace(part, dest)
    finally:
        part.unlink(missing_ok=True)
    print(f"saved {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
