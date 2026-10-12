"""Errors the plugin host shows to an admin. They are not sandbox failures."""


class ExtensionError(Exception):
    """A catalog entry, download, or plugin operation was refused."""
