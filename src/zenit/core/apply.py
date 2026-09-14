"""Apply collected contributions to the project directory."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import jinja2

from zenit.cli.ui import warn as _warn
from zenit.core._filenames import COMPOSE_FILE, ENV_FILES
from zenit.core.filesystem import FileSystem
from zenit.core.handlers.base import HandlerDispatcher
from zenit.core.lockfile import ZenitLockfile, read_lockfile
from zenit.core.manifest import (
    add_python_block,
    fingerprint,
    read_manifest,
    resync_python_blocks,
    write_manifest,
)
from zenit.core.pkg_name import (
    _validate_no_path_traversal,
    resolve_compose_placeholders,
    resolve_dest_placeholder,
)
from zenit.core.render import make_env
from zenit.core.yaml_utils import compose_yaml_dumps, compose_yaml_load
from zenit.schema.exceptions import ZenitError
from zenit.schema.models import LocatorSpec, Manifest, ManifestBlock

if TYPE_CHECKING:
    from zenit.core.context import Context
    from zenit.schema.models import (
        ComposeService,
        Contributions,
        EnvVar,
        InjectionPoint,
    )


def apply_contributions(
    ctx: Context,
    fs: FileSystem,
    contributions: Contributions,
    injection_points: dict[str, InjectionPoint],
    render_vars: dict[str, object],
    *,
    manifest: Manifest | None = None,
    lockfile: ZenitLockfile | None = None,
) -> None:
    """Modify the generated project directory in-place according to *contributions*.
    Steps (in order):

    1. Create directories.
    2. Write / copy / render individual files.
    3. Apply structural injections (via HandlerDispatcher) and record in manifest.
    4. Merge compose services and volumes into ``compose.yml`` (if present).
    5. Append env vars to ``.env`` and ``.env.example`` (if present).
    6. Record per-addon manifest entries (env, compose, deps, recipes).
    7. Run each addon's optional ``post_apply`` hook.
    8. Write updated manifest to .zenit.toml.

    Template-owned entries (env vars, compose services/volumes, deps, recipes)
    are recorded separately by ``_stamp_template_manifest`` in scaffold.py and
    are NOT recorded here - ``contributions`` may contain template items merged
    in from ``collect_all``, but ownership of those belongs to the template, not
    to any addon.
    """
    project_dir = ctx.project_dir
    pkg_name = str(render_vars["pkg_name"])

    for d in contributions.dirs:
        dest = resolve_dest_placeholder(d, pkg_name)
        _validate_no_path_traversal(dest, project_dir)
        fs.create_dir(dest)

    # Pre-render {{pkg_name}} placeholders in compose service fields
    resolve_compose_placeholders(contributions.compose_services, pkg_name)

    string_env = make_env()
    file_envs: dict[Path, jinja2.Environment] = {}

    if lockfile is None:
        lockfile = read_lockfile(project_dir)

    for fc in contributions.files:
        dest = resolve_dest_placeholder(fc.dest, pkg_name)
        _validate_no_path_traversal(dest, project_dir)

        if lockfile is not None and dest in lockfile.template_file_paths:
            _warn(
                f"'{dest}' was written by the Copier template. "
                f"Overwriting with addon-provided content."
            )

        if fc.content is not None:
            if fc.template:
                rendered = string_env.from_string(fc.content).render(**render_vars)
                fs.write_file(dest, rendered)
            else:
                fs.write_file(dest, fc.content)
        elif fc.source is not None:
            src_path = Path(fc.source)
            if not src_path.is_absolute():
                raise ZenitError(
                    f"Internal error: the source path for '{fc.dest}' is relative ('{fc.source}'). "
                    f"FileContribution.source must be an absolute path. "
                    f"This is a bug in the template or addon - please report it."
                )
            if fc.template:
                loader_dir = src_path.parent
                if loader_dir not in file_envs:
                    file_envs[loader_dir] = make_env(loader_dir)
                content = (
                    file_envs[loader_dir]
                    .get_template(src_path.name)
                    .render(**render_vars)
                )
                fs.write_file(dest, content)
            else:
                fs.copy_file(src_path, dest)

    manifest_external = manifest is not None
    if manifest is None:
        manifest = read_manifest(project_dir)
    dispatcher = HandlerDispatcher()

    # Sort injections by point + content so that multiple addons
    # injecting at the same point produce deterministic, reproducible
    # output regardless of addon discovery order.
    sorted_injections = sorted(
        contributions.injections,
        key=lambda inj: (inj.point, inj.content),
    )

    prev_point: str | None = None

    for inj in sorted_injections:
        point = injection_points.get(inj.point)
        if point is None:
            continue

        if inj.templates and ctx.template not in inj.templates:
            continue

        resolved_file = resolve_dest_placeholder(point.file, pkg_name)
        _validate_no_path_traversal(resolved_file, project_dir)
        file_path = project_dir / resolved_file

        if not file_path.exists():
            _warn(
                f"Injection point '{inj.point}' targets "
                f"non-existent file '{resolved_file}' - skipping."
            )
            continue

        content = inj.content
        # Strip the leading \n from non-first injections at the same point
        # so that multiple related imports (e.g. lifespan_imports) end up
        # in the same import block instead of being separated by a blank line.
        if inj.point == prev_point and content.startswith("\n"):
            content = content[1:]
        prev_point = inj.point

        rendered_content = string_env.from_string(content).render(**render_vars)

        # .env files are handled by _merge_env_vars below (contributions.env_vars).
        # Do NOT route them through the dispatcher to avoid dual env-var paths.
        if file_path.name.startswith(".env"):
            continue

        _, start_line, end_line = dispatcher.apply(
            file_path,
            rendered_content,
            point.locator.name,
            dict(point.locator.args),
        )

        # Only Python files get fingerprint-tracked ManifestBlocks.
        if file_path.suffix == ".py":
            # Read back the written file so the fingerprint covers the actual
            # bytes on disk (libcst may normalise whitespace during round-trip).
            fresh_lines = file_path.read_text(encoding="utf-8").splitlines(
                keepends=True
            )
            block_text = "".join(fresh_lines[start_line - 1 : end_line])
            fp, fp_norm = fingerprint(block_text)
            block = ManifestBlock(
                addon=inj.addon_id,
                point=inj.point,
                file=resolved_file,
                lines=f"{start_line}-{end_line}",
                fingerprint=fp,
                fingerprint_normalised=fp_norm,
                locator=LocatorSpec(
                    name=point.locator.name,
                    args=dict(point.locator.args),
                ),
            )
            add_python_block(manifest, block)

    if contributions.compose_services and (project_dir / COMPOSE_FILE).exists():
        merge_compose(
            ctx, fs, contributions.compose_services, contributions.compose_volumes
        )

    for file_name in ENV_FILES:
        if contributions.env_vars:
            _merge_env_vars(
                ctx, fs, file_name, contributions.env_vars, string_env, render_vars
            )

    for addon_cfg in contributions._addon_configs:
        hooks = addon_cfg._module
        if hooks is not None and hooks.post_apply is not None:
            hooks.post_apply(ctx, fs)

    # Later injections shift lines recorded by earlier ones in the same
    # file. Re-sync tracking data now so the manifest is clean without
    # needing `doctor --fix`. Skip on dry run: nothing was written.
    if not ctx.dry_run:
        resync_python_blocks(project_dir, manifest)

    if not manifest_external and not ctx.dry_run:
        write_manifest(project_dir, manifest)


# ── Internal helpers ──────────────────────────────────────────────────────────


def merge_compose_into_data(
    data: dict[str, Any],
    services: list[ComposeService],
    volumes: list[str],
    replace: bool = False,
) -> None:
    """Merge *services* and *volumes* into *data* dict in-place.

    When *replace* is True, remove any service or volume **not** in the
    provided lists before adding missing ones (full reconciliation).
    """
    if replace:
        svc_names = {s.name for s in services}
        data["services"] = {
            k: v for k, v in data.get("services", {}).items() if k in svc_names
        }
        vol_set = set(volumes)
        data["volumes"] = {
            k: v for k, v in data.get("volumes", {}).items() if k in vol_set
        }

    existing: dict[str, Any] = data.setdefault("services", {})
    for svc in services:
        if svc.name in existing:
            continue
        block: dict[str, Any] = {}
        if svc.image:
            block["image"] = svc.image
        if svc.build:
            block["build"] = svc.build
        if svc.ports:
            block["ports"] = svc.ports
        if svc.volumes:
            block["volumes"] = svc.volumes
        if svc.environment:
            block["environment"] = svc.environment
        if svc.env_file:
            block["env_file"] = svc.env_file
        if svc.command:
            block["command"] = svc.command
        if svc.depends_on:
            block["depends_on"] = svc.depends_on
        if svc.develop_watch:
            develop = block.setdefault("develop", {})
            if isinstance(develop, dict):
                develop["watch"] = svc.develop_watch
        if svc.healthcheck:
            block["healthcheck"] = svc.healthcheck
        existing[svc.name] = block

    vols_section: dict[str, Any] = data.setdefault("volumes", {})
    for vol_name in volumes:
        if vol_name not in vols_section:
            vols_section[vol_name] = None


def merge_compose(
    ctx: Context,
    fs: FileSystem,
    services: list[ComposeService],
    volumes: list[str],
    replace: bool = False,
) -> None:
    """Add *services* and *volumes* to ``compose.yml``, skipping duplicates.

    When *replace* is True, remove any service or volume **not** in the
    provided lists before adding missing ones (full reconciliation).
    """
    compose_path = ctx.project_dir / COMPOSE_FILE
    data: dict[str, Any] = (
        compose_yaml_load(compose_path.read_text(encoding="utf-8")) or {}
    )
    merge_compose_into_data(data, services, volumes, replace=replace)
    fs.write_file(COMPOSE_FILE, compose_yaml_dumps(data))


def _merge_env_vars(
    ctx: Context,
    fs: FileSystem,
    file_name: str,
    env_vars: list[EnvVar],
    render_env: jinja2.Environment | None = None,
    render_vars: dict[str, object] | None = None,
) -> None:
    """Merge env vars into *file_name* (creating it if needed).

    Keys already present are replaced in place, later contributors win.
    Addon order is the CLI order, so ``-a sqlalchemy,postgres`` ends with
    the postgres ``DATABASE_URL``. New keys are appended.

    When *render_env* and *render_vars* are provided, env var default values
    are rendered through Jinja2 before being written.  This resolves template
    variables such as ``(( pkg_name ))`` in DATABASE_URL.
    """
    env_path = ctx.project_dir / file_name
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""

    lines = text.splitlines(keepends=True)
    key_to_index = {
        line.split("=", 1)[0].strip(): i
        for i, line in enumerate(lines)
        if "=" in line and not line.strip().startswith("#")
    }

    changed = False
    for v in env_vars:
        default = v.default
        if render_env is not None and render_vars is not None:
            default = render_env.from_string(default).render(**render_vars)
        line = f"{v.key}={default}"
        if v.comment:
            line += f"  # {v.comment}"
        line += "\n"
        if v.key in key_to_index:
            if lines[key_to_index[v.key]] != line:
                lines[key_to_index[v.key]] = line
                changed = True
        else:
            key_to_index[v.key] = len(lines)
            lines.append(line)
            changed = True

    if changed:
        fs.write_file(file_name, "".join(lines))
