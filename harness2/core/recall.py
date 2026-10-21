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

"""Recall blocks: memory text injected at the end of an always-on file between markers.

`target_file` picks the file from the surface (`memory_file`, else the first always-on file),
`room` is how many characters of text fit under the always-on cap once the block's wrapping
is counted, `inject` appends one block to a copy of the tree, and `strip` removes every block
again. The module knows nothing about memory backends; it only edits trees.
"""

from __future__ import annotations

import re

from . import context
from . import tree as harness_tree
from .tree import SurfaceSpec

BEGIN = "<!-- memory-connector:begin -->"
END = "<!-- memory-connector:end -->"

_SEP = "\n\n"
_WRAP_LEN = len(_SEP + BEGIN + "\n" + "\n" + END + "\n")
_BLOCK = re.compile(r"(?:\n\n)?" + re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", re.S)


def target_file(surface: SurfaceSpec) -> str:
    """The always-on file a recall block is appended to."""
    return surface.memory_file or surface.always_on[0]


def room(surface: SurfaceSpec, tree: harness_tree.Tree) -> int:
    """Characters of recall text that keep the always-on text within `always_on_max`.

    Zero or negative when the always-on text is already at or near the cap.
    """
    used = len(harness_tree.always_on_text(surface, tree))
    return surface.always_on_max - used - _WRAP_LEN


def _block(text: str) -> str:
    return BEGIN + "\n" + text + "\n" + END + "\n"


def inject(surface: SurfaceSpec, tree: harness_tree.Tree, text: str) -> harness_tree.Tree:
    """A copy of `tree` with `text` appended to the target file between BEGIN and END.

    Empty text returns an unchanged copy. Text carrying a grader-verdict marker, BEGIN or END
    raises ValueError. A target file missing from the tree is created holding only the block.
    """
    out = dict(tree)
    if not text:
        return out
    context.assert_tree_has_no_grader_verdicts({"RECALL.md": text})
    for marker in (BEGIN, END):
        if marker in text:
            raise ValueError(f"recall text contains a reserved recall delimiter: {marker}")
    rel = target_file(surface)
    if rel in out:
        out[rel] = out[rel] + _SEP + _block(text)
    else:
        out[rel] = _block(text)
    return out


def strip(tree: harness_tree.Tree) -> harness_tree.Tree:
    """A copy of `tree` with every recall block removed from every file.

    A file that held nothing but a block (as `inject` creates it) is dropped from the copy.
    """
    out: harness_tree.Tree = {}
    for rel, body in tree.items():
        if BEGIN not in body:
            out[rel] = body
            continue
        stripped = _BLOCK.sub("", body)
        if stripped or not body.startswith(BEGIN):
            out[rel] = stripped
    return out
