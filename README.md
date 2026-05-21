# BlockFlow Preset Registry

Public registry of preset recipes for the [BlockFlow](https://github.com/Hearmeman24/BlockFlow) preset installer.

A preset is a self-contained JSON file that tells BlockFlow how to:
- Install the model files a workflow needs onto a ComfyGen RunPod endpoint's network volume
- Apply the workflow itself to the ComfyGen block

BlockFlow's preset installer subsystem (`sgs-ui-wisp-las.3`) reads from this registry, fetches the JSON for the chosen preset, downloads the listed models to the volume via `comfy-gen download`, and persists the workflow as the ComfyGen block's default workflow.

## Layout

```
schema/
  preset.schema.json           # JSON Schema for the preset format
registry/
  <preset-id>/
    preset.json                # the preset entry (validated against the schema)
    workflow.json              # optional: workflow body when not embedded inline
tools/
  validate.py                  # CI script: schema-validate every preset + sanity-check sums
.github/
  workflows/
    validate.yml               # runs tools/validate.py on every PR
```

## How to contribute a preset

1. Test your workflow against a ComfyGen endpoint end-to-end. The preset is only useful if it actually generates.
2. Open the [Preset proposal issue template](https://github.com/Hearmeman24/BlockFlow/issues/new?template=preset_proposal.md) on the BlockFlow repo. Include:
   - The workflow JSON (ComfyUI API format)
   - The list of model files (source URL + destination subfolder under `ComfyUI/models/` + approximate GB)
   - The ComfyGen min-version you tested against
3. Once the proposal is accepted, open a PR here adding `registry/<your-id>/preset.json`.
4. CI runs `tools/validate.py` which checks:
   - JSON validates against `schema/preset.schema.json`
   - `disk_size_estimate_gb` matches the sum of model sizes (±10%)
   - All `url` fields are reachable (HEAD 200)
   - SHA256s (if present) are valid hex

## Schema in one sentence

A preset is a JSON object with `id`, `name`, `comfygen_min_version`, a `workflow` (inline JSON or remote URL + SHA256), a list of `models` (each with source URL, destination path under `ComfyUI/models/`, size in GB, optional SHA256), and a `disk_size_estimate_gb` that matches the sum of model sizes.

See [`schema/preset.schema.json`](schema/preset.schema.json) for the full schema.

## Starter presets

| ID | What | Tier | Disk |
|---|---|---|---|
| `qwen-image-lighting` | Qwen Image 2512 + 4-step Lightning distillation. State-of-the-art text rendering + photorealism in 4 sampling steps. | Recommended | ~65 GB |

Future additions will land via PRs from contributors using the [preset proposal issue template](https://github.com/Hearmeman24/BlockFlow/issues/new?template=preset_proposal.md).

## License

MIT. See [`LICENSE`](LICENSE).
