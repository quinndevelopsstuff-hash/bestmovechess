import pytest

from app import config


@pytest.fixture(autouse=True)
def isolated_profiles(tmp_path, monkeypatch):
    """Never read or write the real PROFILE_DIR from a test."""
    monkeypatch.setattr(config, "PROFILE_DIR", str(tmp_path / "profiles"))
    from app.vision import calibration
    monkeypatch.setattr(calibration, "BUNDLED_DIR", tmp_path / "bundled")   # tests start with no profiles
