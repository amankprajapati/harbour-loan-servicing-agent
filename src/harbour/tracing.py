"""Span export, shaped like OTLP but written as line-delimited JSON rather
than protobuf, so the grading table's `results/traces/otlp.jsonl` can be
produced without an OTLP collector running. Every tool call and every
model call gets a span; every span carries `case_id` as an attribute,
which is the join key the ledger and the defect detectors both use.

This is the direct fix for "the bill spikes without traffic spiking and
nobody can say which cases cause it": before this, no record existed that
tied a token count or a wall-clock duration to a specific case at all.
"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


def _new_id(n_bytes: int) -> str:
    return uuid.uuid4().hex[: n_bytes * 2]


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    case_id: str
    start_ns: int
    end_ns: int | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"

    def to_otlp_like(self) -> dict[str, Any]:
        return {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "parentSpanId": self.parent_span_id,
            "name": self.name,
            "startTimeUnixNano": self.start_ns,
            "endTimeUnixNano": self.end_ns,
            "durationMs": None if self.end_ns is None else (self.end_ns - self.start_ns) / 1e6,
            "status": self.status,
            "attributes": {"case_id": self.case_id, **self.attributes},
        }


class Tracer:
    """One Tracer per case (or per process, callers' choice). Spans
    export as they close, so a crash mid-case still leaves every
    already-closed span on disk -- there is no "we buffer the whole trace
    and lose it on a crash" failure mode.
    """

    def __init__(self, export_path: str | Path, *, case_id: str, trace_id: str | None = None):
        self.export_path = Path(export_path)
        self.export_path.parent.mkdir(parents=True, exist_ok=True)
        self.case_id = case_id
        self.trace_id = trace_id or _new_id(16)
        self._stack: list[str] = []

    def _write(self, span: Span) -> None:
        with self.export_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(span.to_otlp_like(), default=str) + "\n")

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Span]:
        parent = self._stack[-1] if self._stack else None
        span = Span(
            trace_id=self.trace_id,
            span_id=_new_id(8),
            parent_span_id=parent,
            name=name,
            case_id=self.case_id,
            start_ns=time.time_ns(),
            attributes=dict(attributes),
        )
        self._stack.append(span.span_id)
        try:
            yield span
            span.status = "ok"
        except Exception:
            span.status = "error"
            raise
        finally:
            span.end_ns = time.time_ns()
            self._stack.pop()
            self._write(span)


def read_spans_for_case(export_path: str | Path, case_id: str) -> list[dict[str, Any]]:
    path = Path(export_path)
    if not path.exists():
        return []
    spans = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if record.get("attributes", {}).get("case_id") == case_id:
                spans.append(record)
    return spans
