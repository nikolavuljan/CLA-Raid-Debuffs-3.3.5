#!/usr/bin/env python3
"""
Render CLA coverage JSON (output of cla_parser.py) into a simple HTML heatmap.

Example:
    python3 render_cla_heatmap.py pumper.json -o pumper.html
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render CLA coverage JSON as an HTML heatmap.")
    parser.add_argument("json_file", type=Path, help="Path to cla_parser output JSON.")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Destination HTML file (default: <json_file>.html in the same directory).",
    )
    parser.add_argument(
        "--title",
        type=str,
        default=None,
        help="Optional page title for the heatmap.",
    )
    return parser.parse_args(argv)


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def blend(color_a: Sequence[int], color_b: Sequence[int], t: float) -> str:
    r = int(round(lerp(color_a[0], color_b[0], t)))
    g = int(round(lerp(color_a[1], color_b[1], t)))
    b = int(round(lerp(color_a[2], color_b[2], t)))
    return f"#{r:02x}{g:02x}{b:02x}"


COLOR_MIN = (139, 0, 0)      # dark red
COLOR_MID = (255, 215, 0)    # gold
COLOR_MAX = (26, 152, 80)    # green
COLOR_EMPTY = "#d9d9d9"


def value_to_color(value: Optional[float]) -> str:
    if value is None or math.isnan(value):
        return COLOR_EMPTY
    v = max(0.0, min(1.0, value))
    if v <= 0.5:
        return blend(COLOR_MIN, COLOR_MID, v / 0.5 if v > 0 else 0.0)
    return blend(COLOR_MID, COLOR_MAX, (v - 0.5) / 0.5 if v < 1 else 1.0)


def format_percent(value: Optional[float]) -> str:
    if value is None or math.isnan(value):
        return "—"
    return f"{value * 100:.1f}%"


def render_heatmap(data: Dict[str, object], title: Optional[str] = None) -> str:
    categories = data.get("categories", [])
    table = data.get("table", [])
    if not isinstance(categories, list) or not isinstance(table, list):
        raise ValueError("Invalid JSON structure: missing 'categories' or 'table'.")

    column_meta = [
        {"key": "boss", "label": "Boss / Result", "classes": [], "group": None, "effect": None},
        {"key": "overall", "label": "Overall", "classes": [], "group": None, "effect": None},
    ]
    for cat in categories:
        column_meta.append(
            {
                "key": cat["label"],
                "label": cat["label"],
                "classes": cat.get("classes", []),
                "group": cat.get("group"),
                "effect": cat.get("effect"),
            }
        )
    column_labels = [meta["key"] for meta in column_meta]

    rows_html: List[str] = []
    for summary in table:
        if not isinstance(summary, dict):
            continue
        row_cells: List[str] = []
        boss_label = summary.get("boss", "")
        row_cells.append(f'<th class="boss-col" scope="row">{boss_label}</th>')

        for key in column_labels[1:]:
            value = summary.get(key)
            if isinstance(value, (int, float)):
                color = value_to_color(float(value))
                label = format_percent(float(value))
            else:
                color = COLOR_EMPTY
                label = "—"
            row_cells.append(
                f'<td style="background:{color};" title="{label}">{label}</td>'
            )
        rows_html.append("<tr>" + "".join(row_cells) + "</tr>")

    page_title = (
        title
        or data.get("log")
        or "CLA Coverage Heatmap"
    )

    # Build multi-row header with group bands
    top_row_cells: List[str] = []
    idx = 0
    column_count = len(column_meta)
    while idx < column_count:
        meta_entry = column_meta[idx]
        label = meta_entry["label"]
        group = meta_entry.get("group")
        class_list = meta_entry.get("classes") or []
        if not group:
            sub_html_parts = []
            if class_list:
                sub_html_parts.append(" • ".join(class_list))
            effect = meta_entry.get("effect")
            if effect:
                sub_html_parts.append(effect)
            sub_html = ""
            if sub_html_parts:
                sub_html = "".join(
                    f'<div class="header-sub">{part}</div>' for part in sub_html_parts
                )
            top_row_cells.append(
                f'<th class="group-sticky" rowspan="2"><div class="header-label">{label}</div>{sub_html}</th>'
            )
            idx += 1
        else:
            span = 0
            j = idx
            while j < column_count and column_meta[j].get("group") == group:
                span += 1
                j += 1
            top_row_cells.append(
                f'<th class="group-header" colspan="{span}">{group}</th>'
            )
            idx = j

    bottom_row_cells: List[str] = []
    for meta_entry in column_meta:
        group = meta_entry.get("group")
        if not group:
            continue
        label = meta_entry["label"]
        class_list = meta_entry.get("classes") or []
        effect = meta_entry.get("effect")
        sub_parts = []
        if class_list:
            sub_parts.append(" • ".join(class_list))
        if effect:
            sub_parts.append(effect)
        sub_html = "".join(f'<div class="header-sub">{part}</div>' for part in sub_parts)
        bottom_row_cells.append(
            f'<th><div class="header-label">{label}</div>{sub_html}</th>'
        )

    top_row_html = "".join(top_row_cells)
    bottom_row_html = "".join(bottom_row_cells)

    style = """
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; background: #f6f6f6; }
    h1 { margin-bottom: 12px; font-size: 24px; }
    .meta { color: #555; margin-bottom: 20px; }
    table { border-collapse: collapse; min-width: 960px; }
    th, td { border: 1px solid #bfbfbf; padding: 8px 10px; text-align: center; }
    th { background: #f0f0f0; position: sticky; top: 0; }
    th.boss-col { text-align: left; font-weight: 600; min-width: 280px; background: #f8f8f8; }
    tr:nth-child(even) > th.boss-col { background: #f3f3f3; }
    td { color: #1f1f1f; font-weight: 600; }
    .header-label { font-weight: 600; }
    .header-sub { font-size: 12px; color: #555; margin-top: 4px; white-space: nowrap; }
    .group-header { text-transform: uppercase; font-size: 12px; letter-spacing: 0.08em; background: #e6e6e6; }
    .group-sticky { background: #f0f0f0; }
    """

    meta_lines = []
    if "generated_at" in data:
        meta_lines.append(f"Generated: {data['generated_at']}")
    if "log" in data:
        meta_lines.append(f"Source log: {data['log']}")
    meta_html = " | ".join(meta_lines)

    rows_markup = "\n".join(rows_html)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{page_title}</title>
  <style>{style}</style>
</head>
<body>
  <h1>{page_title}</h1>
  <div class="meta">{meta_html}</div>
  <table>
    <thead>
      <tr>{top_row_html}</tr>
      <tr>{bottom_row_html}</tr>
    </thead>
    <tbody>
      {rows_markup}
    </tbody>
  </table>
</body>
</html>
"""
    return html


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    json_path = args.json_file
    if not json_path.exists():
        raise SystemExit(f"JSON file not found: {json_path}")
    with json_path.open("r", encoding="utf-8") as fp:
        data = json.load(fp)

    html = render_heatmap(data, title=args.title)

    output_path = args.output
    if output_path is None:
        output_path = json_path.with_suffix(".html")
    output_path.write_text(html, encoding="utf-8")
    print(f"Heatmap written to {output_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
