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
import os
import urllib.error
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


# Cloudflare (which fronts civitai.com) blocks the default `Python-urllib/x.y`
# User-Agent with `error code: 1010` ("browser signature banned"). Setting a
# project-specific UA gets us through. The actual value doesn't matter much
# as long as it's not Python's default.
_VALIDATOR_UA = (
    "blockflow-validator/1.0 (+https://github.com/Hearmeman24/blockflow-presets)"
)


def _mask_civitai_token(url: str) -> str:
    """Strip ?token=... from a URL so error messages don't leak secrets."""
    import re
    return re.sub(r"([?&])token=[^&]+", r"\1token=REDACTED", url)


def check_url(url: str, timeout: int = 10) -> str | None:
    # CivitAI requires auth on every download URL. Two non-obvious quirks
    # discovered while wiring this up:
    #   1) Cloudflare blocks Python's default UA outright (error code 1010).
    #      Setting any reasonable User-Agent fixes that.
    #   2) Authorization: Bearer makes CivitAI redirect to S3 with a broken
    #      signature ("Missing x-amz-content-sha256") → 400. The endpoint's
    #      intended auth path is the `?token=<x>` query parameter, which
    #      redirects cleanly to a signed S3 URL → 200 on HEAD.
    # Contributors without CIVITAI_TOKEN still get a graceful skip on
    # 401/403 so they can run the validator locally without a token.
    is_civitai = "civitai.com" in url
    civitai_token = os.environ.get("CIVITAI_TOKEN", "")

    request_url = url
    if is_civitai and civitai_token:
        sep = "&" if "?" in url else "?"
        request_url = f"{url}{sep}token={civitai_token}"

    headers = {"User-Agent": _VALIDATOR_UA}
    # Use HEAD; both the non-civitai (GitHub raw etc.) and the civitai
    # token-query path return 200 on HEAD.
    method = "HEAD"

    try:
        req = urllib.request.Request(request_url, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status >= 400:
                return f"{method} {_mask_civitai_token(request_url)} returned {resp.status}"
    except urllib.error.HTTPError as exc:
        if is_civitai and not civitai_token and exc.code in (401, 403):
            return None
        return f"{method} {_mask_civitai_token(request_url)} failed: {exc}"
    except Exception as exc:
        return f"{method} {_mask_civitai_token(request_url)} failed: {exc}"
    return None


_LOCAL_URL_PREFIXES = (
    "https://github.com/Hearmeman24/blockflow-presets/raw/main/registry/",
    "https://raw.githubusercontent.com/Hearmeman24/blockflow-presets/main/registry/",
    "https://raw.githubusercontent.com/Hearmeman24/blockflow-presets/raw/main/registry/",
)


def _load_workflow_json(workflow_entry: dict, preset_dir: Path) -> tuple[dict | None, str | None]:
    """Resolve a preset.workflows[] entry to its parsed JSON body for
    cross-checking settings entries against. Returns (workflow_dict, warning).
    For external URLs (not the registry's own raw paths), returns (None, warning)
    so the caller can skip — we don't want CI to make network calls."""
    if isinstance(workflow_entry.get("json"), dict):
        return workflow_entry["json"], None
    url = workflow_entry.get("url")
    if not url:
        return None, "workflow entry has neither 'json' nor 'url'"
    for prefix in _LOCAL_URL_PREFIXES:
        if url.startswith(prefix):
            rel = url[len(prefix):]
            local_path = preset_dir.parent / rel
            if not local_path.exists():
                return None, f"workflow URL maps to local path {local_path} but file is missing"
            try:
                return json.loads(local_path.read_text()), None
            except json.JSONDecodeError as exc:
                return None, f"workflow at {local_path} is not valid JSON: {exc}"
    return None, f"workflow URL is external ({url}); skipping settings cross-check"


def check_workflow_settings(preset: dict, preset_path: Path) -> list[str]:
    """For each workflows[].settings entry, verify that node_id exists in the
    referenced workflow JSON and that field is a real input key on that node.
    External-URL workflows produce a warning but do not fail."""
    errors: list[str] = []
    preset_dir = preset_path.parent
    for w_idx, workflow_entry in enumerate(preset.get("workflows") or []):
        settings = workflow_entry.get("settings") or []
        if not settings:
            continue
        workflow_label = workflow_entry.get("name", f"workflows[{w_idx}]")
        body, warning = _load_workflow_json(workflow_entry, preset_dir)
        if body is None:
            if warning:
                print(f"  WARN ({workflow_label}): {warning}")
            continue
        for s_idx, setting in enumerate(settings):
            node_id = setting.get("node_id")
            field = setting.get("field")
            node = body.get(node_id) if isinstance(body, dict) else None
            if not isinstance(node, dict):
                errors.append(
                    f"{preset_path}: workflows[{w_idx}] ('{workflow_label}') "
                    f"settings[{s_idx}]: node_id '{node_id}' not found in workflow JSON"
                )
                continue
            inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
            if field not in inputs:
                errors.append(
                    f"{preset_path}: workflows[{w_idx}] ('{workflow_label}') "
                    f"settings[{s_idx}]: field '{field}' is not an input of node "
                    f"'{node_id}' (class_type={node.get('class_type')!r}, "
                    f"available inputs: {sorted(inputs.keys())})"
                )
    return errors


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

    # workflows[].settings cross-check against workflow JSON
    errors.extend(check_workflow_settings(preset, preset_path))

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
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="PRESET_ID",
        help=(
            "Restrict validation to one or more preset IDs (repeatable). "
            "Used by CI to validate just the preset(s) a PR touched, "
            "instead of the whole registry."
        ),
    )
    args = parser.parse_args()

    if not SCHEMA_PATH.exists():
        print(f"missing schema: {SCHEMA_PATH}", file=sys.stderr)
        return 2
    schema = json.loads(SCHEMA_PATH.read_text())

    preset_paths = sorted(REGISTRY_DIR.glob("*/preset.json"))
    if args.only:
        wanted = set(args.only)
        preset_paths = [p for p in preset_paths if p.parent.name in wanted]
        missing = wanted - {p.parent.name for p in preset_paths}
        if missing:
            print(
                f"--only specified {sorted(missing)} but no matching preset(s) found "
                f"under {REGISTRY_DIR}",
                file=sys.stderr,
            )
            return 2

    if not preset_paths:
        if args.only:
            print("nothing to validate after --only filter; exiting OK", flush=True)
            return 0
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
