"""zenit doctor - inspect a project's health against zenit's expectations."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from importlib.metadata import version as get_version
from pathlib import Path

from ruamel.yaml import YAML

from zenit.addons._registry import get_addon, list_addons
from zenit.cli.ui import BOLD, DIM, GREEN, RED, RESET, YELLOW
from zenit.core._filenames import (
    COMMON_FILES,
    COMPOSE_FILE,
    ENV_FILES,
    JUSTFILE_NAME,
    PYPROJECT_FILE,
)
from zenit.core._paths import get_zenit_root
from zenit.core.collect import collect_all
from zenit.core.constants import RECIPE_NAME_RE
from zenit.core.lockfile import SCHEMA_VERSION, ZenitLockfile, read_lockfile
from zenit.core.manifest import dep_package_name, read_manifest, write_manifest
from zenit.core.pkg_name import normalise_pkg_name, resolve_dest_placeholder
from zenit.schema.models import (
    AddonConfig,
    AddonMeta,
    Contributions,
    DependencyEntry,
    EntrySource,
    EnvEntry,
    Manifest,
    ManifestBlock,
    OwnedEntry,
    TemplateConfig,
)
from zenit.templates._load_config import load_template_config


class Severity(Enum):
    OK = "ok"
    WARN = "warn"
    ERROR = "error"


@dataclass
class HealthIssue:
    severity: Severity
    message: str
    hint: str = ""


@dataclass
class HealthResult:
    category: str
    issues: list[HealthIssue] = field(default_factory=list)

    def ok(self, message: str) -> None:
        self.issues.append(HealthIssue(Severity.OK, message))

    def warn(self, message: str, hint: str = "") -> None:
        self.issues.append(HealthIssue(Severity.WARN, message, hint))

    def error(self, message: str, hint: str = "") -> None:
        self.issues.append(HealthIssue(Severity.ERROR, message, hint))

    @property
    def has_errors(self) -> bool:
        return any(i.severity == Severity.ERROR for i in self.issues)

    @property
    def has_warnings(self) -> bool:
        return any(i.severity == Severity.WARN for i in self.issues)


def run_doctor(
    project_dir: Path,
    *,
    fix: bool = False,
) -> list[HealthResult]:
    lockfile = read_lockfile(project_dir)
    if lockfile is None:
        return [_check_metadata(project_dir)]

    manifest = read_manifest(project_dir)

    if fix:
        _fix_python_blocks(project_dir, manifest)
        manifest = read_manifest(project_dir)

    available_meta = list_addons()
    available_configs = [get_addon(m.id) for m in available_meta]

    zenit_root = get_zenit_root()
    template_config: TemplateConfig | None = None
    addon_configs: list[AddonConfig] = []
    contributions: Contributions | None = None
    template_load_error: str | None = None

    if lockfile.template:
        try:
            template_config = load_template_config(zenit_root, lockfile.template)
            addon_configs = [c for c in available_configs if c.id in lockfile.addons]
            contributions = collect_all(template_config, addon_configs)
        except Exception as exc:
            template_load_error = str(exc)

    results: list[HealthResult] = []
    results.append(_check_metadata(project_dir))
    results.append(_check_template_health(project_dir, lockfile, manifest))
    results.append(_check_manifest_schema(project_dir, lockfile, manifest))

    if template_load_error:
        unavailable = HealthResult("Template configuration")
        if lockfile.template_source == "copier":
            source = lockfile.template_uri or lockfile.template
            unavailable.warn(
                f"Template '{source}' was imported from Copier and is not "
                f"available as a local zenit template.",
                hint="Migrated templates are snapshots. zenit cannot reload them. "
                "Template-dependent checks are skipped.",
            )
        else:
            unavailable.warn(
                f"Template '{lockfile.template}' is not available locally.",
                hint="The template file may have been deleted or renamed. "
                "Template-dependent checks are skipped.",
            )
        results.append(unavailable)
    else:
        results.append(
            _check_dependencies(project_dir, lockfile, template_config, contributions)
        )
        results.append(
            _check_files(project_dir, lockfile, template_config, addon_configs)
        )
        results.append(
            _check_compose(
                project_dir, lockfile, template_config, addon_configs, contributions
            )
        )
        results.append(
            _check_env(project_dir, lockfile, template_config, contributions)
        )

    results.append(_check_addon_health(project_dir, lockfile, available_configs))
    results.append(_check_manifest_env(project_dir, manifest))
    results.append(_check_manifest_compose(project_dir, manifest))
    results.append(_check_manifest_deps(project_dir, manifest))
    results.append(_check_manifest_recipes(project_dir, manifest))
    results.append(_check_python_line_presence(project_dir, manifest))
    results.append(_check_python_integrity(project_dir, manifest))
    results.append(_check_copier_unmanaged_content(project_dir, lockfile, manifest))

    return results


# ── Manifest-driven fast-tier checks ─────────────────────────────────────────


type _AnyEntry = ManifestBlock | EnvEntry | OwnedEntry | DependencyEntry


def _check_manifest_schema(
    project_dir: Path, lockfile: ZenitLockfile, manifest: Manifest | None = None
) -> HealthResult:
    """Verify schema_version == 2 and manifest has no orphan blocks."""
    result = HealthResult("Manifest schema")
    if manifest is None:
        manifest = read_manifest(project_dir)

    if lockfile.schema_version != SCHEMA_VERSION:
        result.warn(
            f"Project schema_version is {lockfile.schema_version}, expected {SCHEMA_VERSION}.",
            hint=(
                "This project was scaffolded with an older version of zenit. "
                "Re-scaffold or run 'zenit doctor' after upgrading to confirm compatibility."
            ),
        )
    else:
        result.ok(f"schema_version is {SCHEMA_VERSION}.")

    addon_ids = set(lockfile.addons)

    def _orphan_addons_in(
        entries: Sequence[_AnyEntry], attr: str = "addon"
    ) -> set[str]:
        return {
            getattr(e, attr)
            for e in entries
            if getattr(e, attr) and getattr(e, attr) not in addon_ids
        }

    orphan_addons = (
        _orphan_addons_in(manifest.python_blocks)
        | _orphan_addons_in(manifest.env)
        | _orphan_addons_in(manifest.compose_services)
        | _orphan_addons_in(manifest.compose_volumes)
        | _orphan_addons_in(manifest.dependencies)
        | _orphan_addons_in(manifest.just_recipes)
    )
    if orphan_addons:
        result.error(
            f"Manifest contains blocks for addons not in lockfile: {', '.join(sorted(orphan_addons))}.",
            hint="Run 'zenit remove <addon>' to clean up, or re-scaffold the project.",
        )
    else:
        result.ok("No orphan manifest blocks.")

    return result


def _check_manifest_env(
    project_dir: Path, manifest: Manifest | None = None
) -> HealthResult:
    """All manifest.env keys must be present in .env and .env.example."""
    result = HealthResult("Manifest env integrity")
    if manifest is None:
        manifest = read_manifest(project_dir)

    if not manifest.env:
        result.ok("No manifest env entries to verify.")
        return result

    for file_name in ENV_FILES:
        env_path = project_dir / file_name
        if not env_path.exists():
            result.warn(
                f"'{file_name}' is missing - cannot verify env integrity.",
                hint=f"Restore '{file_name}' or run 'zenit add' for the relevant addon.",
            )
            continue

        text = env_path.read_text(encoding="utf-8")
        # Extract keys properly to avoid substring false-positives
        file_keys = {
            line.split("=", 1)[0].strip()
            for line in text.splitlines()
            if "=" in line and not line.strip().startswith("#")
        }
        for entry in manifest.env:
            if entry.key not in file_keys:
                result.error(
                    f"Manifest env key '{entry.key}' (owned by '{entry.addon or 'template'}') "
                    f"is missing from '{file_name}'.",
                    hint=f"Add '{entry.key}=<<value>' to '{file_name}', "
                    f"or run 'zenit remove {entry.addon}' to clean up the manifest.",
                )
            else:
                result.ok(f"'{entry.key}' is present in '{file_name}'.")

    return result


def _check_manifest_compose(
    project_dir: Path, manifest: Manifest | None = None
) -> HealthResult:
    """All manifest compose_services and compose_volumes must exist in compose.yml."""
    result = HealthResult("Manifest compose integrity")
    if manifest is None:
        manifest = read_manifest(project_dir)

    if not manifest.compose_services and not manifest.compose_volumes:
        result.ok("No manifest compose entries to verify.")
        return result

    compose_path = project_dir / COMPOSE_FILE
    if not compose_path.exists():
        if manifest.compose_services or manifest.compose_volumes:
            result.error(
                "compose.yml is missing but manifest records compose services/volumes.",
                hint="Restore compose.yml or remove the relevant addon.",
            )
        return result

    try:
        data = YAML().load(compose_path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        result.error(
            f"compose.yml could not be parsed: {e}",
            hint="Fix the YAML syntax and re-run 'zenit doctor'.",
        )
        return result

    services: dict[str, object] = data.get("services", {})
    volumes: dict[str, object] = data.get("volumes", {})

    for entry in manifest.compose_services:
        if entry.name not in services:
            result.error(
                f"Manifest service '{entry.name}' (owned by '{entry.addon or 'template'}') "
                f"is missing from compose.yml.",
                hint=f"Run 'zenit add {entry.addon}' to restore it, "
                f"or 'zenit remove {entry.addon}' to clean up.",
            )
        else:
            result.ok(f"Service '{entry.name}' is present in compose.yml.")

    for entry in manifest.compose_volumes:
        if entry.name not in volumes:
            result.error(
                f"Manifest volume '{entry.name}' (owned by '{entry.addon or 'template'}') "
                f"is missing from compose.yml.",
                hint="Restore the volume definition in compose.yml.",
            )
        else:
            result.ok(f"Volume '{entry.name}' is present in compose.yml.")

    return result


def _check_manifest_deps(
    project_dir: Path, manifest: Manifest | None = None
) -> HealthResult:
    """All manifest.dependencies must be present in pyproject.toml."""
    result = HealthResult("Manifest dependency integrity")
    if manifest is None:
        manifest = read_manifest(project_dir)

    if not manifest.dependencies:
        result.ok("No manifest dependencies to verify.")
        return result

    pyproject_path = project_dir / PYPROJECT_FILE
    if not pyproject_path.exists():
        result.error(
            "pyproject.toml is missing - cannot verify manifest dependencies.",
            hint="Restore pyproject.toml.",
        )
        return result

    try:
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        result.error(
            f"pyproject.toml could not be parsed: {e}",
            hint="Fix the TOML syntax and re-run 'zenit doctor'.",
        )
        return result

    raw_deps: list[str] = data.get("project", {}).get("dependencies", [])
    dev_group: list[str] = data.get("dependency-groups", {}).get("dev", []) or data.get(
        "project", {}
    ).get("optional-dependencies", {}).get("dev", [])

    installed = {dep_package_name(d) for d in raw_deps}
    installed_dev = {dep_package_name(d) for d in dev_group}

    missing: list[DependencyEntry] = []
    for dep in manifest.dependencies:
        bucket = installed_dev if dep.dev else installed
        if dep_package_name(dep.package) not in bucket:
            missing.append(dep)

    if missing:
        for dep in missing:
            kind = "dev " if dep.dev else ""
            result.error(
                f"Manifest {kind}dependency '{dep.package}' "
                f"(owned by '{dep.addon or 'template'}') is missing from pyproject.toml.",
                hint=f"Run 'uv add {dep.spec}' or 'zenit add {dep.addon}' to restore it.",
            )
    else:
        total = len(manifest.dependencies)
        label = "manifest dependency" if total == 1 else "manifest dependencies"
        result.ok(f"All {total} {label} are present.")

    return result


def _check_manifest_recipes(
    project_dir: Path, manifest: Manifest | None = None
) -> HealthResult:
    """All manifest.just_recipes must exist in the justfile."""
    result = HealthResult("Manifest just-recipe integrity")
    if manifest is None:
        manifest = read_manifest(project_dir)

    if not manifest.just_recipes:
        result.ok("No manifest just-recipes to verify.")
        return result

    justfile_path = project_dir / JUSTFILE_NAME
    if not justfile_path.exists():
        result.warn(
            "justfile is missing - cannot verify just-recipe integrity.",
            hint="Restore the justfile.",
        )
        return result

    text = justfile_path.read_text(encoding="utf-8")
    existing_names = set(RECIPE_NAME_RE.findall(text))

    missing: list[OwnedEntry] = []
    for entry in manifest.just_recipes:
        if entry.name not in existing_names:
            missing.append(entry)

    if missing:
        for entry in missing:
            result.error(
                f"Manifest just-recipe '{entry.name}' (owned by '{entry.addon or 'template'}') "
                f"is missing from the justfile.",
                hint=f"Run 'zenit remove {entry.addon} && zenit add {entry.addon}' to reinstall it.",
            )
    else:
        total = len(manifest.just_recipes)
        label = "manifest just-recipe" if total == 1 else "manifest just-recipes"
        result.ok(f"All {total} {label} are present.")

    return result


def _check_python_line_presence(
    project_dir: Path, manifest: Manifest | None = None
) -> HealthResult:
    """Fast check: each ManifestBlock's line range still exists in the file."""
    result = HealthResult("Python block line presence")
    if manifest is None:
        manifest = read_manifest(project_dir)

    if not manifest.python_blocks:
        result.ok("No Python blocks recorded in manifest.")
        return result

    for block in manifest.python_blocks:
        file_path = project_dir / block.file
        if not file_path.exists():
            result.error(
                f"File '{block.file}' (containing '{block.point}' block for '{block.addon}') "
                f"is missing.",
                hint=f"Restore the file or run 'zenit remove {block.addon}'.",
            )
            continue

        try:
            _, end_str = block.lines.split("-")
            end_line = int(end_str)
        except ValueError:
            result.warn(
                f"Block '{block.point}' for '{block.addon}' has malformed lines field: {block.lines!r}.",
            )
            continue

        text = file_path.read_text(encoding="utf-8")
        line_count = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
        if end_line > line_count:
            result.error(
                f"Block '{block.point}' for addon '{block.addon}' records lines up to "
                f"{end_line}, but '{block.file}' only has {line_count} lines.",
                hint=(
                    f"The file may have been truncated. Run 'zenit doctor' "
                    f"for a full check, or 'zenit remove {block.addon}' to clean up."
                ),
            )
        else:
            result.ok(
                f"Block '{block.point}' for '{block.addon}' is present in '{block.file}'."
            )

    return result


