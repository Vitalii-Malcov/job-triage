"""The `imported_modules` fixture (tests/conftest.py) sees every STATIC import form."""

import importlib
import sys

import pytest


@pytest.fixture()
def make_module(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))

    def make(files: dict[str, str], name: str):
        for relative, source in files.items():
            path = tmp_path / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
        for loaded in [m for m in sys.modules if m.split(".")[0] in {"fx_pkg", "fx_single"}]:
            del sys.modules[loaded]
        return importlib.import_module(name)

    return make


def test_plain_aliased_dotted_and_from_imports(imported_modules, make_module):
    module = make_module(
        {
            "fx_single.py": (
                '"""mentions httpx and urllib.request in prose only."""\n'
                "import os\n"
                "import urllib.request as ur\n"
                "from http import client as hc\n"
                "from collections.abc import Mapping\n"
            )
        },
        "fx_single",
    )
    imported = imported_modules(module)
    assert {"os", "urllib", "urllib.request", "http", "http.client", "collections.abc"} <= imported
    assert "httpx" not in imported  # a docstring mention is not an import


def test_relative_imports_are_resolved_against_the_package(imported_modules, make_module):
    module = make_module(
        {
            "fx_pkg/__init__.py": "",
            "fx_pkg/telegram.py": "class TelegramNotifier: ...\n",
            "fx_pkg/sub/__init__.py": "",
            "fx_pkg/sub/mod.py": (
                "from ..telegram import TelegramNotifier as Notifier\n"
                "from . import sibling\n"
                "from .sibling import thing\n"
            ),
            "fx_pkg/sub/sibling.py": "thing = 1\n",
        },
        "fx_pkg.sub.mod",
    )
    imported = imported_modules(module)
    assert "fx_pkg.telegram" in imported and "fx_pkg.telegram.TelegramNotifier" in imported
    assert {"fx_pkg.sub.sibling", "fx_pkg.sub.sibling.thing"} <= imported


def test_dynamic_imports_are_a_documented_blind_spot(imported_modules, make_module):
    module = make_module(
        {"fx_single.py": "import importlib\nclient = importlib.import_module('httpx')\n"},
        "fx_single",
    )
    imported = imported_modules(module)
    assert "importlib" in imported and "httpx" not in imported  # static scan only
