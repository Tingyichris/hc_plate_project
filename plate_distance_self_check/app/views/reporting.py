"""Presentation boundary for canonical Markdown reports.

The formatters remain compatible with their historic public import paths, but
controllers call them through this module so orchestration does not depend on
where a particular report implementation lives.
"""

from __future__ import annotations


def render_handoff_report(summary: dict, rows: list[dict]) -> str:
    """Render handoff structured results as Markdown."""
    from app.models.handoff import build_handoff_markdown

    return build_handoff_markdown(summary, rows)


def render_evaluation_report(evaluation: dict) -> str:
    """Render canonical evaluation data as Markdown."""
    from app.models.evaluation import markdown

    return markdown(evaluation)
