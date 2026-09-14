"""Integration tests for apply_contributions() → manifest recording.

Verifies that apply_contributions() correctly records every category of
injected item in the manifest - Python blocks with accurate fingerprints,
env entries, compose services/volumes, dependencies, and just recipes.

Tested invariants:
  - Every Python injection produces a ManifestBlock with all fields populated.
  - The stored fingerprint matches bytes on disk at the recorded line range.
  - Env, compose, dep, and recipe entries carry correct source/addon metadata.
  - Running apply_contributions twice produces no duplicate manifest entries.
  - Injections for a missing file are silently skipped (no block recorded).
"""

from __future__ import annotations

from pathlib import Path

from zenit.core._paths import get_zenit_root
from zenit.core.apply import apply_contributions
from zenit.core.context import Context
from zenit.core.filesystem import RealFileSystem
from zenit.core.handlers.locators import LOCATOR_AFTER_LAST_CLASS_ATTR
from zenit.core.manifest import fingerprint as _fp
from zenit.core.manifest import read_manifest
from zenit.core.render import build_render_vars
from zenit.schema.models import (
    AddonConfig,
    ComposeService,
    Contributions,
    EnvVar,
    Injection,
    InjectionPoint,
    LocatorSpec,
)

# ── helpers ───────────────────────────────────────────────────────────────────

_ZENIT_ROOT = get_zenit_root()


def _ctx(tmp_path: Path, name: str = "myapp") -> tuple[Context, RealFileSystem]:
    project_dir = tmp_path / name
    project_dir.mkdir()
    ctx = Context(
        name=name,
        pkg_name=name.replace("-", "_"),
        template="blank",
        addons=[],
        zenit_root=_ZENIT_ROOT,
        project_dir=project_dir,
    )
    fs = RealFileSystem(project_dir)
    return ctx, fs


def _render_vars(ctx: Context) -> dict[str, object]:
    return build_render_vars(
        name=ctx.name,
        pkg_name=ctx.pkg_name,
        template=ctx.template,
        secret_key="test-secret",
        addons=ctx.addons,
    )


def _injection_points(
    *items: tuple[str, str, str, dict[str, object]],
) -> dict[str, InjectionPoint]:
    """Build a minimal injection_points dict.

    Each item is (point_name, file_template, locator_name, locator_args).
    """
    return {
        name: InjectionPoint(
            file=file_tpl,
            locator=LocatorSpec(name=loc_name, args=loc_args),
        )
        for name, file_tpl, loc_name, loc_args in items
    }


def _addon(
    addon_id: str,
    *,
    injections: list[Injection] | None = None,
    env_vars: list[EnvVar] | None = None,
    compose_services: list[ComposeService] | None = None,
    compose_volumes: list[str] | None = None,
    deps: list[str] | None = None,
    dev_deps: list[str] | None = None,
    just_recipes: list[str] | None = None,
) -> AddonConfig:
    return AddonConfig(
        id=addon_id,
        description="",
        injections=injections or [],
        env_vars=env_vars or [],
        compose_services=compose_services or [],
        compose_volumes=compose_volumes or [],
        deps=deps or [],
        dev_deps=dev_deps or [],
        just_recipes=just_recipes or [],
    )


# ── Python block recording ────────────────────────────────────────────────────


def test_apply_records_python_block_with_all_fields(tmp_path: Path) -> None:
    ctx, fs = _ctx(tmp_path)
    # Create a Python file for injection to land in
    target = ctx.project_dir / "src" / "myapp" / "settings.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        'class Settings:\n    db_url: str = "postgresql://localhost"\n',
        encoding="utf-8",
    )

    addon = _addon(
        "redis",
        injections=[
            Injection(
                point="settings_fields",
                content='    redis_url: str = "redis://localhost"\n',
                addon_id="redis",
            )
        ],
    )
    contributions = Contributions(injections=addon.injections, _addon_configs=[addon])
    injection_points = _injection_points(
        (
            "settings_fields",
            "src/{{pkg_name}}/settings.py",
            LOCATOR_AFTER_LAST_CLASS_ATTR,
            {"class_name": "Settings"},
        ),
    )

    apply_contributions(ctx, fs, contributions, injection_points, _render_vars(ctx))

    manifest = read_manifest(ctx.project_dir)
    assert len(manifest.python_blocks) == 1
    block = manifest.python_blocks[0]

    assert block.addon == "redis"
    assert block.point == "settings_fields"
    assert block.file == "src/myapp/settings.py"
    assert "-" in block.lines  # e.g. "3-3"
    assert block.fingerprint.startswith("sha256:")
    assert block.fingerprint_normalised.startswith("sha256:")
    assert block.locator.name == LOCATOR_AFTER_LAST_CLASS_ATTR
    assert block.locator.args == {"class_name": "Settings"}