# ── Thorough tier ──────────────────────────────────────────────────────────────


def _check_python_integrity(
    project_dir: Path, manifest: Manifest | None = None
) -> HealthResult:
    """Parse each file, extract block, recompute fingerprints.

    Reports the actual position of each block, not the potentially stale
    recorded position.  Uses ``relocate_block`` (which re-runs the locator)
    to find where the block currently is.
    """
    from zenit.core.handlers.python_handler import relocate_block
    from zenit.core.manifest import fingerprint as compute_fingerprint

    result = HealthResult("Python block integrity")
    if manifest is None:
        manifest = read_manifest(project_dir)

    if not manifest.python_blocks:
        result.ok("No Python blocks to verify.")
        return result

    for block in manifest.python_blocks:
        file_path = project_dir / block.file
        if not file_path.exists():
            result.error(
                f"File '{block.file}' is missing (block '{block.point}' for '{block.addon}').",
                hint=f"Run 'zenit remove {block.addon}' to clean up the manifest.",
            )
            continue

        try:
            start_str, end_str = block.lines.split("-")
            start_line = int(start_str)
            end_line = int(end_str)
        except ValueError:
            result.warn(
                f"Block '{block.point}' for '{block.addon}' has malformed lines: {block.lines!r}."
            )
            continue

        lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
        if end_line > len(lines):
            result.error(
                f"Block '{block.point}' for '{block.addon}' extends beyond end of '{block.file}'.",
                hint=f"Run 'zenit remove {block.addon}' or restore the file.",
            )
            continue

        extracted = "".join(lines[start_line - 1 : end_line])

        try:
            fp, fp_norm = compute_fingerprint(extracted)
        except Exception as e:
            result.warn(
                f"Could not parse block '{block.point}' in '{block.file}': {e}",
                hint="The block may not be valid Python. Run 'zenit remove' to clean up.",
            )
            continue

        if fp == block.fingerprint:
            result.ok(
                f"Block '{block.point}' for '{block.addon}' in '{block.file}' "
                f"at lines {block.lines} is unchanged."
            )
        elif fp_norm == block.fingerprint_normalised:
            result.warn(
                f"Block '{block.point}' for '{block.addon}' in '{block.file}' "
                f"at lines {block.lines} was reformatted "
                f"(normalised fingerprint matches).",
                hint="Run 'zenit doctor' after reformatting to confirm.",
            )
        else:
            actual = relocate_block(file_path, block)
            if actual is not None:
                actual_str = f"{actual[0]}-{actual[1]}"
                result.warn(
                    f"Block '{block.point}' for '{block.addon}' in '{block.file}' "
                    f"has drifted - recorded at lines {block.lines}, "
                    f"actually at lines {actual_str}.",
                    hint="Run 'zenit doctor --fix' to update the manifest's line tracking.",
                )
            else:
                result.error(
                    f"Block '{block.point}' for '{block.addon}' in '{block.file}' "
                    f"has been modified (fingerprint mismatch at lines {block.lines}).",
                    hint=(
                        f"Run 'zenit doctor --fix' to re-sync line tracking. "
                        f"If it persists, the block content was changed - "
                        f"run 'zenit remove {block.addon}' and 'zenit add {block.addon}' "
                        f"to re-inject, or restore the original block."
                    ),
                )

    return result


