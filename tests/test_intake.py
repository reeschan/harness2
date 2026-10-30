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

"""TaskSpec checks, the dict round trip, and the connector registry of core.intake."""

import copy
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from harness2.core import intake
from harness2.core.intake import RawTask, TaskConnector, TaskSpec
from harness2.core.spec import BenchPrompts


class FakeConnector:
    """A connector that only records what it is handed."""

    def __init__(self, **options):
        self.options = options
        self.delivered: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def poll(self):
        return [RawTask(id="t1", title="one")]

    def fetch_files(self, raw, dest: Path) -> None:
        return None

    def deliver(self, task_id, deliverable_dir, summary) -> None:
        self.delivered.append(task_id)

    def fail(self, task_id, reason) -> None:
        self.failed.append((task_id, reason))


NOT_A_CONNECTOR = 42


def _minimal() -> dict:
    return {
        "task_id": "t-1",
        "domain": "law",
        "prompt": "Draft the memo.",
        "files": [],
        "deliverable": {"what": "a memo"},
        "prompts": {"work": "drafting legal memos"},
    }


def _full() -> dict:
    return {
        "task_id": "t-1",
        "domain": "law",
        "prompt": "Draft the memo.",
        "files": ["a.txt", "dir/b.pdf"],
        "deliverable": {"what": "a memo", "where": "memo"},
        "prompts": {
            "work": "drafting legal memos",
            "domain_word": "practice area",
            "deliverable_word": "memo",
            "yardstick": "a partner would sign it",
        },
        "private_criteria": ["cites the statute", "under two pages"],
    }


class ValidateTaskSpecTest(unittest.TestCase):
    def assertOneProblem(self, obj, *needles):
        problems = intake.validate_task_spec(obj)
        self.assertEqual(len(problems), 1, problems)
        for needle in needles:
            self.assertIn(needle, problems[0])

    def test_minimal_and_full_pass(self):
        self.assertEqual(intake.validate_task_spec(_minimal()), [])
        self.assertEqual(intake.validate_task_spec(_full()), [])

    def test_not_an_object(self):
        self.assertOneProblem(["x"], "must be a object", "array")

    def test_each_missing_required_key(self):
        for key in ("task_id", "domain", "prompt", "files", "deliverable", "prompts"):
            with self.subTest(key=key):
                d = _minimal()
                del d[key]
                self.assertOneProblem(d, "missing", repr(key))
        d = _minimal()
        del d["deliverable"]["what"]
        self.assertOneProblem(d, "deliverable", "missing", "'what'")
        d = _minimal()
        del d["prompts"]["work"]
        self.assertOneProblem(d, "prompts", "missing", "'work'")

    def test_wrong_types(self):
        cases = {
            "task_id": 3,
            "domain": None,
            "prompt": ["x"],
            "files": "a.txt",
            "deliverable": "memo",
            "prompts": "work",
            "private_criteria": "c",
        }
        for key, value in cases.items():
            with self.subTest(key=key):
                d = _minimal()
                d[key] = value
                self.assertOneProblem(d, key, "must be a")
        d = _minimal()
        d["files"] = ["a.txt", 7]
        self.assertOneProblem(d, "files[1]", "must be a string", "integer")
        d = _minimal()
        d["deliverable"]["where"] = 1
        self.assertOneProblem(d, "deliverable.where", "must be a string")
        d = _minimal()
        d["prompts"]["yardstick"] = False
        self.assertOneProblem(d, "prompts.yardstick", "boolean")

    def test_extra_keys(self):
        d = _minimal()
        d["score"] = 1
        self.assertOneProblem(d, "unexpected key", "'score'")
        d = _minimal()
        d["deliverable"]["format"] = "pdf"
        self.assertOneProblem(d, "deliverable", "'format'")
        d = _minimal()
        d["prompts"]["inv_levers"] = "x"
        self.assertOneProblem(d, "prompts", "'inv_levers'")

    def test_patterns_and_min_length(self):
        for bad in ("", "-lead", "has space", "a/b", "x" * 129, "ok\n"):
            with self.subTest(task_id=bad):
                d = _minimal()
                d["task_id"] = bad
                self.assertOneProblem(d, "task_id", "pattern")
        d = _minimal()
        d["domain"] = "a b"
        self.assertOneProblem(d, "domain", "pattern")
        d = _minimal()
        d["prompt"] = ""
        self.assertOneProblem(d, "prompt", "at least 1")

    def test_paths(self):
        for bad, needle in (
            ("/etc/passwd", "absolute"),
            ("C:/x.txt", "absolute"),
            ("../x.txt", "'..'"),
            ("a/../../x", "'..'"),
            ("a\\b.txt", "backslash"),
        ):
            with self.subTest(path=bad):
                d = _minimal()
                d["files"] = ["ok.txt", bad]
                self.assertOneProblem(d, "files[1]", needle)
                d = _minimal()
                d["deliverable"]["where"] = bad
                self.assertOneProblem(d, "deliverable.where", needle)
        d = _minimal()
        d["files"] = ["a..b.txt"]
        self.assertEqual(intake.validate_task_spec(d), [])

    def test_control_characters_in_paths(self):
        for bad, needle in (("a\x00b", "NUL character"), ("a\x1fb", "control character")):
            with self.subTest(path=bad):
                d = _minimal()
                d["files"] = [bad]
                self.assertOneProblem(d, "files[0]", needle)
                d = _minimal()
                d["deliverable"]["where"] = bad
                self.assertOneProblem(d, "deliverable.where", needle)

    def test_bench_prompts_rules(self):
        d = _minimal()
        d["prompts"]["work"] = "follow the rubric"
        self.assertOneProblem(d, "prompts:", "'rubric'")
        d = _minimal()
        d["prompts"]["yardstick"] = "a high score"
        self.assertOneProblem(d, "prompts:", "'score'")

    def test_many_problems_are_all_reported(self):
        d = {"task_id": 1, "extra": True, "files": ["/a"]}
        problems = intake.validate_task_spec(d)
        self.assertGreaterEqual(len(problems), 6, problems)


