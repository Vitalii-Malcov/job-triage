"""Architecture guard (Task 24): the `app` package keeps a layered, cycle-free import graph.

* no module-level import cycle (a cycle forces function-level imports and hidden ordering). Imports
  inside module-level `if` / `try` / `with` blocks count as module-level (they run on import);
  imports under `if TYPE_CHECKING:` are type-only and ignored; relative imports are resolved;
  literal `importlib.import_module("app.x")` / `__import__("app.x")` calls count as imports;
* function-level imports of `app` modules are a ratchet: the number of distinct deferred
  dependency edges may only go DOWN. A new one needs a reason (an optional dependency, a heavy
  import), not a cycle workaround;
* no module imports an unbounded number of other `app` modules at module level (orchestrators have
  a documented ceiling);
* the lease helpers stay a leaf: neither an eager NOR a deferred import of another `app.services`
  module.

The analysis is a pure function over `{module name: source}`, so the synthetic regression cases
below prove the guard itself would catch what it claims to.
"""

import ast
import collections
import pathlib

APP = pathlib.Path(__file__).resolve().parents[1] / "app"

# Distinct function-level `app` dependency edges that exist today. Lower this when one is removed;
# never raise it to hide a cycle.
LAZY_EDGE_CEILING = 13
# Distinct module-level `app` dependencies of the most connected module (an orchestrator).
FAN_OUT_CEILING = 22


def _package_of(name: str, is_package: bool) -> str:
    return name if is_package else name.rpartition(".")[0]