def _fix_python_blocks(project_dir: Path, manifest: Manifest) -> int:
    """Re-sync stale line numbers and fingerprints in *manifest*.

    For each ``ManifestBlock``, re-runs the stored locator to find where the
    block actually is in the current file, then updates ``block.lines`` and
    recomputes ``fingerprint`` / ``fingerprint_normalised`` from the actual
    content at that location.  Only modifies zenit tracking data - never
    touches user code.

    Returns the number of blocks whose tracking data was updated.
    """
    from zenit.core.manifest import resync_python_blocks

    fixed = resync_python_blocks(project_dir, manifest)

    if fixed > 0:
        write_manifest(project_dir, manifest)

    return fixed


def _check_template_health(
    project_dir: Path,
    lockfile: ZenitLockfile,
    manifest: Manifest | None = None,
) -> HealthResult:
    """Check template health for Copier-sourced projects.

    For native projects, returns an OK result immediately.
    For Copier projects, reports warnings about file path tracking and
    an error if the template had ``_tasks`` that were not executed.
    """
    result = HealthResult("Template health")

    if lockfile.template_source != "copier":
        result.ok("Project uses a native template.")
        return result

    if lockfile.template_has_tasks:
        stub_path = project_dir / ".zenit-tasks.md"
        if stub_path.exists():
            text = stub_path.read_text(encoding="utf-8")
            task_count = text.count("\n## ") + (1 if text.startswith("## ") else 0)
            result.error(
                f"Project has {task_count} pending task(s) from Copier _tasks "
                f"that failed or were not executed.",
                hint="Check .zenit-tasks.md for the commands and error output.",
            )
        else:
            result.error(
                "This project has pending manual steps from Copier _tasks that "
                "were not executed automatically.",
                hint=(
                    "Check .zenit-tasks.md in the project root "
                    "for the list of commands to run manually."
                ),
            )

    file_count = len(lockfile.template_file_paths)
    if file_count > 0:
        result.warn(
            f"{file_count} file(s) are tracked as Copier template outputs "
            f"(may not be managed by zenit add/remove).",
        )
    else:
        result.ok("No Copier template file paths tracked.")

    result.ok(f"Copier template source: {lockfile.template_uri or lockfile.template}.")

    return result


