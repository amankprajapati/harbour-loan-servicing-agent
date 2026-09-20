from __future__ import annotations

import json

from harbour import ledger as ledger_module
from harbour import tracing as tracing_module


def test_span_writes_on_close_with_duration(tmp_path):
    export_path = tmp_path / "otlp.jsonl"
    tracer = tracing_module.Tracer(export_path, case_id="CASE-T1")
    with tracer.span("tool_call", tool="get_loan"):
        pass
    spans = tracing_module.read_spans_for_case(export_path, "CASE-T1")
    assert len(spans) == 1
    assert spans[0]["name"] == "tool_call"
    assert spans[0]["attributes"]["tool"] == "get_loan"
    assert spans[0]["status"] == "ok"
    assert spans[0]["durationMs"] is not None


def test_span_marks_error_status_but_still_writes(tmp_path):
    export_path = tmp_path / "otlp.jsonl"
    tracer = tracing_module.Tracer(export_path, case_id="CASE-T2")
    try:
        with tracer.span("tool_call", tool="issue_refund"):
            raise ValueError("boom")
    except ValueError:
        pass
    spans = tracing_module.read_spans_for_case(export_path, "CASE-T2")
    assert len(spans) == 1
    assert spans[0]["status"] == "error"


def test_nested_spans_have_correct_parent(tmp_path):
    export_path = tmp_path / "otlp.jsonl"
    tracer = tracing_module.Tracer(export_path, case_id="CASE-T3")
    with tracer.span("case") as outer:
        with tracer.span("tool_call") as inner:
            pass
    spans = {s["name"]: s for s in tracing_module.read_spans_for_case(export_path, "CASE-T3")}
    assert spans["tool_call"]["parentSpanId"] == outer.span_id
    assert spans["case"]["parentSpanId"] is None


def test_spans_from_other_cases_are_excluded(tmp_path):
    export_path = tmp_path / "otlp.jsonl"
    tracer_a = tracing_module.Tracer(export_path, case_id="CASE-A")
    tracer_b = tracing_module.Tracer(export_path, case_id="CASE-B")
    with tracer_a.span("x"):
        pass
    with tracer_b.span("y"):
        pass
    assert len(tracing_module.read_spans_for_case(export_path, "CASE-A")) == 1
    assert len(tracing_module.read_spans_for_case(export_path, "CASE-B")) == 1


def test_estimate_cost_usd_scales_with_tokens():
    cost_small = ledger_module.estimate_cost_usd(1000, 500)
    cost_large = ledger_module.estimate_cost_usd(2000, 1000)
    assert cost_large == round(cost_small * 2, 8)
    assert cost_small > 0


def test_ledger_writer_and_cost_summary(tmp_path):
    export_path = tmp_path / "ledger.jsonl"
    writer = ledger_module.LedgerWriter(export_path)
    writer.record(
        ledger_module.LedgerEntry(
            case_id="CASE-L1",
            call_id="call-1",
            model_id="harbour-planner-v1-scripted",
            tokens_in=1000,
            tokens_out=200,
            cost_usd=ledger_module.estimate_cost_usd(1000, 200),
            created_at=0.0,
        )
    )
    writer.record(
        ledger_module.LedgerEntry(
            case_id="CASE-L1",
            call_id="call-2",
            model_id="harbour-planner-v1-scripted",
            tokens_in=500,
            tokens_out=100,
            cost_usd=ledger_module.estimate_cost_usd(500, 100),
            created_at=1.0,
        )
    )
    writer.record(
        ledger_module.LedgerEntry(
            case_id="CASE-L2",
            call_id="call-3",
            model_id="harbour-planner-v1-scripted",
            tokens_in=100,
            tokens_out=50,
            cost_usd=ledger_module.estimate_cost_usd(100, 50),
            created_at=2.0,
        )
    )

    summary = ledger_module.cost_summary_by_case(export_path)
    assert summary["CASE-L1"]["calls"] == 2
    assert summary["CASE-L1"]["tokens_in"] == 1500
    assert summary["CASE-L2"]["calls"] == 1
    assert summary["CASE-L1"]["cost_usd"] > summary["CASE-L2"]["cost_usd"]


def test_read_ledger_on_missing_file_returns_empty(tmp_path):
    assert ledger_module.read_ledger(tmp_path / "does_not_exist.jsonl") == []


def test_read_spans_on_missing_file_returns_empty(tmp_path):
    assert tracing_module.read_spans_for_case(tmp_path / "does_not_exist.jsonl", "CASE-X") == []