def analyze(modules: dict[str, str], packages: frozenset[str] = frozenset()):
    """`(eager, lazy)`: module -> set of imported `app` modules, by import time."""
    eager: dict[str, set[str]] = collections.defaultdict(set)
    lazy: dict[str, set[str]] = collections.defaultdict(set)

    class Visitor(ast.NodeVisitor):
        def __init__(self, name: str) -> None:
            self.name = name
            self.depth = 0  # >0: inside a function / lambda (deferred)
            self.type_only = 0

        def _record(self, target: str) -> None:
            if self.type_only or target not in modules or target == self.name:
                return
            (lazy if self.depth else eager)[self.name].add(target)

        def _function(self, node) -> None:
            self.depth += 1
            self.generic_visit(node)
            self.depth -= 1

        visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _function

        def visit_If(self, node: ast.If) -> None:
            test = ast.unparse(node.test)
            if test in {"TYPE_CHECKING", "typing.TYPE_CHECKING"}:
                self.type_only += 1
                for child in node.body:
                    self.visit(child)
                self.type_only -= 1
                for child in node.orelse:
                    self.visit(child)
            else:
                self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            """`importlib.import_module("app.x")` / `__import__("app.x")` with a literal name."""
            func = ast.unparse(node.func)
            if (
                func in {"importlib.import_module", "import_module", "__import__"}
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                self._record(node.args[0].value)
            self.generic_visit(node)

        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                self._record(alias.name)

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            base = node.module or ""
            if node.level:
                package = _package_of(self.name, self.name in packages)
                for _ in range(node.level - 1):
                    package = package.rpartition(".")[0]
                base = f"{package}.{base}" if base else package
            self._record(base)
            for alias in node.names:  # `from pkg import submodule`
                self._record(f"{base}.{alias.name}")

    for name, source in modules.items():
        Visitor(name).visit(ast.parse(source))
    return eager, lazy


def cycles(graph: dict[str, set[str]], modules) -> list[list[str]]:
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    found: list[list[str]] = []
    counter = [0]

    def visit(node: str) -> None:
        index[node] = low[node] = counter[0]
        counter[0] += 1
        stack.append(node)
        on_stack.add(node)
        for other in graph.get(node, ()):
            if other not in index:
                visit(other)
                low[node] = min(low[node], low[other])
            elif other in on_stack:
                low[node] = min(low[node], index[other])
        if low[node] == index[node]:
            component = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                component.append(member)
                if member == node:
                    break
            if len(component) > 1:
                found.append(sorted(component))

    for module in modules:
        if module not in index:
            visit(module)
    return found


def _load_app():
    modules: dict[str, str] = {}
    packages: set[str] = set()
    for path in APP.rglob("*.py"):
        name = ".".join(("app", *path.relative_to(APP).with_suffix("").parts))
        if name.endswith(".__init__"):
            name = name[: -len(".__init__")]
            packages.add(name)
        modules[name] = path.read_text(encoding="utf-8")
    return modules, frozenset(packages)


# ------------------------------------------------------------------ the real tree


def test_no_module_level_import_cycle():
    modules, packages = _load_app()
    eager, _lazy = analyze(modules, packages)
    assert cycles(eager, modules) == []


def test_function_level_app_dependencies_only_go_down():
    modules, packages = _load_app()
    _eager, lazy = analyze(modules, packages)
    edges = sum(len(targets) for targets in lazy.values())
    assert edges <= LAZY_EDGE_CEILING, (edges, {k: sorted(v) for k, v in lazy.items()})


def test_no_module_has_unbounded_fan_out():
    modules, packages = _load_app()
    eager, _lazy = analyze(modules, packages)
    worst = max(eager.items(), key=lambda item: len(item[1]))
    assert len(worst[1]) <= FAN_OUT_CEILING, (worst[0], len(worst[1]))


def test_the_run_lease_helpers_are_a_leaf_for_eager_and_deferred_imports():
    modules, packages = _load_app()
    eager, lazy = analyze(modules, packages)
    leaf = "app.services.run_leases"
    assert not {t for t in eager[leaf] | lazy[leaf] if t.startswith("app.services")}


# ------------------------------------------------------------------ the guard itself


def _synthetic(sources: dict[str, str], packages=()):
    return analyze(sources, frozenset(packages))


def test_a_cycle_through_relative_imports_is_detected():
    sources = {
        "app.pkg": "",
        "app.pkg.a": "from . import b\n",
        "app.pkg.b": "from .a import thing\n",
    }
    eager, _ = _synthetic(sources, packages={"app.pkg"})
    assert cycles(eager, sources) == [["app.pkg.a", "app.pkg.b"]]


def test_a_cycle_hidden_in_a_conditional_module_level_import_is_detected():
    sources = {
        "app.a": "try:\n    import app.b\nexcept ImportError:\n    pass\n",
        "app.b": "if True:\n    from app import a\n",
    }
    eager, lazy = _synthetic(sources)
    assert cycles(eager, sources) == [["app.a", "app.b"]]
    assert not lazy


def test_function_level_imports_are_deferred_not_eager():
    sources = {"app.a": "def f():\n    import app.b\n", "app.b": "import app.a\n"}
    eager, lazy = _synthetic(sources)
    assert cycles(eager, sources) == []
    assert lazy["app.a"] == {"app.b"}


def test_type_checking_imports_are_ignored():
    sources = {
        "app.a": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import app.b\n",
        "app.b": "import app.a\n",
    }
    eager, lazy = _synthetic(sources)
    assert cycles(eager, sources) == [] and not lazy


def test_a_leaf_violation_is_visible_for_eager_and_deferred_imports():
    sources = {
        "app.services.automation": "",
        "app.services.run_leases": (
            "from . import automation\n\ndef f():\n    import app.services.automation\n"
        ),
        "app.services": "",
    }
    eager, lazy = _synthetic(sources, packages={"app.services"})
    assert "app.services.automation" in eager["app.services.run_leases"]
    assert "app.services.automation" in lazy["app.services.run_leases"]


def test_importlib_calls_with_a_literal_name_are_import_edges():
    sources = {
        "app.a": 'import importlib\nb = importlib.import_module("app.b")\n',
        "app.b": "import app.a\n",
        "app.services.run_leases": (
            "import importlib\n"
            "\n"
            "def f():\n"
            '    return importlib.import_module("app.services.automation")\n'
        ),
        "app.services.automation": "",
    }
    eager, lazy = _synthetic(sources)
    assert cycles(eager, sources) == [["app.a", "app.b"]]  # eager through the literal call
    assert "app.services.automation" in lazy["app.services.run_leases"]  # deferred in a function


def test_a_non_literal_importlib_call_is_not_guessed():
    sources = {"app.a": "import importlib\nimportlib.import_module(NAME)\n", "app.b": ""}
    eager, lazy = _synthetic(sources)
    assert not eager and not lazy


def test_dunder_import_with_a_literal_name_is_an_import_edge_at_both_scopes():
    sources = {
        "app.a": '__import__("app.b")\n',
        "app.b": "import app.a\n",
        "app.services.run_leases": ('def f():\n    return __import__("app.services.automation")\n'),
        "app.services.automation": "",
    }
    eager, lazy = _synthetic(sources)
    assert cycles(eager, sources) == [["app.a", "app.b"]]
    assert "app.services.automation" in lazy["app.services.run_leases"]
    assert "app.services.automation" not in eager["app.services.run_leases"]
