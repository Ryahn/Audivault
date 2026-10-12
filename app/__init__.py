"""Audivault — an open-source, self-hosted audiobook server with Audiobookshelf API compatibility."""

__version__ = "1.2.0"

# Plugin interface version. Independent of __version__. A plugin whose catalog
# `api` is not exactly this value cannot be enabled.
PLUGIN_API_VERSION = 1

# Version reported to clients. Apps such as AudioBooth gate features on the
# Audiobookshelf server version, so we report a modern compatible version.
ABS_COMPAT_VERSION = "2.30.0"
