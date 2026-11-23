"""Off-GCP / Cross-Cloud OTLP Trace & Log Analytics Ingestion into Project A.

Demonstrates how an off-GCP agent (running on AWS EKS, Azure AKS, or on-prem
Kubernetes via Workload Identity Federation / AIR sidecar / ADC) correlates its:
  1. OpenTelemetry GenAI spans (`invoke_agent`, `call_llm`, `execute_tool`) via
     Google Cloud's OTLP endpoint (`https://telemetry.googleapis.com/v1/traces`)
     with `x-goog-user-project: <PROJECT_A_ID>` and `gcp.project_id=<PROJECT_A_ID>`,
  2. Outbound W3C `traceparent` header (`00-<trace_id>-<span_id>-01`) across
     `enterprise-egress-agw` (stitched via `TelemetryPolicy`
     `enterprise-egress-agw-telemetry`) and `internal-mcp-server`, and
  3. Structured Cloud Logging entries (`https://logging.googleapis.com/v2/entries:write`)
     with `trace="projects/<PROJECT_A_ID>/traces/<trace_id>"` and `spanId` into
     Project A's `_Default` Log Analytics bucket (`_AllLogs`).

Usage:
  python3 agents/cross_cloud_otlp_ingest.py
"""

from datetime import datetime, timezone
import os
import secrets
import time
from typing import Any

import google.auth
from google.auth.transport.requests import Request
import httpx

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ENV_FILE = os.path.join(os.path.dirname(_SCRIPT_DIR), ".env")
if os.path.exists(_ENV_FILE):
    with open(_ENV_FILE, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#"):
                continue
            if _line.startswith("export "):
                _line = _line[len("export ") :].strip()
            if "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

PROJECT_A_ID = os.environ.get("PROJECT_A_ID", "your-project-a-id")
REGION = os.environ.get("REGION", "us-central1")
OFF_GCP_AGENT_ID = os.environ.get(
    "OFF_GCP_AGENT_ID",
    f"urn:agent:{PROJECT_A_ID}:eks-secops-remediation-agent",
)
OFF_GCP_SERVICE_NAME = os.environ.get(
    "OFF_GCP_SERVICE_NAME",
    "eks-secops-remediation-agent",
)
OFF_GCP_CLOUD_PROVIDER = os.environ.get("OFF_GCP_CLOUD_PROVIDER", "aws")


def _get_auth_headers(quota_project: str = PROJECT_A_ID) -> dict[str, str]:
    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    creds.refresh(Request())
    return {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json",
        "x-goog-user-project": quota_project,
    }


def export_otlp_trace_to_project_a(
    trace_id: str,
    spans: list[dict[str, Any]],
    project_a_id: str = PROJECT_A_ID,
    agent_urn: str = OFF_GCP_AGENT_ID,
    service_name: str = OFF_GCP_SERVICE_NAME,
    cloud_provider: str = OFF_GCP_CLOUD_PROVIDER,
) -> int:
    """Sends OTLP spans directly to Project A's telemetry.googleapis.com/v1/traces."""
    otlp_payload = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "gcp.project_id", "value": {"stringValue": project_a_id}},
                        {"key": "gcp.agent.id", "value": {"stringValue": agent_urn}},
                        {"key": "service.name", "value": {"stringValue": service_name}},
                        {"key": "cloud.provider", "value": {"stringValue": cloud_provider}},
                        {"key": "cloud.platform", "value": {"stringValue": f"{cloud_provider}_eks"}},
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {
                            "name": "openinference.instrumentation.google_adk",
                            "version": "1.0.0",
                        },
                        "spans": spans,
                    }
                ],
            }
        ]
    }
    headers = _get_auth_headers(project_a_id)
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(
            "https://telemetry.googleapis.com/v1/traces",
            headers=headers,
            json=otlp_payload,
        )
        resp.raise_for_status()
        return resp.status_code


def write_correlated_log_to_project_a(
    trace_id: str,
    span_id: str,
    message: str,
    payload: dict[str, Any],
    project_a_id: str = PROJECT_A_ID,
    agent_urn: str = OFF_GCP_AGENT_ID,
    service_name: str = OFF_GCP_SERVICE_NAME,
    cloud_provider: str = OFF_GCP_CLOUD_PROVIDER,
) -> int:
    """Writes a trace-correlated structured log into Project A's _Default Log Analytics bucket."""
    body = {
        "logName": f"projects/{project_a_id}/logs/off-gcp-agent-telemetry",
        "resource": {
            "type": "generic_task",
            "labels": {
                "project_id": project_a_id,
                "location": REGION,
                "namespace": cloud_provider,
                "job": service_name,
                "task_id": agent_urn,
            },
        },
        "entries": [
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "severity": "INFO",
                "trace": f"projects/{project_a_id}/traces/{trace_id}",
                "spanId": span_id,
                "traceSampled": True,
                "jsonPayload": {
                    "message": message,
                    "component": "off-gcp-agent-otlp-bridge",
                    "agent_id": agent_urn,
                    "service_name": service_name,
                    "cloud_provider": cloud_provider,
                    **payload,
                },
            }
        ],
    }
    headers = _get_auth_headers(project_a_id)
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(
            "https://logging.googleapis.com/v2/entries:write",
            headers=headers,
            json=body,
        )
        resp.raise_for_status()
        return resp.status_code