def _suggest_addons_for_entries(
    entries: list[str],
    entry_type: str,
    available: list[AddonMeta],
) -> list[str]:
    """Return addon IDs whose known contributions overlap with *entries*."""
    suggestions: list[str] = []
    for meta in available:
        try:
            cfg = get_addon(meta.id)
        except Exception:
            continue

        matched = False
        if entry_type == "env":
            matched = any(ev.key in entries for ev in cfg.env_vars)
        elif entry_type == "deps":
            cfg_pkgs = {dep_package_name(d) for d in cfg.deps + cfg.dev_deps}
            matched = bool(cfg_pkgs & set(entries))
        elif entry_type == "compose_services":
            matched = any(svc.name in entries for svc in cfg.compose_services)
        elif entry_type == "compose_volumes":
            matched = any(vol in entries for vol in cfg.compose_volumes)

        if matched:
            suggestions.append(meta.id)

    return suggestions


def _check_copier_unmanaged_content(
    project_dir: Path,
    lockfile: ZenitLockfile,
    manifest: Manifest | None = None,
) -> HealthResult:
    """For Copier-migrated projects, report unmanaged content by component.

    Lists env vars, deps, compose services, and volumes that are still
    source=TEMPLATE with addon=\"\", and suggests which native addon could
    take ownership.
    """
    result = HealthResult("Unmanaged content")

    if lockfile.template_source != "copier":
        result.ok("Project uses a native template - no unmanaged content expected.")
        return result

    if manifest is None:
        manifest = read_manifest(project_dir)

    template_entries: dict[str, list[str]] = {
        "env": [],
        "deps": [],
        "compose_services": [],
        "compose_volumes": [],
        "just_recipes": [],
    }

    for e in manifest.env:
        if e.source == EntrySource.TEMPLATE and e.addon == "":
            template_entries["env"].append(e.key)

    for d in manifest.dependencies:
        if d.source == EntrySource.TEMPLATE and d.addon == "":
            template_entries["deps"].append(d.package)

    for s in manifest.compose_services:
        if s.source == EntrySource.TEMPLATE and s.addon == "":
            template_entries["compose_services"].append(s.name)

    for v in manifest.compose_volumes:
        if v.source == EntrySource.TEMPLATE and v.addon == "":
            template_entries["compose_volumes"].append(v.name)

    for r in manifest.just_recipes:
        if r.source == EntrySource.TEMPLATE and r.addon == "":
            template_entries["just_recipes"].append(r.name)

    available = list_addons()
    total_unmanaged = sum(len(v) for v in template_entries.values())

    if total_unmanaged == 0:
        result.ok("All manifest content is managed by native addons.")
        return result

    for entry_type, entries in template_entries.items():
        if not entries:
            continue
        suggestions = _suggest_addons_for_entries(entries, entry_type, available)
        display = ", ".join(entries[:5])
        if len(entries) > 5:
            display += "..."
        hint = (
            f"Run: zenit add {' | zenit add '.join(suggestions)}"
            if suggestions
            else "No matching native addon found - content must be managed manually."
        )
        result.warn(
            f"{len(entries)} unmanaged {entry_type}: {display}",
            hint=hint,
        )

    return result


