"""
Convert Power Apps canvas `.pa.yaml` (coauth format) into `.fx.yaml`
(legacy format consumed by `pac canvas pack`).

The two formats encode the same control tree:

  .pa.yaml                         .fx.yaml
  ─────────                        ─────────
  Screens:                         scrName As screen:
    scrName:                           Fill: =...
      Properties:
        Fill: =...                     ctrl As button:
      Children:                            Text: ="..."
        - ctrl:
            Control: Classic/Button
            Properties:
              Text: ="..."

Tree transformations:
  1. Drop `Screens:` / `Properties:` / `Children:` wrapper keys.
  2. Combine `Control:` + `Variant:` into `As <type>:` header.
  3. Named list items become indented named blocks.
  4. Quote names whose type contains a space: `"name As 'Text input'":`
  5. For `App.pa.yaml` emit `App As appinfo:` plus a mandatory
     `Host As hostControl.DefaultHostControlVariant:` child block.
  6. Multi-line formula strings are emitted as `|-` block scalars.

Public entry points:
    convert_screen_yaml(pa_text: str) -> str
    convert_app_yaml(pa_text: str) -> str
    convert_directory(src_dir: Path, out_dir: Path) -> None
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml


# Control type mapping: (pa_control, pa_variant) -> fx_type_header
TYPE_MAP: dict[tuple[str, str | None], str] = {
    ("Label", None): "label",
    ("Classic/Button", None): "button",
    ("Classic/DropDown", None): "dropdown",
    ("TextInput", None): "'Text input'",
    ("Gallery", "Vertical"): "gallery.galleryVertical",
    ("GroupContainer", "AutoLayout"): "groupContainer.horizontalAutoLayoutContainer",
    ("Camera", None): "camera",
    ("Image", None): "image",
}

INDENT = "    "  # 4 spaces, matches pac canvas unpack output


def _resolve_type(control: str, variant: str | None) -> str:
    key = (control, variant)
    if key in TYPE_MAP:
        return TYPE_MAP[key]
    # Fall back to control-only key
    key_fallback = (control, None)
    if key_fallback in TYPE_MAP:
        return TYPE_MAP[key_fallback]
    raise ValueError(f"No .fx.yaml mapping for Control={control!r} Variant={variant!r}")


def _header(name: str, fx_type: str, indent_level: int) -> str:
    prefix = INDENT * indent_level
    # If fx_type contains a space (e.g. "'Text input'"), the whole
    # `name As type` key must be double-quoted per the pac canvas format.
    if " " in fx_type:
        return f'{prefix}"{name} As {fx_type}":'
    return f"{prefix}{name} As {fx_type}:"


def _emit_property(key: str, value: Any, indent_level: int) -> list[str]:
    """Emit a single property line (or multi-line block scalar)."""
    prefix = INDENT * indent_level

    # Normalize value to string. PyYAML preserves multiline `|-` blocks
    # as strings containing '\n'.
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (int, float)):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        text = str(value)

    if "\n" in text:
        lines = [f"{prefix}{key}: |-"]
        inner_prefix = INDENT * (indent_level + 1)
        for ln in text.split("\n"):
            lines.append(f"{inner_prefix}{ln}" if ln else "")
        return lines

    return [f"{prefix}{key}: {text}"]


def _emit_properties(props: dict, indent_level: int) -> list[str]:
    """Emit all properties of a control, alphabetized."""
    lines: list[str] = []
    for key in sorted(props.keys(), key=str):
        lines.extend(_emit_property(key, props[key], indent_level))
    return lines


def _emit_control(name: str, body: dict, indent_level: int) -> list[str]:
    control = body.get("Control")
    variant = body.get("Variant")
    if control is None:
        raise ValueError(f"Control {name!r} is missing `Control:` key")

    fx_type = _resolve_type(control, variant)
    lines: list[str] = [_header(name, fx_type, indent_level)]

    props = dict(body.get("Properties", {}) or {})

    # Enrich properties with format-specific defaults that `pac canvas pack`
    # requires but .pa.yaml omits as implicit in the control type.
    if fx_type == "gallery.galleryVertical" and "Layout" not in props:
        props["Layout"] = "=Layout.Vertical"

    # GroupContainer auto-layout: Studio authors these defaults during
    # coauth; omitting them produces containers that render in Studio but
    # collapse or misalign on mobile (verified against the Field Issue
    # Logger reference .fx.yaml). Inject only when the user hasn't set them.
    if fx_type == "groupContainer.horizontalAutoLayoutContainer":
        autolayout_defaults = {
            "LayoutMode": "=LayoutMode.Auto",
            "maximumHeight": "=11360",
            "maximumWidth": "=640",
            "LayoutMinWidth": "=250",
            "LayoutGridColumns": "=6",
            "LayoutGridRows": "=6",
            "ZIndex": "=1",
        }
        for k, v in autolayout_defaults.items():
            if k not in props:
                props[k] = v

    lines.extend(_emit_properties(props, indent_level + 1))

    children = body.get("Children", []) or []
    for child_entry in children:
        # Child is a single-key dict: {child_name: {Control: ..., ...}}
        if not isinstance(child_entry, dict) or len(child_entry) != 1:
            raise ValueError(
                f"Child under {name!r} is not a single-key mapping: {child_entry!r}"
            )
        [(child_name, child_body)] = child_entry.items()
        lines.append("")  # blank line before nested control
        lines.extend(_emit_control(child_name, child_body, indent_level + 1))

    return lines


def convert_screen_yaml(pa_text: str) -> str:
    data = yaml.safe_load(pa_text)
    if not isinstance(data, dict) or "Screens" not in data:
        raise ValueError(".pa.yaml must have top-level `Screens:` key")

    screens = data["Screens"]
    if not isinstance(screens, dict) or len(screens) != 1:
        raise ValueError("Expected exactly one screen per file")

    [(screen_name, screen_body)] = screens.items()

    lines: list[str] = [f"{screen_name} As screen:"]
    props = (screen_body or {}).get("Properties", {}) or {}
    lines.extend(_emit_properties(props, 1))

    children = (screen_body or {}).get("Children", []) or []
    for child_entry in children:
        if not isinstance(child_entry, dict) or len(child_entry) != 1:
            raise ValueError(
                f"Screen {screen_name!r} child is not a single-key mapping: {child_entry!r}"
            )
        [(child_name, child_body)] = child_entry.items()
        lines.append("")
        lines.extend(_emit_control(child_name, child_body, 1))

    lines.append("")  # trailing newline
    return "\n".join(lines)


def convert_app_yaml(pa_text: str) -> str:
    data = yaml.safe_load(pa_text)
    if not isinstance(data, dict) or "App" not in data:
        raise ValueError("App.pa.yaml must have top-level `App:` key")

    app_body = data["App"] or {}
    props = app_body.get("Properties", {}) or {}

    lines: list[str] = ["App As appinfo:"]

    # BackEnabled default if not explicitly set
    if "BackEnabled" not in props:
        lines.append(f"{INDENT}BackEnabled: =true")

    lines.extend(_emit_properties(props, 1))

    # Mandatory Host child
    lines.append("")
    lines.append(f"{INDENT}Host As hostControl.DefaultHostControlVariant:")
    for host_prop in ("OnCancel", "OnEdit", "OnNew", "OnSave", "OnView"):
        lines.append(f"{INDENT}{INDENT}{host_prop}: =false")

    lines.append("")
    return "\n".join(lines)


def convert_directory(src_dir: Path, out_dir: Path) -> list[Path]:
    """Convert every `.pa.yaml` in src_dir to `.fx.yaml` in out_dir.

    Returns the list of output files written.
    """
    if not src_dir.is_dir():
        raise FileNotFoundError(f"Source directory not found: {src_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for pa_path in sorted(src_dir.glob("*.pa.yaml")):
        text = pa_path.read_text(encoding="utf-8")
        stem = pa_path.name[: -len(".pa.yaml")]
        out_path = out_dir / f"{stem}.fx.yaml"
        if stem == "App":
            fx_text = convert_app_yaml(text)
        else:
            fx_text = convert_screen_yaml(text)
        out_path.write_text(fx_text, encoding="utf-8")
        written.append(out_path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert Power Apps .pa.yaml source to .fx.yaml legacy format"
    )
    parser.add_argument("--source", required=True, help="Directory with .pa.yaml files")
    parser.add_argument("--out", required=True, help="Directory to write .fx.yaml files")
    args = parser.parse_args(argv)

    written = convert_directory(Path(args.source), Path(args.out))
    for p in written:
        print(f"wrote {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