def build_w3c_traceparent(trace_id: str, span_id: str, sampled: bool = True) -> str:
    """Returns a W3C Trace Context `traceparent` header string (`00-<trace_id>-<span_id>-01`)."""
    flags = "01" if sampled else "00"
    return f"00-{trace_id}-{span_id}-{flags}"


def run_demo_correlated_trace() -> None:
    """Emits a full correlated off-GCP agent trace + Log Analytics entry into Project A."""
    trace_id = secrets.token_hex(16)
    root_span_id = secrets.token_hex(8)
    llm_span_id = secrets.token_hex(8)
    tool_span_id = secrets.token_hex(8)

    t0 = time.time_ns()
    t1 = t0 + 45_000_000
    t2 = t1 + 320_000_000
    t3 = t2 + 180_000_000
    t4 = t3 + 25_000_000

    spans = [
        {
            "traceId": trace_id,
            "spanId": root_span_id,
            "name": f"invoke_agent {OFF_GCP_SERVICE_NAME}",
            "kind": 1,
            "startTimeUnixNano": str(t0),
            "endTimeUnixNano": str(t4),
            "attributes": [
                {"key": "gen_ai.operation.name", "value": {"stringValue": "invoke_agent"}},
                {"key": "gen_ai.agent.name", "value": {"stringValue": OFF_GCP_SERVICE_NAME}},
                {"key": "gcp.agent.id", "value": {"stringValue": OFF_GCP_AGENT_ID}},
                {"key": "cloud.provider", "value": {"stringValue": OFF_GCP_CLOUD_PROVIDER}},
            ],
        },
        {
            "traceId": trace_id,
            "spanId": llm_span_id,
            "parentSpanId": root_span_id,
            "name": "call_llm gemini-2.5-flash",
            "kind": 3,
            "startTimeUnixNano": str(t1),
            "endTimeUnixNano": str(t2),
            "attributes": [
                {"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
                {"key": "gen_ai.system", "value": {"stringValue": "vertex_ai"}},
                {"key": "gen_ai.request.model", "value": {"stringValue": "gemini-2.5-flash"}},
                {
                    "key": "gen_ai.prompt",
                    "value": {
                        "stringValue": "Check active CVE alerts for payment-service via Central Agent Gateway."
                    },
                },
                {
                    "key": "gen_ai.completion",
                    "value": {
                        "stringValue": "Calling get_active_cve_alerts(service_name='payment-service')."
                    },
                },
                {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "142"}},
                {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "28"}},
            ],
        },
        {
            "traceId": trace_id,
            "spanId": tool_span_id,
            "parentSpanId": root_span_id,
            "name": "execute_tool get_active_cve_alerts",
            "kind": 3,
            "startTimeUnixNano": str(t2),
            "endTimeUnixNano": str(t3),
            "attributes": [
                {"key": "gen_ai.operation.name", "value": {"stringValue": "execute_tool"}},
                {"key": "gen_ai.tool.name", "value": {"stringValue": "get_active_cve_alerts"}},
                {
                    "key": "http.request.header.traceparent",
                    "value": {"stringValue": build_w3c_traceparent(trace_id, tool_span_id)},
                },
            ],
        },
    ]

    otlp_status = export_otlp_trace_to_project_a(trace_id, spans)
    log_status = write_correlated_log_to_project_a(
        trace_id=trace_id,
        span_id=tool_span_id,
        message="Off-GCP agent invoked get_active_cve_alerts via Central Agent Gateway",
        payload={
            "tool_name": "get_active_cve_alerts",
            "service_name_arg": "payment-service",
            "w3c_traceparent": build_w3c_traceparent(trace_id, tool_span_id),
        },
    )
    print(f"Exported Off-GCP OTLP Trace to Project A ({PROJECT_A_ID}): HTTP {otlp_status}")
    print(f"Wrote Correlated Log Entry to Project A (_Default Log Analytics): HTTP {log_status}")
    print(f"Trace ID: {trace_id}")
    print(
        f"Trace Explorer URL: https://console.cloud.google.com/traces/explorer;traceId={trace_id}?project={PROJECT_A_ID}"
    )


if __name__ == "__main__":
    run_demo_correlated_trace()
