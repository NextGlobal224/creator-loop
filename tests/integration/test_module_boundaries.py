"""Enforce independently importable services and presentation boundaries."""

import ast
import importlib
import pkgutil
import unittest
from pathlib import Path
from unittest.mock import patch

import creator_loop.backend


class ModuleBoundaryTests(unittest.TestCase):
    def test_backend_has_no_frontend_dependency(self):
        package = Path(creator_loop.backend.__file__).parent
        for path in package.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn("creator_loop.frontend", node.module or "", path)
                if isinstance(node, ast.Import):
                    self.assertFalse(
                        any(
                            "creator_loop.frontend" in alias.name
                            for alias in node.names
                        ),
                        path,
                    )

    def test_frontend_contains_no_sql_or_viral_evaluation(self):
        package = Path(creator_loop.backend.__file__).parents[1] / "frontend"
        for path in package.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    self.assertNotIn(
                        ast.unparse(node.func),
                        {"assess", "_connect_write", "open_readonly", "db.execute"},
                        path,
                    )

    def test_import_all_backends_starts_no_process_or_connection(self):
        with (
            patch("subprocess.Popen", side_effect=AssertionError("Process on import")),
            patch(
                "socket.create_connection",
                side_effect=AssertionError("Network on import"),
            ),
        ):
            for info in pkgutil.walk_packages(
                creator_loop.backend.__path__, creator_loop.backend.__name__ + "."
            ):
                importlib.import_module(info.name)

    def test_compatibility_import_is_same_implementation(self):
        old = importlib.import_module("creator_loop.drafts")
        new = importlib.import_module("creator_loop.backend.creator.drafts")
        self.assertIs(old, new)


if __name__ == "__main__":
    unittest.main()
