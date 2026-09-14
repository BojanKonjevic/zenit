<h1 align="center">
  <a href="https://bojankonjevic.github.io/zenit/" target="_blank" rel="noopener noreferrer">
    zenit
  </a>
</h1>

<p align="center">
  Scaffold Python projects without lock-in. Add what you need. Remove cleanly.
</p>

<p align="center">
  <a href="https://github.com/BojanKonjevic/zenit/actions/workflows/ci.yml">
    <img src="https://github.com/BojanKonjevic/zenit/actions/workflows/ci.yml/badge.svg" alt="CI">
  </a>
  <a href="https://github.com/BojanKonjevic/zenit/blob/main/LICENSE">
    <img src="https://img.shields.io/github/license/BojanKonjevic/zenit" alt="License">
  </a>
  <a href="https://pypi.org/project/zenit/">
    <img src="https://img.shields.io/pypi/v/zenit" alt="PyPI">
  </a>
  <a href="https://pypi.org/project/zenit/">
    <img src="https://img.shields.io/pypi/pyversions/zenit" alt="Python">
  </a>
  <a href="https://codecov.io/gh/BojanKonjevic/zenit">
    <img src="https://img.shields.io/codecov/c/github/BojanKonjevic/zenit" alt="Coverage">
  </a>
</p>

---

**Zenit** is a CLI that scaffolds Python projects from declarative templates and addons, then keeps managing them over time. Generate a FastAPI app, add Redis later with one command, remove it just as cleanly. Generated code has zero runtime dependency on Zenit. Delete `.zenit.toml` and the project keeps working exactly the same.

