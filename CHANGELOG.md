# Changelog

All notable changes to this project are documented here. Only versions published on PyPI are listed. Early 1.0.x releases shipped fast with one fix each, so they are summarized in one line apiece.

## [Unreleased]

## [1.1.2] - 2026-09-14

Fixes found by generating a demo project and running doctor on it:

- Manifest line tracking re-syncs automatically after create, add, and remove. Fresh projects pass `doctor` with no drift warnings, and add/remove cycles keep it clean without `--fix`
- `.env` merge replaces duplicate keys in place instead of appending. `DATABASE_URL` appears once, and the postgres value wins whenever postgres is selected regardless of addon order
- Duplicate dependencies across template and addons are deduped in the generated `pyproject.toml`

## [1.1.1] - 2026-08-04

Small follow up to 1.1.0.

- Docs: README shows the non interactive one liner front and center, plus an asciinema demo
- Studio: responsive layout for small screens
- CI: test coverage reporting added
- Docs: clarified supported Python versions (3.12+)

## [1.1.0] - 2026-08-03

- Build: fixed Python version metadata in the published package
- Accumulated three months of work since 1.0.9: the `migrate` command, Studio, `doctor` checks, and the full test suite as described below

At this point the project is: two templates (`blank`, `fastapi`), nine addons (`docker`, `postgres`, `redis`, `sqlalchemy`, `sqlmodel`, `auth-manual`, `celery`, `sentry`, `github-actions`), lifecycle commands (`create`, `add`, `remove`, `doctor`, `list`, `config`, `graph`, `migrate`), structural Python injection via libcst with fingerprint tracked removal, atomic multi file writes with rollback, dry run everywhere, strict mypy, ruff clean, 1,300+ tests with CI on Ubuntu and Windows across Python 3.12 to 3.14.

## 1.0.x - 2026-05-06 to 2026-05-18

Rapid iteration, each release carried one small change:

- 1.0.9: README update
- 1.0.8: general cleanup
- 1.0.7: fixed package data (MANIFEST) so templates and addons ship in the wheel
- 1.0.4: removed `scaffold` from the CLI
- 1.0.3: more defensive addon applicability checks
- 1.0.2: removed sentinel comments from generated projects, added `zenit.toml`
- 1.0.1: optional config file for defaults

## [1.0.0] - 2026-05-06

First public release on PyPI.
