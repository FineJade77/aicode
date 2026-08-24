#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MANIFEST_SCHEMA_VERSION = 1
RUNTIME_FILES = ("app", "pyproject.toml", "requirements.lock.txt")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install aicode CLI and its versioned Python Runtime.")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--system-site-packages", action="store_true")
    parser.add_argument("--skip-deps", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_root = args.source_root.resolve()
    prefix = args.prefix.expanduser().resolve()
    cli_source = args.cli.resolve()
    version = validate_version(args.version)
    runtime_source = source_root / "runtime"

    validate_sources(runtime_source, cli_source)

    install_root = prefix / "lib" / "aicode"
    bin_dir = prefix / "bin"
    version_dir = install_root / version
    manifest_path = install_root / "manifest.json"
    cli_path = bin_dir / "aicode"
    cli_backup = bin_dir / ".aicode.previous"
    install_root.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)

    staging = Path(tempfile.mkdtemp(prefix=f".{version}.staging-", dir=install_root))
    backup = install_root / f".{version}.previous"
    replaced_version = False
    activated_version = False
    replaced_cli = False
    activated_cli = False
    try:
        stage_runtime(runtime_source, staging / "runtime")
        create_venv(
            args.python,
            staging / "venv",
            system_site_packages=args.system_site_packages,
            skip_deps=args.skip_deps,
            requirements=staging / "runtime" / "requirements.lock.txt",
        )

        if backup.exists():
            shutil.rmtree(backup)
        if version_dir.exists():
            os.replace(version_dir, backup)
            replaced_version = True
        os.replace(staging, version_dir)
        activated_version = True

        cli_backup.unlink(missing_ok=True)
        if cli_path.exists():
            os.replace(cli_path, cli_backup)
            replaced_cli = True
        atomic_copy(cli_source, cli_path, mode=0o755)
        activated_cli = True
        atomic_write_json(
            manifest_path,
            {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "version": version,
                "runtime_dir": f"{version}/runtime",
                "python": f"{version}/venv/bin/python",
            },
        )
    except Exception:
        if activated_cli and cli_path.exists():
            cli_path.unlink()
        if replaced_cli and cli_backup.exists():
            os.replace(cli_backup, cli_path)
        if activated_version and version_dir.exists():
            shutil.rmtree(version_dir)
        if replaced_version and backup.exists():
            os.replace(backup, version_dir)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)

    if backup.exists():
        shutil.rmtree(backup)
    cli_backup.unlink(missing_ok=True)

    print(f"installed aicode {version}")
    print(f"  cli: {cli_path}")
    print(f"  runtime: {version_dir / 'runtime'}")
    print(f"  python: {version_dir / 'venv' / 'bin' / 'python'}")
    print(f"  manifest: {manifest_path}")
    return 0


def validate_version(raw: str) -> str:
    version = raw.strip()
    if not version or version in {".", ".."} or "/" in version or "\\" in version:
        raise ValueError(f"invalid version: {raw!r}")
    return version


def validate_sources(runtime_source: Path, cli_source: Path) -> None:
    if not (runtime_source / "app" / "server" / "main.py").is_file():
        raise FileNotFoundError(f"Runtime entrypoint not found under {runtime_source}")
    if not cli_source.is_file():
        raise FileNotFoundError(f"CLI binary not found: {cli_source}")


def stage_runtime(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True)
    for name in RUNTIME_FILES:
        item = source / name
        target = destination / name
        if item.is_dir():
            shutil.copytree(
                item,
                target,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "*.egg-info"),
            )
        else:
            shutil.copy2(item, target)


def create_venv(
    python: str,
    destination: Path,
    *,
    system_site_packages: bool,
    skip_deps: bool,
    requirements: Path,
) -> None:
    command = [python, "-m", "venv"]
    if system_site_packages:
        command.append("--system-site-packages")
    command.append(str(destination))
    subprocess.run(command, check=True)

    venv_python = destination / "bin" / "python"
    if not skip_deps:
        subprocess.run(
            [
                str(venv_python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-r",
                str(requirements),
            ],
            check=True,
        )
    subprocess.run(
        [
            str(venv_python),
            "-c",
            "import fastapi, httpx, pydantic, uvicorn",
        ],
        check=True,
    )


def atomic_copy(source: Path, destination: Path, *, mode: int) -> None:
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    try:
        shutil.copy2(source, temporary)
        temporary.chmod(mode)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(destination: Path, payload: dict[str, object]) -> None:
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o644)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
