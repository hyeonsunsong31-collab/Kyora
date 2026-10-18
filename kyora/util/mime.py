"""Content types that are the same on every platform.

``mimetypes.guess_type`` reads the Windows registry, where installed software often maps
``.js`` to ``text/plain`` and ``.zip`` to ``application/x-zip-compressed`` (found on the
GitHub Windows runners). A module script served as ``text/plain`` is refused by browsers, so
Kyora answers from its own table first and falls back to a ``MimeTypes`` instance built from
Python's built-in table only (it never reads the registry or /etc/mime.types).
"""
from __future__ import annotations

import mimetypes
from pathlib import PurePosixPath

TABLE = {
    ".html": "text/html", ".htm": "text/html", ".js": "application/javascript", ".mjs": "application/javascript",
    ".cjs": "application/javascript", ".json": "application/json", ".map": "application/json",
    ".wasm": "application/wasm", ".css": "text/css", ".svg": "image/svg+xml", ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp", ".avif": "image/avif",
    ".ico": "image/x-icon", ".bmp": "image/bmp", ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".oga": "audio/ogg",
    ".wav": "audio/wav", ".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac", ".mp4": "video/mp4",
    ".webm": "video/webm", ".ogv": "video/ogg", ".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv",
    ".xml": "application/xml", ".glb": "model/gltf-binary", ".gltf": "model/gltf+json", ".ttf": "font/ttf",
    ".otf": "font/otf", ".woff": "font/woff", ".woff2": "font/woff2", ".glsl": "text/plain", ".frag": "text/plain",
    ".vert": "text/plain", ".wgsl": "text/plain", ".obj": "text/plain", ".mtl": "text/plain",
    ".bin": "application/octet-stream", ".zip": "application/zip", ".pdf": "application/pdf",
    ".py": "text/x-python", ".ts": "text/plain", ".yaml": "text/yaml", ".yml": "text/yaml",
}
_BUILTIN = mimetypes.MimeTypes()  # Python's own table; the module-level db may include the registry


def guess_type(name: str) -> str | None:
    """The content type for ``name`` by extension, or None when unknown."""
    ext = PurePosixPath(str(name).replace("\\", "/")).suffix.lower()
    if not ext:
        return None
    return TABLE.get(ext) or _BUILTIN.guess_type("x" + ext)[0]


__all__ = ["TABLE", "guess_type"]
