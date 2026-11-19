"""The only places the evals call the system under test.

The harness was built before the pipeline (WP2), core (WP3) and analyst (WP6) landed, so it
codes against these entry points (also recorded in DECISIONS.md):

    level.pipeline.run.process_message(message_id: int) -> None
        Run every stage for one inbound message and its attachments, as the worker job does.
    level.core.cells.rebuild_cells(rfx_id: int) -> None
        Rebuild the `cell` table from claims, terms, assumptions and review resolutions.
    level.agents.analyst.ask(rfx_id: int, question: str) -> str | object with `.text`
        Answer one question in a fresh thread, from a plain Python call (no Streamlit).

If one is missing, the suite that needs it is reported as "not built", never faked.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable


class NotBuilt(RuntimeError):
    """The package that provides this entry point has not landed yet."""


def _resolve(module: str, func: str) -> Callable:
    try:
        return getattr(importlib.import_module(module), func)
    except (ImportError, AttributeError) as exc:
        raise NotBuilt(f"{module}.{func} is not available yet ({exc})") from exc


def process_message(message_id: int) -> None:
    _resolve("level.pipeline.run", "process_message")(message_id)


def rebuild_cells(rfx_id: int) -> None:
    _resolve("level.core.cells", "rebuild_cells")(rfx_id)


def ask_analyst(rfx_id: int, question: str) -> str:
    reply = _resolve("level.agents.analyst", "ask")(rfx_id, question)
    return reply if isinstance(reply, str) else str(getattr(reply, "text", reply))
