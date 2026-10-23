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

"""Connector settings (`options`), `check_options`, the optional `close`, and run_domain
closing every connector it builds."""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness2.core import memory, schedule
from harness2.core.tree import SurfaceSpec
from harness2.demo import DemoAdapter, DemoProposer

SURFACE = SurfaceSpec(
    name="test",
    always_on=("AGENTS.md",),
    always_on_max=1000,
    always_on_cap_reason="test",
    skill_dir="skills",
    skill_name_rule="test",
    trigger_hint="test",
    autotrigger_file="AGENTS.md",
    trigger_line="test",
    subagent_requires_mode=False,
    plugin_dir=None,
    hooks_file=None,
)

DOMAIN = "accounting"
K = 1


class RecordingConnector:
    """A MemoryConnector that records its option keywords, close calls, and close error."""

    def __init__(self, root=None, domain="d", surface=None, **options):
        self.root = root
        self.domain = domain
        self.surface = surface
        self.options = dict(options)
        self.closed = 0
        self.close_error = None

    def entries(self):
        return []

    def retain(self, task_ids):
        return None

    def add(self, task_id, harness, **kwargs):
        return None

    def context_dir(self):
        return None

    def close(self):
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


class NoCloseConnector:
    """A MemoryConnector with no close method."""

    domain = "d"

    def entries(self):
        return []

    def retain(self, task_ids):
        return None

    def add(self, task_id, harness, **kwargs):
        return None

    def context_dir(self):
        return None


class NoneCloseConnector(NoCloseConnector):
    """A MemoryConnector whose close attribute is None, so there is nothing to call."""

    close = None


def url_factory(root=None, domain="d", surface=None, url=None):
    """A factory with one named option, for check_options and module:Attr lookup."""
    return RecordingConnector(root=root, domain=domain, surface=surface, url=url)


def strict_factory(root=None, domain="d", surface=None):
    """A factory that takes no option beyond the three harness2 supplies."""
    return RecordingConnector(root=root, domain=domain, surface=surface)


def any_options_factory(root=None, domain="d", surface=None, **options):
    """A factory with `**kwargs`, so it takes any option."""
    return RecordingConnector(root=root, domain=domain, surface=surface, **options)


class UnreadableSignature:
    """A callable whose signature cannot be read."""

    @property
    def __signature__(self):
        raise ValueError("no signature available")

    def __call__(self, **kwargs):
        return RecordingConnector(**kwargs)


class FakeEntryPoint:
    def __init__(self, name, target):
        self.name = name
        self._target = target

    def load(self):
        return self._target


class BaseTestCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def register(self, name, factory):
        memory.register(name, factory)
        self.addCleanup(memory._REGISTRY.pop, name, None)


class OptionsTests(BaseTestCase):
    def test_options_reach_the_factory(self):
        self.register("opts", any_options_factory)
        conn = memory.connector_for(
            self.root,
            "dom",
            SURFACE,
            name="opts",
            env={},
            options={"url": "x", "token": "t"},
        )
        self.assertEqual(conn.options, {"url": "x", "token": "t"})

    def test_no_options_calls_the_factory_as_before(self):
        self.register("opts", strict_factory)
        conn = memory.connector_for(self.root, "dom", SURFACE, name="opts", env={})
        self.assertEqual(conn.options, {})
        self.assertEqual(conn.root, self.root)
        self.assertEqual(conn.domain, "dom")
        self.assertIs(conn.surface, SURFACE)

    def test_reserved_option_keys_raise(self):
        self.register("opts", any_options_factory)
        for key in ("root", "domain", "surface"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError) as caught:
                    memory.connector_for(
                        self.root, "dom", SURFACE, name="opts", env={}, options={key: "x"}
                    )
                self.assertIn(key, str(caught.exception))


