import pytest


@pytest.fixture(autouse=True)
def _use_temp_media_root(settings, tmp_path):
    """File-upload tests must never write into the real dev media/ directory."""
    settings.MEDIA_ROOT = str(tmp_path)
