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

"""Model connectors: resolution, capabilities, fence requirements and the default connector."""

import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness2.core import models

REPO = Path(__file__).resolve().parents[1]


class Minimal:
    def routes(self, role, *, task_id, substrate):
        return [models.ModelChoice(runner=substrate, model="m")]


class WithSession(Minimal):
    def run_session(self, role, request):
        return models.SessionResult(text="", ok=True)


class WithFences(WithSession):
    def fences(self):
        return {"claude": GOOD_FENCE}


class Full(WithFences):
    def record(self, role, task_id, choice, outcome):
        pass

    def close(self):
        pass


class FakeEntryPoint:
    def __init__(self, name, target):
        self.name = name
        self._target = target

    def load(self):
        return self._target


def declaration(**changes):
    base = {
        "capabilities": frozenset({"read", "write", "search"}),
        "workdir_only": True,
        "asks_human": False,
        "harness_credentials": False,
        "runner": "claude",
    }
    base.update(changes)
    return models.FenceDeclaration(**base)


GOOD_FENCE = declaration()


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        saved = dict(models._REGISTRY)
        self.addCleanup(lambda: (models._REGISTRY.clear(), models._REGISTRY.update(saved)))

    def test_default_name_and_env(self):
        self.assertEqual(models.connector_name(env={}), "env")
        self.assertEqual(models.connector_name(env={models.ENV_VAR: "x"}), "x")
        self.assertEqual(models.connector_name(name="y", env={models.ENV_VAR: "x"}), "y")

    def test_registered_wins_over_entry_point(self):
        models.register("dup", Minimal)
        eps = [FakeEntryPoint("dup", Full)]
        with mock.patch.object(models, "_entry_points", return_value=eps):
            self.assertIs(models.resolve_factory("dup"), Minimal)

    def test_entry_point(self):
        eps = [FakeEntryPoint("viaep", Full)]
        with mock.patch.object(models, "_entry_points", return_value=eps):
            self.assertIs(models.resolve_factory("viaep"), Full)
            self.assertIn("viaep", models.names())

    def test_entry_point_not_callable(self):
        eps = [FakeEntryPoint("bad", 3)]
        with mock.patch.object(models, "_entry_points", return_value=eps):
            with self.assertRaises(TypeError):
                models.resolve_factory("bad")

    def test_module_attr(self):
        factory = models.resolve_factory(f"{__name__}:Minimal")
        self.assertIs(factory, Minimal)

    def test_module_attr_errors(self):
        for name in ("nomodule_xyz:Attr", f"{__name__}:Missing", "a:b:c"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                models.resolve_factory(name)

    def test_unknown_name_lists_known(self):
        with self.assertRaises(ValueError) as ctx:
            models.resolve_factory("nope")
        self.assertIn("env", str(ctx.exception))

    def test_connector_for_uses_env_var_then_default(self):
        models.register("mine", Minimal)
        self.assertIsInstance(
            models.connector_for(env={models.ENV_VAR: "mine"}),
            Minimal,
        )
        self.assertIsInstance(models.connector_for(env={}), models.EnvModelConnector)

    def test_connector_for_passes_options(self):
        class Opt(Minimal):
            def __init__(self, flag=False):
                self.flag = flag

        models.register("opt", Opt)
        self.assertTrue(models.connector_for(name="opt", options={"flag": True}).flag)

    def test_connector_for_rejects_non_connector(self):
        models.register("junk", lambda: object())
        with self.assertRaises(TypeError):
            models.connector_for(name="junk")

    def test_check_options(self):
        def factory(a, *, b=1):
            return Minimal()

        models.check_options(factory, {"a": 1, "b": 2})
        with self.assertRaises(ValueError) as ctx:
            models.check_options(factory, {"c": 1, "a": 1})
        self.assertIn("['c']", str(ctx.exception))
        models.check_options(lambda **kw: Minimal(), {"anything": 1})


class CapabilitiesTests(unittest.TestCase):
    def test_minimal_gets_defaults(self):
        caps = models.capabilities(Minimal())
        self.assertIsNone(caps.run_session)
        self.assertIsNone(caps.record)
        self.assertEqual(caps.fences(), {})
        self.assertIsNone(caps.close())

    def test_full_reads_methods(self):
        conn = Full()
        caps = models.capabilities(conn)
        self.assertEqual(caps.run_session, conn.run_session)
        self.assertEqual(caps.record, conn.record)
        self.assertEqual(caps.fences(), {"claude": GOOD_FENCE})

    def test_run_session_without_fences_is_type_error(self):
        with self.assertRaises(TypeError):
            models.capabilities(WithSession())

    def test_fences_not_callable_is_type_error(self):
        conn = WithSession()
        conn.fences = {"claude": GOOD_FENCE}
        with self.assertRaises(TypeError):
            models.capabilities(conn)

    def test_none_counts_as_missing(self):
        conn = Minimal()
        conn.close = None
        self.assertIsNone(models.capabilities(conn).close())

    def test_protocol_needs_only_routes(self):
        self.assertIsInstance(Minimal(), models.ModelConnector)
        self.assertNotIsInstance(object(), models.ModelConnector)


class FenceTests(unittest.TestCase):
    def test_improver_fence_is_the_one_condition(self):
        limit = models.IMPROVER_FENCE
        self.assertEqual(limit.capabilities, frozenset({"read", "write", "search"}))
        self.assertTrue(limit.workdir_only)
        self.assertFalse(limit.asks_human)
        self.assertFalse(limit.harness_credentials)
        self.assertEqual(
            models.CAPABILITIES, ("read", "write", "search", "commands", "network", "mcp")
        )

    def test_satisfies_exact_and_stricter(self):
        self.assertTrue(models.satisfies(GOOD_FENCE))
        self.assertTrue(models.satisfies(declaration(capabilities=frozenset({"read"}))))
        self.assertTrue(models.satisfies(declaration(capabilities=frozenset())))
        self.assertTrue(models.satisfies(declaration(capabilities=("read", "write"))))

    def test_satisfies_false_cases(self):
        cases = {
            "commands": declaration(capabilities=frozenset({"read", "commands"})),
            "network": declaration(capabilities=frozenset({"read", "network"})),
            "mcp": declaration(capabilities=frozenset({"search", "mcp"})),
            "workdir_only false": declaration(workdir_only=False),
            "asks_human true": declaration(asks_human=True),
            "harness_credentials true": declaration(harness_credentials=True),
        }
        for label, decl in cases.items():
            with self.subTest(label):
                self.assertFalse(models.satisfies(decl))

    def test_runner_name_does_not_matter(self):
        for runner in ("claude", "opencode", "anything", ""):
            with self.subTest(runner):
                self.assertTrue(models.satisfies(declaration(runner=runner)))
                self.assertFalse(models.satisfies(declaration(runner=runner, asks_human=True)))

        class Names(WithSession):
            def fences(self):
                return {
                    "claude": GOOD_FENCE,
                    "opencode": GOOD_FENCE,
                    "anything": GOOD_FENCE,
                    "weak": declaration(workdir_only=False),
                }

        self.assertEqual(sorted(models.fenced_runners(Names())), ["anything", "claude", "opencode"])

    def test_fenced_runners_without_fences(self):
        self.assertEqual(models.fenced_runners(Minimal()), {})

    def test_fence_problems_each_item(self):
        cases = {
            "commands": declaration(capabilities=frozenset({"commands"})),
            "unknown capability": declaration(capabilities=frozenset({"read", "teleport"})),
            "workdir_only": declaration(workdir_only=False),
            "asks_human": declaration(asks_human=True),
            "harness_credentials": declaration(harness_credentials=True),
            "bool as 1": declaration(workdir_only=1),
            "capabilities str": declaration(capabilities=3),
            "capabilities content": declaration(capabilities=("read", 3)),
            "capabilities None": declaration(capabilities=None),
            "runner type": declaration(runner=None),
            "dict": dataclasses.asdict(GOOD_FENCE),
            "None": None,
        }
        self.assertEqual(models.fence_problems(GOOD_FENCE), [])
        for label, decl in cases.items():
            with self.subTest(label):
                self.assertTrue(models.fence_problems(decl))
                self.assertFalse(models.satisfies(decl))
        self.assertIn("commands", models.fence_problems(cases["commands"])[0])
        self.assertIn("teleport", models.fence_problems(cases["unknown capability"])[0])
        self.assertIn("workdir_only", models.fence_problems(cases["bool as 1"])[0])

    def test_fenced_runners_tolerates_bad_fences(self):
        class NotMapping(WithSession):
            def fences(self):
                return [("claude", GOOD_FENCE)]

        self.assertEqual(models.fenced_runners(NotMapping()), {})
        self.assertEqual(models.fence_report(NotMapping()), {})

        class BadValues(WithSession):
            def fences(self):
                return {"claude": dataclasses.asdict(GOOD_FENCE), "other": None}

        self.assertEqual(models.fenced_runners(BadValues()), {})

    def test_fence_report(self):
        class Mixed(WithSession):
            def fences(self):
                return {"claude": GOOD_FENCE, "other": GOOD_FENCE}

        self.assertEqual(models.fence_report(Mixed()), {"claude": [], "other": []})

        class Weak(WithSession):
            def fences(self):
                return {"opencode": declaration(workdir_only=False, asks_human=True)}

        problems = models.fence_report(Weak())["opencode"]
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("workdir_only" in p for p in problems))


