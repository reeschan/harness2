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

"""Render the README architecture diagram into self-contained light and dark SVGs.

Requires the D2 CLI (https://d2lang.com). Icons are bundled into each SVG as data URIs.

    python3 assets/architecture/src/render.py
"""

from __future__ import annotations

import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent

PALETTES = {
    "light": {
        "icons": "icons-light",
        "bg": "#FFFFFF",
        "text": "#1F2328",
        "box": "#FFFFFF",
        "box_stroke": "#8C959F",
        "edge": "#3B5BA9",
        "edge_text": "#57606A",
        "accent": "#E8710A",
        "accent_text": "#B35400",
        "cli_bg": "#F6F8FA",
        "cli_stroke": "#57606A",
        "core_bg": "#EAF2FE",
        "core_stroke": "#4285F4",
        "inner_bg": "#D2E3FC",
        "sub_bg": "#FEF7E0",
        "sub_stroke": "#F9AB00",
        "bench_bg": "#E6F4EA",
        "bench_stroke": "#34A853",
        "model_bg": "#FCE8E6",
        "model_stroke": "#EA4335",
    },
    "dark": {
        "icons": "icons-dark",
        "bg": "#0D1117",
        "text": "#E6EDF3",
        "box": "#161B22",
        "box_stroke": "#6E7681",
        "edge": "#79C0FF",
        "edge_text": "#C9D1D9",
        "accent": "#FFA657",
        "accent_text": "#FFA657",
        "cli_bg": "#161B22",
        "cli_stroke": "#8B949E",
        "core_bg": "#0C2D6B",
        "core_stroke": "#58A6FF",
        "inner_bg": "#123A85",
        "sub_bg": "#3D2E00",
        "sub_stroke": "#D29922",
        "bench_bg": "#0F3A1E",
        "bench_stroke": "#3FB950",
        "model_bg": "#4A1414",
        "model_stroke": "#F85149",
    },
}


def main() -> None:
    template = (HERE / "architecture.d2.tmpl").read_text()
    for mode, palette in PALETTES.items():
        source = template
        for key, value in palette.items():
            source = source.replace("{{" + key + "}}", value)
        d2_path = HERE / f"architecture-{mode}.d2"
        d2_path.write_text(source)
        svg_path = OUT / f"architecture-{mode}.svg"
        subprocess.run(
            ["d2", "--layout=elk", "--pad=40", "--theme=0", str(d2_path), str(svg_path)],
            check=True,
            cwd=HERE,
        )
        d2_path.unlink()
        svg_path.chmod(0o644)


if __name__ == "__main__":
    main()
