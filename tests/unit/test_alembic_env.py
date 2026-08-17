"""Unit tests for Alembic setup: alembic.ini and migration 001_initial.py.

These tests verify structural correctness without needing a DB connection.
They only check file existence, module imports, and attribute values.
"""

from __future__ import annotations

import ast
from configparser import ConfigParser
from pathlib import Path

# Project root for path resolution
PROJECT_ROOT = Path(__file__).parent.parent.parent


class TestAlembicIni:
    """Tests for alembic.ini existence and content."""

    def test_alembic_ini_exists(self) -> None:
        """alembic.ini must exist at the project root."""
        alembic_ini = PROJECT_ROOT / "alembic.ini"
        assert alembic_ini.exists(), f"alembic.ini not found at {alembic_ini}"

    def test_alembic_target_must_come_from_process_environment(self) -> None:
        """Static config and docs must not contain a default database credential."""
        alembic_ini = PROJECT_ROOT / "alembic.ini"
        ini_content = alembic_ini.read_text()
        readme_content = (PROJECT_ROOT / "README.md").read_text()

        config = ConfigParser(interpolation=None)
        config.read_string(ini_content)

        assert config.get("alembic", "sqlalchemy.url") == "", (
            "alembic.ini must leave sqlalchemy.url empty; alembic/env.py injects POSTGRES_URL"
        )
        assert "brain:brain" not in ini_content
        assert "brain:brain" not in readme_content

    def test_alembic_ini_has_file_template(self) -> None:
        """alembic.ini must have file_template for sequential naming."""
        alembic_ini = PROJECT_ROOT / "alembic.ini"
        content = alembic_ini.read_text()
        assert "file_template" in content, "alembic.ini must define file_template"

    def test_alembic_ini_script_location(self) -> None:
        """alembic.ini must specify script_location = alembic."""
        alembic_ini = PROJECT_ROOT / "alembic.ini"
        content = alembic_ini.read_text()
        assert "script_location" in content, "alembic.ini must have script_location"
        assert "alembic" in content, "alembic.ini script_location must point to alembic"


class TestAlembicVersionsDir:
    """Tests for alembic/versions/ directory and migration file."""

    def test_alembic_versions_dir_exists(self) -> None:
        """alembic/versions/ directory must exist."""
        versions_dir = PROJECT_ROOT / "alembic" / "versions"
        assert versions_dir.exists(), f"alembic/versions/ not found at {versions_dir}"
        assert versions_dir.is_dir(), "alembic/versions must be a directory"

    def test_migration_001_exists(self) -> None:
        """Migration file starting with 001 must exist."""
        versions_dir = PROJECT_ROOT / "alembic" / "versions"
        migration_files = list(versions_dir.glob("001*.py"))
        assert len(migration_files) >= 1, f"No 001*.py migration found in {versions_dir}"

    def test_migration_001_is_named_initial(self) -> None:
        """Migration file must be named 001_initial.py."""
        migration_file = PROJECT_ROOT / "alembic" / "versions" / "001_initial.py"
        assert migration_file.exists(), f"001_initial.py not found at {migration_file}"