# ── Legacy fast-tier checks (unchanged) ───────────────────────────────────────


def _check_metadata(project_dir: Path) -> HealthResult:
    """Check .zenit.toml validity, addon compatibility, and version skew."""

    result = HealthResult("Metadata")
    lockfile = read_lockfile(project_dir)

    if lockfile is None:
        result.error(
            ".zenit.toml is missing.",
            hint="Re-scaffold the project or create .zenit.toml manually.",
        )
        return result
    result.ok(".zenit.toml is present and valid.")

    if not lockfile.template:
        result.error(
            ".zenit.toml has no template field.",
            hint="Add 'template = \"blank\"' or 'template = \"fastapi\"' to [project].",
        )
    else:
        result.ok(f"Template is '{lockfile.template}'.")

    available_meta = list_addons()
    known_ids = {m.id for m in available_meta}
    unknown = [a for a in lockfile.addons if a not in known_ids]
    if unknown:
        result.error(
            f"Unknown addon(s) in .zenit.toml: {', '.join(unknown)}.",
            hint="These addons are not recognised by the current zenit version. "
            "You may need to upgrade zenit or remove them from .zenit.toml.",
        )
    else:
        if lockfile.addons:
            result.ok(f"All installed addons are known: {', '.join(lockfile.addons)}.")
        else:
            result.ok("No addons installed.")

    requires_map = {m.id: m.requires for m in available_meta}
    for addon_id in lockfile.addons:
        for req in requires_map.get(addon_id, []):
            if req not in lockfile.addons:
                result.error(
                    f"Addon '{addon_id}' requires '{req}' but it is not installed.",
                    hint=f"Run 'zenit add {req}' to install the missing dependency.",
                )
            else:
                result.ok(f"Addon '{addon_id}' dependency '{req}' is satisfied.")

    if lockfile.zenit_version:
        try:
            current = get_version("zenit")
            if current != lockfile.zenit_version:
                result.warn(
                    f"Project was scaffolded with zenit {lockfile.zenit_version}, "
                    f"current version is {current}.",
                    hint="Run 'zenit doctor' after upgrading to check for compatibility issues.",
                )
            else:
                result.ok(f"Zenit version matches: {current}.")
        except Exception:
            result.warn(
                "Could not determine current zenit version.",
                hint="Install zenit as a package to enable version checks.",
            )
    else:
        result.warn(
            ".zenit.toml has no zenit_version field.",
            hint="This project was scaffolded with an older version of zenit.",
        )

    return result


