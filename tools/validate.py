#!/usr/bin/env python3
"""CI validator for the BlockFlow preset registry.

Validates every preset under registry/<id>/preset.json against the schema,
sanity-checks disk_size_estimate_gb against the sum of model sizes, and (if
--check-urls is passed) does a HEAD request on every model URL to confirm
reachability.

Usage:
    python tools/validate.py                 # schema + size-sum checks
    python tools/validate.py --check-urls    # also verify URLs (slow; CI)
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

try:
    import jsonschema
except ImportError:
    print("jsonschema not installed. Run: pip install jsonschema", file=sys.stderr)
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "schema" / "preset.schema.json"
REGISTRY_DIR = REPO_ROOT / "registry"

SIZE_SUM_TOLERANCE = 0.10  # accept ±10% mismatch between sum-of-models and disk_size_estimate_gb


def check_size_sum(preset: dict, source: Path) -> str | None:
    """Returns an error message if disk_size_estimate_gb diverges too much from
    the sum of model.size_gb fields."""
    models = preset.get("models", [])
    estimate = preset.get("disk_size_estimate_gb", 0)
    total = sum(m.get("size_gb", 0) for m in models)
    if total == 0:
        return f"{source}: total model size is 0 (must be > 0)"
    # Accept estimates that are between (sum * 1.0) and (sum * 1.4) to allow
    # for a buffer; reject when estimate is < sum (under-promises disk).
    if estimate < total:
        return (
            f"{source}: disk_size_estimate_gb ({estimate}) is less than sum of "
            f"model sizes ({total:.1f}). Estimate must cover all models + buffer."
        )
    if estimate > total * (1 + SIZE_SUM_TOLERANCE) * 2:
        return (
            f"{source}: disk_size_estimate_gb ({estimate}) is more than double "
            f"the sum of model sizes ({total:.1f}). Over-estimating wastes volume."
        )
    return None


def check_url(url: str, timeout: int = 10) -> str | None:
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status >= 400:
                return f"HEAD {url} returned {resp.status}"
    except Exception as exc:
        return f"HEAD {url} failed: {exc}"
    return None


def validate_preset(preset_path: Path, schema: dict, check_urls: bool) -> list[str]:
    errors: list[str] = []
    try:
        preset = json.loads(preset_path.read_text())
    except json.JSONDecodeError as exc:
        return [f"{preset_path}: invalid JSON: {exc}"]

    # Schema validation
    try:
        jsonschema.validate(instance=preset, schema=schema)
    except jsonschema.ValidationError as exc:
        errors.append(f"{preset_path}: schema violation: {exc.message} (at {list(exc.path)})")
        return errors  # don't continue if schema fails

    # ID matches directory name
    expected_id = preset_path.parent.name
    if preset["id"] != expected_id:
        errors.append(
            f"{preset_path}: id '{preset['id']}' doesn't match directory name '{expected_id}'"
        )

    # Size-sum sanity
    size_err = check_size_sum(preset, preset_path)
    if size_err:
        errors.append(size_err)

    # URL reachability (optional, CI-only)
    if check_urls:
        for m in preset.get("models", []):
            err = check_url(m["url"])
            if err:
                errors.append(f"{preset_path}: model URL: {err}")
        workflow = preset.get("workflow", {})
        if "url" in workflow:
            err = check_url(workflow["url"])
            if err:
                errors.append(f"{preset_path}: workflow URL: {err}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check-urls",
        action="store_true",
        help="HEAD-check every model URL for reachability (slow; CI use).",
    )
    args = parser.parse_args()

    if not SCHEMA_PATH.exists():
        print(f"missing schema: {SCHEMA_PATH}", file=sys.stderr)
        return 2
    schema = json.loads(SCHEMA_PATH.read_text())

    preset_paths = sorted(REGISTRY_DIR.glob("*/preset.json"))
    if not preset_paths:
        print(f"no presets found under {REGISTRY_DIR}", file=sys.stderr)
        return 2

    all_errors: list[str] = []
    for path in preset_paths:
        errs = validate_preset(path, schema, args.check_urls)
        if errs:
            all_errors.extend(errs)
            print(f"FAIL {path.relative_to(REPO_ROOT)}")
            for e in errs:
                print(f"  - {e}")
        else:
            print(f"OK   {path.relative_to(REPO_ROOT)}")

    if all_errors:
        print(f"\n{len(all_errors)} error(s) across {len(preset_paths)} preset(s)", file=sys.stderr)
        return 1
    print(f"\n{len(preset_paths)} preset(s) validated successfully")
    return 0


if __name__ == "__main__":
    sys.exit(main())
