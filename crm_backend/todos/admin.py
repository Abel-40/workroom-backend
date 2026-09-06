"""Deliberately empty.

TodoItem is private to its owner (see todos/models.py). Registering it in
the Django admin would create a second, non-API way to read every user's
private list, so it stays unregistered.
"""