def _check_dependencies(
    project_dir: Path,
    lockfile: ZenitLockfile,
    template_config: TemplateConfig | None = None,
    contributions: Contributions | None = None,
) -> HealthResult:
    """Check that pyproject.toml contains all deps expected by the template and addons."""

    result = HealthResult("Dependencies")

    pyproject_path = project_dir / PYPROJECT_FILE
    if not pyproject_path.exists():
        result.error(
            "pyproject.toml is missing.",
            hint="zenit cannot verify dependencies without pyproject.toml.",
        )
        return result
    result.ok("pyproject.toml is present.")

    try:
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        result.error(
            f"pyproject.toml could not be parsed: {e}",
            hint="Fix the TOML syntax error and re-run 'zenit doctor'.",
        )
        return result

    raw_deps: list[str] = data.get("project", {}).get("dependencies", [])
    dev_group: list[str] = data.get("dependency-groups", {}).get("dev", []) or data.get(
        "project", {}
    ).get("optional-dependencies", {}).get("dev", [])

    installed_deps = {dep_package_name(d) for d in raw_deps}
    installed_dev_deps = {dep_package_name(d) for d in dev_group}

    if template_config is None or contributions is None:
        zenit_root = get_zenit_root()
        try:
            template_config = load_template_config(zenit_root, lockfile.template)
        except Exception:
            result.warn(
                f"Could not load template '{lockfile.template}' to verify deps.",
                hint="The template may have changed since this project was scaffolded.",
            )
            return result
        available_meta = list_addons()
        selected_addon_configs = [
            get_addon(m.id) for m in available_meta if m.id in lockfile.addons
        ]
        contributions = collect_all(template_config, selected_addon_configs)

    expected_deps = contributions.deps
    expected_dev_deps = contributions.template_dev_deps + contributions.dev_deps

    missing_deps = [
        d for d in expected_deps if dep_package_name(d) not in installed_deps
    ]
    missing_dev_deps = [
        d for d in expected_dev_deps if dep_package_name(d) not in installed_dev_deps
    ]

    if missing_deps:
        for dep in missing_deps:
            result.error(
                f"Runtime dependency '{dep}' is missing from pyproject.toml.",
                hint="Add it under [project] dependencies or run 'zenit add' again.",
            )
    else:
        result.ok("All expected runtime dependencies are present.")

    if missing_dev_deps:
        for dep in missing_dev_deps:
            result.warn(
                f"Dev dependency '{dep}' is missing from pyproject.toml.",
                hint="Add it under [dependency-groups] dev.",
            )
    else:
        result.ok("All expected dev dependencies are present.")

    return result


