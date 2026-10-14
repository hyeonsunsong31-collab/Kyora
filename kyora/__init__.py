"""Kyora - AI game-development studio.

Kyora orchestrates four external systems in fixed roles over their own APIs:
NVIDIA Nemotron-3-Ultra (text/design director), Moonshot Kimi-K3 (planner and
judge), Claude Code at max effort (code engine) and NVIDIA Tokkio (avatar
presenter). It is an orchestration application, not a merged model. The core
uses only the Python standard library so the same build runs on Windows,
Android (Termux) and iOS (iSH).
"""
__version__ = "0.1.0"
