"""
Deploy a Power Apps canvas app from `.pa.yaml` source via the pac CLI.

Pipeline:
  1. pac canvas download  → current app .msapp
  2. pac canvas unpack    → scaffold source tree
  3. inject bundled control templates (pkgs + ControlTemplates.json entries)
  4. pa_to_fx.convert_directory → Src/*.fx.yaml
  5. pac canvas pack      → new .msapp
  6. pac solution export  → solution .zip
  7. swap new .msapp into solution zip (replaces CanvasApps/<name>.msapp)
  8. pac solution import --force-overwrite → target environment

Usage:
    python -m deploy_canvas \\
        --source ./canvas-apps/my-app \\
        --app-id <canvas-app-guid> \\
        --environment https://<org>.crm.dynamics.com \\
        --solution <SolutionUniqueName>
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import pa_to_fx


TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd, check=check)


def inject_templates(unpacked_dir: Path) -> None:
    """Merge bundled control templates into an unpacked .msapp tree.

    Copies `pkgs/*` files and merges `control_templates.json` entries into
    the unpacked tree's `ControlTemplates.json`. Existing entries/files are
    preserved (the scaffold's own templates win).
    """
    bundle_pkgs = TEMPLATES_DIR / "pkgs"
    target_pkgs = unpacked_dir / "pkgs"
    target_pkgs.mkdir(parents=True, exist_ok=True)

    for src in bundle_pkgs.rglob("*"):
        if src.is_file():
            rel = src.relative_to(bundle_pkgs)
            dst = target_pkgs / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists():
                shutil.copyfile(src, dst)

    tpl_bundle = json.loads((TEMPLATES_DIR / "control_templates.json").read_text(encoding="utf-8"))
    target_ct = unpacked_dir / "ControlTemplates.json"
    current = json.loads(target_ct.read_text(encoding="utf-8"))
    added = []
    for k, v in tpl_bundle.items():
        if k not in current:
            current[k] = v
            added.append(k)
    target_ct.write_text(json.dumps(current, indent=2), encoding="utf-8")
    if added:
        print(f"injected control templates: {', '.join(added)}")


def _msapp_name(msapp: Path) -> str | None:
    """Read `Properties.Name` from a .msapp (zipped canvas document)."""
    try:
        with zipfile.ZipFile(msapp) as zf:
            with zf.open("Properties.json") as f:
                return json.load(f).get("Name")
    except Exception:
        return None


def swap_msapp_in_solution(solution_zip: Path, new_msapp: Path, app_name: str) -> None:
    """Replace the canvas app .msapp matching `app_name` inside a solution zip.

    Matches against `Properties.Name` inside each CanvasApps/*.msapp. Raises
    if zero or multiple candidates match.
    """
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        with zipfile.ZipFile(solution_zip) as zf:
            zf.extractall(work)

        canvas_dir = work / "CanvasApps"
        all_msapps = sorted(canvas_dir.glob("*.msapp")) if canvas_dir.exists() else []
        if not all_msapps:
            raise RuntimeError(f"No CanvasApps/*.msapp found in {solution_zip}")

        matches = [m for m in all_msapps if _msapp_name(m) == app_name]
        if not matches:
            names = ", ".join(f"{m.name}={_msapp_name(m)!r}" for m in all_msapps)
            raise RuntimeError(
                f"No canvas app named {app_name!r} in solution. Found: {names}"
            )
        if len(matches) > 1:
            names = ", ".join(m.name for m in matches)
            raise RuntimeError(
                f"Multiple canvas apps named {app_name!r}: {names}"
            )

        target = matches[0]
        print(f"replacing {target.name} in solution zip (matched by Name={app_name!r})")
        shutil.copyfile(new_msapp, target)

        solution_zip.unlink()
        with zipfile.ZipFile(solution_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in work.rglob("*"):
                if f.is_file():
                    zf.write(f, f.relative_to(work))




def deploy(
    source: Path,
    app_id: str,
    app_name: str,
    environment: str,
    solution: str,
    work_dir: Path,
) -> None:
    if not source.is_dir():
        raise FileNotFoundError(f"Source dir not found: {source}")

    work_dir.mkdir(parents=True, exist_ok=True)
    msapp_in = work_dir / "original.msapp"
    unpacked = work_dir / "unpacked"
    msapp_out = work_dir / "new.msapp"
    solution_zip = work_dir / "solution.zip"

    # 1. download
    run(["pac", "canvas", "download", "--name", app_id,
         "-f", str(msapp_in), "-o", "--environment", environment])

    # 2. unpack
    run(["pac", "canvas", "unpack", "--msapp", str(msapp_in),
         "--sources", str(unpacked)])

    # 3. inject templates
    inject_templates(unpacked)

    # 4. write source .pa.yaml into the unpacked tree.
    #
    # `pac canvas pack` reads the canonical coauth source from
    # `Other/Src/*.pa.yaml` (newer format), NOT the legacy `Src/*.fx.yaml`.
    # When `Other/Src/*.pa.yaml` exists with content (always true after a
    # roundtrip), pack ignores `.fx.yaml` and any edits to it are silently
    # dropped. Copy our authored .pa.yaml directly into Other/Src/ — our
    # source format matches what unpack produces there, so this is a
    # straight file copy with no conversion.
    #
    # Also keep emitting .fx.yaml in Src/ as a fallback for fresh scaffolds
    # whose `Other/Src/*.pa.yaml` is empty/minimal — pack falls back to
    # .fx.yaml in that case.
    shadow_dir = unpacked / "Other" / "Src"
    shadow_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for pa_file in sorted(source.glob("*.pa.yaml")):
        dst = shadow_dir / pa_file.name
        shutil.copyfile(pa_file, dst)
        copied.append(pa_file.name)
    print(f"copied {len(copied)} .pa.yaml files into Other/Src/")

    src_dir = unpacked / "Src"
    written = pa_to_fx.convert_directory(source, src_dir)
    print(f"also converted {len(written)} files into Src/ (fallback)")

    # 5. pack
    run(["pac", "canvas", "pack", "--sources", str(unpacked),
         "--msapp", str(msapp_out)])

    # 6. export solution
    run(["pac", "solution", "export", "--name", solution,
         "--path", str(solution_zip), "--managed", "false", "--overwrite",
         "--environment", environment])

    # 7. swap .msapp
    swap_msapp_in_solution(solution_zip, msapp_out, app_name)

    # 8. import
    run(["pac", "solution", "import", "--path", str(solution_zip),
         "--force-overwrite", "--environment", environment])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deploy a canvas app from .pa.yaml source via pac CLI"
    )
    parser.add_argument("--source", required=True, help="Directory of .pa.yaml files")
    parser.add_argument("--app-id", required=True, help="Canvas app GUID")
    parser.add_argument("--app-name", required=True,
                        help="Canvas app display name (matched against Properties.Name "
                             "in CanvasApps/*.msapp inside the solution zip)")
    parser.add_argument("--environment", required=True, help="Dataverse environment URL")
    parser.add_argument("--solution", required=True, help="Solution unique name")
    parser.add_argument(
        "--keep-temp",
        help="Preserve intermediate artifacts in this directory (for debugging)",
    )
    args = parser.parse_args(argv)

    if args.keep_temp:
        work = Path(args.keep_temp).resolve()
        deploy(Path(args.source).resolve(), args.app_id, args.app_name,
               args.environment, args.solution, work)
        print(f"intermediate artifacts preserved in {work}")
    else:
        with tempfile.TemporaryDirectory() as td:
            deploy(Path(args.source).resolve(), args.app_id, args.app_name,
                   args.environment, args.solution, Path(td))

    return 0


if __name__ == "__main__":
    sys.exit(main())