def _check_files(
    project_dir: Path,
    lockfile: ZenitLockfile,
    template_config: TemplateConfig | None = None,
    addon_configs: list[AddonConfig] | None = None,
) -> HealthResult:
    """Check that all files generated by the template and addons still exist."""

    result = HealthResult("Generated files")

    pkg_name = normalise_pkg_name(project_dir.name)

    if template_config is None or addon_configs is None:
        zenit_root = get_zenit_root()
        try:
            template_config = load_template_config(zenit_root, lockfile.template)
        except Exception:
            result.warn(
                f"Could not load template '{lockfile.template}' to verify files.",
                hint="The template may have changed since this project was scaffolded.",
            )
            return result
        available_meta = list_addons()
        addon_configs = [
            get_addon(m.id) for m in available_meta if m.id in lockfile.addons
        ]

    all_files = [("template", fc) for fc in template_config.files] + [
        (addon.id, fc) for addon in addon_configs for fc in addon.files
    ]

    missing: list[tuple[str, str]] = []
    checked = 0

    for source, fc in all_files:
        dest = resolve_dest_placeholder(fc.dest, pkg_name)
        if dest.endswith("__init__.py"):
            continue
        checked += 1
        if not (project_dir / dest).exists():
            missing.append((source, dest))

    if not missing:
        result.ok(f"All {checked} expected files are present.")
    else:
        if checked > len(missing):
            result.ok(
                f"{checked - len(missing)} of {checked} expected files are present."
            )
        for source, dest in missing:
            label = "template" if source == "template" else f"addon '{source}'"
            result.error(
                f"'{dest}' is missing (generated by {label}).",
                hint=f"This file was generated at scaffold time. "
                f"Restore it or re-run 'zenit add {source}' if it was an addon.",
            )

    for fname in COMMON_FILES:
        if not (project_dir / fname).exists():
            result.warn(
                f"'{fname}' is missing.",
                hint="This file is generated for all zenit projects. It may have been deleted.",
            )

    return result


def _check_addon_health(
    project_dir: Path,
    lockfile: ZenitLockfile,
    available: list[AddonConfig] | None = None,
) -> HealthResult:
    """Call each installed addon's health_check hook if it defines one."""

    result = HealthResult("Addon integrity")

    if not lockfile.addons:
        result.ok("No addons installed.")
        return result

    if available is None:
        available = [get_addon(m.id) for m in list_addons()]
    addon_map = {cfg.id: cfg for cfg in available}

    any_checks = False
    for addon_id in lockfile.addons:
        cfg = addon_map.get(addon_id)
        if cfg is None:
            continue
        hooks = cfg._module
        if hooks is None or hooks.health_check is None:
            continue

        any_checks = True
        try:
            issues = hooks.health_check(project_dir, lockfile)
            for issue in issues:
                result.issues.append(issue)
        except Exception as e:
            result.warn(
                f"health_check for '{addon_id}' raised an error: {e}",
                hint="This is a bug in the addon's health_check function.",
            )

    if not any_checks:
        result.ok("No addon integrity checks defined.")

    return result