class RoundTripTest(unittest.TestCase):
    def test_from_dict_builds_spec(self):
        spec = intake.task_spec_from_dict(_full())
        self.assertIsInstance(spec, TaskSpec)
        self.assertEqual(spec.files, ("a.txt", "dir/b.pdf"))
        self.assertEqual(spec.deliverable_where, "memo")
        self.assertEqual(spec.prompts.domain_word, "practice area")
        self.assertEqual(spec.private_criteria, ("cites the statute", "under two pages"))

    def test_defaults(self):
        spec = intake.task_spec_from_dict(_minimal())
        self.assertEqual(spec.deliverable_where, "")
        self.assertEqual(spec.private_criteria, ())
        self.assertEqual(spec.prompts, BenchPrompts(work="drafting legal memos"))

    def test_round_trip(self):
        d = _full()
        self.assertEqual(
            intake.task_spec_to_dict(intake.task_spec_from_dict(d), include_private=True), d
        )
        filled = intake.task_spec_to_dict(
            intake.task_spec_from_dict(_minimal()), include_private=True
        )
        self.assertEqual(
            intake.task_spec_to_dict(intake.task_spec_from_dict(filled), include_private=True),
            filled,
        )
        self.assertEqual(filled["deliverable"]["where"], "")
        self.assertEqual(filled["prompts"]["domain_word"], "domain")
        self.assertEqual(filled["private_criteria"], [])

    def test_public_form_drops_private_criteria(self):
        public = intake.task_spec_to_dict(
            intake.task_spec_from_dict(_full()), include_private=False
        )
        self.assertNotIn("private_criteria", public)
        self.assertEqual(intake.validate_task_spec(public), [])
        self.assertEqual(intake.task_spec_from_dict(public).private_criteria, ())

    def test_from_dict_raises_with_every_problem(self):
        d = _minimal()
        d["task_id"] = "bad id"
        d["files"] = ["../x"]
        with self.assertRaises(ValueError) as cm:
            intake.task_spec_from_dict(d)
        self.assertIn("task_id", str(cm.exception))
        self.assertIn("files[0]", str(cm.exception))

    def test_specs_are_frozen(self):
        spec = intake.task_spec_from_dict(_minimal())
        with self.assertRaises(AttributeError):
            spec.task_id = "other"  # type: ignore[misc]
        raw = RawTask(id="r")
        self.assertEqual(raw.meta, {})
        with self.assertRaises(AttributeError):
            raw.id = "other"  # type: ignore[misc]


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self._saved = copy.copy(intake._REGISTRY)

    def tearDown(self):
        intake._REGISTRY.clear()
        intake._REGISTRY.update(self._saved)

    def test_fake_is_a_task_connector(self):
        self.assertIsInstance(FakeConnector(), TaskConnector)
        self.assertNotIsInstance(object(), TaskConnector)

    def test_default_is_local_and_lazy(self):
        self.assertEqual(intake.DEFAULT, "local")
        self.assertEqual(intake.ENV_VAR, "HARNESS2_TASK_CONNECTOR")
        self.assertEqual(intake.connector_name(env={}), "local")
        self.assertEqual(intake.connector_name(env={intake.ENV_VAR: ""}), "local")
        self.assertIn("local", intake.names())

    def test_import_does_not_load_local_connector(self):
        code = (
            "import sys, harness2.core.intake; print('harness2.core.intake_local' in sys.modules)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout.strip(), "False")

    def test_select_by_name(self):
        intake.register("fake", FakeConnector)
        conn = intake.connector_for(name="fake", env={intake.ENV_VAR: "nope"}, root="/r")
        self.assertIsInstance(conn, FakeConnector)
        self.assertEqual(conn.options, {"root": "/r"})
        self.assertEqual(intake.names(), sorted(["fake", *self._saved]))

    def test_select_by_env(self):
        intake.register("fake", FakeConnector)
        conn = intake.connector_for(env={intake.ENV_VAR: "fake"})
        self.assertIsInstance(conn, FakeConnector)
        self.assertEqual(intake.connector_name(env={intake.ENV_VAR: "fake"}), "fake")

    def test_import_path(self):
        conn = intake.connector_for(name=f"{__name__}:FakeConnector", x=1)
        self.assertIsInstance(conn, TaskConnector)
        self.assertEqual(conn.options, {"x": 1})

    def test_unknown_name(self):
        with self.assertRaises(ValueError) as cm:
            intake.connector_for(name="nope")
        self.assertIn("unknown task connector 'nope'", str(cm.exception))
        self.assertIn("local", str(cm.exception))

    def test_broken_import_paths(self):
        for bad in ("no_such_module_xyz:Thing", f"{__name__}:Missing", "a:b:c", ":X", "m:"):
            with self.subTest(name=bad):
                with self.assertRaises(ValueError) as cm:
                    intake.connector_for(name=bad)
                self.assertIn("task connector", str(cm.exception))

    def test_not_callable_import_target(self):
        with self.assertRaises(TypeError):
            intake.connector_for(name=f"{__name__}:NOT_A_CONNECTOR")

    def test_factory_result_not_a_connector(self):
        intake.register("bad", lambda **options: object())
        with self.assertRaises(TypeError) as cm:
            intake.connector_for(name="bad")
        self.assertIn("not a TaskConnector", str(cm.exception))


class FullConnector(FakeConnector):
    """A connector with every optional capability."""

    def __init__(self, **options):
        super().__init__(**options)
        self.workspace_policy = intake.WorkspacePolicy(
            exclude=(".tasks",), guard=lambda worktree, source: None
        )
        self.scrub_env = ["SECRET_KEY", "OTHER_TOKEN"]
        self.closed = 0

    def _judge(self, run_dir, task_id, criteria):
        return {"n_passed": 1, "n_criteria": 1, "all_pass": True}

    def judge(self):
        return self._judge

    def report(self, task_id):
        return f"{task_id}: ok"

    def close(self):
        self.closed += 1


class CapabilitiesTest(unittest.TestCase):
    def test_defaults_for_a_plain_connector(self):
        caps = intake.capabilities(FakeConnector())
        self.assertEqual(caps.workspace_policy, intake.WorkspacePolicy())
        self.assertEqual(caps.workspace_policy.exclude, ())
        self.assertIsNone(caps.workspace_policy.guard)
        self.assertEqual(caps.scrub_env, ())
        self.assertIsNone(caps.judge)
        self.assertIsNone(caps.report)
        self.assertIsNone(caps.close())

    def test_none_attributes_count_as_missing(self):
        conn = FakeConnector()
        conn.workspace_policy = None
        conn.scrub_env = None
        caps = intake.capabilities(conn)
        self.assertEqual(caps.workspace_policy, intake.WorkspacePolicy())
        self.assertEqual(caps.scrub_env, ())

    def test_every_capability_is_read(self):
        conn = FullConnector()
        caps = intake.capabilities(conn)
        self.assertIs(caps.workspace_policy, conn.workspace_policy)
        self.assertEqual(caps.workspace_policy.exclude, (".tasks",))
        self.assertEqual(caps.scrub_env, ("SECRET_KEY", "OTHER_TOKEN"))
        result = caps.judge(Path("/r"), "t1", ["c"])
        self.assertEqual(result, {"n_passed": 1, "n_criteria": 1, "all_pass": True})
        self.assertEqual(caps.report("t1"), "t1: ok")
        caps.close()
        self.assertEqual(conn.closed, 1)

    def test_base_harness_is_read_once(self):
        conn = FakeConnector()
        calls = []
        conn.base_harness = lambda: calls.append(1) or {"systemprompt.md": "Work like this.\n"}
        caps = intake.capabilities(conn)
        self.assertEqual(caps.base_harness, {"systemprompt.md": "Work like this.\n"})
        self.assertEqual(calls, [1])
        self.assertIsNone(intake.capabilities(FakeConnector()).base_harness)
        conn.base_harness = lambda: None
        self.assertIsNone(intake.capabilities(conn).base_harness)

    def test_base_harness_of_the_wrong_type_raises(self):
        for value, word in (
            (lambda: ["systemprompt.md"], "list"),
            (lambda: {"systemprompt.md": 1}, "int"),
            ("text", "str"),
        ):
            conn = FakeConnector()
            conn.base_harness = value
            with self.subTest(word=word), self.assertRaises(TypeError) as ctx:
                intake.capabilities(conn)
            self.assertIn("base_harness", str(ctx.exception))
            self.assertIn(word, str(ctx.exception))

    def test_judge_method_may_return_none(self):
        conn = FakeConnector()
        conn.judge = lambda: None
        self.assertIsNone(intake.capabilities(conn).judge)

    def test_wrong_types_raise(self):
        cases = {
            "workspace_policy": ("workspace_policy", {"exclude": ("x",)}, "dict"),
            "scrub_env str": ("scrub_env", "SECRET", "str"),
            "scrub_env int": ("scrub_env", 3, "int"),
            "scrub_env item": ("scrub_env", ["OK", 1], "int"),
            "judge": ("judge", 1, "int"),
            "judge result": ("judge", lambda: "not callable", "str"),
            "report": ("report", "text", "str"),
            "close": ("close", 0, "int"),
            "exclude": ("workspace_policy", intake.WorkspacePolicy(exclude=["a"]), "list"),
            "guard": ("workspace_policy", intake.WorkspacePolicy(guard="g"), "str"),
        }
        for label, (attr, value, type_name) in cases.items():
            with self.subTest(case=label):
                conn = FakeConnector()
                setattr(conn, attr, value)
                with self.assertRaises(TypeError) as cm:
                    intake.capabilities(conn)
                self.assertIn(attr, str(cm.exception))
                self.assertIn(type_name, str(cm.exception))


class FakeEntryPoint:
    def __init__(self, name, target):
        self.name = name
        self._target = target
        self.loaded = 0

    def load(self):
        self.loaded += 1
        return self._target


class EntryPointTest(unittest.TestCase):
    def setUp(self):
        self._saved = copy.copy(intake._REGISTRY)

    def tearDown(self):
        intake._REGISTRY.clear()
        intake._REGISTRY.update(self._saved)

    def _patch(self, *eps):
        calls = []

        def entry_points(**kwargs):
            calls.append(kwargs)
            return list(eps)

        patcher = mock.patch("importlib.metadata.entry_points", entry_points)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_group_name(self):
        self.assertEqual(intake.ENTRY_POINT_GROUP, "harness2.task_connectors")

    def test_resolve_by_entry_point_name(self):
        calls = self._patch(FakeEntryPoint("ext", FakeConnector))
        self.assertIs(intake.resolve_factory("ext"), FakeConnector)
        self.assertEqual(calls[0], {"group": intake.ENTRY_POINT_GROUP})
        conn = intake.connector_for(name="ext", root="/r")
        self.assertIsInstance(conn, FakeConnector)
        self.assertEqual(conn.options, {"root": "/r"})
        conn = intake.connector_for(env={intake.ENV_VAR: "ext"})
        self.assertIsInstance(conn, FakeConnector)
        self.assertIn("ext", intake.names())
        self.assertIn("local", intake.names())

    def test_builtin_registration_comes_first(self):
        ep = FakeEntryPoint("fake", FullConnector)
        self._patch(ep)
        intake.register("fake", FakeConnector)
        self.assertIs(intake.resolve_factory("fake"), FakeConnector)
        self.assertNotIsInstance(intake.connector_for(name="fake"), FullConnector)
        self.assertEqual(ep.loaded, 0)
        self.assertEqual(intake.names().count("fake"), 1)

    def test_entry_point_comes_before_import_path(self):
        self._patch(FakeEntryPoint(f"{__name__}:FakeConnector", FullConnector))
        self.assertIs(intake.resolve_factory(f"{__name__}:FakeConnector"), FullConnector)

    def test_import_path_still_works(self):
        self._patch(FakeEntryPoint("ext", FullConnector))
        self.assertIs(intake.resolve_factory(f"{__name__}:FakeConnector"), FakeConnector)

    def test_unknown_name_lists_registered_and_entry_points(self):
        self._patch(FakeEntryPoint("ext", FakeConnector))
        for call in (
            lambda: intake.resolve_factory("nope"),
            lambda: intake.connector_for(name="nope"),
        ):
            with self.assertRaises(ValueError) as cm:
                call()
            self.assertIn("unknown task connector 'nope'", str(cm.exception))
            self.assertIn("local", str(cm.exception))
            self.assertIn("ext", str(cm.exception))

    def test_entry_point_errors(self):
        class Broken:
            name = "broken"

            def load(self):
                raise ImportError("no module")

        self._patch(Broken(), FakeEntryPoint("number", 42))
        with self.assertRaises(ValueError):
            intake.connector_for(name="broken")
        with self.assertRaises(TypeError):
            intake.connector_for(name="number")

    def test_only_local_is_built_in(self):
        self._patch()
        self.assertEqual(intake.names(), ["local"])
        self.assertEqual(sorted(intake._REGISTRY), ["local"])


if __name__ == "__main__":
    unittest.main()
