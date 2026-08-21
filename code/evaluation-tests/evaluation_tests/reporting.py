"""Write machine-readable evidence and dependency-free SVG figures."""

from __future__ import annotations

import csv
import json
import math
import shutil
from html import escape
from pathlib import Path
from typing import Any

COLORS = {
    "green": "#2e7d32",
    "green_light": "#a5d6a7",
    "orange": "#ef6c00",
    "orange_light": "#ffcc80",
    "red": "#c62828",
    "blue": "#1565c0",
    "blue_light": "#90caf9",
    "ink": "#1f2937",
    "muted": "#64748b",
    "grid": "#cbd5e1",
    "paper": "#ffffff",
}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_quality_chart(path: Path, metrics: dict[str, float | int]) -> None:
    width, height = 820, 360
    left, right = 235, 75
    plot_width = width - left - right
    rows = [
        (
            "Reference-acceptable plans",
            int(metrics["true_accept"]),
            int(metrics["false_reject"]),
            "correctly accepted",
            "incorrectly rejected",
        ),
        (
            "Reference-rejected plans",
            int(metrics["true_reject"]),
            int(metrics["false_accept"]),
            "correctly rejected",
            "semantic false accepts",
        ),
    ]
    maximum = max(first + second for _, first, second, _, _ in rows)
    parts = [_svg_start(width, height, "Hard-gate calibration against reference labels")]
    parts.append(_text(30, 38, "Hard-gate calibration against reference labels", 20, weight=700))
    parts.append(
        _text(
            30,
            64,
            "Counts from the labelled synthetic calibration set (n = 12)",
            13,
            fill=COLORS["muted"],
        )
    )
    y_positions = (125, 220)
    for (label, correct, incorrect, correct_label, incorrect_label), y in zip(
        rows, y_positions, strict=True
    ):
        parts.append(_text(30, y + 25, label, 14, weight=600))
        correct_width = plot_width * correct / maximum
        incorrect_width = plot_width * incorrect / maximum
        parts.append(_rect(left, y, correct_width, 44, COLORS["green"], radius=4))
        if incorrect:
            parts.append(
                _rect(left + correct_width, y, incorrect_width, 44, COLORS["orange"], radius=4)
            )
        parts.append(_text(left + 10, y + 28, str(correct), 14, fill="#ffffff", weight=700))
        if incorrect:
            parts.append(
                _text(
                    left + correct_width + 10,
                    y + 28,
                    str(incorrect),
                    14,
                    fill="#ffffff",
                    weight=700,
                )
            )
        parts.append(_text(left, y + 64, correct_label, 12, fill=COLORS["muted"]))
        if incorrect:
            parts.append(
                _text(
                    left + correct_width + 4,
                    y + 64,
                    incorrect_label,
                    12,
                    fill=COLORS["orange"],
                )
            )
    parts.append(_legend(205, 315, COLORS["green"], "hard gate agrees with reference"))
    parts.append(_legend(500, 315, COLORS["orange"], "requires semantic QA"))
    parts.append("</svg>\n")
    _write_svg(path, parts)


def write_equivalence_chart(path: Path, results: list[dict[str, Any]]) -> None:
    width, height = 820, 400
    left, right = 180, 80
    plot_width = width - left - right
    maximum_log = max(math.log10(max(1, int(row["cases"]))) for row in results)
    parts = [_svg_start(width, height, "Exhaustive Boolean and SMT equivalence cases")]
    parts.append(_text(30, 38, "Exhaustive Boolean–SMT comparison", 20, weight=700))
    parts.append(
        _text(
            30,
            64,
            "Verified input combinations; bar length uses a log10 scale",
            13,
            fill=COLORS["muted"],
        )
    )
    for index, row in enumerate(results):
        y = 105 + index * 62
        cases = int(row["cases"])
        disagreements = int(row["disagreements"])
        bar_width = plot_width * math.log10(max(1, cases)) / maximum_log
        parts.append(_text(35, y + 23, f"width {row['width']}", 14, weight=600))
        parts.append(_rect(left, y, bar_width, 34, COLORS["blue"], radius=4))
        parts.append(_text(left + 9, y + 22, f"{cases:,} cases", 12, fill="#ffffff", weight=700))
        badge_fill = COLORS["green"] if disagreements == 0 else COLORS["red"]
        parts.append(_rect(width - 150, y, 108, 34, badge_fill, radius=17))
        parts.append(
            _text(
                width - 96,
                y + 22,
                f"{disagreements} mismatch" + ("es" if disagreements != 1 else ""),
                11,
                fill="#ffffff",
                weight=700,
                anchor="middle",
            )
        )
    total = sum(int(row["cases"]) for row in results)
    parts.append(
        _text(
            30,
            height - 30,
            f"Total exhaustive comparisons: {total:,}",
            13,
            fill=COLORS["muted"],
        )
    )
    parts.append("</svg>\n")
    _write_svg(path, parts)


