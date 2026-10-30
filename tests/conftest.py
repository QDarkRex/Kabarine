"""Every test gets its own DATA_PATH, so health checks never measure the real disk's
downloads folder or delete anything in it."""
import pytest


@pytest.fixture(autouse=True)
def _isolated_data_path(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_PATH", str(tmp_path / "data"))