class CheckOptionsTests(BaseTestCase):
    def test_accepts_keys_the_factory_takes(self):
        self.register("url", url_factory)
        memory.check_options("url", {"url": "x"})
        memory.check_options("url", {})

    def test_rejects_unknown_keys_sorted_and_named(self):
        self.register("url", url_factory)
        with self.assertRaises(ValueError) as caught:
            memory.check_options("url", {"zeta": 1, "alpha": 2})
        message = str(caught.exception)
        self.assertIn("url", message)
        self.assertIn("alpha", message)
        self.assertIn("zeta", message)
        self.assertLess(message.index("alpha"), message.index("zeta"))

    def test_kwargs_factory_takes_anything(self):
        self.register("any", any_options_factory)
        memory.check_options("any", {"url": "x", "token": "y"})

    def test_reserved_keys_raise(self):
        self.register("any", any_options_factory)
        for key in ("root", "domain", "surface"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError) as caught:
                    memory.check_options("any", {key: "x"})
                self.assertIn(key, str(caught.exception))

    def test_unreadable_signature_is_not_checked(self):
        memory.register("opaque", UnreadableSignature())
        self.addCleanup(memory._REGISTRY.pop, "opaque", None)
        memory.check_options("opaque", {"anything": 1})

    def test_same_result_for_registered_entry_point_and_import_path(self):
        import_path = f"{__name__}:url_factory"
        self.register("url", url_factory)
        patcher = patch.object(
            memory, "_entry_points", lambda: [FakeEntryPoint("ext", url_factory)]
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("url", "ext", import_path):
            with self.subTest(name=name):
                memory.check_options(name, {"url": "x"})
                with self.assertRaises(ValueError) as caught:
                    memory.check_options(name, {"nope": 1})
                message = str(caught.exception)
                self.assertIn(name, message)
                self.assertIn("nope", message)


class CloseTests(BaseTestCase):
    def test_calls_the_method_once(self):
        conn = RecordingConnector()
        memory.close(conn)
        self.assertEqual(conn.closed, 1)

    def test_without_a_method_does_nothing(self):
        memory.close(NoCloseConnector())

    def test_none_does_nothing(self):
        memory.close(NoneCloseConnector())

    def test_non_callable_close_raises_type_error(self):
        conn = NoneCloseConnector()
        conn.close = 42
        with self.assertRaises(TypeError) as caught:
            memory.close(conn)
        self.assertIn("close", str(caught.exception))


class RunDomainOptionsTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.adapter = DemoAdapter(self.root / "run")
        self.proposer = DemoProposer(self.adapter.spec.channels["proposer"])
        env = patch.dict(os.environ, {})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(memory.ENV_VAR, None)

    def factory(self, made, *, close_error_for=()):
        def build(root=None, domain="d", surface=None, **options):
            conn = RecordingConnector(root=root, domain=domain, surface=surface, **options)
            if Path(root).name in close_error_for:
                conn.close_error = RuntimeError(f"close {Path(root).name}")
            made.append(conn)
            return conn

        return build

    def run_domain(self, task_ids, *, memory_options=None):
        return schedule.run_domain(
            DOMAIN,
            task_ids,
            K,
            adapter=self.adapter,
            prop=self.proposer,
            runs_root=self.adapter.spec.runs_root,
            library_root=self.root / "library",
            memory_options=memory_options,
        )

    def test_options_reach_factories_and_every_mode_is_closed_once(self):
        made = []
        self.register("recording", self.factory(made))
        with patch.dict(os.environ, {memory.ENV_VAR: "recording"}):
            out = self.run_domain(self.adapter.tasks(), memory_options={"url": "x"})
        self.assertEqual((out["done"], out["failed"]), (len(self.adapter.tasks()), 0))
        self.assertEqual(len(made), len(schedule.MODES))
        self.assertEqual([conn.options for conn in made], [{"url": "x"}] * len(made))
        self.assertEqual([conn.closed for conn in made], [1] * len(made))

    def test_first_close_error_is_raised_after_closing_all(self):
        made = []
        self.register("recording", self.factory(made, close_error_for={"agg"}))
        with patch.dict(os.environ, {memory.ENV_VAR: "recording"}):
            with self.assertRaises(RuntimeError) as caught:
                self.run_domain([])
        self.assertIn("close agg", str(caught.exception))
        self.assertEqual([conn.closed for conn in made], [1] * len(made))

    def test_body_error_wins_and_created_connectors_are_closed(self):
        made = []
        self.register("recording", self.factory(made))
        real = memory.connector_for
        calls = []

        def flaky(*args, **kwargs):
            calls.append(args)
            if len(calls) == 2:
                raise RuntimeError("body boom")
            return real(*args, **kwargs)

        with patch.dict(os.environ, {memory.ENV_VAR: "recording"}):
            with patch.object(memory, "connector_for", flaky):
                with self.assertRaises(RuntimeError) as caught:
                    self.run_domain([])
        self.assertIn("body boom", str(caught.exception))
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0].closed, 1)

    def test_close_errors_are_reported_when_the_body_raises(self):
        made = []
        self.register("recording", self.factory(made, close_error_for={"agg"}))
        real = memory.connector_for
        calls = []

        def flaky(*args, **kwargs):
            calls.append(args)
            if len(calls) == 2:
                raise RuntimeError("body boom")
            return real(*args, **kwargs)

        stderr = io.StringIO()
        with patch.dict(os.environ, {memory.ENV_VAR: "recording"}):
            with patch.object(memory, "connector_for", flaky):
                with contextlib.redirect_stderr(stderr):
                    with self.assertRaises(RuntimeError) as caught:
                        self.run_domain([])
        self.assertIn("body boom", str(caught.exception))
        self.assertEqual(made[0].closed, 1)
        self.assertIn(
            "warning: memory connector close failed: close agg",
            stderr.getvalue(),
        )

    def test_run_sweep_passes_memory_options_through(self):
        seen = []

        def fake_run_domain(domain, task_ids, k, **kwargs):
            seen.append(kwargs.get("memory_options"))
            return {
                "domain": domain,
                "n_tasks": len(task_ids),
                "seed": kwargs.get("seed"),
                "done": 0,
                "failed": 0,
                "tasks": [],
            }

        with patch.object(schedule, "run_domain", fake_run_domain):
            out = schedule.run_sweep(
                1,
                adapter=self.adapter,
                prop=self.proposer,
                runs_root=self.adapter.spec.runs_root,
                library_root=self.root / "library",
                memory_options={"url": "x"},
            )
        self.assertEqual(out["n_domains"], 1)
        self.assertEqual(seen, [{"url": "x"}])


if __name__ == "__main__":
    unittest.main()
