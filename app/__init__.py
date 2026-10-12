"""Audivault — an open-source, self-hosted audiobook server with Audiobookshelf API compatibility."""

__version__ = "1.1.0"

# Version reported to clients. Apps such as AudioBooth gate features on the
# Audiobookshelf server version, so we report a modern compatible version.
ABS_COMPAT_VERSION = "2.30.0"
