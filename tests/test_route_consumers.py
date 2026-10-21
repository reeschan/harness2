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

"""Generated files, run identity and the bridge default follow the role -> route table."""

import argparse
import dataclasses
import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from harness2 import config, install
from harness2.cli import bridge, run_sweep


def _routes(**changes) -> dict:
    """Replacement routes for the given roles, built from the current ones."""
    return {
        role: dataclasses.replace(config.route(role), **fields) for role, fields in changes.items()
    }


class _Named:
    def __init__(self, name):
        self.name = name


class _FakeAdapter:
    def __init__(self, substrate):
        self.substrate = _Named(substrate)
        self.spec = _Named("fake")

    def tasks(self, split):
        return ["t2", "t1"]

    def domain_of(self, task):
        return "d1"


class _Parsed(Exception):
    pass


class RouteConsumersTest(unittest.TestCase):
    def test_configure_codex_reads_executor_cx(self):
        routes = _routes(
            **{"executor.cx": {"model": "model-cx-a", "endpoint": "http://route.invalid/v1"}}
        )
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            with (
                patch.dict(config.ROLES, routes),
                patch.object(config, "CODEX_HOME_DIR", home),
                patch.dict(os.environ, {"HARNESS2_CODEX_BASE_URL": "http://env.invalid/v1"}),
            ):
                install.configure_codex()
                data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
                self.assertIsNone(install.codex_config_error())
        self.assertEqual(data["model"], "model-cx-a")
        provider = data["model_providers"][data["model_provider"]]
        self.assertEqual(provider["base_url"], "http://route.invalid/v1")

    def test_check_identity_reads_routes(self):
        routes = _routes(
            **{
                "executor.oc": {"model": "prov/model-oc-a"},
                "executor.cx": {"model": "model-cx-a", "endpoint": "http://route.invalid/v1"},
                "improver.opencode": {"model": "prov/model-imp-a"},
                "judge.wb": {"model": "model-judge-a"},
                "judge.jb": {"endpoint": "http://judge.invalid/v1"},
            }
        )
        found = {}
        for sub in ("oc", "cx"):
            args = argparse.Namespace(
                substrate=sub, split="all", seed=None, only_domains="", max_tasks=None, k=1
            )
            with tempfile.TemporaryDirectory() as tmp, patch.dict(config.ROLES, routes):
                ns = Path(tmp) / "ns"
                run_sweep._check_identity(_FakeAdapter(sub), args, ns)
                found[sub] = json.loads((ns / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(found["oc"]["executor"], "prov/model-oc-a")
        self.assertEqual(found["cx"]["executor"], "model-cx-a")
        self.assertEqual(found["oc"]["endpoint"], "")
        self.assertEqual(found["cx"]["endpoint"], "http://route.invalid/v1")
        for sub in ("oc", "cx"):
            self.assertEqual(found[sub]["judge"], "model-judge-a")
            self.assertEqual(found[sub]["improver"], "prov/model-imp-a")
            self.assertEqual(found[sub]["judge_endpoint"], "http://judge.invalid/v1")

    def test_bridge_model_default_reads_executor_cx(self):
        original = argparse.ArgumentParser.parse_args

        def grab(self, args=None, namespace=None):
            raise _Parsed(original(self, [], namespace))

        routes = _routes(**{"executor.cx": {"model": "model-cx-b"}})
        with (
            patch.dict(config.ROLES, routes),
            patch.object(argparse.ArgumentParser, "parse_args", grab),
            self.assertRaises(_Parsed) as caught,
        ):
            bridge.main([])
        self.assertEqual(caught.exception.args[0].model, "model-cx-b")


if __name__ == "__main__":
    unittest.main()
