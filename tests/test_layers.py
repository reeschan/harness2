# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The layers of harness2: each module imports only from its own unit and the layers below.

This file holds the one table of harness2's layers. A layer is a group of harness2's packages
that may import from the layers below it and never from the layers above it (GLOSSARY.md).
Two units in the same layer never import each other.

The check reads every `.py` file under `harness2/` with `ast`, so it sees imports inside
functions (lazy imports) and relative imports (`from ..evaluators import rules`) as well as the
imports at the top of a file.
"""

import ast
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

PACKAGE = "harness2"

# The layers, lowest first. Each unit is a package or a module directly under `harness2`;
# every module inside a unit package (for example any module added to `harness2.core`) belongs
# to that unit. A module may import from its own unit and from the units of lower layers.
LAYERS = (
    # The base: the package itself and the shared settings directly under it.
    ("base", ("harness2", "harness2.config")),
    # The method: scheduling, the trees, prompts, traces, the run graph and evaluation.
    ("core", ("harness2.core",)),
    # Built on core and independent of each other: the bundled evaluators, the executors, the
    # contract tests for connector authors, and the installation of benchmark tools and runner
    # overlays (which only reads the settings).
    (
        "evaluators, substrate and testing",
        ("harness2.evaluators", "harness2.substrate", "harness2.testing", "harness2.install"),
    ),
    # Built on the executors: the benchmark adapters, the local demo adapter used by
    # harness2-demo, and the readiness checks for benchmarks and their runtimes.
    ("benchmarks", ("harness2.benchmarks", "harness2.demo", "harness2.preflight")),
    # The harness2-* commands.
    ("cli", ("harness2.cli",)),
    # Optional plugins for code outside harness2; nothing in harness2 imports them.
    ("plugins", ("harness2.plugins",)),
)

ROOT_UNIT = PACKAGE


def _layer_of():
    """Map each unit to (index of its layer, name of its layer)."""
    found = {}
    for index, (name, units) in enumerate(LAYERS):
        for unit in units:
            found[unit] = (index, name)
    return found


def _unit_of(module, layer_of):
    """The unit a dotted module name under harness2 belongs to, or None if it has none."""
    parts = module.split(".")
    if parts[0] != PACKAGE:
        return None
    if len(parts) == 1:
        return ROOT_UNIT
    candidate = f"{PACKAGE}.{parts[1]}"
    return candidate if candidate in layer_of else None


def _module_name(path, root):
    """The dotted name of a file under `root` and whether it is a package's `__init__`."""
    parts = list(path.relative_to(root).with_suffix("").parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts = parts[:-1]
    return ".".join(parts), is_package


def _imported(node, module, is_package):
    """The dotted harness2 names one import statement brings in."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if node.level == 0:
        base = node.module or ""
    else:
        package = module.split(".") if is_package else module.split(".")[:-1]
        keep = len(package) - (node.level - 1)
        if keep <= 0:
            return []
        base = ".".join(package[:keep] + ([node.module] if node.module else []))
    # `from harness2 import config` imports the module harness2.config, so each name is
    # read as a possible submodule; a name that is not a unit falls back to its parent.
    return [f"{base}.{alias.name}" if alias.name != "*" else base for alias in node.names]


def check_layers(root):
    """Return one message per import that breaks the layer table, and per unassigned module.

    `root` is the directory that holds the `harness2` package.
    """
    root = Path(root)
    layer_of = _layer_of()
    problems = []
    for path in sorted((root / PACKAGE).rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        module, is_package = _module_name(path, root)
        source_unit = _unit_of(module, layer_of)
        if source_unit is None or (source_unit == ROOT_UNIT and module != PACKAGE):
            problems.append(f"{rel}: {module} is in no layer; add it to LAYERS")
            continue
        source_index, source_layer = layer_of[source_unit]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for target in _imported(node, module, is_package):
                target_unit = _unit_of(target, layer_of)
                if target_unit is None:
                    if target.split(".")[0] != PACKAGE:
                        continue
                    # An attribute of the package itself, such as `from harness2 import x`
                    # where x is not a unit: it comes from harness2/__init__.py.
                    target_unit = ROOT_UNIT
                if target_unit == source_unit:
                    continue
                target_index, target_layer = layer_of[target_unit]
                where = f"{rel}:{node.lineno} imports {target}"
                if target_index > source_index:
                    problems.append(
                        f"{where}: {target_unit} is in the {target_layer} layer, above "
                        f"{source_unit} in the {source_layer} layer"
                    )
                elif target_index == source_index:
                    problems.append(
                        f"{where}: {target_unit} and {source_unit} are both in the "
                        f"{source_layer} layer, whose units do not import each other"
                    )
    return problems


class LayerTests(unittest.TestCase):
    def test_harness2_follows_the_layers(self):
        problems = check_layers(REPO)
        self.assertEqual(problems, [], "\n".join(problems))

    def test_every_unit_in_the_table_exists(self):
        for _, units in LAYERS:
            for unit in units:
                parts = unit.split(".")
                package = REPO.joinpath(*parts)
                module = package.with_suffix(".py")
                with self.subTest(unit=unit):
                    self.assertTrue(
                        (package / "__init__.py").is_file() or module.is_file(),
                        f"{unit} is in LAYERS but not in the repository",
                    )

    def test_each_unit_is_in_one_layer(self):
        units = [unit for _, layer_units in LAYERS for unit in layer_units]
        self.assertEqual(len(units), len(set(units)))

    def test_plugins_and_the_top_level_modules_have_a_place(self):
        layer_of = _layer_of()
        for unit in (
            "harness2.config",
            "harness2.plugins",
            "harness2.demo",
            "harness2.install",
            "harness2.preflight",
        ):
            with self.subTest(unit=unit):
                self.assertIn(unit, layer_of)


class CheckTests(unittest.TestCase):
    """The check itself, on small trees written to a temporary directory."""

    def tree(self, files):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        packages = (
            "harness2",
            "harness2/core",
            "harness2/evaluators",
            "harness2/substrate",
            "harness2/testing",
            "harness2/benchmarks",
            "harness2/cli",
            "harness2/plugins",
        )
        for package in packages:
            (root / package).mkdir(parents=True, exist_ok=True)
            (root / package / "__init__.py").write_text("", encoding="utf-8")
        for name in ("config", "demo", "install", "preflight"):
            (root / "harness2" / f"{name}.py").write_text("", encoding="utf-8")
        for rel, text in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(text), encoding="utf-8")
        return root

    def assert_names(self, problems, rel, target):
        self.assertTrue(
            any(rel in problem and target in problem for problem in problems),
            f"no problem names {rel} and {target}: {problems}",
        )

    def test_a_clean_tree_passes(self):
        root = self.tree(
            {
                "harness2/core/graph.py": "from harness2 import config\nfrom . import trace\n",
                "harness2/core/trace.py": "",
                "harness2/evaluators/rules.py": "from ..core import graph\n",
                "harness2/cli/evaluate.py": "from harness2 import evaluators\n",
            }
        )
        self.assertEqual(check_layers(root), [])

    def test_a_lazy_relative_import_upward_fails_and_names_the_file(self):
        root = self.tree(
            {
                "harness2/core/evaluate.py": """
                    def _rules_factory(**options):
                        from ..evaluators.rules import RulesEvaluator

                        return RulesEvaluator(**options)
                """,
                "harness2/evaluators/rules.py": "class RulesEvaluator:\n    pass\n",
            }
        )
        problems = check_layers(root)
        self.assertEqual(len(problems), 1, problems)
        self.assert_names(problems, "harness2/core/evaluate.py:3", "harness2.evaluators.rules")
        self.assertIn("above", problems[0])

    def test_importing_a_package_relatively_from_its_parent_fails(self):
        root = self.tree({"harness2/core/evaluate.py": "from .. import evaluators\n"})
        self.assert_names(check_layers(root), "harness2/core/evaluate.py", "harness2.evaluators")

    def test_an_absolute_import_upward_fails(self):
        root = self.tree(
            {
                "harness2/core/report.py": "import harness2.cli.judge\n",
                "harness2/cli/judge.py": "",
            }
        )
        self.assert_names(check_layers(root), "harness2/core/report.py", "harness2.cli.judge")

    def test_the_base_does_not_import_core(self):
        root = self.tree({"harness2/config.py": "from harness2.core import tree\n"})
        self.assert_names(check_layers(root), "harness2/config.py", "harness2.core.tree")

    def test_evaluators_substrate_and_testing_do_not_import_each_other(self):
        root = self.tree(
            {
                "harness2/evaluators/rules.py": "from harness2.substrate import base\n",
                "harness2/substrate/base.py": "",
                "harness2/testing/evaluator_contract.py": """
                    def make():
                        from harness2.evaluators import rules

                        return rules
                """,
            }
        )
        problems = check_layers(root)
        self.assertEqual(len(problems), 2, problems)
        self.assert_names(problems, "harness2/evaluators/rules.py", "harness2.substrate.base")
        self.assert_names(
            problems, "harness2/testing/evaluator_contract.py:3", "harness2.evaluators.rules"
        )
        self.assertTrue(all("both in the" in problem for problem in problems), problems)

    def test_benchmarks_may_use_substrate(self):
        root = self.tree(
            {
                "harness2/benchmarks/lab.py": (
                    "from harness2.substrate.opencode import OpencodeSubstrate\n"
                    "from harness2 import install\n"
                ),
                "harness2/substrate/opencode.py": "class OpencodeSubstrate:\n    pass\n",
                "harness2/preflight.py": "from harness2.substrate import opencode\n",
            }
        )
        self.assertEqual(check_layers(root), [])

    def test_substrate_does_not_import_benchmarks(self):
        root = self.tree(
            {
                "harness2/substrate/codex.py": "from ..benchmarks import tasks\n",
                "harness2/benchmarks/tasks.py": "",
            }
        )
        self.assert_names(check_layers(root), "harness2/substrate/codex.py", "harness2.benchmarks")

    def test_nothing_imports_plugins(self):
        root = self.tree({"harness2/cli/intake.py": "from harness2.plugins import mcp\n"})
        self.assert_names(check_layers(root), "harness2/cli/intake.py", "harness2.plugins.mcp")

    def test_a_new_module_in_a_unit_package_needs_no_table_entry(self):
        root = self.tree({"harness2/core/newly_added.py": "from . import graph\n"})
        (root / "harness2/core/graph.py").write_text("", encoding="utf-8")
        self.assertEqual(check_layers(root), [])

    def test_a_new_top_level_module_must_be_placed(self):
        root = self.tree({"harness2/unplaced.py": ""})
        self.assert_names(check_layers(root), "harness2/unplaced.py", "no layer")


if __name__ == "__main__":
    unittest.main()
