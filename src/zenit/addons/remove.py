"""Remove-addon pipeline - undoes a single addon applied to an existing project."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit
import typer
from tomlkit.items import Array

from zenit.addons._registry import get_addon, list_addons
from zenit.addons.checks import check_can_remove
from zenit.cli.prompt import prompt_multi_addon
from zenit.cli.ui import (
    DIM,
    RED,
    RESET,
    abort,
    addon_summary,
    bullet_list,
    dry_header,
    dry_run_banner,
    error,
    info,
    success,
    warn,
)
from zenit.core._filenames import COMPOSE_FILE, ENV_FILES, JUSTFILE_NAME, PYPROJECT_FILE
from zenit.core._paths import get_zenit_root
from zenit.core.constants import RECIPE_NAME_RE
from zenit.core.context import Context
from zenit.core.dependency import DependencyGraph
from zenit.core.filesystem import atomic_write_text
from zenit.core.handlers import HandlerDispatcher
from zenit.core.lockfile import ZenitLockfile, read_lockfile, write_zenit_toml
from zenit.core.manifest import (
    dep_package_name,
    read_manifest,
    remove_blocks_for_addon,
    resync_python_blocks,
)
from zenit.core.manifest import (
    fingerprint as _fingerprint,
)
from zenit.core.pkg_name import normalise_pkg_name, resolve_dest_placeholder
from zenit.core.render import build_render_vars, make_env
from zenit.core.rollback import addon_or_rollback
from zenit.core.yaml_utils import compose_yaml_dumps, compose_yaml_load
from zenit.schema.exceptions import ZenitError
from zenit.schema.models import (
    AddonConfig,
    ComposeService,
    FileContribution,
    Manifest,
    ManifestBlock,
)
from zenit.templates._load_config import load_template_config


def _remove_one(
    addon_id: str,
    dry_run: bool,
    yes: bool,
    project_dir: Path,
) -> None:
    """Thin wrapper: call *remove_addon*, convert ``ZenitError`` to exit."""
    try:
        remove_addon(addon_id, dry_run=dry_run, yes=yes, project_dir=project_dir)
    except ZenitError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc


def _check_fuzzy_blocks(
    manifest: Manifest,
    addon_id: str,
    project_dir: Path,
) -> list[tuple[str, str, str]]:
    """Return list of ``(file, point, lines)`` for blocks that would hit fuzzy removal.

    For each Python block belonging to *addon_id*, reads the current file at
    the recorded line range and compares fingerprints.  Blocks where neither
    the exact fingerprint (Stage A) nor the normalised fingerprint (Stage B)
    match are returned - these would fall through to Stage C (fuzzy) removal.
    """
    result: list[tuple[str, str, str]] = []
    for block in manifest.python_blocks:
        if block.addon != addon_id:
            continue
        file_path = project_dir / block.file
        if not file_path.exists():
            continue
        source = file_path.read_text(encoding="utf-8")
        lines = source.splitlines(keepends=True)

        start_str, end_str = block.lines.split("-")
        rec_start = int(start_str) - 1
        rec_end = int(end_str) - 1

        if rec_start < 0 or rec_end >= len(lines):
            continue

        candidate = "".join(lines[rec_start : rec_end + 1])
        fp, fp_norm = _fingerprint(candidate)

        if fp == block.fingerprint or fp_norm == block.fingerprint_normalised:
            continue

        result.append((block.file, block.point, block.lines))
    return result


def remove_addon(
    addon_id: str,
    dry_run: bool = False,
    yes: bool = False,
    project_dir: Path | None = None,
) -> None:
    """Remove a single addon from an existing zenit project."""

    if project_dir is None:
        project_dir = Path.cwd()

    lockfile = check_can_remove(project_dir, addon_id)

    template = lockfile.template
    pkg_name = normalise_pkg_name(project_dir.name)
    addon_cfg = get_addon(addon_id)

    if dry_run:
        _dry_remove(project_dir, addon_id, addon_cfg, lockfile, pkg_name)
        return

    addon_summary("remove", addon_id, project_dir, template)

    # ── read manifest once, before any prompts or removals ──────────────────
    manifest = read_manifest(project_dir)

    # ── fuzzy-block check ─────────────────────────────────────────────────
    fuzzy_blocks = _check_fuzzy_blocks(manifest, addon_id, project_dir)
    if fuzzy_blocks:
        warn("Some injected code has been modified since it was added:")
        for file, point, lines in fuzzy_blocks:
            warn(f"  {file}:{point}  (lines {lines})")
        print()

        if yes:
            error(
                "Re-run without --yes to interactively confirm fuzzy removal, "
                "or inspect the modified files and remove the injected code manually."
            )
            raise typer.Exit(1)

        if sys.stdin.isatty():
            try:
                raw = (
                    input(f"  Use fuzzy matching to remove it? {DIM}[y/N]{RESET}  ")
                    .strip()
                    .lower()
                )
            except (EOFError, KeyboardInterrupt):
                abort()
            if raw not in ("y", "yes"):
                warn("Aborted.")
                raise typer.Exit(0)
        else:
            warn("Non-interactive mode - proceeding automatically.")

    if sys.stdin.isatty() and not yes:
        try:
            raw = input(f"  Proceed? {DIM}[Y/n]{RESET}  ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            abort()
        if raw not in ("", "y", "yes"):
            warn("Aborted.")
            raise typer.Exit(0)
    elif not yes:
        warn("Non-interactive mode - proceeding automatically.")

    # ── destructive operations (wrapped in rollback context) ─────────────────
    with addon_or_rollback(project_dir, addon_id):
        # ── files ──────────────────────────────────────────────────────────────
        removed_files = _remove_files(project_dir, addon_cfg, pkg_name)

        # ── restore template files that this addon had overridden ──────────────
        remaining_addons = [a for a in lockfile.addons if a != addon_id]
        _restore_overridden_template_files(
            project_dir,
            template,
            pkg_name,
            project_dir.name,
            removed_files,
            remaining_addons,
        )

        # ── injections (physical removal only - manifest written at the end) ────
        _undo_injections_physical(project_dir, manifest, addon_id)

        # ── compose services ────────────────────────────────────────────────────
        # Docker owns all compose entries in the manifest, but _refresh_compose
        # keeps compose.yml in sync with the current set of installed addons.
        if addon_id == "docker":
            # Docker removal: _remove_files deletes compose.yml entirely.
            removed_services = []
            removed_volumes = []
        elif "docker" in lockfile.addons:
            # Reconcile compose.yml based on remaining addons.
            from zenit.addons.add import _refresh_compose

            ctx_for_refresh = Context(
                name=project_dir.name,
                pkg_name=pkg_name,
                template=lockfile.template,
                addons=[a for a in lockfile.addons if a != addon_id],
                zenit_root=get_zenit_root(),
                project_dir=project_dir,
            )
            _refresh_compose(ctx_for_refresh, project_dir, manifest)
            removed_services = [s.name for s in addon_cfg.compose_services]
            removed_volumes = list(addon_cfg.compose_volumes)
        else:
            # Legacy: no docker installed, remove per-addon as before.
            removed_services = _remove_compose_services(
                project_dir,
                manifest,
                addon_id,
                addon_services=addon_cfg.compose_services,
            )
            removed_volumes = _remove_compose_volumes(
                project_dir,
                manifest,
                addon_id,
                addon_volumes=addon_cfg.compose_volumes,
            )

        # ── env vars ─────────────────────────────────────────────────────────────
        removed_env_vars = _remove_env_vars(project_dir, manifest, addon_id)

        # ── deps ──────────────────────────────────────────────────────────────
        removed_deps, removed_dev_deps = _remove_deps(project_dir, manifest, addon_id)

        # ── justfile recipes ──────────────────────────────────────────────────
        removed_recipes = _remove_just_recipes(project_dir, manifest, addon_id)

        # ── backfill recipes when docker is removed (docker → native) ────────
        if addon_id == "docker":
            from zenit.addons.add import _backfill_just_recipes

            remaining = [a for a in lockfile.addons if a != addon_id]
            ctx_for_backfill = Context(
                name=project_dir.name,
                pkg_name=pkg_name,
                template=lockfile.template,
                addons=remaining,
                zenit_root=get_zenit_root(),
                project_dir=project_dir,
            )
            backfilled = _backfill_just_recipes(
                ctx_for_backfill, project_dir, manifest, addon_id
            )
            removed_recipes.extend(backfilled)

        # ── ruff excludes ────────────────────────────────────────────────────
        removed_ruff_excludes = _remove_ruff_excludes(project_dir, manifest, addon_id)

        # ── tool overrides ───────────────────────────────────────────────────
        removed_tool_overrides = _remove_tool_overrides(project_dir, manifest, addon_id)

        # ── manifest + lockfile (single atomic write) ────────────────────────────
        remove_blocks_for_addon(manifest, addon_id)
        # Removals shift lines of the blocks that stay. Re-sync their
        # tracking data so `doctor` stays clean without `--fix`.
        resync_python_blocks(project_dir, manifest)
        new_addons = [a for a in lockfile.addons if a != addon_id]
        write_zenit_toml(
            project_dir,
            template=template,
            addons=new_addons,
            template_source=lockfile.template_source,
            template_uri=lockfile.template_uri,
            template_has_tasks=lockfile.template_has_tasks,
            template_file_paths=lockfile.template_file_paths,
            manifest=manifest,
        )

    # ── output ────────────────────────────────────────────────────────────
    print()
    success(f"Addon '{addon_id}' removed from '{project_dir.name}'.")

    if removed_files:
        bullet_list("Files removed:", removed_files, bullet="-", bullet_color=RED)

    if removed_deps or removed_dev_deps:
        bullet_list(
            "Dependencies removed from pyproject.toml:",
            removed_deps,
            bullet="-",
            bullet_color=RED,
        )
        if removed_dev_deps:
            bullet_list(
                "", removed_dev_deps, bullet="-", bullet_color=RED, suffix="(dev)"
            )
        info("Run 'uv sync' to uninstall them.")

    if removed_recipes:
        bullet_list(
            "Just recipes removed:", removed_recipes, bullet="-", bullet_color=RED
        )

    if removed_ruff_excludes:
        bullet_list(
            "Ruff excludes removed from pyproject.toml:",
            removed_ruff_excludes,
            bullet="-",
            bullet_color=RED,
        )

    if removed_tool_overrides:
        bullet_list(
            "Tool overrides removed from pyproject.toml:",
            removed_tool_overrides,
            bullet="-",
            bullet_color=RED,
        )

    if removed_services:
        bullet_list(
            "Compose services removed:", removed_services, bullet="-", bullet_color=RED
        )

    if removed_volumes:
        bullet_list(
            "Compose volumes removed:", removed_volumes, bullet="-", bullet_color=RED
        )

    if removed_env_vars:
        bullet_list("Env vars removed:", removed_env_vars, bullet="-", bullet_color=RED)

    print()


# ── Removal helpers ───────────────────────────────────────────────────────────
# Removing a zenit-managed addon involves two categories:
#   1. Structured-entry removal - compose services/volumes, env vars, deps, and
#      just recipes are tracked as manifest entries and removed by dedicated
#      helpers (_remove_compose_services, _remove_env_vars, etc.).
#   2. Line-range removal - Python code injections are tracked as ManifestBlock
#      entries (with line ranges and fingerprints) and removed by
#      _undo_injections_physical via HandlerDispatcher.
# This separation keeps each removal path simple and explicit.


def _file_content_matches_contribution(full: Path, fc: FileContribution) -> bool:
    """Check if *full*'s current content matches what the addon contributed.

    Only checks non-template inline content and source-file content (template
    content requires render vars that aren't available at remove time).
    """
    if fc.template:
        return True
    if fc.content is not None:
        return full.read_text(encoding="utf-8") == fc.content
    if fc.source is not None:
        try:
            src = Path(fc.source).read_text(encoding="utf-8")
            return full.read_text(encoding="utf-8") == src
        except OSError:
            return True
    return True


def _remove_files(
    project_dir: Path, addon_cfg: AddonConfig, pkg_name: str
) -> list[str]:
    """Delete files that were created by this addon. Returns list of removed paths."""

    removed: list[str] = []

    # Delete all contributed files except empty __init__.py
    for fc in addon_cfg.files:
        dest = resolve_dest_placeholder(fc.dest, pkg_name)
        if dest.endswith("__init__.py") and fc.content == "":
            continue
        full = project_dir / dest
        if full.exists():
            if not _file_content_matches_contribution(full, fc):
                warn(
                    f"'{dest}' was modified since addon installation - skipping deletion"
                )
                continue
            full.unlink()
            removed.append(dest)
            _prune_empty_parents(full.parent, project_dir)

    # Delete empty __init__.py only in truly empty directories
    for fc in addon_cfg.files:
        dest = resolve_dest_placeholder(fc.dest, pkg_name)
        if not (dest.endswith("__init__.py") and fc.content == ""):
            continue
        full = project_dir / dest
        if not full.exists():
            continue
        parent = full.parent
        if parent.exists() and not any(p for p in parent.iterdir() if p != full):
            full.unlink()
            removed.append(dest)
            _prune_empty_parents(parent, project_dir)

    return removed


_ADDONS_CONDITIONAL_RE = re.compile(r'\[\%\s+if\s+"([a-zA-Z0-9_-]+)"\s+in\s+addons')


def _check_addons_drift(
    raw_template: str,
    remaining_addons: list[str],
    dest: str,
) -> bool:
    """Return True if *raw_template* references addons not in *remaining_addons*.

    Template content with ``[% if "<addon>" in addons %]`` rendered with a
    different addon set would produce different output than at scaffold time.
    Skip the restore to avoid silent content drift.
    """
    referenced = set(_ADDONS_CONDITIONAL_RE.findall(raw_template))
    missing = referenced - set(remaining_addons)
    if missing:
        warn(
            f"Template file '{dest}' references addon(s) "
            f"{', '.join(sorted(missing))} via conditional Jinja2. "
            f"Skipping restoration to avoid content drift."
        )
        return True
    return False


def _restore_overridden_template_files(
    project_dir: Path,
    template_id: str,
    pkg_name: str,
    project_name: str,
    removed_files: list[str],
    remaining_addons: list[str],
) -> None:
    """Re-create template-owned files that were overridden by the removed addon.

    When an addon contributes a file at the same destination as the template
    (e.g. ``tests/conftest.py``), the addon's version wins at add time.  On
    removal, the addon's file is deleted; this function restores the template's
    original version so that the project remains functional.
    """
    try:
        template_config = load_template_config(get_zenit_root(), template_id)
    except (FileNotFoundError, ZenitError):
        return

    env = make_env()
    render_vars = build_render_vars(
        name=project_name,
        pkg_name=pkg_name,
        template=template_id,
        addons=remaining_addons,
    )

    removed_set = set(removed_files)
    for fc in template_config.files:
        dest = resolve_dest_placeholder(fc.dest, pkg_name)
        if dest not in removed_set:
            continue

        full_path = project_dir / dest
        if fc.source is not None:
            src = Path(fc.source)
            if fc.template:
                raw = src.read_text(encoding="utf-8")
                if _check_addons_drift(raw, remaining_addons, dest):
                    continue
                loader = make_env(src.parent)
                content = loader.get_template(src.name).render(**render_vars)
            else:
                content = src.read_text(encoding="utf-8")
            full_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(full_path, content)
        elif fc.content is not None:
            if fc.template:
                if _check_addons_drift(fc.content, remaining_addons, dest):
                    continue
                content = env.from_string(fc.content).render(**render_vars)
            else:
                content = fc.content
            full_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(full_path, content)


def _prune_empty_parents(directory: Path, stop_at: Path) -> None:
    """Remove empty directories walking upward, stopping at stop_at."""
    while directory != stop_at and directory.is_relative_to(stop_at):
        try:
            if not any(directory.iterdir()):
                directory.rmdir()
                directory = directory.parent
            else:
                break
        except OSError:
            break


def _undo_injections_physical(
    project_dir: Path,
    manifest: Manifest,
    addon_id: str,
) -> None:
    """Physically remove all Python blocks injected by *addon_id*.

    Uses the pre-read *manifest* to find recorded blocks, dispatches each to
    the appropriate handler's remove(), and stops.  It does NOT mutate or
    write the manifest - that is the caller's responsibility, once all other
    physical removals have also succeeded.  This keeps manifest writes atomic
    with respect to the full removal sequence.

    Detects overlapping blocks (from different addons or re-injections) and
    uses ``relocate_block`` for overlapping blocks instead of relying on
    recorded line numbers, which may be stale after a prior removal shifts
    the file content.
    """

    from zenit.core.handlers.python_handler import relocate_block

    dispatcher = HandlerDispatcher()

    # Group blocks by file.
    by_file: dict[str, list[ManifestBlock]] = {}
    for b in manifest.python_blocks:
        if b.addon == addon_id:
            by_file.setdefault(b.file, []).append(b)

    for file, blocks in by_file.items():
        file_path = project_dir / file
        if not file_path.exists():
            print(
                f"Warning: '{file}' is missing - skipping removal of "
                f"'{blocks[0].point}' injection(s) for addon '{addon_id}'. "
                f"Run 'zenit doctor' to verify project integrity.",
                file=sys.stderr,
            )
            continue

        # Sort bottom-to-top so non-overlapping removals don't shift
        # subsequent line numbers.
        blocks.sort(key=lambda b: -int(b.lines.split("-")[0]))

        # Check for pairwise overlap.
        has_overlap = False
        parsed_blocks: list[tuple[ManifestBlock, int, int]] = [
            (b, int(b.lines.split("-")[0]), int(b.lines.split("-")[1])) for b in blocks
        ]
        for i in range(len(parsed_blocks)):
            for j in range(i + 1, len(parsed_blocks)):
                _, s1, e1 = parsed_blocks[i]
                _, s2, e2 = parsed_blocks[j]
                if not (e1 < s2 or e2 < s1):
                    has_overlap = True
                    break
            if has_overlap:
                break

        if has_overlap:
            # Overlapping blocks: remove one at a time, re-locating each
            # subsequent block via its stored locator so we don't rely on
            # stale line numbers.
            for block in blocks:
                relocated = relocate_block(file_path, block)
                if relocated is None:
                    dispatcher.remove(file_path, block)
                else:
                    # Build a synthetic block with the relocated position
                    # so the handler's fingerprint cascade still works.
                    from copy import deepcopy

                    adj = deepcopy(block)
                    adj.lines = f"{relocated[0]}-{relocated[1]}"
                    dispatcher.remove(file_path, adj)
        else:
            # No overlap - safe to remove bottom-to-top from recorded lines.
            for block in blocks:
                dispatcher.remove(file_path, block)


def _remove_compose_services(
    project_dir: Path,
    manifest: Manifest,
    addon_id: str,
    addon_services: list[ComposeService] | None = None,
) -> list[str]:
    """Remove compose services that belong to this addon from compose.yml.

    Removes services recorded in the manifest (normal path) AND services
    the addon defines but were never recorded in the manifest (e.g. because
    compose contributions were gated at add time).
    """
    compose_path = project_dir / COMPOSE_FILE
    if not compose_path.exists():
        return []

    data: dict[str, Any] = (
        compose_yaml_load(compose_path.read_text(encoding="utf-8")) or {}
    )
    services: dict[str, Any] = data.get("services", {})

    removed: list[str] = []

    for entry in manifest.compose_services:
        if entry.addon != addon_id:
            continue
        if entry.name in services:
            del services[entry.name]
            removed.append(entry.name)

    # Remove services the addon defines but the manifest may not track
    # (out-of-sync state from earlier gate behaviour).
    if addon_services is not None:
        for svc in addon_services:
            if svc.name not in removed and svc.name in services:
                del services[svc.name]
                removed.append(svc.name)

    if removed:
        if not data.get("services"):
            data.pop("services", None)
        if not data.get("volumes"):
            data.pop("volumes", None)
        if not data:
            compose_path.unlink(missing_ok=True)
        else:
            atomic_write_text(compose_path, compose_yaml_dumps(data))

    return removed


def _remove_compose_volumes(
    project_dir: Path,
    manifest: Manifest,
    addon_id: str,
    addon_volumes: list[str] | None = None,
) -> list[str]:
    """Remove named volumes that belong to this addon from compose.yml.

    Removes volumes recorded in the manifest (normal path) AND volumes
    the addon defines but were never recorded in the manifest.

    Returns list of removed volume names.
    """
    compose_path = project_dir / COMPOSE_FILE
    if not compose_path.exists():
        return []

    data: dict[str, Any] = (
        compose_yaml_load(compose_path.read_text(encoding="utf-8")) or {}
    )
    vols: dict[str, Any] = data.get("volumes", {})

    removed_names: list[str] = []
    for entry in manifest.compose_volumes:
        if entry.addon != addon_id:
            continue
        if entry.name in vols:
            del vols[entry.name]
            removed_names.append(entry.name)

    if addon_volumes is not None:
        for vol_name in addon_volumes:
            if vol_name not in removed_names and vol_name in vols:
                del vols[vol_name]
                removed_names.append(vol_name)

    if removed_names:
        if not data.get("services"):
            data.pop("services", None)
        if not data.get("volumes"):
            data.pop("volumes", None)
        if not data:
            compose_path.unlink(missing_ok=True)
        else:
            atomic_write_text(compose_path, compose_yaml_dumps(data))

    return removed_names


def _remove_env_vars(
    project_dir: Path,
    manifest: Manifest,
    addon_id: str,
) -> list[str]:
    """Remove env var lines owned by this addon. Returns removed keys."""

    keys_to_remove = {e.key for e in manifest.env if e.addon == addon_id}
    if not keys_to_remove:
        return []
    removed: list[str] = []

    for file_name in ENV_FILES:
        env_path = project_dir / file_name
        if not env_path.exists():
            continue
        lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
        new_lines: list[str] = []
        for line in lines:
            if "=" not in line:
                new_lines.append(line)
                continue
            key = line.split("=", 1)[0].strip()
            if key in keys_to_remove:
                removed.append(key)
                continue
            new_lines.append(line)
        atomic_write_text(env_path, "".join(new_lines))

    return list(dict.fromkeys(removed))


def _partition_deps(
    items: list[str],
    names_to_remove: set[str],
) -> tuple[list[str], list[str]]:
    removed: list[str] = []
    kept: list[str] = []
    for d in items:
        if dep_package_name(str(d)) in names_to_remove:
            removed.append(str(d))
        else:
            kept.append(d)
    return removed, kept


def _remove_deps(
    project_dir: Path, manifest: Manifest, addon_id: str
) -> tuple[list[str], list[str]]:
    """Remove deps contributed by this addon from pyproject.toml.

    Returns (removed_deps, removed_dev_deps).
    """

    pyproject_path = project_dir / PYPROJECT_FILE
    if not pyproject_path.exists():
        return [], []

    doc = tomlkit.parse(pyproject_path.read_text(encoding="utf-8"))

    deps_to_remove = {
        d.package for d in manifest.dependencies if d.addon == addon_id and not d.dev
    }
    dev_deps_to_remove = {
        d.package for d in manifest.dependencies if d.addon == addon_id and d.dev
    }

    project_deps = doc.get("project", {}).get("dependencies", [])
    if isinstance(project_deps, Array):
        removed, kept = _partition_deps([str(d) for d in project_deps], deps_to_remove)
        if removed:
            del project_deps[:]
            for d in kept:
                project_deps.append(d)
    else:
        removed = []

    _dev_doc = doc.get("dependency-groups", {})
    if "dev" in _dev_doc:
        dev_group = _dev_doc["dev"]
    else:
        dev_group = doc.get("project", {}).get("optional-dependencies", {}).get("dev")
    if isinstance(dev_group, (list, Array)):
        removed_dev, kept_dev = _partition_deps(
            [str(d) for d in dev_group], dev_deps_to_remove
        )
        if removed_dev:
            del dev_group[:]
            for d in kept_dev:
                dev_group.append(d)
    else:
        removed_dev = []

    if removed or removed_dev:
        atomic_write_text(pyproject_path, tomlkit.dumps(doc))

    return removed, removed_dev


@dataclass
class _JustBlock:
    """A parsed block from a justfile."""

    kind: str  # "setting", "alias", "recipe", "blank", "other"
    lines: list[str]
    recipe_name: str = ""  # only for kind == "recipe"


def _parse_justfile_blocks(lines: list[str]) -> list[_JustBlock]:
    """Parse a justfile into a list of ``_JustBlock``.

    Two-pass friendly: each block is self-contained so callers can filter
    by kind or ``recipe_name`` and reconstruct without line-level heuristics.
    """
    blocks: list[_JustBlock] = []
    i = 0
    pending_attrs: list[str] = []

    while i < len(lines):
        line = lines[i]
        stripped = line.rstrip()

        if not stripped:
            if pending_attrs:
                blocks.append(_JustBlock("other", pending_attrs))
                pending_attrs = []
            blocks.append(_JustBlock("blank", [line]))
            i += 1

        elif stripped.startswith("set ") and ":" in stripped:
            if pending_attrs:
                blocks.append(_JustBlock("other", pending_attrs))
                pending_attrs = []
            blocks.append(_JustBlock("setting", [line]))
            i += 1

        elif stripped.startswith("alias ") and ":=" in stripped:
            if pending_attrs:
                blocks.append(_JustBlock("other", pending_attrs))
                pending_attrs = []
            blocks.append(_JustBlock("alias", [line]))
            i += 1

        elif stripped.startswith("[") and stripped.endswith("]"):
            # Recipe attribute - accumulate until the next recipe header.
            pending_attrs.append(line)
            i += 1

        elif not line[0].isspace() and not stripped.startswith("#") and ":" in stripped:
            # Recipe header
            m = RECIPE_NAME_RE.match(stripped)
            name = m.group(1) if m else ""
            recipe_lines = pending_attrs + [line]
            pending_attrs = []
            i += 1
            # Consume body (indented lines and blank lines within the body)
            while i < len(lines):
                next_line = lines[i]
                next_stripped = next_line.rstrip()
                if next_stripped and not next_line[0].isspace():
                    break
                recipe_lines.append(next_line)
                i += 1
            blocks.append(_JustBlock("recipe", recipe_lines, recipe_name=name))

        elif stripped.startswith("#"):
            pending_attrs.append(line)
            i += 1

        else:
            if pending_attrs:
                blocks.append(_JustBlock("other", pending_attrs))
                pending_attrs = []
            blocks.append(_JustBlock("other", [line]))
            i += 1

    if pending_attrs:
        blocks.append(_JustBlock("other", pending_attrs))

    return blocks


def _remove_just_recipes(
    project_dir: Path,
    manifest: Manifest,
    addon_id: str,
) -> list[str]:
    """Remove just recipes contributed by this addon from the justfile.

    Uses a two-pass block parser (``_parse_justfile_blocks``) that correctly
    handles recipe attributes (``[private]``), aliases, settings, and blank
    lines.
    """

    justfile_path = project_dir / JUSTFILE_NAME
    if not justfile_path.exists():
        return []

    recipe_names = {r.name for r in manifest.just_recipes if r.addon == addon_id}
    if not recipe_names:
        return []

    text = justfile_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    blocks = _parse_justfile_blocks(lines)
    new_blocks = [
        b for b in blocks if not (b.kind == "recipe" and b.recipe_name in recipe_names)
    ]

    # Collapse consecutive blank blocks to one
    result_lines: list[str] = []
    prev_was_blank = False
    for block in new_blocks:
        is_blank = block.kind == "blank"
        if is_blank and prev_was_blank:
            continue
        result_lines.extend(block.lines)
        prev_was_blank = is_blank

    atomic_write_text(justfile_path, "".join(result_lines))
    return list(recipe_names)


def _remove_ruff_excludes(
    project_dir: Path, manifest: Manifest, addon_id: str
) -> list[str]:
    """Remove ruff exclude entries contributed by this addon from pyproject.toml."""

    pyproject_path = project_dir / PYPROJECT_FILE
    if not pyproject_path.exists():
        return []

    to_remove = {e.name for e in manifest.ruff_excludes if e.addon == addon_id}
    if not to_remove:
        return []

    doc = tomlkit.parse(pyproject_path.read_text(encoding="utf-8"))
    ruff = doc.get("tool", {}).get("ruff")
    if ruff is None:
        return []

    exclude = ruff.get("exclude")
    if not isinstance(exclude, Array):
        return []

    removed: list[str] = []
    kept: list[str] = []
    for item in exclude:
        s = str(item)
        if s in to_remove:
            removed.append(s)
        else:
            kept.append(s)

    if removed:
        del exclude[:]
        for d in kept:
            exclude.append(d)
        atomic_write_text(pyproject_path, tomlkit.dumps(doc))

    return removed


def _remove_tool_overrides(
    project_dir: Path, manifest: Manifest, addon_id: str
) -> list[str]:
    """Remove tool override entries contributed by this addon from pyproject.toml."""

    pyproject_path = project_dir / PYPROJECT_FILE
    if not pyproject_path.exists():
        return []

    entries = [t for t in manifest.tool_overrides if t.addon == addon_id]
    if not entries:
        return []

    doc = tomlkit.parse(pyproject_path.read_text(encoding="utf-8"))
    removed: list[str] = []
    modified = False

    # Group entries by section
    by_section: dict[str, set[str]] = {}
    for entry in entries:
        by_section.setdefault(entry.section, set()).add(entry.module)

    for section, modules in by_section.items():
        overrides = doc.get("tool", {}).get(section, {}).get("overrides")
        if not isinstance(overrides, list):
            continue

        kept_overrides: list[Any] = []
        for override in overrides:
            if not isinstance(override, dict):
                kept_overrides.append(override)
                continue
            mod_list = override.get("module")
            if isinstance(mod_list, list) and any(str(m) in modules for m in mod_list):
                removed.append(f"{section}:{mod_list}")
                modified = True
            else:
                kept_overrides.append(override)

        if modified:
            del overrides[:]
            for o in kept_overrides:
                overrides.append(o)

    if modified:
        atomic_write_text(pyproject_path, tomlkit.dumps(doc))

    return removed


def _dry_remove(
    project_dir: Path,
    addon_id: str,
    addon_cfg: AddonConfig,
    lockfile: ZenitLockfile,
    pkg_name: str,
) -> None:
    """Print what `zenit remove` would do without writing anything."""

    dry_run_banner("remove", addon_id)

    dry_header("Files that would be removed")
    # Show all non-empty-init files
    for fc in addon_cfg.files:
        dest = resolve_dest_placeholder(fc.dest, pkg_name)
        if dest.endswith("__init__.py") and fc.content == "":
            continue
        full = project_dir / dest
        if full.exists():
            print(f"  {RED}-{RESET} {dest}")
        else:
            print(f"  {DIM}  {dest}  (already missing){RESET}")
    # Show empty __init__.py only if parent would be truly empty
    all_this_addon_dests = {
        resolve_dest_placeholder(fc.dest, pkg_name) for fc in addon_cfg.files
    }
    all_this_addon_paths = {project_dir / d for d in all_this_addon_dests}
    for fc in addon_cfg.files:
        dest = resolve_dest_placeholder(fc.dest, pkg_name)
        if not (dest.endswith("__init__.py") and fc.content == ""):
            continue
        full = project_dir / dest
        parent = full.parent
        if not parent.exists():
            continue
        surviving_children = set(parent.iterdir()) - all_this_addon_paths
        if surviving_children:
            continue
        if full.exists():
            print(f"  {RED}-{RESET} {dest}")
        else:
            print(f"  {DIM}  {dest}  (already missing){RESET}")

    if addon_cfg.compose_services:
        bullet_list(
            "Compose services that would be removed:",
            [svc.name for svc in addon_cfg.compose_services],
            bullet="-",
            bullet_color=RED,
        )

    if addon_cfg.env_vars:
        bullet_list(
            "Env vars that would be removed:",
            [ev.key for ev in addon_cfg.env_vars],
            bullet="-",
            bullet_color=RED,
        )

    if addon_cfg.deps or addon_cfg.dev_deps:
        bullet_list(
            "Dependencies that would be removed from pyproject.toml:",
            addon_cfg.deps,
            bullet="-",
            bullet_color=RED,
        )
        if addon_cfg.dev_deps:
            bullet_list(
                "", addon_cfg.dev_deps, bullet="-", bullet_color=RED, suffix="(dev)"
            )

    print()


def remove_addon_interactive(
    dry_run: bool = False, yes: bool = False, project_dir: Path | None = None
) -> None:
    """Interactive TUI for removing a single addon from an existing project."""

    if project_dir is None:
        project_dir = Path.cwd()
    lockfile = read_lockfile(project_dir)

    if lockfile is None:
        error(
            "No .zenit.toml found. 'zenit remove' only works in projects scaffolded by zenit."
        )
        raise typer.Exit(1)

    if not lockfile.addons:
        error("No addons are installed in this project.")
        raise typer.Exit(1)

    available_meta = list_addons()
    graph = DependencyGraph.build_from_meta(available_meta)
    installed = [get_addon(aid) for aid in lockfile.addons]

    requires_map = {m.id: m.requires for m in available_meta}

    zenit_root = get_zenit_root()
    template_required: set[str] = set()
    if lockfile.template_source == "native":
        try:
            template_config = load_template_config(zenit_root, lockfile.template)
            template_required = set(template_config.requires_addons)
        except FileNotFoundError:
            pass

    items = []

    installed.sort(key=lambda c: c.id)

    for addon in installed:
        reasons: list[str] = []
        if addon.id in template_required:
            reasons.append(f"__template__{lockfile.template}")
        items.append((addon.id, addon.description, reasons))

    selected = prompt_multi_addon(
        items,
        context="remove",
        prompt="Select addon(s) to remove:",
        requires_map=requires_map,
    )

    if not selected:
        raise typer.Exit(0)

    # Process dependents before their dependencies (leaves first).
    sorted_ids = list(graph.tsort_reverse(set(selected)))
    if len(sorted_ids) > 1:
        from zenit.core.rollback import batch_snapshot

        with batch_snapshot(project_dir, f"addons: {', '.join(sorted_ids)}"):
            for addon_id in sorted_ids:
                _remove_one(addon_id, dry_run, yes, project_dir)
    else:
        for addon_id in sorted_ids:
            _remove_one(addon_id, dry_run, yes, project_dir)
