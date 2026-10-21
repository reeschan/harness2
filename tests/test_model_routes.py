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

"""The role -> route table in config agrees with the constants derived from it."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Every variable config reads for models. They are set to "" (= unset for env()) so a .env
# next to the package cannot fill them in through load_dotenv's setdefault.
CLEARED = (
    "HARNESS2_SOLVER_MODEL",
    "HARNESS2_CODEX_MODEL",
    "HARNESS2_IMPROVER_MODEL",
    "HARNESS2_JUDGE_MODEL",
    "HARNESS2_JUDGE_BASE_URL",
    "HARNESS2_JUDGE_API_KEY",
    "HARNESS2_CODEX_BASE_URL",
    "HARNESS2_CODEX_CONTAINER_URL",
    "HARNESS2_CODEX_API_KEY",
    "HARNESS2_VERTEX_PROJECT",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
)

CHILD = """
import json
from dataclasses import asdict
from harness2 import config
try:
    config.route("no.such.role")
    missing = "no error"
except ValueError as e:
    missing = "ValueError: " + str(e)
except Exception as e:
    missing = type(e).__name__
print(json.dumps({
    "roles": {k: asdict(v) for k, v in config.ROLES.items()},
    "reprs": [repr(v) for v in config.ROLES.values()],
    "route_is_table": all(config.route(k) is v for k, v in config.ROLES.items()),
    "missing": missing,
    "consts": {n: getattr(config, n) for n in (
        "SOLVER_MODEL_OC", "SOLVER_MODEL_CX", "IMPROVER_MODEL", "JB_JUDGE_MODEL",
        "JB_JUDGE_API_BASE", "LAB_JUDGE_MODEL", "CODEX_BASE_URL", "PROPOSER_MODEL_BY_CHANNEL",
    )},
}))
"""

ROLE_KEYS = {
    "executor.oc",
    "executor.cx",
    "improver.opencode",
    "improver.claude_cli",
    "judge.jb",
    "judge.lab",
    "judge.wb",
    "normalizer",
}

JUDGE_KEY = "dummy-judge-key-111"
CODEX_KEY = "dummy-codex-key-222"

COMBOS = {
    "empty": {},
    "vertex": {
        "HARNESS2_SOLVER_MODEL": "prov/model-a",
        "HARNESS2_JUDGE_MODEL": "gemini-dummy",
        "GOOGLE_CLOUD_PROJECT": "dummy-proj",
        "HARNESS2_JUDGE_API_KEY": JUDGE_KEY,
        "HARNESS2_CODEX_API_KEY": CODEX_KEY,
    },
    "split": {
        "HARNESS2_SOLVER_MODEL": "prov/model-a",
        "HARNESS2_IMPROVER_MODEL": "other/model-b",
        "HARNESS2_CODEX_MODEL": "model-c",
        "HARNESS2_JUDGE_MODEL": "claude-dummy",
        "GOOGLE_CLOUD_LOCATION": "dummy-region",
        "HARNESS2_CODEX_BASE_URL": "http://127.0.0.1:9/v1",
        "HARNESS2_CODEX_API_KEY": CODEX_KEY,
    },
    "judge_base": {
        "HARNESS2_SOLVER_MODEL": "prov/model-a",
        "HARNESS2_JUDGE_MODEL": "gemini-dummy",
        "GOOGLE_CLOUD_PROJECT": "dummy-proj",
        "HARNESS2_JUDGE_BASE_URL": "https://judge.example/v1",
        "HARNESS2_JUDGE_API_KEY": JUDGE_KEY,
    },
}


def load(values):
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("HARNESS2_") and not k.startswith("GOOGLE_CLOUD_")
    }
    env.update(dict.fromkeys(CLEARED, ""))
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


class ModelRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snaps = {name: load(values) for name, values in COMBOS.items()}

    def test_role_keys(self):
        for name, snap in self.snaps.items():
            with self.subTest(combo=name):
                self.assertEqual(set(snap["roles"]), ROLE_KEYS)
                self.assertTrue(snap["route_is_table"])

    def test_routes_match_constants(self):
        for name, snap in self.snaps.items():
            with self.subTest(combo=name):
                roles, consts = snap["roles"], snap["consts"]
                self.assertEqual(roles["executor.oc"]["model"], consts["SOLVER_MODEL_OC"])
                self.assertEqual(roles["executor.cx"]["model"], consts["SOLVER_MODEL_CX"])
                self.assertEqual(roles["executor.cx"]["endpoint"], consts["CODEX_BASE_URL"])
                self.assertEqual(roles["improver.opencode"]["model"], consts["IMPROVER_MODEL"])
                self.assertEqual(roles["judge.jb"]["model"], consts["JB_JUDGE_MODEL"])
                self.assertEqual(roles["judge.jb"]["endpoint"], consts["JB_JUDGE_API_BASE"])
                self.assertEqual(roles["judge.lab"]["model"], consts["LAB_JUDGE_MODEL"])
                self.assertEqual(
                    consts["PROPOSER_MODEL_BY_CHANNEL"],
                    {
                        "opencode": roles["improver.opencode"]["model"],
                        "claude_cli": roles["improver.claude_cli"]["model"],
                    },
                )

    def test_derived_values(self):
        split = self.snaps["split"]["consts"]
        self.assertEqual(split["SOLVER_MODEL_CX"], "model-c")
        self.assertEqual(split["IMPROVER_MODEL"], "other/model-b")
        self.assertEqual(split["LAB_JUDGE_MODEL"], "vertex/claude-dummy")
        self.assertEqual(split["CODEX_BASE_URL"], "http://127.0.0.1:9/v1")
        vertex = self.snaps["vertex"]["consts"]
        self.assertEqual(vertex["SOLVER_MODEL_CX"], "model-a")
        self.assertEqual(vertex["JB_JUDGE_MODEL"], "google/gemini-dummy")
        self.assertIn("/projects/dummy-proj/", vertex["JB_JUDGE_API_BASE"])
        base = self.snaps["judge_base"]["consts"]
        self.assertEqual(base["JB_JUDGE_MODEL"], "gemini-dummy")
        self.assertEqual(base["JB_JUDGE_API_BASE"], "https://judge.example/v1")

    def test_normalizer_has_no_model(self):
        for name, snap in self.snaps.items():
            with self.subTest(combo=name):
                self.assertEqual(snap["roles"]["normalizer"]["model"], "")

    def test_no_key_values_in_routes(self):
        for name, snap in self.snaps.items():
            with self.subTest(combo=name):
                text = "\n".join(snap["reprs"]) + json.dumps(snap["roles"])
                self.assertNotIn(JUDGE_KEY, text)
                self.assertNotIn(CODEX_KEY, text)
                self.assertEqual(snap["roles"]["judge.jb"]["auth_env"], "HARNESS2_JUDGE_API_KEY")
                self.assertEqual(snap["roles"]["executor.cx"]["auth_env"], "HARNESS2_CODEX_API_KEY")

    def test_unknown_role_is_value_error(self):
        for name, snap in self.snaps.items():
            with self.subTest(combo=name):
                self.assertTrue(snap["missing"].startswith("ValueError: "), snap["missing"])
                self.assertIn("normalizer", snap["missing"])


if __name__ == "__main__":
    unittest.main()
