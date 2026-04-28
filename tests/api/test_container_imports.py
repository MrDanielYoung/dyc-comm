"""Regression test for the container-style package layout.

The Docker image (apps/api/Dockerfile) copies ``apps/api/app`` into
``/app/app`` and runs ``uvicorn app.main:app`` from ``WORKDIR /app``. In
that environment the API package is importable as ``app``, not
``apps.api.app``. Any absolute ``from apps.api.app ...`` import in
runtime code therefore crashes the container at startup with
``ModuleNotFoundError: No module named 'apps'``.

This test simulates the container layout by:

1. Removing the repository root from ``sys.path`` so the
   ``apps.api.app`` import path is not resolvable.
2. Adding ``apps/api`` to ``sys.path`` so the package is importable as
   ``app`` (matching the container's ``/app`` WORKDIR + ``COPY app
   ./app`` layout).
3. Importing ``app.main`` (and ``app.cli``) and asserting they load.

It will fail if a runtime module re-introduces an absolute
``apps.api.app`` import that breaks container startup.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
API_DIR = REPO_ROOT / "apps" / "api"


def _resolved_path_entries() -> set[str]:
    candidates = {str(REPO_ROOT), str(REPO_ROOT) + "/", "", "."}
    candidates.add(str(REPO_ROOT.resolve()))
    return candidates


def test_api_package_imports_under_container_layout() -> None:
    container_modules = (
        "app",
        "app.main",
        "app.classifier",
        "app.cli",
        "apps",
        "apps.api",
        "apps.api.app",
        "apps.api.app.main",
        "apps.api.app.classifier",
        "apps.api.app.cli",
    )
    saved_modules = {
        name: sys.modules.pop(name) for name in container_modules if name in sys.modules
    }
    saved_path = list(sys.path)

    drop = _resolved_path_entries()
    sys.path[:] = [p for p in sys.path if p not in drop]
    sys.path.insert(0, str(API_DIR))

    try:
        main_mod = importlib.import_module("app.main")
        assert hasattr(main_mod, "app"), "app.main must expose the FastAPI app"
        cli_mod = importlib.import_module("app.cli")
        assert cli_mod is not None
    finally:
        for name in container_modules:
            sys.modules.pop(name, None)
        sys.modules.update(saved_modules)
        sys.path[:] = saved_path
