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

"""normalize_task's retry, the passthrough and model normalizers, and the registry."""

import json
import unittest
from unittest.mock import patch

from harness2 import config
from harness2.core import intake, normalize
from harness2.core.intake import RawTask, TaskSpec
from harness2.core.normalize import (
    ModelNormalizer,
    NormalizeError,
    Normalizer,
    PassthroughNormalizer,
    normalize_task,
    normalizer_for,
)
from harness2.core.proposer import Proposer, Reply
from harness2.core.spec import ChannelSpec


def good_spec(task_id: str = "t1") -> dict:
    return {
        "task_id": task_id,
        "domain": "accounting",
        "prompt": "Sum the ledger.",
        "files": ["ledger.csv"],
        "deliverable": {"what": "the total", "where": "total.txt"},
        "prompts": {"work": "reconcile a ledger"},
        "private_criteria": ["Total is 85.15"],
    }


def bad_spec() -> dict:
    spec = good_spec()
    del spec["prompt"]
    return spec


class ScriptedNormalizer:
    """Returns (or raises) the scripted answers in order and records each call."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[dict] = []

    def normalize(self, raw, *, domains, feedback=""):
        self.calls.append({"raw": raw, "domains": domains, "feedback": feedback})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeProposer(Proposer):
    """Replies with scripted text; no model service is used."""

    def __init__(self, *texts: str):
        super().__init__(ChannelSpec("opencode", "local/deterministic"))
        self.texts = list(texts)
        self.missions: list[str] = []
        self.labels: list[str] = []

    def call(self, mission, context_dir, label, **kwargs) -> Reply:
        self.missions.append(mission)
        self.labels.append(label)
        reply = Reply(label=label, text=self.texts.pop(0), ok=True, attempts=1)
        self._calls.append(reply)
        return reply


RAW = RawTask(id="t1", title="Ledger", body="Sum ledger.csv", files=("ledger.csv",))


class NormalizeTaskTest(unittest.TestCase):
    def test_first_pass_calls_once(self):
        norm = ScriptedNormalizer(good_spec())
        spec = normalize_task(norm, RAW, domains=["accounting"])
        self.assertIsInstance(spec, TaskSpec)
        self.assertEqual(spec.task_id, "t1")
        self.assertEqual(spec.private_criteria, ("Total is 85.15",))
        self.assertEqual(len(norm.calls), 1)
        self.assertEqual(norm.calls[0]["feedback"], "")

    def test_second_try_gets_first_problems_as_feedback(self):
        norm = ScriptedNormalizer(bad_spec(), good_spec())
        spec = normalize_task(norm, RAW, domains=[])
        self.assertEqual(spec.prompt, "Sum the ledger.")
        self.assertEqual(len(norm.calls), 2)
        problems = intake.validate_task_spec(bad_spec())
        self.assertTrue(problems)
        for problem in problems:
            self.assertIn(problem, norm.calls[1]["feedback"])

    def test_two_failures_raise_normalize_error(self):
        second = bad_spec()
        second["files"] = ["../x"]
        norm = ScriptedNormalizer(bad_spec(), second)
        with self.assertRaises(NormalizeError) as ctx:
            normalize_task(norm, RAW, domains=[])
        self.assertIsInstance(ctx.exception, ValueError)
        self.assertTrue(ctx.exception.problems)
        self.assertEqual(ctx.exception.problems, tuple(intake.validate_task_spec(second)))
        self.assertEqual(len(norm.calls), 2)

    def test_exception_is_a_problem(self):
        norm = ScriptedNormalizer(RuntimeError("model went away"), good_spec())
        spec = normalize_task(norm, RAW, domains=[])
        self.assertEqual(spec.task_id, "t1")
        self.assertIn("model went away", norm.calls[1]["feedback"])

    def test_two_exceptions_raise_normalize_error(self):
        norm = ScriptedNormalizer(RuntimeError("first"), RuntimeError("second"))
        with self.assertRaises(NormalizeError) as ctx:
            normalize_task(norm, RAW, domains=[])
        self.assertEqual(ctx.exception.problems, ("second",))
        self.assertEqual(len(norm.calls), 2)

    def test_domains_are_passed_through(self):
        domains = ["accounting", "legal"]
        norm = ScriptedNormalizer(bad_spec(), good_spec())
        normalize_task(norm, RAW, domains=domains)
        self.assertEqual([c["domains"] for c in norm.calls], [domains, domains])


class ModelNormalizerTest(unittest.TestCase):
    def test_mission_has_domains_schema_and_task(self):
        prop = FakeProposer("```json\n" + json.dumps(good_spec()) + "\n```")
        out = ModelNormalizer(prop).normalize(RAW, domains=["accounting", "legal"])
        self.assertEqual(out, good_spec())
        mission = prop.missions[0]
        self.assertIn(json.dumps(intake.TASK_SPEC_SCHEMA), mission)
        self.assertIn("- accounting", mission)
        self.assertIn("- legal", mission)
        self.assertIn("Reuse an existing domain", mission)
        self.assertIn("private_criteria", mission)
        for text in (RAW.id, RAW.title, RAW.body, "ledger.csv"):
            self.assertIn(text, mission)
        self.assertNotIn("Problems with the previous output", mission)
        self.assertEqual(prop.labels, ["normalize.t1"])

    def test_feedback_goes_into_mission(self):
        prop = FakeProposer("```json\n{}\n```")
        ModelNormalizer(prop, label_prefix="n").normalize(
            RAW, domains=[], feedback="prompt is missing"
        )
        self.assertIn("Problems with the previous output", prop.missions[0])
        self.assertIn("prompt is missing", prop.missions[0])
        self.assertEqual(prop.labels, ["n.t1"])

    def test_reads_the_last_json_block(self):
        first = good_spec("first")
        last = good_spec("last")
        text = (
            "Draft:\n```json\n" + json.dumps(first) + "\n```\n"
            "Final:\n```json\n" + json.dumps(last) + "\n```\n"
        )
        out = ModelNormalizer(FakeProposer(text)).normalize(RAW, domains=[])
        self.assertEqual(out["task_id"], "last")

    def test_backticks_inside_a_json_string_do_not_close_the_block(self):
        spec = good_spec()
        spec["prompt"] = "use ```code``` here"
        prop = FakeProposer("Here:\n```json\n" + json.dumps(spec) + "\n```\n")
        out = normalize_task(ModelNormalizer(prop), RAW, domains=[])
        self.assertEqual(out.prompt, "use ```code``` here")
        self.assertEqual(len(prop.missions), 1)

    def test_backticks_inside_a_json_string_direct(self):
        text = "x\n```json\n" + json.dumps({"prompt": "use ```code``` here"}) + "\n```\n"
        self.assertEqual(normalize._last_json_block(text), {"prompt": "use ```code``` here"})

    def test_unclosed_last_block_raises(self):
        text = '```json\n{"a": 1}\n```\nfinal:\n```json\n{"b": 2'
        with self.assertRaises(ValueError) as ctx:
            normalize._last_json_block(text)
        self.assertIn("not closed", str(ctx.exception))

    def test_unclosed_last_block_is_a_problem_for_normalize_task(self):
        draft = "```json\n" + json.dumps(good_spec("draft")) + "\n```\nfinal:\n```json\n{"
        prop = FakeProposer(draft, "```json\n" + json.dumps(good_spec()) + "\n```")
        spec = normalize_task(ModelNormalizer(prop), RAW, domains=[])
        self.assertEqual(spec.task_id, "t1")
        self.assertEqual(len(prop.missions), 2)
        self.assertIn("not closed", prop.missions[1])

    def test_no_json_block_raises(self):
        with self.assertRaises(ValueError):
            ModelNormalizer(FakeProposer("no block here")).normalize(RAW, domains=[])

    def test_no_json_block_then_good_passes_normalize_task(self):
        prop = FakeProposer("no block here", "```json\n" + json.dumps(good_spec()) + "\n```")
        spec = normalize_task(ModelNormalizer(prop), RAW, domains=[])
        self.assertEqual(spec.task_id, "t1")
        self.assertIn("```json", prop.missions[1].split("## Problems")[1])

    def test_no_json_block_twice_fails(self):
        prop = FakeProposer("nothing", "still nothing")
        with self.assertRaises(NormalizeError):
            normalize_task(ModelNormalizer(prop), RAW, domains=[])
        self.assertEqual(len(prop.missions), 2)


class PassthroughTest(unittest.TestCase):
    def test_meta_task_spec(self):
        raw = RawTask(id="t1", body="not json", meta={"task_spec": good_spec()})
        self.assertEqual(PassthroughNormalizer().normalize(raw, domains=[]), good_spec())

    def test_body_json(self):
        raw = RawTask(id="t1", body=json.dumps(good_spec()))
        self.assertEqual(PassthroughNormalizer().normalize(raw, domains=[]), good_spec())

    def test_task_id_is_filled_from_raw(self):
        spec = good_spec()
        del spec["task_id"]
        raw = RawTask(id="from-raw", body=json.dumps(spec))
        out = PassthroughNormalizer().normalize(raw, domains=[])
        self.assertEqual(out["task_id"], "from-raw")
        raw = RawTask(id="from-raw", meta={"task_spec": spec})
        self.assertEqual(PassthroughNormalizer().normalize(raw, domains=[])["task_id"], "from-raw")
        self.assertNotIn("task_id", spec)

    def test_body_not_json_raises(self):
        with self.assertRaises(ValueError):
            PassthroughNormalizer().normalize(RawTask(id="t1", body="hello"), domains=[])

    def test_body_not_json_fails_normalize_task(self):
        with self.assertRaises(NormalizeError) as ctx:
            normalize_task(PassthroughNormalizer(), RawTask(id="t1", body="hello"), domains=[])
        self.assertTrue(ctx.exception.problems)


class RegistryTest(unittest.TestCase):
    def test_default_is_passthrough(self):
        self.assertEqual(normalize.DEFAULT, "passthrough")
        self.assertEqual(normalize.normalizer_name(env={}), "passthrough")
        self.assertIsInstance(normalizer_for(env={}), PassthroughNormalizer)
        self.assertIn("passthrough", normalize.names())
        self.assertIn("model", normalize.names())

    def test_env_var(self):
        prop = FakeProposer()
        norm = normalizer_for(env={normalize.ENV_VAR: "model"}, proposer=prop)
        self.assertIsInstance(norm, ModelNormalizer)
        self.assertIs(norm.prop, prop)
        self.assertIsInstance(norm, Normalizer)

    def test_explicit_name_wins_over_env(self):
        norm = normalizer_for(name="passthrough", env={normalize.ENV_VAR: "model"})
        self.assertIsInstance(norm, PassthroughNormalizer)

    def test_model_without_route_model_raises(self):
        empty = config.ModelRoute("normalizer", "", "")
        with patch.dict(config.ROLES, {"normalizer": empty}):
            with self.assertRaises(ValueError) as ctx:
                normalizer_for(name="model", env={})
        self.assertIn("normalizer", str(ctx.exception))

    def test_model_with_route_builds_proposer(self):
        route = config.ModelRoute("normalizer", "local", "local/deterministic")
        with patch.dict(config.ROLES, {"normalizer": route}):
            norm = normalizer_for(name="model", env={})
        self.assertIsInstance(norm, ModelNormalizer)
        self.assertEqual(norm.prop.spec.model, "local/deterministic")
        self.assertEqual(norm.prop.spec.kind, config.DEFAULT_PROPOSER_CHANNEL)

    def test_import_path(self):
        norm = normalizer_for(name="harness2.core.normalize:PassthroughNormalizer", env={})
        self.assertIsInstance(norm, PassthroughNormalizer)

    def test_unknown_name_raises(self):
        with self.assertRaises(ValueError):
            normalizer_for(name="nope", env={})

    def test_bad_import_path_raises(self):
        with self.assertRaises(ValueError):
            normalizer_for(name="harness2.core.normalize:Missing", env={})


if __name__ == "__main__":
    unittest.main()
