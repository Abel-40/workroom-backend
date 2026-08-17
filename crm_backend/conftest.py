import pytest


@pytest.fixture(autouse=True)
def _use_temp_media_root(settings, tmp_path):
    """File-upload tests must never write into the real dev media/ directory."""
    settings.MEDIA_ROOT = str(tmp_path)


@pytest.fixture(autouse=True)
def _run_celery_tasks_eagerly(settings):
    """No broker/worker is running during tests -- .delay() must execute the
    real task body inline instead of silently doing nothing."""
    settings.CELERY_TASK_ALWAYS_EAGER = True


@pytest.fixture(autouse=True)
def _disable_rate_limiting(settings):
    """utils/rate_limit.py counters live in real Redis, external to each
    test's DB transaction rollback -- leaving this on would make the suite
    depend on a running Redis and go flaky as counts accumulate across
    runs."""
    settings.RATE_LIMIT_ENABLED = False
