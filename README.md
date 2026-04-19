# 365collective-canvas-tools

Delivery infrastructure for Power Apps canvas apps authored as `.pa.yaml` source.

## What it does

Copies authored `.pa.yaml` source files into the canonical location `pac canvas pack` reads, then runs the full deploy pipeline:

```
local .pa.yaml
     ↓  (file copy → unpacked/Other/Src/*.pa.yaml)
     ↓  (also: pa_to_fx converter → unpacked/Src/*.fx.yaml as fallback)
.msapp file (pac canvas pack)
     ↓  (solution zip swap)
solution.zip with updated canvas app
     ↓  (pac solution import --force-overwrite)
Target environment
```

## Why

`pac canvas pack` reads the canonical coauth source from `Other/Src/*.pa.yaml` (newer format), not `Src/*.fx.yaml` (legacy). When the unpacked tree's `.pa.yaml` exists with content (always true after a roundtrip), pack ignores `.fx.yaml` and any edits to it are silently dropped. Our authored format already matches what unpack produces in `Other/Src/`, so the deploy is a straight file copy.

The legacy `.fx.yaml` conversion (via `pa_to_fx`) is still emitted as a fallback for fresh scaffolds whose `Other/Src/*.pa.yaml` is empty/minimal — pack falls back to `.fx.yaml` in that case.

## Prerequisites

- Python 3.10+
- `pac` CLI (Power Platform CLI) 2.6+ authenticated to the target environment
- `pyyaml`

## Usage

```bash
PYTHONPATH=src python -m deploy_canvas \
  --source ./canvas-apps/my-app \
  --app-id <canvas-app-guid> \
  --app-name "My Canvas App" \
  --environment https://<org>.crm.dynamics.com \
  --solution <SolutionUniqueName>
```

Flags:

| Flag | Description |
| --- | --- |
| `--source` | Directory containing `App.pa.yaml` + `scr*.pa.yaml` files |
| `--app-id` | Canvas app GUID — used for `pac canvas download --name` |
| `--app-name` | Canvas app display name — used to pick the right `CanvasApps/*.msapp` inside the solution zip when the solution contains multiple canvas apps (matched against `Properties.Name` in each embedded msapp) |
| `--environment` | Dataverse environment URL |
| `--solution` | Unique name of the solution containing the canvas app |
| `--keep-temp` | Preserve intermediate artifacts in a named directory for debugging |

## Supported controls

| `.pa.yaml` | `.fx.yaml` |
|---|---|
| `Control: Label` | `As label:` |
| `Control: Classic/Button` | `As button:` |
| `Control: Classic/DropDown` | `As dropdown:` |
| `Control: TextInput` | `As 'Text input':` |
| `Control: Gallery` + `Variant: Vertical` | `As gallery.galleryVertical:` |
| `Control: GroupContainer` + `Variant: AutoLayout` | `As groupContainer.horizontalAutoLayoutContainer:` (direction controlled by `LayoutDirection`) |
| `Control: Camera` | `As camera:` |
| `Control: Image` | `As image:` |

Classic/TextInput is **not** supported — use modern `TextInput` with `Placeholder` and reference `.Value` in formulas.

## Auto-injected properties

Power Apps Studio writes several internal-layout defaults into `.fx.yaml` during an authoring session that `.pa.yaml` omits as implicit. `pac canvas pack` does not backfill them, and containers rendered without them collapse or misalign on mobile even though they display in Studio. The converter injects these automatically when the user hasn't set them explicitly:

**On every `GroupContainer` + `Variant: AutoLayout`** (→ `groupContainer.horizontalAutoLayoutContainer`):

```
LayoutMode: =LayoutMode.Auto
maximumHeight: =11360
maximumWidth: =640
LayoutMinWidth: =250
LayoutGridColumns: =6
LayoutGridRows: =6
ZIndex: =1
```

**On every `Gallery` + `Variant: Vertical`** (→ `gallery.galleryVertical`):

```
Layout: =Layout.Vertical
```

The defaults are matched against the Field Issue Logger reference — a known-working canvas app that was built end-to-end in Studio with coauth. If a `.pa.yaml` source explicitly sets any of these properties, the user value wins.

## Running just the converter (without deploy)

```bash
python -m pa_to_fx --source ./canvas-apps/my-app --out ./scratch/fx
```

Emits `App.fx.yaml` + `scr*.fx.yaml` into `--out`.
