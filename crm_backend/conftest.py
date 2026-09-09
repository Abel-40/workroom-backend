import pytest


def pytest_configure(config):
    """Hash test passwords with MD5 instead of PBKDF2.

    Django 6.0 defaults to 1,200,000 PBKDF2 iterations, which is correct in
    production and costs **2.6 seconds per password** on this machine. The
    suite creates a user for nearly every actor in every test -- six or seven
    per test in the task and visibility modules alone -- so almost all of the
    wall-clock time was spent deliberately making hashes slow to compute.

    This is set here rather than as an autouse fixture because the fixture
    would be too late: `test_access_matrix.py` builds its world in
    `setUpTestData`, which runs before any function-scoped fixture, and would
    have gone on paying full price.

    Nothing in the suite asserts on the algorithm. Password *strength* rules
    are validated by `validate_password`, which is a separate mechanism and is
    unaffected.
    """
    from django.conf import settings

    settings.PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']


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
