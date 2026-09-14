"""Integration tests - scaffold real projects into tmp_path and verify the results."""

from zenit.doctor.doctor import Severity, run_doctor

# ── blank template ────────────────────────────────────────────────────────────


class TestBlankTemplate:
    def test_project_directory_created(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapp", "blank", [])
        assert project_dir.exists()
        assert project_dir.is_dir()

    def test_package_structure(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapp", "blank", [])
        assert (project_dir / "src" / "myapp" / "__init__.py").exists()
        assert (project_dir / "src" / "myapp" / "main.py").exists()
        assert (project_dir / "src" / "myapp" / "__main__.py").exists()

    def test_tests_directory(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapp", "blank", [])
        assert (project_dir / "tests" / "test_main.py").exists()

    def test_common_files(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapp", "blank", [])
        assert (project_dir / ".gitignore").exists()
        assert (project_dir / ".gitattributes").exists()
        assert (project_dir / ".pre-commit-config.yaml").exists()

    def test_pyproject_toml_exists_and_contains_name(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapp", "blank", [])
        pyproject = (project_dir / "pyproject.toml").read_text()
        assert 'name = "myapp"' in pyproject

    def test_pyproject_toml_contains_pytest(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapp", "blank", [])
        pyproject = (project_dir / "pyproject.toml").read_text()
        assert "pytest" in pyproject

    def test_justfile_exists(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapp", "blank", [])
        assert (project_dir / "justfile").exists()

    def test_justfile_contains_base_recipes(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapp", "blank", [])
        justfile = (project_dir / "justfile").read_text()
        for recipe in ["test:", "lint:", "fmt:", "check:", "run:"]:
            assert recipe in justfile, f"missing recipe: {recipe}"

    def test_justfile_run_recipe_uses_pkg_name(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapp", "blank", [])
        justfile = (project_dir / "justfile").read_text()
        assert "myapp" in justfile

    def test_main_py_contains_project_name(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapp", "blank", [])
        main = (project_dir / "src" / "myapp" / "main.py").read_text()
        assert "myapp" in main

    def test_init_py_contains_version(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapp", "blank", [])
        init = (project_dir / "src" / "myapp" / "__init__.py").read_text()
        assert "__version__" in init

    def test_hyphenated_name_uses_underscore_pkg(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("my-app", "blank", [])
        assert (project_dir / "src" / "my_app" / "__init__.py").exists()
        justfile = (project_dir / "justfile").read_text()
        assert "my_app" in justfile
        assert (
            "my-app" not in justfile.split("my-app")[0]
        )  # name in metadata ok, not in commands

    def test_git_repo_initialised(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapp", "blank", [])
        assert (project_dir / ".git").exists()
        import subprocess

        log = subprocess.run(
            ["git", "log", "-1", "--oneline"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        assert log.returncode == 0
        assert "Initial commit" in log.stdout

    def test_git_working_tree_clean_after_scaffold(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapp", "blank", [])
        import subprocess

        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        assert status.returncode == 0
        assert status.stdout.strip() == "", (
            f"Expected clean working tree, got:\n{status.stdout}"
        )

    def test_no_duplicate_recipes_in_justfile(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapp", "blank", [])
        justfile = (project_dir / "justfile").read_text()
        recipe_lines = [
            line.split(":")[0].strip()
            for line in justfile.splitlines()
            if line
            and not line[0].isspace()
            and ":" in line
            and not line.startswith("#")
        ]
        assert len(recipe_lines) == len(set(recipe_lines)), (
            f"Duplicate recipes found: {[r for r in recipe_lines if recipe_lines.count(r) > 1]}"
        )


# ── blank + docker ────────────────────────────────────────────────────────────


class TestBlankWithDocker:
    def test_dockerfile_created(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapp", "blank", ["docker"])
        assert (project_dir / "Dockerfile").exists()

    def test_compose_yml_created(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapp", "blank", ["docker"])
        assert (project_dir / "compose.yml").exists()

    def test_dockerignore_created(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapp", "blank", ["docker"])
        assert (project_dir / ".dockerignore").exists()

    def test_docker_recipes_in_justfile(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapp", "blank", ["docker"])
        justfile = (project_dir / "justfile").read_text()
        assert "docker-up:" in justfile
        assert "docker-down:" in justfile

    def test_no_duplicate_recipes(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapp", "blank", ["docker"])
        justfile = (project_dir / "justfile").read_text()
        recipe_lines = [
            line.split(":")[0].strip()
            for line in justfile.splitlines()
            if line
            and not line[0].isspace()
            and ":" in line
            and not line.startswith("#")
        ]
        assert len(recipe_lines) == len(set(recipe_lines))


# ── fastapi template ──────────────────────────────────────────────────────────


class TestFastapiTemplate:
    def test_project_directory_created(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapi", "fastapi", ["docker"])
        assert project_dir.exists()

    def test_package_structure(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapi", "fastapi", ["docker"])
        src = project_dir / "src" / "myapi"
        assert (src / "main.py").exists()
        assert (src / "settings.py").exists()
        assert (src / "lifecycle.py").exists()
        assert (src / "exceptions.py").exists()

    def test_api_structure(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapi", "fastapi", ["docker"])
        src = project_dir / "src" / "myapi"
        assert (src / "api" / "router.py").exists()
        assert (src / "api" / "routes" / "health.py").exists()

    def test_settings_py_has_settings_class(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapi", "fastapi", ["docker"])
        settings = (project_dir / "src" / "myapi" / "settings.py").read_text()
        assert "class Settings" in settings

    def test_settings_py_no_database_url(self, tmp_path, scaffold_project):
        """Plain fastapi skeleton has no database_url - it comes from postgres addon."""
        project_dir = scaffold_project("myapi", "fastapi", ["docker"])
        settings = (project_dir / "src" / "myapi" / "settings.py").read_text()
        assert "database_url" not in settings

    def test_lifecycle_py_no_engine_dispose(self, tmp_path, scaffold_project):
        """Plain fastapi skeleton has no engine.dispose() - it comes from sqlalchemy addon."""
        project_dir = scaffold_project("myapi", "fastapi", ["docker"])
        lifecycle = (project_dir / "src" / "myapi" / "lifecycle.py").read_text()
        assert "engine.dispose" not in lifecycle

    def test_fastapi_recipes_in_justfile(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapi", "fastapi", ["docker"])
        justfile = (project_dir / "justfile").read_text()
        assert "run:" in justfile

    def test_no_duplicate_recipes(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapi", "fastapi", ["docker"])
        justfile = (project_dir / "justfile").read_text()
        recipe_lines = [
            line.split(":")[0].strip()
            for line in justfile.splitlines()
            if line
            and not line[0].isspace()
            and ":" in line
            and not line.startswith("#")
        ]
        assert len(recipe_lines) == len(set(recipe_lines))

    def test_git_repo_initialised(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapi", "fastapi", ["docker"])
        assert (project_dir / ".git").exists()
        import subprocess

        log = subprocess.run(
            ["git", "log", "-1", "--oneline"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        assert log.returncode == 0
        assert "Initial commit" in log.stdout


# ── fastapi + all addons ──────────────────────────────────────────────────────


class TestFastapiAllAddons:
    ADDONS = [
        "docker",
        "postgres",
        "sqlalchemy",
        "redis",
        "celery",
        "sentry",
        "github-actions",
    ]

    def test_scaffolds_successfully(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapi", "fastapi", self.ADDONS)
        assert project_dir.exists()

    def test_doctor_clean_after_scaffold(self, tmp_path, scaffold_project):
        """Multiple addons injecting into the same file must leave the
        manifest line tracking exact - no drift warnings, no errors."""
        project_dir = scaffold_project("myapi", "fastapi", self.ADDONS)
        results = run_doctor(project_dir)
        problems = [
            f"{r.category}: {i.message}"
            for r in results
            for i in r.issues
            if i.severity is not Severity.OK
        ]
        assert problems == []

    def test_single_database_url_in_dotenv(self, tmp_path, scaffold_project):
        """sqlalchemy and postgres both declare DATABASE_URL - exactly one
        line survives, with the postgres value winning."""
        project_dir = scaffold_project("myapi", "fastapi", self.ADDONS)
        text = (project_dir / ".env").read_text()
        assert text.count("DATABASE_URL=") == 1
        assert "postgresql+asyncpg" in text

    def test_no_duplicate_python_dotenv_in_pyproject(
        self, tmp_path, scaffold_project
    ):
        project_dir = scaffold_project("myapi", "fastapi", self.ADDONS)
        text = (project_dir / "pyproject.toml").read_text()
        assert text.count("python-dotenv") == 1

    def test_redis_integration_file(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapi", "fastapi", self.ADDONS)
        assert (project_dir / "src" / "myapi" / "integrations" / "redis.py").exists()

    def test_celery_tasks_files(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapi", "fastapi", self.ADDONS)
        tasks = project_dir / "src" / "myapi" / "tasks"
        assert (tasks / "celery_app.py").exists()
        assert (tasks / "example_tasks.py").exists()

    def test_sentry_integration_file(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapi", "fastapi", self.ADDONS)
        assert (project_dir / "src" / "myapi" / "integrations" / "sentry.py").exists()

    def test_github_actions_workflow(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapi", "fastapi", self.ADDONS)
        assert (project_dir / ".github" / "workflows" / "ci.yml").exists()

    def test_ci_yml_has_postgres_service(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapi", "fastapi", self.ADDONS)
        ci = (project_dir / ".github" / "workflows" / "ci.yml").read_text()
        assert "postgres" in ci

    def test_ci_yml_has_redis_service(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapi", "fastapi", self.ADDONS)
        ci = (project_dir / ".github" / "workflows" / "ci.yml").read_text()
        assert "redis" in ci

    def test_compose_yml_has_all_services(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapi", "fastapi", self.ADDONS)
        compose = (project_dir / "compose.yml").read_text()
        for service in ["app:", "db:", "redis:", "celery-worker:", "celery-beat:"]:
            assert service in compose, f"missing compose service: {service}"

    def test_settings_py_has_database_url(self, tmp_path, scaffold_project):
        """database_url comes from postgres addon injection."""
        project_dir = scaffold_project("myapi", "fastapi", self.ADDONS)
        settings = (project_dir / "src" / "myapi" / "settings.py").read_text()
        assert "database_url" in settings

    def test_settings_py_has_redis_url(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapi", "fastapi", self.ADDONS)
        settings = (project_dir / "src" / "myapi" / "settings.py").read_text()
        assert "redis_url" in settings

    def test_settings_py_has_sentry_dsn(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapi", "fastapi", self.ADDONS)
        settings = (project_dir / "src" / "myapi" / "settings.py").read_text()
        assert "sentry_dsn" in settings

    def test_lifecycle_py_disposes_engine(self, tmp_path, scaffold_project):
        """engine.dispose() comes from sqlalchemy addon injection."""
        project_dir = scaffold_project("myapi", "fastapi", self.ADDONS)
        lifecycle = (project_dir / "src" / "myapi" / "lifecycle.py").read_text()
        assert "engine.dispose" in lifecycle

    def test_lifecycle_py_calls_init_sentry(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapi", "fastapi", self.ADDONS)
        lifecycle = (project_dir / "src" / "myapi" / "lifecycle.py").read_text()
        assert "init_sentry" in lifecycle

    def test_all_addon_recipes_in_justfile(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapi", "fastapi", self.ADDONS)
        justfile = (project_dir / "justfile").read_text()
        for recipe in [
            "docker-up:",
            "docker-down:",
            "migrate",
            "upgrade:",
            "downgrade:",
            "wait-db:",
            "db-create:",
            "db-reset:",
            "redis-up:",
            "redis-down:",
            "redis-cli:",
            "celery-up:",
            "celery-down:",
            "celery-flower:",
            "celery-logs:",
            "sentry-check:",
            "sentry-test:",
        ]:
            assert recipe in justfile, f"missing recipe: {recipe}"

    def test_no_duplicate_recipes(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapi", "fastapi", self.ADDONS)
        justfile = (project_dir / "justfile").read_text()
        recipe_lines = [
            line.split(":")[0].strip()
            for line in justfile.splitlines()
            if line
            and not line[0].isspace()
            and ":" in line
            and not line.startswith("#")
        ]
        assert len(recipe_lines) == len(set(recipe_lines)), (
            f"Duplicate recipes: {[r for r in recipe_lines if recipe_lines.count(r) > 1]}"
        )

    def test_env_file_has_database_url(self, tmp_path, scaffold_project):
        """DATABASE_URL comes from postgres addon."""
        project_dir = scaffold_project("myapi", "fastapi", self.ADDONS)
        env = (project_dir / ".env").read_text()
        assert "DATABASE_URL=" in env

    def test_env_file_has_redis_url(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapi", "fastapi", self.ADDONS)
        env = (project_dir / ".env").read_text()
        assert "REDIS_URL=" in env

    def test_env_file_has_sentry_dsn(self, tmp_path, scaffold_project):
        project_dir = scaffold_project("myapi", "fastapi", self.ADDONS)
        env = (project_dir / ".env").read_text()
        assert "SENTRY_DSN=" in env
