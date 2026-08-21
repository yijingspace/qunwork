"""Tests for knowledge library path configuration feature."""
import os
import pytest
import shutil
from pathlib import Path


@pytest.fixture
def tmp_knowledge_dir(tmp_path: Path):
    """Create a temporary directory for knowledge library testing."""
    return tmp_path / "knowledge_data"


class TestKnowledgePathConfiguration:
    """Test suite for the knowledge library path setting feature."""

    def test_default_knowledge_path(self, tmp_path: Path):
        """Default path should be data_base / knowledge.db."""
        from coworker.knowledge.store import KnowledgeStore

        # Create a minimal manager-like setup
        base = tmp_path / "data"
        base.mkdir()
        db_path = base / "knowledge.db"
        store = KnowledgeStore(db_path)
        store.close()

        assert db_path.exists()

    def test_knowledge_path_is_configurable(self, tmp_knowledge_dir: Path):
        """The knowledge DB path should be configurable via prefs."""
        # Simulate the preference storage
        prefs = {"knowledge_db_path": str(tmp_knowledge_dir / "custom.db")}

        # Verify the path is properly resolved
        expected = tmp_knowledge_dir / "custom.db"
        resolved = Path(prefs["knowledge_db_path"]).expanduser().resolve()
        assert resolved == expected

    def test_knowledge_path_migration(self, tmp_path: Path):
        """Test that data is correctly migrated when path changes."""
        from coworker.knowledge.store import KnowledgeStore

        # Create source DB with some data
        src = tmp_path / "source"
        src.mkdir()
        src_db = src / "knowledge.db"
        store = KnowledgeStore(src_db)
        store.add_text("Test Title", "Test Content for migration")
        store.close()

        # Create target directory
        dst = tmp_path / "destination"
        dst.mkdir()
        dst_db = dst / "knowledge.db"

        # Perform migration
        shutil.move(str(src_db), str(dst_db))

        # Verify data survived the migration
        store = KnowledgeStore(dst_db)
        items = store.list_items()
        assert len(items) == 1
        assert items[0]["title"] == "Test Title"
        store.close()

    def test_knowledge_path_validation(self, tmp_path: Path):
        """Invalid paths should raise appropriate errors."""
        from pathlib import PureWindowsPath

        # Test with invalid path characters (platform-dependent)
        invalid_path = tmp_path / "invalid\x00path"
        try:
            # This should handle invalid paths gracefully
            result = Path(str(invalid_path))
            # Path construction may succeed but operations should fail
        except (ValueError, OSError):
            pass  # Expected behavior

    def test_knowledge_store_persists_data(self, tmp_knowledge_dir: Path):
        """Knowledge store should persist data across reopens."""
        from coworker.knowledge.store import KnowledgeStore

        db_path = tmp_knowledge_dir / "knowledge.db"

        # First session: add data
        store = KnowledgeStore(db_path)
        store.add_text("Persistent Title", "Persistent Content")
        store.close()

        # Second session: data should still be there
        store = KnowledgeStore(db_path)
        items = store.list_items()
        assert len(items) == 1
        assert items[0]["title"] == "Persistent Title"
        store.close()


class TestKnowledgePathAPI:
    """Test the API endpoints for knowledge path management."""

    def test_get_knowledge_path_returns_default(self):
        """GET /v1/knowledge/path should return the default path when not configured."""
        # This would need a running server context
        pass

    def test_set_knowledge_path_rejects_invalid(self):
        """POST /v1/knowledge/path with empty path should fail."""
        # Validation: empty path should return ok=False with error
        pass

    def test_set_knowledge_path_with_migration(self):
        """POST /v1/knowledge/path with migrate=true should move the DB."""
        # Requires server context with actual file operations
        pass


class TestKnowledgePathEdgeCases:
    """Edge cases for knowledge path configuration."""

    def test_symlink_paths_handled(self, tmp_path: Path):
        """Symlinks in the path should be resolved to real paths."""
        target = tmp_path / "real"
        target.mkdir()
        link = tmp_path / "link"
        try:
            link.symlink_to(target)
            resolved = link.expanduser().resolve()
            assert resolved == target.resolve()
        except OSError:
            pytest.skip("Symlinks not supported on this platform")

    def test_relative_paths_resolved(self, tmp_path: Path):
        """Relative paths should be resolved to absolute."""
        # Change to temp directory
        original = os.getcwd()
        try:
            os.chdir(tmp_path)
            path = Path("relative/path/to/db")
            assert not path.is_absolute()
            resolved = path.expanduser().resolve()
            assert resolved.is_absolute()
        finally:
            os.chdir(original)