def _check_compose(
    project_dir: Path,
    lockfile: ZenitLockfile,
    template_config: TemplateConfig | None = None,
    addon_configs: list[AddonConfig] | None = None,
    contributions: Contributions | None = None,
) -> HealthResult:
    """Check compose.yml for expected services and duplicate definitions."""

    result = HealthResult("Compose")

    compose_path = project_dir / COMPOSE_FILE
    if not compose_path.exists():
        result.ok("No compose.yml - skipping compose check.")
        return result

    try:
        data = YAML().load(compose_path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        result.error(
            f"compose.yml could not be parsed: {e}",
            hint="Fix the YAML syntax error and re-run 'zenit doctor'.",
        )
        return result

    services: dict[str, object] = data.get("services", {})

    raw_text = compose_path.read_text(encoding="utf-8")
    service_name_counts: dict[str, int] = {}
    in_services = False
    services_indent = 0
    for line in raw_text.splitlines():
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if stripped == "services:":
            in_services = True
            services_indent = indent
            continue
        if in_services:
            if not stripped:
                continue
            if indent <= services_indent:
                in_services = False
                continue
            if indent != services_indent + 2:
                continue
            m = re.match(r"^([a-zA-Z0-9_-]+):$", stripped)
            if m:
                name = m.group(1)
                service_name_counts[name] = service_name_counts.get(name, 0) + 1

    duplicates = [n for n, c in service_name_counts.items() if c > 1]
    if duplicates:
        for name in duplicates:
            result.error(
                f"Service '{name}' is defined more than once in compose.yml.",
                hint="Remove the duplicate service definition.",
            )
    else:
        result.ok("No duplicate service definitions in compose.yml.")

    if template_config is None or addon_configs is None or contributions is None:
        zenit_root = get_zenit_root()
        try:
            template_config = load_template_config(zenit_root, lockfile.template)
            available_meta = list_addons()
            addon_configs = [
                get_addon(m.id) for m in available_meta if m.id in lockfile.addons
            ]
            contributions = collect_all(template_config, addon_configs)
        except Exception:
            result.warn(
                "Could not load template/addon config to verify compose services.",
            )
            return result

    for svc in contributions.compose_services:
        if svc.name not in services:
            result.error(
                f"Expected compose service '{svc.name}' is missing from compose.yml.",
                hint="Re-run 'zenit add' for the addon that provides this service.",
            )
        else:
            result.ok(f"Compose service '{svc.name}' is present.")

    return result


def _check_env(
    project_dir: Path,
    lockfile: ZenitLockfile,
    template_config: TemplateConfig | None = None,
    contributions: Contributions | None = None,
) -> HealthResult:
    """Check that .env and .env.example contain expected env vars."""

    result = HealthResult("Env vars")

    if template_config is None or contributions is None:
        zenit_root = get_zenit_root()
        try:
            template_config = load_template_config(zenit_root, lockfile.template)
        except Exception:
            result.warn(
                f"Could not load template '{lockfile.template}' to verify env vars.",
            )
            return result
        available_meta = list_addons()
        addon_configs = [
            get_addon(m.id) for m in available_meta if m.id in lockfile.addons
        ]
        contributions = collect_all(template_config, addon_configs)
    expected_keys = [ev.key for ev in contributions.env_vars]

    if not expected_keys:
        result.ok("No env vars expected.")
        return result

    for file_name in ENV_FILES:
        env_path = project_dir / file_name
        if not env_path.exists():
            result.warn(
                f"'{file_name}' is missing.",
                hint=f"Expected env vars: {', '.join(expected_keys)}.",
            )
            continue

        text = env_path.read_text(encoding="utf-8")
        file_keys = {
            line.split("=", 1)[0].strip()
            for line in text.splitlines()
            if "=" in line and not line.strip().startswith("#")
        }
        for key in expected_keys:
            if key not in file_keys:
                result.error(
                    f"'{key}' is missing from '{file_name}'.",
                    hint=f"Add '{key}=<<value>' to '{file_name}'.",
                )
            else:
                result.ok(f"'{key}' is present in '{file_name}'.")

    return result


# ── Rendering ──────────────────────────────────────────────────────────────────


def print_results(results: list[HealthResult]) -> bool:
    """Print health check results. Returns True if any errors are present."""

    has_errors = False
    for result in results:
        if not result.issues:
            continue
        print(f"\n  {BOLD}{result.category}{RESET}")
        for issue in result.issues:
            if issue.severity == Severity.OK:
                symbol = f"{GREEN}✓{RESET}"
            elif issue.severity == Severity.WARN:
                symbol = f"{YELLOW}⚠{RESET}"
            else:
                symbol = f"{RED}✗{RESET}"
                has_errors = True
            print(f"    {symbol}  {issue.message}")
            if issue.hint:
                print(f"       {DIM}{issue.hint}{RESET}")

    return has_errors