class DataTests(unittest.TestCase):
    def test_roles(self):
        self.assertEqual(models.ROLES, (models.Role.EXECUTOR, models.Role.IMPROVER))

    def test_frozen_and_defaults(self):
        choice = models.ModelChoice("opencode", "p/m")
        self.assertEqual(choice.effort, "")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            choice.model = "x"
        self.assertEqual(models.Tokens(), models.Tokens(None, None))
        self.assertFalse(models.SessionResult(text="", ok=False).unavailable)
        self.assertEqual(models.Outcome(ok=True).run_id, "")


CHILD = """
import json
from harness2.core import models
out = {}
for substrate in ("opencode", "codex", "other"):
    out["executor." + substrate] = [
        [c.runner, c.model, c.effort]
        for c in models.EnvModelConnector().routes("executor", task_id="t", substrate=substrate)
    ]
out["improver"] = [
    [c.runner, c.model, c.effort]
    for c in models.EnvModelConnector().routes("improver", task_id="t", substrate="opencode")
]
caps = models.capabilities(models.EnvModelConnector())
out["caps"] = [caps.run_session is None, caps.record is None, caps.fences()]
print(json.dumps(out))
"""


def run_child(values):
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("HARNESS2_") and not k.startswith("GOOGLE_CLOUD_")
    }
    for key in (
        "HARNESS2_SOLVER_MODEL",
        "HARNESS2_CODEX_MODEL",
        "HARNESS2_IMPROVER_MODEL",
        "HARNESS2_IMPROVER_CHANNEL",
    ):
        env[key] = ""
    env.update(values)
    env["PYTHONPATH"] = str(REPO)
    with tempfile.TemporaryDirectory() as cwd:
        proc = subprocess.run(
            [sys.executable, "-c", CHILD],
            env=env,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return json.loads(proc.stdout)


class EnvConnectorTests(unittest.TestCase):
    def test_empty_environment_gives_no_candidates(self):
        out = run_child({})
        self.assertEqual(out["executor.opencode"], [])
        self.assertEqual(out["executor.codex"], [])
        self.assertEqual(out["improver"], [])

    def test_executor_and_improver_from_environment(self):
        out = run_child(
            {
                "HARNESS2_SOLVER_MODEL": "prov/model-a",
                "HARNESS2_CODEX_MODEL": "model-c",
                "HARNESS2_IMPROVER_MODEL": "other/model-b",
            }
        )
        self.assertEqual(out["executor.opencode"], [["opencode", "prov/model-a", ""]])
        self.assertEqual(out["executor.codex"], [["codex", "model-c", ""]])
        self.assertEqual(out["executor.other"], [])
        self.assertEqual(out["improver"], [["opencode", "other/model-b", ""]])

    def test_improver_channel_claude_cli(self):
        out = run_child(
            {"HARNESS2_IMPROVER_CHANNEL": "claude_cli", "HARNESS2_IMPROVER_MODEL": "opus"}
        )
        self.assertEqual(out["improver"], [["claude_cli", "opus", ""]])

    def test_no_optional_capabilities(self):
        self.assertEqual(run_child({})["caps"], [True, True, {}])


if __name__ == "__main__":
    unittest.main()
