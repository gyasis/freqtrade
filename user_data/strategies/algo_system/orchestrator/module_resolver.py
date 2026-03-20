"""
orchestrator/module_resolver.py
Dynamic filesystem class loader for IAlgoModule implementations.
Mirrors freqtrade's IResolver pattern (importlib + inspect.getmembers).
"""
from __future__ import annotations

import importlib.util
import inspect
import logging
from pathlib import Path
from typing import List, Type

logger = logging.getLogger("algo_system.resolver")


class ModuleResolver:
    """
    Discovers IAlgoModule subclasses by scanning a directory tree.

    Mirrors freqtrade's IResolver._get_valid_object pattern:
    1. Walk all .py files under modules_path
    2. Load each as a module spec
    3. Inspect members for IAlgoModule subclasses
    4. Return valid, non-abstract classes
    """

    def discover_modules(self, modules_path: str) -> List[Type]:
        """
        Scan modules_path recursively for IAlgoModule subclasses.
        Returns list of concrete (non-abstract) class objects.
        Does NOT instantiate them.
        """
        from ..base.ialgo_module import IAlgoModule

        found: List[Type] = []
        base_path = Path(modules_path)

        if not base_path.exists():
            logger.warning("modules_path does not exist: %s", modules_path)
            return found

        for py_file in sorted(base_path.rglob("*.py")):
            if py_file.name.startswith("_"):
                continue  # skip __init__.py etc.
            try:
                mod = self._load_module_from_file(py_file)
                if mod is None:
                    continue
                for _name, obj in inspect.getmembers(mod, inspect.isclass):
                    if (
                        obj is not IAlgoModule
                        and issubclass(obj, IAlgoModule)
                        and not inspect.isabstract(obj)
                        and hasattr(obj, "module_id")
                    ):
                        found.append(obj)
                        logger.debug(
                            "Discovered module class: %s from %s", obj.module_id, py_file
                        )
            except Exception as exc:
                logger.warning("Error scanning %s: %s", py_file, exc)

        logger.info(
            "ModuleResolver: discovered %d module class(es) in %s", len(found), modules_path
        )
        return found

    def _load_module_from_file(self, py_file: Path):
        """Load a Python file as a module. Returns None on failure."""
        try:
            module_name = (
                f"_lats_discovered.{py_file.stem}_{hash(str(py_file)) & 0xFFFF:04x}"
            )
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec is None or spec.loader is None:
                return None
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            return mod
        except Exception as exc:
            logger.debug("Could not load %s: %s", py_file, exc)
            return None