def write_latency_chart(path: Path, results: list[dict[str, Any]]) -> None:
    width, height = 860, 480
    left, right, top, bottom = 85, 30, 85, 145
    plot_width = width - left - right
    plot_height = height - top - bottom
    widths = sorted({int(row["width"]) for row in results})
    values = [max(0.1, float(row["median_microseconds"])) for row in results]
    minimum_log = math.floor(math.log10(min(values)))
    maximum_log = math.ceil(math.log10(max(values)))
    if maximum_log == minimum_log:
        maximum_log += 1

    def y_for(value: float) -> float:
        fraction = (math.log10(max(0.1, value)) - minimum_log) / (maximum_log - minimum_log)
        return top + plot_height * (1 - fraction)

    parts = [_svg_start(width, height, "Validator latency by bit-vector width")]
    parts.append(_text(30, 38, "Validator latency by permission-vector width", 20, weight=700))
    parts.append(
        _text(
            30,
            64,
            "Median per-candidate latency; logarithmic y-axis",
            13,
            fill=COLORS["muted"],
        )
    )
    for exponent in range(minimum_log, maximum_log + 1):
        value = 10**exponent
        y = y_for(value)
        parts.append(_line(left, y, width - right, y, COLORS["grid"], dash="4 5"))
        parts.append(
            _text(
                left - 12,
                y + 4,
                _format_microseconds(value),
                11,
                fill=COLORS["muted"],
                anchor="end",
            )
        )

    group_width = plot_width / len(widths)
    bar_width = min(42, group_width * 0.28)
    by_key = {(int(row["width"]), str(row["engine"])): row for row in results}
    for index, vector_width in enumerate(widths):
        center = left + group_width * (index + 0.5)
        for offset, (engine, color) in enumerate(
            (("Direct bitset", COLORS["blue"]), ("Z3 bit-vector", COLORS["orange"]))
        ):
            row = by_key[(vector_width, engine)]
            value = float(row["median_microseconds"])
            x = center + (offset - 0.5) * (bar_width + 8) - bar_width / 2
            y = y_for(value)
            parts.append(_rect(x, y, bar_width, top + plot_height - y, color, radius=3))
            parts.append(
                _text(
                    x + bar_width / 2,
                    y - 7,
                    _format_microseconds(value),
                    10,
                    fill=color,
                    weight=700,
                    anchor="middle",
                )
            )
        label = f"{vector_width:,}"
        parts.append(_text(center, top + plot_height + 28, label, 12, anchor="middle"))
    parts.append(
        _text(
            width / 2,
            height - 20,
            "permission-vector width (bits)",
            12,
            anchor="middle",
        )
    )
    parts.append(_legend(260, height - 58, COLORS["blue"], "Direct bitset"))
    parts.append(_legend(470, height - 58, COLORS["orange"], "Z3 bit-vector"))
    parts.append("</svg>\n")
    _write_svg(path, parts)


def copy_figures(result_directory: Path, paper_image_directory: Path) -> None:
    paper_image_directory.mkdir(parents=True, exist_ok=True)
    for name in ("qa-calibration.svg", "validator-equivalence.svg", "validator-latency.svg"):
        shutil.copyfile(result_directory / name, paper_image_directory / name)


def _format_microseconds(value: float) -> str:
    if value >= 1_000:
        return f"{value / 1_000:.1f} ms"
    if value >= 10:
        return f"{value:.0f} µs"
    return f"{value:.2f} µs"


def _svg_start(width: int, height: int, description: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{escape(description)}">\n'
        f'<rect width="{width}" height="{height}" fill="{COLORS["paper"]}"/>\n'
    )


def _write_svg(path: Path, parts: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(parts), encoding="utf-8")


def _text(
    x: float,
    y: float,
    value: str,
    size: int,
    *,
    fill: str | None = None,
    weight: int = 400,
    anchor: str = "start",
) -> str:
    color = fill or COLORS["ink"]
    return (
        f'<text x="{x:.2f}" y="{y:.2f}" font-family="Arial, Helvetica, sans-serif" '
        f'font-size="{size}" font-weight="{weight}" text-anchor="{anchor}" '
        f'fill="{color}">{escape(value)}</text>\n'
    )


def _rect(x: float, y: float, width: float, height: float, fill: str, *, radius: float) -> str:
    return (
        f'<rect x="{x:.2f}" y="{y:.2f}" width="{max(0, width):.2f}" '
        f'height="{height:.2f}" rx="{radius:.2f}" fill="{fill}"/>\n'
    )


def _line(x1: float, y1: float, x2: float, y2: float, stroke: str, *, dash: str = "") -> str:
    dash_attribute = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
        f'stroke="{stroke}" stroke-width="1"{dash_attribute}/>\n'
    )


def _legend(x: float, y: float, color: str, label: str) -> str:
    return _rect(x, y - 12, 18, 18, color, radius=3) + _text(x + 27, y + 2, label, 12)
