"""Merge contributions from the template and selected addons."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from zenit.core.constants import DEFAULT_DEV_DEPS
from zenit.core.manifest import dep_package_name
from zenit.schema.exceptions import ZenitError
from zenit.schema.models import (
    Contributions,
    FileContribution,
)

if TYPE_CHECKING:
    from zenit.schema.models import AddonConfig, TemplateConfig


def _merge_addon_contributions(
    c: Contributions, addon_configs: list[AddonConfig]
) -> None:
    """Merge contributions from all addon configs into *c*."""
    for addon in addon_configs:
        c.dirs.extend(addon.dirs)
        c.files.extend(addon.files)
        c.compose_services.extend(addon.compose_services)
        c.compose_volumes.extend(addon.compose_volumes)
        c.env_vars.extend(addon.env_vars)
        c.deps.extend(addon.deps)
        c.dev_deps.extend(addon.dev_deps)
        c.recipes.addon.extend(addon.just_recipes)
        for section, overrides in addon.tool_overrides.items():
            c.tool_overrides.setdefault(section, []).extend(overrides)
        c.ruff_excludes.extend(addon.ruff_excludes)
        for inj in addon.injections:
            cloned = replace(inj, addon_id=addon.id)
            c.injections.append(cloned)
    c._addon_configs = addon_configs


def _dedupe_deps(deps: list[str]) -> list[str]:
    """Drop duplicate package entries, keeping the first occurrence.

    The template and several addons commonly declare the same package
    (e.g. ``python-dotenv``). Without this the generated pyproject.toml
    lists it twice.
    """
    seen: set[str] = set()
    unique: list[str] = []
    for dep in deps:
        name = dep_package_name(dep)
        if name not in seen:
            seen.add(name)
            unique.append(dep)
    return unique


def _resolve_content(fc: FileContribution) -> str | None:
    """Return the literal content of a FileContribution.

    If *fc* has inline ``content``, return it directly.  If *fc* has a
    ``source`` path, read the file from disk.
    """
    if fc.content is not None:
        return fc.content
    if fc.source is not None:
        try:
            return Path(fc.source).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
    return None


def collect_all(
    template_config: TemplateConfig,
    addon_configs: list[AddonConfig],
) -> Contributions:
    """Merge contributions from the template and all selected addons."""
    c = Contributions()

    c.dirs.extend(template_config.dirs)
    c.files.extend(template_config.files)
    c.compose_services.extend(template_config.compose_services)
    c.compose_volumes.extend(template_config.compose_volumes)
    c.env_vars.extend(template_config.env_vars)
    c.deps.extend(template_config.deps)
    c.template_dev_deps.extend(template_config.dev_deps)
    if not template_config.dev_deps:
        c.template_dev_deps.extend(DEFAULT_DEV_DEPS)
    c.recipes.template.extend(template_config.just_recipes)

    for inj in template_config.injections:
        cloned = replace(inj, addon_id="template")
        c.injections.append(cloned)

    _merge_addon_contributions(c, addon_configs)

    c.deps = _dedupe_deps(c.deps)
    c.dev_deps = _dedupe_deps(c.dev_deps)
    c.template_dev_deps = _dedupe_deps(c.template_dev_deps)

    # ---- deduplicate file contributions ----
    seen: dict[str, tuple[str, FileContribution]] = {}
    all_labeled: list[tuple[str, FileContribution]] = [
        ("template", fc) for fc in template_config.files
    ] + [(addon.id, fc) for addon in addon_configs for fc in addon.files]
    for label, fc in all_labeled:
        dest = fc.dest
        if dest not in seen:
            seen[dest] = (label, fc)
            continue
        prev_label, prev_fc = seen[dest]
        # Two empty stubs (e.g. __init__.py) overlapping is fine.
        if fc.content == "" and prev_fc.content == "":
            continue
        # Identical source or content → no conflict.
        if fc.source is not None and fc.source == prev_fc.source:
            continue
        if fc.content is not None and fc.content == prev_fc.content:
            continue

        # Addon overrides template for the same destination.
        if prev_label == "template" and label != "template":
            seen[dest] = (label, fc)
            continue

        # One has source, the other has content - resolve and compare actual text.
        fc_content = _resolve_content(fc)
        prev_content = _resolve_content(prev_fc)
        if fc_content is None and prev_content is None:
            raise ZenitError(
                f"Conflict: both '{prev_label}' and '{label}' want to write '{dest}', "
                f"but neither source file can be read.\n"
                f"  '{prev_label}' source: {prev_fc.source or 'inline content'}\n"
                f"  '{label}' source: {fc.source or 'inline content'}\n"
                f"Fix: check that both source files exist and are readable."
            )
        if (
            fc_content is not None
            and prev_content is not None
            and fc_content == prev_content
        ):
            continue

        raise ZenitError(
            f"Conflict: both '{prev_label}' and '{label}' want to write '{dest}'.\n"
            f"  '{prev_label}' source: {prev_fc.source or 'inline content'}\n"
            f"  '{label}' source: {fc.source or 'inline content'}\n"
            f"Fix: remove or rename the conflicting file in one of the addons/templates."
        )

    return c


def collect_addon_only(addon_configs: list[AddonConfig]) -> Contributions:
    """Collect contributions from addons only, no template files/dirs/recipes.

    Used by ``add_addon`` so that adding an addon to an existing project never
    re-renders and overwrites files that the template already wrote at scaffold time.
    """
    c = Contributions()
    _merge_addon_contributions(c, addon_configs)
    c.deps = _dedupe_deps(c.deps)
    c.dev_deps = _dedupe_deps(c.dev_deps)
    return c
