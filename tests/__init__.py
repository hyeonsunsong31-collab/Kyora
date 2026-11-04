"""Kyora's tests. They never use the developer's own keys.

A source checkout, or an unpacked private release, may hold the user's private keys: ``config/keys.local.json``
(Settings saves keys there) and key variables in the environment (``OPENROUTER_API_KEY`` and the others that
``kyora.config.Config.load`` reads). A test that loaded them would send a real key to a real endpoint, spend
the user's credits and fail. Importing this package removes those variables from the test process, and
:func:`copy_config` copies a config folder without ``keys.local.json``. Tests that need a key variable set it
themselves (``mock.patch.dict(os.environ, ...)``).
"""
from __future__ import annotations

import os
import shutil

# The variables Config.load fills empty key slots from (kyora/config.py)
KEY_ENV = ("OPENROUTER_API_KEY", "NVIDIA_API_KEY", "ANTHROPIC_API_KEY")
LOCAL_KEYS_NAME = "keys.local.json"

for _name in KEY_ENV:
    os.environ.pop(_name, None)


def copy_config(src, dst):
    """Copy a config folder for a test, leaving out the developer's private keys (``keys.local.json``)."""
    return shutil.copytree(src, dst, ignore=shutil.ignore_patterns(LOCAL_KEYS_NAME))
