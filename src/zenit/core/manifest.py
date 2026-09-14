"""Zenit manifest - .zenit.toml [manifest] section.

The manifest is the single source of truth for everything zenit has injected
into a project.  It is written at scaffold time and updated by every
``zenit add`` / ``zenit remove``.  ``zenit doctor`` reads it to verify
integrity.

Schema version
--------------
``MANIFEST_SCHEMA_VERSION = 2`` - bump this constant if the normalisation
algorithm or the manifest structure changes in a breaking way.  ``read()``
will warn (not error) when the stored version differs from the current one.

Normalisation contract
----------------------
``fingerprint_normalised`` is defined as SHA-256 of the string produced by:

    1. Parse the code with ``libcst.parse_module(code)``.
    2. Serialise back via ``.code`` (canonical libcst output).
    3. Strip trailing whitespace from every line.
    4. Collapse runs of 3+ consecutive newlines to exactly two newlines.

**Do not change this definition without bumping MANIFEST_SCHEMA_VERSION.**
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path
from typing import Any

import tomlkit
import tomlkit.items
from jinja2 import Environment

from zenit.core._filenames import LOCKFILE_NAME
from zenit.core.constants import RECIPE_NAME_RE
from zenit.schema.models import (
    AddonConfig,
    DependencyEntry,
    EntrySource,
    EnvEntry,
    LocatorSpec,
    Manifest,
    ManifestBlock,
    OwnedEntry,
    ToolOverrideEntry,
)

MANIFEST_SCHEMA_VERSION = 2


# ── Public API ────────────────────────────────────────────────────────────────


def read_manifest(project_dir: Path) -> Manifest:
    """Read the ``[manifest]`` section from *project_dir*/.zenit.toml.

    Returns an empty ``Manifest`` if the file is absent, cannot be parsed,
    or has no ``[manifest]`` section (e.g. a v1 project).
    """
    path = project_dir / LOCKFILE_NAME
    if not path.exists():
        return Manifest()

    try:
        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(
            f"Warning: could not parse '{path}': {exc}. "
            f"Manifest will be treated as empty. Run 'zenit doctor' to verify.",
            file=sys.stderr,
        )
        return Manifest()

    raw: Any = doc.get("manifest", {})
    if not isinstance(raw, dict):
        return Manifest()

    return _decode_manifest(raw)


def write_manifest(project_dir: Path, manifest: Manifest) -> None:
    """Write *manifest* into the ``[manifest]`` section of *project_dir*/.zenit.toml.

    This is a thin wrapper around :func:`write_zenit_toml` for cases where
    only the manifest section needs updating.  When both ``[project]`` and
    ``[manifest]`` need updating, prefer ``write_zenit_toml`` directly for a
    single atomic write.
    """
    from zenit.core.lockfile import write_zenit_toml

    write_zenit_toml(project_dir, manifest=manifest)


# ── Manifest mutation helpers ─────────────────────────────────────────────────


def _add_owned_entry(
    container: list[Any],
    key_attr: str,
    entry: Any,
    *,
    source: EntrySource,
    addon: str,
) -> bool:
    """Record *entry* in *container*, adopting TEMPLATE-sourced entries.

    If an entry with the same *key_attr* value already exists and has
    ``source == TEMPLATE``, it is adopted (source changed, addon set).
    Returns True if adoption occurred.

    If an ADDON-sourced entry already exists with the same key but a
    different addon, a warning is emitted and the original is kept.
    """
    existing = next(
        (e for e in container if getattr(e, key_attr) == getattr(entry, key_attr)),
        None,
    )
    if existing is not None:
        if existing.source == EntrySource.TEMPLATE:
            existing.source = source
            existing.addon = addon
            return True
        if existing.addon != addon:
            print(
                f"Warning: addon '{addon}' declares '{getattr(entry, key_attr)}' "
                f"which is already declared by addon '{existing.addon}'. "
                f"Keeping the existing entry.",
                file=sys.stderr,
            )
        return False
    entry.source = source
    entry.addon = addon
    container.append(entry)
    return False


def add_python_block(manifest: Manifest, block: ManifestBlock) -> None:
    manifest.python_blocks.append(block)


def resync_python_blocks(project_dir: Path, manifest: Manifest) -> int:
    """Re-sync stale line numbers and fingerprints in *manifest*.

    Later injections or removals in the same file shift lines, so recorded
    ``block.lines`` go stale within a single create/add/remove run. Re-run
    each block's stored locator to find where it actually is now and update
    ``block.lines`` plus both fingerprints from the content at that location.

    Only mutates in-memory tracking data, never touches project files and
    never writes the manifest. Callers persist via their normal manifest
    write. Returns the number of fields updated.
    """
    from zenit.core.handlers.python_handler import relocate_block

    fixed = 0
    for block in manifest.python_blocks:
        file_path = project_dir / block.file
        if not file_path.exists():
            continue

        try:
            actual = relocate_block(file_path, block)
        except Exception:
            continue

        if actual is None:
            continue

        actual_str = f"{actual[0]}-{actual[1]}"
        if actual_str != block.lines:
            block.lines = actual_str
            fixed += 1

        text = file_path.read_text(encoding="utf-8")
        all_lines = text.splitlines(keepends=True)
        start_idx = actual[0] - 1
        block_lines = all_lines[start_idx : actual[1]]
        content = "".join(block_lines)
        fp, fp_norm = fingerprint(content)
        if fp != block.fingerprint:
            block.fingerprint = fp
            fixed += 1
        if fp_norm != block.fingerprint_normalised:
            block.fingerprint_normalised = fp_norm
            fixed += 1

    return fixed


def remove_blocks_for_addon(manifest: Manifest, addon_id: str) -> None:
    """Remove all manifest entries that belong to *addon_id*."""
    manifest.python_blocks = [b for b in manifest.python_blocks if b.addon != addon_id]
    manifest.env = [e for e in manifest.env if e.addon != addon_id]
    manifest.compose_services = [
        s for s in manifest.compose_services if s.addon != addon_id
    ]
    manifest.compose_volumes = [
        v for v in manifest.compose_volumes if v.addon != addon_id
    ]
    manifest.compose_app_env = [
        e for e in manifest.compose_app_env if e.addon != addon_id
    ]
    manifest.compose_app_depends_on = [
        d for d in manifest.compose_app_depends_on if d.addon != addon_id
    ]
    manifest.dependencies = [d for d in manifest.dependencies if d.addon != addon_id]
    manifest.just_recipes = [r for r in manifest.just_recipes if r.addon != addon_id]
    manifest.ruff_excludes = [e for e in manifest.ruff_excludes if e.addon != addon_id]
    manifest.tool_overrides = [
        t for t in manifest.tool_overrides if t.addon != addon_id
    ]


def add_env_entry(
    manifest: Manifest, key: str, source: EntrySource, addon: str
) -> bool:
    """Record *key* in the manifest.

    Delegates to ``_add_owned_entry`` for adoption and dedup.
    """
    return _add_owned_entry(
        manifest.env,
        "key",
        EnvEntry(key=key, source=source, addon=addon),
        source=source,
        addon=addon,
    )


def add_compose_service(
    manifest: Manifest, name: str, source: EntrySource, addon: str
) -> bool:
    return _add_owned_entry(
        manifest.compose_services,
        "name",
        OwnedEntry(name=name, source=source, addon=addon),
        source=source,
        addon=addon,
    )


def add_compose_volume(
    manifest: Manifest, name: str, source: EntrySource, addon: str
) -> bool:
    return _add_owned_entry(
        manifest.compose_volumes,
        "name",
        OwnedEntry(name=name, source=source, addon=addon),
        source=source,
        addon=addon,
    )


def add_compose_app_env(
    manifest: Manifest, key: str, source: EntrySource, addon: str
) -> bool:
    return _add_owned_entry(
        manifest.compose_app_env,
        "key",
        EnvEntry(key=key, source=source, addon=addon),
        source=source,
        addon=addon,
    )


def add_compose_app_depends_on(
    manifest: Manifest, name: str, source: EntrySource, addon: str
) -> bool:
    return _add_owned_entry(
        manifest.compose_app_depends_on,
        "name",
        OwnedEntry(name=name, source=source, addon=addon),
        source=source,
        addon=addon,
    )


def add_dependency(
    manifest: Manifest,
    package: str,
    spec: str,
    source: EntrySource,
    addon: str,
    dev: bool,
) -> bool:
    return _add_owned_entry(
        manifest.dependencies,
        "package",
        DependencyEntry(
            package=package, spec=spec, source=source, addon=addon, dev=dev
        ),
        source=source,
        addon=addon,
    )


def add_just_recipe(
    manifest: Manifest, name: str, source: EntrySource, addon: str
) -> bool:
    return _add_owned_entry(
        manifest.just_recipes,
        "name",
        OwnedEntry(name=name, source=source, addon=addon),
        source=source,
        addon=addon,
    )


def add_ruff_exclude(
    manifest: Manifest, directory: str, source: EntrySource, addon: str
) -> bool:
    return _add_owned_entry(
        manifest.ruff_excludes,
        "name",
        OwnedEntry(name=directory, source=source, addon=addon),
        source=source,
        addon=addon,
    )


def add_tool_override(
    manifest: Manifest,
    section: str,
    module: str,
    source: EntrySource,
    addon: str,
) -> bool:
    existing = {t.module for t in manifest.tool_overrides if t.addon == addon}
    if module in existing:
        return False
    manifest.tool_overrides.append(
        ToolOverrideEntry(section=section, module=module, source=source, addon=addon)
    )
    return True


def dep_package_name(dep: str) -> str:
    match = re.match(r"^([a-zA-Z0-9_.-]+)", dep)
    return match.group(1).lower().replace("-", "_") if match else dep.lower()


def record_addon_manifest_entries(
    manifest: Manifest,
    addon_cfg: AddonConfig,
    string_env: Environment,
    render_vars: dict[str, object],
) -> list[str]:
    """Record manifest entries for *addon_cfg*.

    When an entry already exists with ``source == TEMPLATE``, it is adopted
    (source changed to ADDON, addon set to the addon's id).

    Returns a list of human-readable adoption descriptions (e.g.
    ``"env:REDIS_URL"``) that callers can display to the user.
    """
    adopted: list[str] = []
    addon_id = addon_cfg.id
    for ev in addon_cfg.env_vars:
        if add_env_entry(manifest, ev.key, source=EntrySource.ADDON, addon=addon_id):
            adopted.append(f"env:{ev.key}")
    for svc in addon_cfg.compose_services:
        if add_compose_service(
            manifest, svc.name, source=EntrySource.ADDON, addon=addon_id
        ):
            adopted.append(f"compose_service:{svc.name}")
    for vol in addon_cfg.compose_volumes:
        if add_compose_volume(manifest, vol, source=EntrySource.ADDON, addon=addon_id):
            adopted.append(f"compose_volume:{vol}")
    for key in addon_cfg.compose_app_env:
        if add_compose_app_env(manifest, key, source=EntrySource.ADDON, addon=addon_id):
            adopted.append(f"compose_app_env:{key}")
    for name in addon_cfg.compose_app_depends_on:
        if add_compose_app_depends_on(
            manifest, name, source=EntrySource.ADDON, addon=addon_id
        ):
            adopted.append(f"compose_app_depends_on:{name}")
    for dep in addon_cfg.deps:
        pkg = dep_package_name(dep)
        if add_dependency(
            manifest,
            pkg,
            dep,
            source=EntrySource.ADDON,
            addon=addon_id,
            dev=False,
        ):
            adopted.append(f"dependency:{pkg}")
    for dep in addon_cfg.dev_deps:
        pkg = dep_package_name(dep)
        if add_dependency(
            manifest,
            pkg,
            dep,
            source=EntrySource.ADDON,
            addon=addon_id,
            dev=True,
        ):
            adopted.append(f"dev_dependency:{pkg}")
    for recipe_raw in addon_cfg.just_recipes:
        rendered = string_env.from_string(recipe_raw).render(**render_vars)
        m = RECIPE_NAME_RE.search(rendered)
        if m and add_just_recipe(
            manifest, m.group(1), source=EntrySource.ADDON, addon=addon_id
        ):
            adopted.append(f"just_recipe:{m.group(1)}")
    for exc in addon_cfg.ruff_excludes:
        if add_ruff_exclude(manifest, exc, source=EntrySource.ADDON, addon=addon_id):
            adopted.append(f"ruff_exclude:{exc}")
    for section, overrides in addon_cfg.tool_overrides.items():
        for override in overrides:
            module_list = override.get("module", [])
            if not isinstance(module_list, list):
                continue
            for mod in module_list:
                if not isinstance(mod, str):
                    continue
                if add_tool_override(
                    manifest, section, mod, source=EntrySource.ADDON, addon=addon_id
                ):
                    adopted.append(f"tool_override:{section}:{mod}")
    return adopted


# ── Fingerprinting ────────────────────────────────────────────────────────────


def _try_canonicalise_fragment(code: str) -> str | None:
    """Attempt to produce canonical libcst output for a code fragment.

    Tries three approaches in order:
    1. Parse ``code`` directly as a full module.
    2. Wrap as a class body (``class _Stub:\n    <code>``).
    3. Wrap as a function body (``def _stub():\n    <code>``).

    Returns canonical code on success, ``None`` if all attempts fail.
    """
    import libcst

    for wrapped in (
        code,
        f"class _Stub:\n    {code}",
        f"def _stub():\n    {code}",
    ):
        try:
            module = libcst.parse_module(wrapped)
            return module.code
        except Exception:
            continue
    return None


def fingerprint(code: str) -> tuple[str, str]:
    """Return ``(fingerprint, fingerprint_normalised)`` for *code*.

    Both values are ``"sha256:<hex>"``.

    See module docstring for the exact normalisation contract.
    Do NOT change ``_normalise`` without bumping ``MANIFEST_SCHEMA_VERSION``.

    If *code* is not a valid Python module (e.g. a class-body fragment such as
    a single annotated attribute), the function tries to wrap the fragment in a
    valid Python construct before falling back to raw-text hashing.  The
    fallback means Stage A and B removal may not match, and removal falls
    through to Stage C (fuzzy match).
    """
    canonical = _try_canonicalise_fragment(code)
    if canonical is None:
        canonical = code
    raw_hash = hashlib.sha256(canonical.encode()).hexdigest()
    norm_hash = hashlib.sha256(_normalise(canonical).encode()).hexdigest()
    return f"sha256:{raw_hash}", f"sha256:{norm_hash}"


def normalised_fingerprint_of(code: str) -> str:
    """Return only the normalised fingerprint for *code*."""
    return fingerprint(code)[1]


def _normalise(code: str) -> str:
    """Canonical normalisation for formatter-resilient fingerprinting.

    Definition (frozen - do not change without bumping MANIFEST_SCHEMA_VERSION):
      1. libcst round-trip  →  canonical serialisation.
      2. Strip trailing whitespace from every line.
      3. Collapse runs of 3+ consecutive newlines to exactly two.
    """
    lines = [line.rstrip() for line in code.splitlines()]
    joined = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", joined)


# ── TOML encode / decode ──────────────────────────────────────────────────────


def _encode_section(
    tbl: tomlkit.items.Table,
    items: list[Any],
    section_name: str,
    field_map: list[tuple[str, str]],
) -> None:
    if not items:
        return
    arr = tomlkit.aot()
    for item in items:
        entry = tomlkit.table()
        for toml_key, attr_name in field_map:
            entry.add(toml_key, getattr(item, attr_name))
        arr.append(entry)
    tbl.add(section_name, arr)


def encode_manifest(m: Manifest) -> tomlkit.items.Table:
    tbl = tomlkit.table()

    if m.python_blocks:
        arr = tomlkit.aot()
        for b in m.python_blocks:
            item = tomlkit.table()
            item.add("addon", b.addon)
            item.add("point", b.point)
            item.add("file", b.file)
            item.add("lines", b.lines)
            item.add("fingerprint", b.fingerprint)
            item.add("fingerprint_normalised", b.fingerprint_normalised)
            item.add("source", b.source.value)
            loc = tomlkit.table()
            loc.add("name", b.locator.name)
            loc.add("args", b.locator.args)
            item.add("locator", loc)
            arr.append(item)
        tbl.add("python_blocks", arr)

    _encode_section(
        tbl, m.env, "env", [("key", "key"), ("source", "source"), ("addon", "addon")]
    )
    _encode_section(
        tbl,
        m.compose_services,
        "compose_services",
        [("name", "name"), ("source", "source"), ("addon", "addon")],
    )
    _encode_section(
        tbl,
        m.compose_volumes,
        "compose_volumes",
        [("name", "name"), ("source", "source"), ("addon", "addon")],
    )
    _encode_section(
        tbl,
        m.compose_app_env,
        "compose_app_env",
        [("key", "key"), ("source", "source"), ("addon", "addon")],
    )
    _encode_section(
        tbl,
        m.compose_app_depends_on,
        "compose_app_depends_on",
        [("name", "name"), ("source", "source"), ("addon", "addon")],
    )
    _encode_section(
        tbl,
        m.dependencies,
        "dependencies",
        [
            ("package", "package"),
            ("spec", "spec"),
            ("source", "source"),
            ("addon", "addon"),
            ("dev", "dev"),
        ],
    )
    _encode_section(
        tbl,
        m.just_recipes,
        "just_recipes",
        [("name", "name"), ("source", "source"), ("addon", "addon")],
    )
    _encode_section(
        tbl,
        m.ruff_excludes,
        "ruff_excludes",
        [("name", "name"), ("source", "source"), ("addon", "addon")],
    )
    _encode_section(
        tbl,
        m.tool_overrides,
        "tool_overrides",
        [
            ("section", "section"),
            ("module", "module"),
            ("source", "source"),
            ("addon", "addon"),
        ],
    )

    return tbl


def _parse_source(raw: str) -> EntrySource:
    """Parse a TOML source field into an EntrySource, defaulting to TEMPLATE."""
    try:
        return EntrySource(raw)
    except ValueError:
        return EntrySource.TEMPLATE


type _FieldMapEntry = tuple[
    str, str, Any, Any
]  # toml_key, attr_name, default, transform


def _decode_section(
    raw: dict[str, Any],
    section_name: str,
    model_cls: type[Any],
    field_map: list[_FieldMapEntry],
) -> list[Any]:
    result: list[Any] = []
    for item in raw.get(section_name, []):
        kwargs: dict[str, Any] = {}
        for toml_key, attr_name, default, transform in field_map:
            val = item.get(toml_key, default)
            if transform is not None:
                val = transform(val)
            kwargs[attr_name] = val
        result.append(model_cls(**kwargs))
    return result


def _decode_manifest(raw: dict[str, Any]) -> Manifest:
    m = Manifest()

    for b in raw.get("python_blocks", []):
        loc = b.get("locator", {})
        m.python_blocks.append(
            ManifestBlock(
                addon=b.get("addon", ""),
                point=b.get("point", ""),
                file=b.get("file", ""),
                lines=b.get("lines", ""),
                fingerprint=b.get("fingerprint", ""),
                fingerprint_normalised=b.get("fingerprint_normalised", ""),
                locator=LocatorSpec(
                    name=loc.get("name", ""),
                    args=dict(loc.get("args", {})),
                ),
                source=_parse_source(b.get("source", EntrySource.ADDON.value)),
            )
        )

    m.env = _decode_section(
        raw,
        "env",
        EnvEntry,
        [
            ("key", "key", "", None),
            ("source", "source", "", _parse_source),
            ("addon", "addon", "", None),
        ],
    )
    m.compose_services = _decode_section(
        raw,
        "compose_services",
        OwnedEntry,
        [
            ("name", "name", "", None),
            ("source", "source", "", _parse_source),
            ("addon", "addon", "", None),
        ],
    )
    m.compose_volumes = _decode_section(
        raw,
        "compose_volumes",
        OwnedEntry,
        [
            ("name", "name", "", None),
            ("source", "source", "", _parse_source),
            ("addon", "addon", "", None),
        ],
    )
    m.compose_app_env = _decode_section(
        raw,
        "compose_app_env",
        EnvEntry,
        [
            ("key", "key", "", None),
            ("source", "source", "", _parse_source),
            ("addon", "addon", "", None),
        ],
    )
    m.compose_app_depends_on = _decode_section(
        raw,
        "compose_app_depends_on",
        OwnedEntry,
        [
            ("name", "name", "", None),
            ("source", "source", "", _parse_source),
            ("addon", "addon", "", None),
        ],
    )
    m.dependencies = _decode_section(
        raw,
        "dependencies",
        DependencyEntry,
        [
            ("package", "package", "", None),
            ("spec", "spec", "", None),
            ("source", "source", "", _parse_source),
            ("addon", "addon", "", None),
            ("dev", "dev", False, bool),
        ],
    )
    m.just_recipes = _decode_section(
        raw,
        "just_recipes",
        OwnedEntry,
        [
            ("name", "name", "", None),
            ("source", "source", "", _parse_source),
            ("addon", "addon", "", None),
        ],
    )
    m.ruff_excludes = _decode_section(
        raw,
        "ruff_excludes",
        OwnedEntry,
        [
            ("name", "name", "", None),
            ("source", "source", "", _parse_source),
            ("addon", "addon", "", None),
        ],
    )
    m.tool_overrides = _decode_section(
        raw,
        "tool_overrides",
        ToolOverrideEntry,
        [
            ("section", "section", "", None),
            ("module", "module", "", None),
            ("source", "source", "", _parse_source),
            ("addon", "addon", "", None),
        ],
    )

    return m