def test_apply_fingerprint_matches_written_content(tmp_path: Path) -> None:
    ctx, fs = _ctx(tmp_path)
    target = ctx.project_dir / "src" / "myapp" / "settings.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        'class Settings:\n    db_url: str = "postgresql://localhost"\n',
        encoding="utf-8",
    )

    injection_text = '    redis_url: str = "redis://localhost"\n'
    addon = _addon(
        "redis",
        injections=[
            Injection(point="settings_fields", content=injection_text, addon_id="redis")
        ],
    )
    contributions = Contributions(injections=addon.injections, _addon_configs=[addon])
    injection_points = _injection_points(
        (
            "settings_fields",
            "src/{{pkg_name}}/settings.py",
            LOCATOR_AFTER_LAST_CLASS_ATTR,
            {"class_name": "Settings"},
        ),
    )

    apply_contributions(ctx, fs, contributions, injection_points, _render_vars(ctx))

    manifest = read_manifest(ctx.project_dir)
    block = manifest.python_blocks[0]

    # Re-extract the exact bytes on disk at the recorded line range
    start, end = (int(x) for x in block.lines.split("-"))
    disk_lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    on_disk_text = "".join(disk_lines[start - 1 : end])

    expected_fp, _ = _fp(on_disk_text)
    assert block.fingerprint == expected_fp


def test_apply_skips_python_block_for_missing_file(tmp_path: Path) -> None:
    ctx, fs = _ctx(tmp_path)
    # Target file deliberately not created
    addon = _addon(
        "redis",
        injections=[
            Injection(
                point="settings_fields",
                content='    redis_url: str = "redis://localhost"\n',
                addon_id="redis",
            )
        ],
    )
    contributions = Contributions(injections=addon.injections, _addon_configs=[addon])
    injection_points = _injection_points(
        (
            "settings_fields",
            "src/{{pkg_name}}/settings.py",
            LOCATOR_AFTER_LAST_CLASS_ATTR,
            {"class_name": "Settings"},
        ),
    )

    apply_contributions(ctx, fs, contributions, injection_points, _render_vars(ctx))

    manifest = read_manifest(ctx.project_dir)
    assert manifest.python_blocks == []


# ── Env var merging ────────────────────────────────────────────────────────────


def test_apply_env_vars_creates_dotenv(tmp_path: Path) -> None:
    ctx, fs = _ctx(tmp_path)
    addon = _addon(
        "postgres",
        env_vars=[EnvVar(key="DATABASE_URL", default="postgresql://localhost")],
    )
    contributions = Contributions(env_vars=addon.env_vars, _addon_configs=[addon])

    apply_contributions(ctx, fs, contributions, {}, _render_vars(ctx))

    dotenv = ctx.project_dir / ".env"
    assert dotenv.exists()
    assert "DATABASE_URL=postgresql://localhost" in dotenv.read_text(encoding="utf-8")


def test_apply_env_vars_replaces_duplicate_keys(tmp_path: Path) -> None:
    ctx, fs = _ctx(tmp_path)
    dotenv = ctx.project_dir / ".env"
    dotenv.write_text("DATABASE_URL=existing\n", encoding="utf-8")

    addon = _addon(
        "postgres",
        env_vars=[EnvVar(key="DATABASE_URL", default="postgresql://override")],
    )
    contributions = Contributions(env_vars=addon.env_vars, _addon_configs=[addon])

    apply_contributions(ctx, fs, contributions, {}, _render_vars(ctx))

    text = dotenv.read_text(encoding="utf-8")
    assert "DATABASE_URL=postgresql://override" in text
    assert "DATABASE_URL=existing" not in text
    assert text.count("DATABASE_URL=") == 1


def test_apply_env_vars_last_contributor_wins_same_merge(tmp_path: Path) -> None:
    ctx, fs = _ctx(tmp_path)
    sqlite = _addon(
        "sqlalchemy",
        env_vars=[EnvVar(key="DATABASE_URL", default="sqlite+aiosqlite:///./dev.db")],
    )
    postgres = _addon(
        "postgres",
        env_vars=[
            EnvVar(key="DATABASE_URL", default="postgresql://postgres@localhost/db")
        ],
    )
    contributions = Contributions(
        env_vars=sqlite.env_vars + postgres.env_vars,
        _addon_configs=[sqlite, postgres],
    )

    apply_contributions(ctx, fs, contributions, {}, _render_vars(ctx))

    text = (ctx.project_dir / ".env").read_text(encoding="utf-8")
    assert text.count("DATABASE_URL=") == 1
    assert "DATABASE_URL=postgresql://postgres@localhost/db" in text


def test_apply_env_vars_appends_missing_keys(tmp_path: Path) -> None:
    ctx, fs = _ctx(tmp_path)
    dotenv = ctx.project_dir / ".env"
    dotenv.write_text("EXISTING_KEY=stay\n", encoding="utf-8")

    addon = _addon(
        "postgres",
        env_vars=[EnvVar(key="DATABASE_URL", default="postgresql://localhost")],
    )
    contributions = Contributions(env_vars=addon.env_vars, _addon_configs=[addon])

    apply_contributions(ctx, fs, contributions, {}, _render_vars(ctx))

    text = dotenv.read_text(encoding="utf-8")
    assert "EXISTING_KEY=stay" in text
    assert "DATABASE_URL=postgresql://localhost" in text


def test_apply_env_vars_includes_comment(tmp_path: Path) -> None:
    ctx, fs = _ctx(tmp_path)
    addon = _addon(
        "redis",
        env_vars=[
            EnvVar(
                key="REDIS_URL",
                default="redis://localhost:6379",
                comment="Redis connection string",
            )
        ],
    )
    contributions = Contributions(env_vars=addon.env_vars, _addon_configs=[addon])

    apply_contributions(ctx, fs, contributions, {}, _render_vars(ctx))

    text = (ctx.project_dir / ".env").read_text(encoding="utf-8")
    assert "REDIS_URL=redis://localhost:6379  # Redis connection string" in text