![zenit flags demo](https://raw.githubusercontent.com/BojanKonjevic/zenit/main/assets/demo-flags.gif)

## Quick Start

```bash
# Install
uv tool install zenit

# Create a project
zenit create my-api

# Add capabilities later
zenit add redis
zenit add auth-manual

# Change your mind cleanly
zenit remove redis

# Check project health
zenit doctor
```

Prefer prompts? `zenit create my-api` walks you through template and addon selection interactively:

![zenit interactive demo](https://raw.githubusercontent.com/BojanKonjevic/zenit/main/assets/demo-interactive.gif)

## Features

| Feature                 | What it means                                                                     |
| ----------------------- | --------------------------------------------------------------------------------- |
| No lock-in              | Generated projects never import Zenit; deleting `.zenit.toml` leaves working code |
| Clean removal           | `zenit remove <addon>` undoes every file, injection, and dependency               |
| Declarative addons      | Addons describe what they add, never run install scripts                          |
| Structural injection    | libcst based code edits that survive formatting and comments                      |
| Fingerprint tracking    | Every injected block is hashed so removal finds exactly that block                |
| Atomic writes           | Multi file applies land together or roll back, never half applied                 |
| Dry run everywhere      | Preview every change before it touches disk                                       |
| Dependency graph        | `zenit graph` renders addon dependencies in the terminal or as DOT/JSON           |
| Per addon health checks | `zenit doctor` verifies each addon is intact                                      |
| Copier migration        | `zenit migrate` imports any Copier template into a Zenit managed project          |

## Templates and addons

Two templates: `blank` (minimal package) and `fastapi` (app with SQLAlchemy and Alembic). Nine addons layer on top, at creation time or later with `zenit add`:

| Addon            | Adds                                                     | Requires     |
| ---------------- | -------------------------------------------------------- | ------------ |
| `docker`         | Dockerfile, compose.yml, .dockerignore                   |              |
| `postgres`       | PostgreSQL driver, DATABASE_URL, compose service         |              |
| `redis`          | Redis service, connection helper, compose service        |              |
| `sqlalchemy`     | SQLAlchemy ORM, Alembic migrations                       |              |
| `sqlmodel`       | SQLModel ORM (Pydantic plus SQLAlchemy)                  | `sqlalchemy` |
| `auth-manual`    | JWT auth: register, login, refresh, logout, current user | `sqlalchemy` |
| `celery`         | Celery worker plus beat scheduler, backed by Redis       | `redis`      |
| `sentry`         | Sentry error tracking plus performance monitoring        |              |
| `github-actions` | CI workflow (lint, type check, test on push and PR)      |              |

Dependency conflicts are resolved with topological sort before anything is written.

## How it works

```
template files + addon manifests
        |
        v
  manifest (.zenit.toml)      records what owns every block
        |
        v
  handlers                    python (libcst) / toml / yaml / env / justfile
        |
        v
  your project files          plain code, no Zenit imports
```

Three mechanisms, each tracked separately in `.zenit.toml`:

1. **File writes.** Jinja renders the file, the manifest stores path plus content hash. On removal the file is deleted only if the hash still matches. If you edited it, Zenit warns first.
2. **Dependency edits.** `pyproject.toml` is edited with tomlkit, preserving formatting and comments. Each package is tagged with its owning addon and removed with it.
3. **Code injection.** The Python handler parses with libcst, finds the anchor structurally (settings field, lifespan hook, router list), splices in the block, and stores a fingerprint. Removal runs four stages in order: exact fingerprint match, normalized match, locator relocation if you moved the code, fuzzy match at 0.85 as last resort.

## Zenit vs Cookiecutter vs Copier

|                                        | Cookiecutter | Copier                          | Zenit                  |
| -------------------------------------- | ------------ | ------------------------------- | ---------------------- |
| Scaffold a project                     | Yes          | Yes                             | Yes                    |
| Manage it afterward (`add` / `remove`) | No           | Partial (update and re-migrate) | Yes                    |
| Remove one capability cleanly          | No           | No (manual)                     | Yes, fingerprint based |
| Structural code edits (AST, not regex) | No           | No                              | Yes, libcst            |
| Failed apply rolls back                | No           | No                              | Yes, batch snapshots   |
| Declarative, script free addons        | n/a          | No (tasks run code)             | Yes                    |

Cookiecutter renders once and walks away. Copier adds updates on top of that model. Zenit treats scaffolding as day one of project management: everything it adds stays removable.

## Studio

Browse templates, inspect addons with their files and dependencies, and build commands visually in the browser: **[bojankonjevic.github.io/zenit/studio/](https://bojankonjevic.github.io/zenit/studio/)**

![zenit studio: fastapi with five addons, dependency tree showing celery pulling in redis](https://raw.githubusercontent.com/BojanKonjevic/zenit/main/assets/studio.png)

## Example output

A FastAPI project generated by v1.1.2 with Docker, SQLAlchemy, Postgres, and Redis:

```bash
zenit create demo-api --template fastapi -a docker,sqlalchemy,postgres,redis
```

Browse the result without installing anything: **[BojanKonjevic/zenit-demo-api](https://github.com/BojanKonjevic/zenit-demo-api)**

## Documentation

Full docs at **[bojankonjevic.github.io/zenit/docs](https://bojankonjevic.github.io/zenit/docs/)**:

- [Getting Started](https://bojankonjevic.github.io/zenit/docs/getting-started) - install, create, and run in under five minutes
- [Architecture](https://bojankonjevic.github.io/zenit/docs/architecture/) - manifest, libcst injection, and rollback
- [Commands](https://bojankonjevic.github.io/zenit/docs/commands/) - `create`, `add`, `remove`, `doctor`, `list`, `config`, `graph`, `migrate`
- [Templates](https://bojankonjevic.github.io/zenit/docs/templates/) - `blank` and `fastapi`
- [Addons](https://bojankonjevic.github.io/zenit/docs/addons/) - all nine, plus how to write your own
- [Contributing](https://bojankonjevic.github.io/zenit/docs/contributing) - standards, tests, and PR process

## Status

Published on [PyPI](https://pypi.org/project/zenit/) at v1.1.2. Strict `mypy`, `ruff` clean, 1,300+ tests green in CI on Ubuntu and Windows across Python 3.12 to 3.14. See [CHANGELOG](CHANGELOG.md).

## License

[MIT](LICENSE)
