from pathlib import Path

from zenit.schema.models import (
    AddonConfig,
    EnvVar,
    FileContribution,
    Injection,
)

_HERE = Path(__file__).parent.absolute()

config = AddonConfig(
    id="sqlalchemy",
    description="SQLAlchemy ORM + Alembic migrations",
    requires=[],
    dirs=[
        "src/{{pkg_name}}/db",
        "alembic/versions",
    ],
    files=[
        FileContribution(
            dest="src/{{pkg_name}}/db/__init__.py",
            content="",
        ),
        FileContribution(
            dest="src/{{pkg_name}}/db/base.py",
            source=str(_HERE / "files" / "src" / "{{pkg_name}}" / "db" / "base.py"),
        ),
        FileContribution(
            dest="src/{{pkg_name}}/db/session.py",
            source=str(
                _HERE / "files" / "src" / "{{pkg_name}}" / "db" / "session.py.j2"
            ),
            template=True,
        ),
        FileContribution(
            dest="src/{{pkg_name}}/models/mixins.py",
            source=str(
                _HERE / "files" / "src" / "{{pkg_name}}" / "models" / "mixins.py"
            ),
        ),
        FileContribution(
            dest="alembic.ini",
            source=str(_HERE / "files" / "alembic.ini.j2"),
            template=True,
        ),
        FileContribution(
            dest="alembic/env.py",
            source=str(_HERE / "files" / "alembic" / "env.py.j2"),
            template=True,
        ),
        FileContribution(
            dest="alembic/script.py.mako",
            source=str(_HERE / "files" / "alembic" / "script.py.mako"),
        ),
        FileContribution(
            dest="tests/conftest.py",
            source=str(_HERE / "files" / "tests" / "conftest.py.j2"),
            template=True,
        ),
    ],
    deps=[
        "sqlalchemy[asyncio]",
        "alembic",
    ],
    dev_deps=[
        "aiosqlite",
    ],
    env_vars=[
        # When postgres is selected it owns DATABASE_URL, so default to its
        # URL regardless of addon order. Otherwise fall back to local sqlite.
        EnvVar(
            key="DATABASE_URL",
            default='[% if "postgres" in addons %]postgresql+asyncpg://postgres:postgres@localhost:5432/(( pkg_name ))[% else %]sqlite+aiosqlite:///./dev.db[% endif %]',
        ),
    ],
    just_recipes=[
        '# generate a new alembic migration\nmigrate msg="":\n    uv run alembic revision --autogenerate -m "{{msg}}"',
        "# apply all pending migrations\nupgrade:\n    uv run alembic upgrade head",
        "# roll back one migration\ndowngrade:\n    uv run alembic downgrade -1",
    ],
    tool_overrides={
        "mypy": [
            {
                "module": ["sqlalchemy.*", "alembic.*"],
                "ignore_missing_imports": True,
            },
        ],
    },
    ruff_excludes=["alembic/"],
    injections=[
        Injection(
            point="settings_fields",
            templates=["fastapi"],
            content='[% if "postgres" not in addons %]\n    database_url: str = "sqlite+aiosqlite:///./dev.db"\n[% endif %]',
        ),
        Injection(
            point="lifespan_imports",
            content="\nfrom .db.session import engine",
        ),
        Injection(
            point="lifespan_shutdown",
            content="    if engine:\n        await engine.dispose()",
        ),
    ],
)
