#!/usr/bin/env python3
"""Build a single `manifest.json` at the repo root from registry/*/preset.json.

BlockFlow fetches the manifest once (with a 1h cache) to enumerate available
presets. The per-preset JSON contains the full install recipe (workflow + model
list) and is fetched on-demand when the user clicks Install.

Usage:
    python tools/build_manifest.py            # writes manifest.json
    python tools/build_manifest.py --check    # exits 1 if manifest.json is stale
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_DIR = REPO_ROOT / "registry"
MANIFEST_PATH = REPO_ROOT / "manifest.json"

# Fields copied into the manifest (omits workflow + models — those stay in
# the per-preset JSON so the manifest stays small).
MANIFEST_FIELDS = (
    "id",
    "name",
    "description",
    "comfygen_min_version",
    "tags",
    "disk_size_estimate_gb",
)


def build_manifest() -> dict:
    presets: list[dict] = []
    for preset_path in sorted(REGISTRY_DIR.glob("*/preset.json")):
        data = json.loads(preset_path.read_text())
        entry = {k: data[k] for k in MANIFEST_FIELDS if k in data}
        # Hint at recommended GPU tier from the tested_against block if present.
        if "tested_against" in data and isinstance(data["tested_against"], dict):
            tier = data["tested_against"].get("gpu_tier")
            if tier:
                entry["gpu_tier_hint"] = tier
        # Where to fetch the full preset detail from.
        entry["preset_url"] = (
            f"https://raw.githubusercontent.com/Hearmeman24/blockflow-presets/"
            f"main/registry/{data['id']}/preset.json"
        )
        presets.append(entry)
    return {
        "manifest_version": 1,
        "presets": presets,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify manifest.json is up-to-date with registry/*. Exit 1 if stale.",
    )
    args = parser.parse_args()

    fresh = build_manifest()
    fresh_text = json.dumps(fresh, indent=2) + "\n"

    if args.check:
        if not MANIFEST_PATH.exists():
            print("manifest.json is missing. Run `python tools/build_manifest.py`.", file=sys.stderr)
            return 1
        if MANIFEST_PATH.read_text() != fresh_text:
            print(
                "manifest.json is stale. Run `python tools/build_manifest.py` "
                "and commit the result.",
                file=sys.stderr,
            )
            return 1
        print("manifest.json is fresh.")
        return 0

    MANIFEST_PATH.write_text(fresh_text)
    print(f"Wrote {MANIFEST_PATH.relative_to(REPO_ROOT)} ({len(fresh['presets'])} preset(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
