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

"""Optional plugins that harness2's core does not import.

Nothing in `harness2.core`, `harness2.cli` or `harness2.benchmarks` imports this package.
A plugin is a building block for code outside harness2 (for example a task connector), which
imports only the plugins it uses. `harness2.plugins.mcp` is a small MCP client.
"""