class TestMigration001Content:
    """Tests for content of 001_initial.py migration."""

    @property
    def migration_path(self) -> Path:
        return PROJECT_ROOT / "alembic" / "versions" / "001_initial.py"

    @property
    def migration_content(self) -> str:
        return self.migration_path.read_text()

    def _get_migration_ast(self) -> ast.Module:
        return ast.parse(self.migration_content)

    def test_migration_has_upgrade_function(self) -> None:
        """Migration must have an upgrade() function."""
        tree = self._get_migration_ast()
        func_names = [node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
        assert "upgrade" in func_names, "Migration must define upgrade() function"

    def test_migration_has_downgrade_function(self) -> None:
        """Migration must have a downgrade() function."""
        tree = self._get_migration_ast()
        func_names = [node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
        assert "downgrade" in func_names, "Migration must define downgrade() function"

    def test_migration_revision_is_001(self) -> None:
        """Migration revision must be '001'."""
        content = self.migration_content
        # Check for revision = "001" or revision = '001'
        assert 'revision = "001"' in content or "revision = '001'" in content, (
            'Migration must have revision = "001"'
        )

    def test_migration_down_revision_is_none(self) -> None:
        """Migration down_revision must be None (first migration)."""
        content = self.migration_content
        assert "down_revision = None" in content, "Migration must have down_revision = None"

    def test_migration_creates_pgvector_extension(self) -> None:
        """Migration upgrade must create the pgvector extension."""
        content = self.migration_content
        assert "CREATE EXTENSION" in content and "vector" in content, (
            "upgrade() must execute CREATE EXTENSION ... vector"
        )

    def test_migration_creates_update_updated_at_function(self) -> None:
        """Migration must create the update_updated_at() PL/pgSQL function."""
        content = self.migration_content
        assert "update_updated_at" in content, (
            "Migration must create update_updated_at() trigger function"
        )

    def test_migration_creates_all_6_tables(self) -> None:
        """Migration must create all 6 tables."""
        content = self.migration_content
        required_tables = [
            "decisions",
            "learnings",
            "snippets",
            "runbooks",
            "adrs",
            "project_contexts",
        ]
        for table in required_tables:
            assert f"CREATE TABLE {table}" in content or f'CREATE TABLE "{table}"' in content, (
                f"Migration must create table '{table}'"
            )

    def test_migration_uses_raw_sql_not_create_table_dsl(self) -> None:
        """Migration must use op.execute() not op.create_table()."""
        content = self.migration_content
        assert "op.execute(" in content, "Migration must use op.execute() for raw SQL"
        # op.create_table is forbidden (doesn't support pgvector types)
        assert "op.create_table(" not in content, (
            "Migration must NOT use op.create_table() DSL (use op.execute() instead)"
        )

    def test_migration_creates_hnsw_indexes(self) -> None:
        """Migration must create HNSW indexes on embedding columns."""
        content = self.migration_content
        assert "hnsw" in content.lower(), "Migration must create HNSW indexes for embedding columns"
        assert "vector_cosine_ops" in content, (
            "HNSW indexes must use vector_cosine_ops operator class"
        )

    def test_migration_creates_updated_at_triggers(self) -> None:
        """Migration must create updated_at triggers for all 6 tables."""
        content = self.migration_content
        assert "CREATE TRIGGER" in content, "Migration must create triggers"
        # Check at least one trigger is linked to update_updated_at
        assert "update_updated_at" in content, "Triggers must call update_updated_at() function"

    def test_migration_downgrade_drops_triggers_before_tables(self) -> None:
        """Downgrade must drop triggers before tables."""
        content = self.migration_content
        drop_trigger_pos = content.find("DROP TRIGGER")
        drop_table_pos = content.find("DROP TABLE")
        assert drop_trigger_pos != -1, "downgrade() must drop triggers"
        assert drop_table_pos != -1, "downgrade() must drop tables"
        # DROP TRIGGER must come before DROP TABLE
        assert drop_trigger_pos < drop_table_pos, "downgrade() must drop triggers BEFORE tables"

    def test_migration_creates_gin_indexes_for_search_vector(self) -> None:
        """Migration must create GIN indexes for search_vector columns."""
        content = self.migration_content
        assert "GIN" in content or "gin" in content.lower(), (
            "Migration must create GIN indexes for full-text search"
        )

    def test_migration_has_search_vector_generated_columns(self) -> None:
        """Migration must have GENERATED ALWAYS AS for search_vector columns."""
        content = self.migration_content
        assert "GENERATED ALWAYS AS" in content, (
            "Migration must create GENERATED ALWAYS AS ... STORED search_vector columns"
        )

    def test_downgrade_drops_trigger_function(self) -> None:
        """Downgrade must drop the update_updated_at trigger function."""
        content = self.migration_content
        assert "DROP FUNCTION" in content, (
            "downgrade() must drop the update_updated_at() trigger function"
        )


class TestAlembicEnvPy:
    """Tests for alembic/env.py content."""

    @property
    def env_path(self) -> Path:
        return PROJECT_ROOT / "alembic" / "env.py"

    @property
    def env_content(self) -> str:
        return self.env_path.read_text()

    def test_env_py_exists(self) -> None:
        """alembic/env.py must exist."""
        assert self.env_path.exists(), f"alembic/env.py not found at {self.env_path}"

    def test_env_py_uses_async_engine(self) -> None:
        """env.py must use async_engine_from_config for online mode."""
        content = self.env_content
        assert "async_engine_from_config" in content, "env.py must use async_engine_from_config"

    def test_env_py_uses_null_pool(self) -> None:
        """env.py must use NullPool to prevent event loop hang."""
        content = self.env_content
        assert "NullPool" in content, "env.py must use NullPool for migration connections"

    def test_env_py_imports_target_metadata(self) -> None:
        """env.py must import target_metadata from brain_v42.db.tables."""
        content = self.env_content
        assert "brain_v42" in content, "env.py must import from brain_v42 (target_metadata)"
        assert "target_metadata" in content, "env.py must set target_metadata"

    def test_env_py_uses_asyncio_run(self) -> None:
        """env.py must use asyncio.run() for online mode."""
        content = self.env_content
        assert "asyncio.run(" in content, "env.py must use asyncio.run() for async migrations"

    def test_env_py_has_run_sync_callback(self) -> None:
        """env.py must use run_sync callback pattern."""
        content = self.env_content
        assert "run_sync" in content, "env.py must use conn.run_sync() for migration execution"


def test_migration_heads_to_latest() -> None:
    """After upgrade, alembic_version table points at the latest revision.

    Each new migration appends one link to the chain; this test is the
    canary that catches missing down_revision wiring when a new revision
    lands on main.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config("alembic.ini")
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()

    # Le head est DÉRIVÉ, pas épinglé : l'invariant est « une seule tête », pas
    # « la tête vaut N ». Épingler le numéro faisait échouer ce canari à chaque
    # migration pour la seule raison qu'une migration avait atterri, ce qui est
    # précisément le cas nominal. Même correction que le commit e29f0e5b.
    latest = max(revision.revision for revision in script.walk_revisions())
    assert heads == [latest], f"Expected a sole head, got {heads}"

    expected_chain = [
        ("041", "040"),
        ("040", "039"),
        ("039", "038"),
        ("037", "036"),
        ("038", "037"),
        ("036", "035"),
        ("035", "034"),
        ("033", "032"),
        ("034", "033"),
        ("032", "031"),
        ("031", "030"),
        ("030", "029"),
        ("029", "028"),
        ("028", "027"),
        ("027", "026"),
        ("026", "025"),
        ("025", "024"),
        ("024", "023"),
        ("023", "022"),
        ("022", "021"),
        ("021", "020"),
        ("020", "019"),
        ("019", "018"),
        ("018", "017"),
        ("017", "016"),
        ("016", "015"),
    ]
    for rev, parent in expected_chain:
        assert script.get_revision(rev).down_revision == parent, (
            f"{rev}.down_revision should be {parent}"
        )
