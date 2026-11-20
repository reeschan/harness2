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

"""Evaluators bundled with harness2.

Importing this package registers each bundled evaluator by name with `harness2.core.evaluate`,
so `evaluate.evaluator_for("rules")` works after `import harness2.evaluators`. The registration
lives here, not in `core`, because `core` sits on the layer below and must not import this
package. An installed package also names `rules` through the entry point of the group
`harness2.evaluators`, so a caller that skips this import can still resolve it.
"""

from __future__ import annotations

from ..core import evaluate
from .rules import NAME as RULES_NAME
from .rules import RulesEvaluator

evaluate.register(RULES_NAME, RulesEvaluator)

__all__ = ["RulesEvaluator"]
