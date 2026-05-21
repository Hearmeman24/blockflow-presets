#!/usr/bin/env python3
"""CI live-install validator: full provision → install → generate → teardown
for a single preset against real RunPod.

Used by .github/workflows/validate.yml's live-install job (triggered when a
maintainer adds the `validate-live` label to a preset PR). Self-contained: no
BlockFlow dependency. Talks to RunPod's REST + GraphQL APIs directly via the
`requests` library + shells out to the `comfy-gen` CLI for model download +
workflow submission.

Bounded cost: ~$1 per run (mostly H100 inference + worker minutes for the
download). Real wall-clock: 15-25 min for the larger presets.

Required env (CI provides via GitHub Actions secrets):
    RUNPOD_API_KEY                  — RunPod API key
    PRESET_TEST_S3_ACCESS           — AWS / R2 access key for ComfyGen output
    PRESET_TEST_S3_SECRET           — secret
    PRESET_TEST_S3_BUCKET           — bucket for SaveImage outputs
    PRESET_TEST_S3_REGION           — e.g. eu-west-2 or 'auto' for R2
    PRESET_TEST_S3_ENDPOINT_URL     — empty for AWS, set for R2

Required argv:
    preset_id                       — directory name under registry/<id>/

Tier: hardcoded to 'performance' (H100 US-KS-2) — proven capacity, fastest
worker spin-up. Volume size derived from preset.disk_size_estimate_gb + 20GB
safety buffer.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
GRAPHQL_URL = "https://api.runpod.io/graphql"
REST_BASE = "https://rest.runpod.io/v1"
V2_BASE = "https://api.runpod.ai/v2"
BASE_DOCKER_IMAGE = "hearmeman/comfyui-serverless:v17"
RUNTIME_REPO_URL = "https://github.com/Hearmeman24/remote-comfy-gen-handler"
ALLOWED_CUDA_VERSIONS = ["12.9", "12.8"]
USER_AGENT = "blockflow-presets-ci/0.1"

PERFORMANCE_TIER = {
    "name": "Performance",
    "gpu_ids": ["NVIDIA H100 NVL", "NVIDIA H100 PCIe"],
    "datacenter": "US-KS-2",
}


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# === RunPod API (minimal port of sgs-ui's runpod_api.py) ====================

def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }


def _http_json(
    method: str, url: str, api_key: str, body: dict | None = None, timeout: int = 30,
) -> dict[str, Any]:
    data = json.dumps(body or {}).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=_headers(api_key))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8")
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        snippet = e.read().decode("utf-8", errors="replace")[:600]
        raise RuntimeError(f"HTTP {e.code} {method} {url}: {snippet}") from e


def graphql(api_key: str, query: str) -> dict[str, Any]:
    resp = _http_json("POST", GRAPHQL_URL, api_key, body={"query": query}, timeout=30)
    if "errors" in resp:
        raise RuntimeError(f"GraphQL errors: {resp['errors']}")
    return resp.get("data", {})


def create_network_volume(api_key: str, *, name: str, size_gb: int, datacenter_id: str) -> dict:
    return _http_json("POST", f"{REST_BASE}/networkvolumes", api_key, body={
        "name": name, "size": size_gb, "dataCenterId": datacenter_id,
    })


def delete_network_volume(api_key: str, volume_id: str) -> None:
    _http_json("DELETE", f"{REST_BASE}/networkvolumes/{volume_id}", api_key)


def create_template(api_key: str, *, name: str, env: dict[str, str]) -> dict:
    env_pairs = ", ".join(
        f'{{ key: "{k}", value: "{v}" }}' for k, v in env.items()
    )
    mutation = f"""
    mutation {{
      saveTemplate(input: {{
        name: "{name}",
        imageName: "{BASE_DOCKER_IMAGE}",
        isServerless: true,
        env: [{env_pairs}],
        volumeInGb: 0,
        containerDiskInGb: 5
      }}) {{
        id
        name
      }}
    }}
    """
    data = graphql(api_key, mutation)
    return data["saveTemplate"]


def delete_template(api_key: str, *, template_name: str) -> None:
    mutation = f'mutation {{ deleteTemplate(templateName: "{template_name}") }}'
    graphql(api_key, mutation)


def create_endpoint(
    api_key: str,
    *,
    name: str,
    template_id: str,
    gpu_type_ids: list[str],
    network_volume_id: str,
    workers_max: int = 1,
) -> dict:
    return _http_json("POST", f"{REST_BASE}/endpoints", api_key, body={
        "name": name,
        "templateId": template_id,
        "gpuTypeIds": gpu_type_ids,
        "networkVolumeId": network_volume_id,
        "workersMin": 0,
        "workersMax": workers_max,
        "idleTimeout": 5,
        "executionTimeoutMs": 3600000,
        "scalerType": "QUEUE_DELAY",
        "scalerValue": 4,
        "flashboot": True,
        "allowedCudaVersions": ALLOWED_CUDA_VERSIONS,
    })


def delete_endpoint(api_key: str, endpoint_id: str) -> None:
    _http_json("DELETE", f"{REST_BASE}/endpoints/{endpoint_id}", api_key)


def update_endpoint_workers(api_key: str, endpoint_id: str, workers_min: int, workers_max: int) -> None:
    _http_json("PATCH", f"{REST_BASE}/endpoints/{endpoint_id}", api_key, body={
        "workersMin": workers_min, "workersMax": workers_max,
    })


# === Test runner ============================================================

def env_for_template() -> dict[str, str]:
    return {
        "RUNTIME_REPO_URL": RUNTIME_REPO_URL,
        "RUNTIME_REPO_REF": "main",
        "AWS_ACCESS_KEY_ID": os.environ["PRESET_TEST_S3_ACCESS"],
        "AWS_SECRET_ACCESS_KEY": os.environ["PRESET_TEST_S3_SECRET"],
        "S3_BUCKET": os.environ["PRESET_TEST_S3_BUCKET"],
        "S3_REGION": os.environ.get("PRESET_TEST_S3_REGION", "auto"),
        "S3_ENDPOINT_URL": os.environ.get("PRESET_TEST_S3_ENDPOINT_URL", ""),
    }


def build_batch_spec(preset: dict) -> list[dict]:
    """Translate preset.models[] into a comfy-gen `download --batch` JSON."""
    spec: list[dict] = []
    for m in preset.get("models", []):
        dest = m.get("dest", "")
        if "/" in dest:
            subfolder, filename = dest.split("/", 1)
        else:
            subfolder, filename = "checkpoints", dest
        spec.append({
            "source": m.get("source", "url") if m.get("source") in ("civitai", "url") else "url",
            "url": m["url"],
            "dest": subfolder,
            "filename": filename,
        })
    return spec


def run_comfy_gen(args: list[str], *, label: str, timeout: int) -> dict:
    """Invoke `comfy-gen ...` and parse JSON stdout. Raises on non-zero exit."""
    log(f"{label}: comfy-gen {' '.join(args[:3])}...")
    proc = subprocess.run(
        ["comfy-gen", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        log(f"{label} FAILED (exit {proc.returncode})")
        log(f"  stderr (tail 2000): {proc.stderr[-2000:]}")
        log(f"  stdout (tail 1000): {proc.stdout[-1000:]}")
        raise RuntimeError(f"{label} failed: exit {proc.returncode}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        log(f"{label}: stdout not valid JSON: {proc.stdout[:500]}")
        raise RuntimeError(f"{label}: non-JSON stdout") from exc


def configure_comfy_gen_cli() -> None:
    """Seed ~/.comfy-gen/config.json from env so the CLI can submit jobs.

    On a fresh CI runner there's no ComfyGen config. comfy-gen reads creds from
    its own config (or env vars matching certain names); we write the config
    file explicitly to avoid relying on env-var pickup."""
    config_dir = Path.home() / ".comfy-gen"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "config.json"
    config_path.write_text(json.dumps({
        "runpod_api_key": os.environ["RUNPOD_API_KEY"],
        "aws_access_key_id": os.environ["PRESET_TEST_S3_ACCESS"],
        "aws_secret_access_key": os.environ["PRESET_TEST_S3_SECRET"],
        "s3_bucket": os.environ["PRESET_TEST_S3_BUCKET"],
        "s3_region": os.environ.get("PRESET_TEST_S3_REGION", "auto"),
        "s3_endpoint_url": os.environ.get("PRESET_TEST_S3_ENDPOINT_URL", ""),
        "timeout_seconds": 1800,
        "poll_interval_seconds": 5,
        "storage_provider": "tmpfiles",
    }, indent=2))
    log(f"wrote {config_path}")


def head_check(url: str) -> int:
    """HEAD a URL, return the status code. Used to verify the output URL
    that the workflow generated actually serves an image."""
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: ci_live_install.py <preset_id>", file=sys.stderr)
        return 2

    preset_id = sys.argv[1]
    preset_path = REPO_ROOT / "registry" / preset_id / "preset.json"
    if not preset_path.exists():
        print(f"missing {preset_path}", file=sys.stderr)
        return 2

    preset = json.loads(preset_path.read_text())

    # Inline workflow OR fetch from URL referenced in the preset
    workflow_dict = preset.get("workflow", {}).get("json")
    if not workflow_dict and "url" in preset.get("workflow", {}):
        # The workflow URL is on github.com for our presets; the file should
        # also be sitting next to preset.json on disk for the CI run
        wf_local = REPO_ROOT / "registry" / preset_id / "workflow.json"
        if wf_local.exists():
            workflow_dict = json.loads(wf_local.read_text())
        else:
            print(f"ERROR: workflow not inline and {wf_local} missing", file=sys.stderr)
            return 2

    api_key = os.environ.get("RUNPOD_API_KEY")
    if not api_key:
        print("RUNPOD_API_KEY not in env", file=sys.stderr)
        return 2

    configure_comfy_gen_cli()

    overall_start = time.time()
    volume: dict | None = None
    template: dict | None = None
    endpoint: dict | None = None

    try:
        suffix = str(int(time.time()))[-8:]
        name = f"blockflow-preset-ci-{preset_id}-{suffix}"
        vol_size_gb = preset["disk_size_estimate_gb"] + 20  # safety buffer

        log(f"provisioning ({preset_id}, vol={vol_size_gb}GB)...")
        volume = create_network_volume(
            api_key, name=name, size_gb=vol_size_gb,
            datacenter_id=PERFORMANCE_TIER["datacenter"],
        )
        log(f"  volume {volume['id']} ({vol_size_gb}GB) in {PERFORMANCE_TIER['datacenter']}")

        template = create_template(api_key, name=f"{name}-template", env=env_for_template())
        log(f"  template {template['id']}")

        endpoint = create_endpoint(
            api_key,
            name=name,
            template_id=template["id"],
            gpu_type_ids=PERFORMANCE_TIER["gpu_ids"],
            network_volume_id=volume["id"],
            workers_max=1,
        )
        log(f"  endpoint {endpoint['id']}")

        # === Install: comfy-gen download --batch ===
        batch_spec = build_batch_spec(preset)
        batch_path = Path("/tmp/ci_batch.json")
        batch_path.write_text(json.dumps(batch_spec))

        run_comfy_gen(
            [
                "download", "--batch", str(batch_path),
                "--endpoint-id", endpoint["id"],
                "--timeout", "3000",
            ],
            label="install",
            timeout=3300,
        )
        log("  install complete")

        # === Generate: comfy-gen submit ===
        wf_path = Path("/tmp/ci_workflow.json")
        wf_path.write_text(json.dumps(workflow_dict))

        result = run_comfy_gen(
            [
                "submit", "--endpoint-id", endpoint["id"],
                "--timeout", "900",
                str(wf_path),
            ],
            label="generate",
            timeout=1000,
        )

        if not result.get("ok"):
            raise RuntimeError(f"generation reported failure: {result}")
        output_url = result.get("output", {}).get("url")
        if not output_url:
            raise RuntimeError(f"no output URL in result: {result}")
        log(f"  output URL: {output_url}")

        # === Verify: HEAD the output URL ===
        status = head_check(output_url)
        if status != 200:
            raise RuntimeError(f"output URL returned HTTP {status}")
        log(f"  HEAD {output_url[:80]}... → HTTP 200 ✓")

        elapsed = int(time.time() - overall_start)
        log(f"PRESET '{preset_id}' VALIDATED in {elapsed}s")
        return 0

    finally:
        # Teardown — best-effort
        if endpoint:
            try:
                update_endpoint_workers(api_key, endpoint["id"], 0, 0)
            except Exception as exc:
                log(f"  warn: drain failed: {exc}")
            try:
                delete_endpoint(api_key, endpoint["id"])
                log(f"  torn down endpoint {endpoint['id']}")
            except Exception as exc:
                log(f"  warn: endpoint delete failed: {exc}")
        if template:
            for attempt in range(6):
                try:
                    delete_template(api_key, template_name=template["name"])
                    log(f"  torn down template {template['name']}")
                    break
                except Exception as exc:
                    if attempt == 5:
                        log(f"  warn: template delete failed after 6 retries: {exc}")
                    else:
                        time.sleep(8)
        if volume:
            for attempt in range(6):
                try:
                    delete_network_volume(api_key, volume["id"])
                    log(f"  torn down volume {volume['id']}")
                    break
                except Exception as exc:
                    if attempt == 5:
                        log(f"  warn: volume delete failed after 6 retries: {exc}")
                    else:
                        time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
