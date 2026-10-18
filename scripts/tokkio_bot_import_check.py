#!/usr/bin/env python3
"""Lay out the Tokkio ACE controller app the way deploy/upload_overlay.py does and load it for real.

Copies the vendored ``workflows/tokkio/5.0.0-ga/src/llm-rag/src`` package, replaces ``bot.py`` with
``tokkio-kyora/src/bot.kyora.py``, adds ``kyora_processor.py`` and uses ``config.kyora.yaml`` as the
config. Then, with the real nvidia-pipecat / pipecat-ai libraries installed:

  1. validates config.kyora.yaml with Tokkio's own pydantic ``Config`` model
  2. imports the patched ``bot`` module (as uvicorn does), with OTEL disabled
  3. checks that the session pipeline factory inserts ``KyoraProcessor`` and calls ``attach_task``

GPU services are not contacted: Riva/Audio2Face clients connect only when a session starts.
Exit 0 when all three pass.
"""
from __future__ import annotations

import importlib
import inspect
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENDOR_SRC = REPO / "vendor" / "tokkio-5.0.0-ga" / "src" / "llm-rag" / "src"


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="tokkio-ace-"))
    pkg = work / "src"
    shutil.copytree(VENDOR_SRC, pkg)
    shutil.copyfile(REPO / "tokkio-kyora" / "src" / "bot.kyora.py", pkg / "bot.py")
    shutil.copyfile(REPO / "tokkio-kyora" / "src" / "kyora_processor.py", pkg / "kyora_processor.py")
    cfg_path = work / "configs" / "config.yaml"
    cfg_path.parent.mkdir()
    shutil.copyfile(REPO / "tokkio-kyora" / "configs" / "config.kyora.yaml", cfg_path)
    # The vendored app resolves animation data relative to its working directory.
    vendor_root = VENDOR_SRC.parent
    for extra in ("configs",):
        src = vendor_root / extra
        if src.is_dir():
            for f in src.rglob("*"):
                if f.is_file() and f.name != "config.yaml":
                    dst = work / extra / f.relative_to(src)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(f, dst)
    os.chdir(work)
    sys.path.insert(0, str(work))
    os.environ.setdefault("CONFIG_PATH", str(cfg_path))
    os.environ.setdefault("OTEL_SDK_DISABLED", "true")
    os.environ.setdefault("REDIS_URL", "redis://localhost:6379")

    ok = True
    import yaml
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    try:
        from src.config import Config
        Config(**raw)
        print(f"[OK  ] config.kyora.yaml validates with Tokkio's Config ({len(raw)} sections; "
              f"NvidiaLLMService.model={raw.get('NvidiaLLMService', {}).get('model')!r})")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"[FAIL] config.kyora.yaml rejected by Tokkio's Config: {type(exc).__name__}: {exc}")

    try:
        bot = importlib.import_module("src.bot")
        print(f"[OK  ] patched bot.py imports with the real libraries (app={type(bot.app).__name__})")
        src = inspect.getsource(bot.create_pipeline_task)
        inserted = "KyoraProcessor(" in src and ".attach_task(" in src
        print(f"[{'OK  ' if inserted else 'FAIL'}] create_pipeline_task inserts KyoraProcessor and attaches the task")
        ok &= inserted
    except Exception as exc:  # noqa: BLE001
        ok = False
        import traceback
        traceback.print_exc(limit=6)
        print(f"[FAIL] patched bot.py does not import: {type(exc).__name__}: {exc}")

    import importlib.metadata as md
    print(f"pipecat-ai {md.version('pipecat-ai')}, nvidia-pipecat {md.version('nvidia-pipecat')}, "
          f"python {sys.version.split()[0]}")
    print("TOKKIO BOT IMPORT CHECK " + ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
