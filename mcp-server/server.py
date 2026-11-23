"""Private Cloud Run MCP Server (Project B: your-project-b-id).

Use Case: SecOps Incident-to-PR Orchestrator
Exposes private enterprise vulnerability scanner alerts, internal deployment
manifests, and remediation audit logging over Streamable HTTP (/mcp).

Security Posture:
- Deployed with --ingress=internal and --no-allow-unauthenticated
- Invoked via Project A's Agent Gateway over PSC Endpoint (240.0.0.10) after
  IAP v2 verifies the calling agent's SPIFFE identity.
"""

from contextvars import ContextVar
from datetime import datetime, timezone
import json
import os
import secrets
import sys
import time
from typing import Any

PORT = int(os.environ.get("PORT", "8080"))
PROJECT_A_ID = os.environ.get("PROJECT_A_ID", os.environ.get("GOOGLE_CLOUD_PROJECT", "your-project-a-id"))
PROJECT_B_ID = os.environ.get("PROJECT_B_ID", os.environ.get("GOOGLE_CLOUD_PROJECT", "your-project-b-id"))
_CURRENT_TRACE_CONTEXT: ContextVar[dict[str, str]] = ContextVar("_CURRENT_TRACE_CONTEXT", default={})


def _emit_correlated_mcp_log_and_span(
    tool_name: str,
    arguments: dict[str, Any],
    start_ns: int,
    end_ns: int,
    status: str = "OK",
) -> None:
    """Emits a structured log line and OTLP child span correlated to Project A's Trace Waterfall."""
    ctx = _CURRENT_TRACE_CONTEXT.get()
    trace_id = ctx.get("trace_id") or secrets.token_hex(16)
    parent_span_id = ctx.get("span_id") or ""
    span_id = secrets.token_hex(8)
    caller_spiffe = ctx.get("caller_spiffe", "spiffe-via-agent-gateway")

    log_entry = {
        "severity": "INFO",
        "message": f"[INTERNAL_MCP_SERVER] Executed tool '{tool_name}' ({status})",
        "component": "internal-secops-mcp-server",
        "tool_name": tool_name,
        "arguments": arguments,
        "caller_spiffe_principal": caller_spiffe,
        "logging.googleapis.com/trace": f"projects/{PROJECT_A_ID}/traces/{trace_id}",
        "logging.googleapis.com/spanId": span_id,
        "logging.googleapis.com/trace_sampled": True,
    }
    print(json.dumps(log_entry), file=sys.stdout, flush=True)

    # Export child span to Project A's Cloud Trace via OTLP HTTP/JSON (telemetry.googleapis.com)
    try:
        import google.auth
        from google.auth.transport.requests import Request
        import httpx

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(Request())
        span_payload: dict[str, Any] = {
            "traceId": trace_id,
            "spanId": span_id,
            "name": f"mcp.tool/{tool_name}",
            "kind": 2,  # SPAN_KIND_SERVER
            "startTimeUnixNano": str(start_ns),
            "endTimeUnixNano": str(end_ns),
            "attributes": [
                {"key": "gen_ai.operation.name", "value": {"stringValue": "execute_tool"}},
                {"key": "gen_ai.tool.name", "value": {"stringValue": tool_name}},
                {"key": "mcp.server.name", "value": {"stringValue": "internal-secops-mcp-server"}},
                {"key": "gcp.project_id", "value": {"stringValue": PROJECT_A_ID}},
            ],
        }
        if len(parent_span_id) == 16:
            span_payload["parentSpanId"] = parent_span_id

        otlp_body = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "gcp.project_id", "value": {"stringValue": PROJECT_A_ID}},
                            {"key": "service.name", "value": {"stringValue": "internal-secops-mcp-server"}},
                            {"key": "cloud.Resource.project_id", "value": {"stringValue": PROJECT_B_ID}},
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "secops.mcp.server"},
                            "spans": [span_payload],
                        }
                    ],
                }
            ]
        }
        with httpx.Client(timeout=2.0) as client:
            client.post(
                "https://telemetry.googleapis.com/v1/traces",
                headers={
                    "Authorization": f"Bearer {creds.token}",
                    "Content-Type": "application/json",
                    "x-goog-user-project": PROJECT_A_ID,
                },
                json=otlp_body,
            )
    except Exception:
        pass

try:
    from mcp.server.fastmcp import FastMCP
    try:
        from mcp.server.transport_security import TransportSecuritySettings
        mcp = FastMCP(
            "internal-secops-mcp-server",
            host="0.0.0.0",
            port=PORT,
            stateless_http=True,
            json_response=True,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=False
            ),
        )
    except (ImportError, TypeError):
        mcp = FastMCP(
            "internal-secops-mcp-server",
            host="0.0.0.0",
            port=PORT,
            stateless_http=True,
            json_response=True,
        )
    _IS_MCP_V2 = False
except ModuleNotFoundError:
    from mcp.server.mcpserver import MCPServer
    mcp = MCPServer(
        "internal-secops-mcp-server",
        stateless_http=True,
        json_response=True,
    )
    _IS_MCP_V2 = True

# Simulated Private Enterprise Vulnerability & Deployment Database (inside Project B)
_CVE_DATABASE: dict[str, list[dict[str, Any]]] = {
    "payment-service": [
        {
            "cve_id": "CVE-2026-41092",
            "severity": "CRITICAL",
            "cvss_score": 9.8,
            "package_name": "cryptography",
            "installed_version": "41.0.3",
            "fixed_version": "42.0.5",
            "affected_file": "requirements.txt",
            "github_repo": "payment-service",
            "description": (
                "Remote memory corruption in PKCS12 parsing when validating "
                "merchant mTLS certificates."
            ),
            "sla_deadline_hours": 4,
            "status": "OPEN",
        },
        {
            "cve_id": "CVE-2026-38811",
            "severity": "HIGH",
            "cvss_score": 7.5,
            "package_name": "urllib3",
            "installed_version": "2.0.4",
            "fixed_version": "2.2.2",
            "affected_file": "requirements.txt",
            "github_repo": "payment-service",
            "description": (
                "Proxy-Authorization header not stripped on cross-origin redirect."
            ),
            "sla_deadline_hours": 24,
            "status": "OPEN",
        },
    ],
    "checkout-worker": [
        {
            "cve_id": "CVE-2026-29104",
            "severity": "MEDIUM",
            "cvss_score": 5.9,
            "package_name": "pydantic",
            "installed_version": "2.4.0",
            "fixed_version": "2.7.1",
            "affected_file": "pyproject.toml",
            "github_repo": "checkout-worker",
            "description": "ReDoS in custom email validator regex.",
            "sla_deadline_hours": 72,
            "status": "OPEN",
        }
    ],
}

_DEPLOYMENT_MANIFESTS: dict[str, dict[str, Any]] = {
    "payment-service": {
        "service_name": "payment-service",
        "environment": "prod-us-central1",
        "gcp_project": "proj-pci-prod-01",
        "current_image_digest": "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
        "oncall_team": "secops-payments@example.com",
        "change_freeze_active": False,
        "requires_3lo_author_attribution": True,
    },
    "checkout-worker": {
        "service_name": "checkout-worker",
        "environment": "prod-us-central1",
        "gcp_project": "proj-commerce-prod-02",
        "current_image_digest": "sha256:4b227777d4dd1fc61c6f884f48641d02b4d121d3fd328cb08b5531fcacdabf8a",
        "oncall_team": "sre-commerce@example.com",
        "change_freeze_active": False,
        "requires_3lo_author_attribution": True,
    },
}

_REMEDIATION_LEDGER: list[dict[str, Any]] = []


@mcp.tool()
def get_active_cve_alerts(service_name: str = "all") -> dict[str, Any]:
    """Retrieve active production CVE alerts from the internal SecOps scanner.

    Args:
        service_name: Name of the internal service (e.g., 'payment-service',
          'checkout-worker', or 'all' to list all services).
    """
    start_ns = time.time_ns()
    if service_name.lower() == "all":
        res = {
            "source": "project-b-internal-secops-scanner",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "services": _CVE_DATABASE,
        }
        _emit_correlated_mcp_log_and_span("get_active_cve_alerts", {"service_name": service_name}, start_ns, time.time_ns())
        return res
    alerts = _CVE_DATABASE.get(service_name, [])
    res = {
        "source": "project-b-internal-secops-scanner",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "service_name": service_name,
        "alert_count": len(alerts),
        "alerts": alerts,
    }
    _emit_correlated_mcp_log_and_span("get_active_cve_alerts", {"service_name": service_name}, start_ns, time.time_ns())
    return res


@mcp.tool()
def get_internal_deployment_manifest(service_name: str) -> dict[str, Any]:
    """Fetch the internal production deployment metadata and policy requirements for a service.

    Args:
        service_name: Name of the service (e.g., 'payment-service').
    """
    start_ns = time.time_ns()
    manifest = _DEPLOYMENT_MANIFESTS.get(service_name)
    if not manifest:
        res = {
            "error": f"Service '{service_name}' not found in internal manifest registry.",
            "available_services": list(_DEPLOYMENT_MANIFESTS.keys()),
        }
        _emit_correlated_mcp_log_and_span("get_internal_deployment_manifest", {"service_name": service_name}, start_ns, time.time_ns(), status="NOT_FOUND")
        return res
    _emit_correlated_mcp_log_and_span("get_internal_deployment_manifest", {"service_name": service_name}, start_ns, time.time_ns())
    return manifest


@mcp.tool()
def record_remediation_ticket(
    cve_id: str,
    service_name: str,
    github_pr_or_issue_url: str,
    remediated_by_github_user: str,
) -> dict[str, Any]:
    """Record a GitHub remediation PR or Issue URL into the internal SecOps compliance ledger.

    Args:
        cve_id: The CVE identifier being remediated (e.g., 'CVE-2026-41092').
        service_name: The affected internal service name (e.g., 'payment-service').
        github_pr_or_issue_url: Full URL of the GitHub Pull Request or Issue created via 3LO.
        remediated_by_github_user: The GitHub username of the engineer who authorized via 3LO.
    """
    start_ns = time.time_ns()
    entry = {
        "ticket_id": f"SECOPS-REM-{len(_REMEDIATION_LEDGER) + 1001}",
        "cve_id": cve_id,
        "service_name": service_name,
        "github_pr_or_issue_url": github_pr_or_issue_url,
        "remediated_by_github_user": remediated_by_github_user,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "compliance_status": "REMEDIATION_IN_PROGRESS",
    }
    _REMEDIATION_LEDGER.append(entry)

    # Update in-memory CVE status if found
    for alert in _CVE_DATABASE.get(service_name, []):
        if alert["cve_id"] == cve_id:
            alert["status"] = "PR_OPENED"
            alert["remediation_url"] = github_pr_or_issue_url

    _emit_correlated_mcp_log_and_span(
        "record_remediation_ticket",
        {
            "cve_id": cve_id,
            "service_name": service_name,
            "github_pr_or_issue_url": github_pr_or_issue_url,
            "remediated_by_github_user": remediated_by_github_user,
        },
        start_ns,
        time.time_ns(),
    )
    return {
        "message": "Remediation recorded in Project B internal SecOps ledger.",
        "ledger_entry": entry,
    }


def _wrap_asgi_with_w3c_traceparent(asgi_app: Any) -> Any:
    """ASGI wrapper that extracts W3C traceparent / X-Cloud-Trace-Context headers from Agent Gateway."""
    async def _wrapped(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            raw_headers = {
                k.decode("latin1").lower(): v.decode("latin1")
                for k, v in (scope.get("headers") or [])
            }
            traceparent = raw_headers.get("traceparent", "")
            x_cloud_trace = raw_headers.get("x-cloud-trace-context", "")
            trace_id = ""
            span_id = ""
            if traceparent:
                parts = traceparent.split("-")
                if len(parts) >= 3:
                    trace_id = parts[1]
                    span_id = parts[2]
            elif x_cloud_trace and "/" in x_cloud_trace:
                t_part, s_part = x_cloud_trace.split("/", 1)
                trace_id = t_part
                span_offset = s_part.split(";")[0]
                if span_offset.isdigit():
                    span_id = f"{int(span_offset):016x}"
            token = _CURRENT_TRACE_CONTEXT.set(
                {
                    "trace_id": trace_id,
                    "span_id": span_id,
                    "caller_spiffe": raw_headers.get(
                        "x-goog-authenticated-user-id",
                        raw_headers.get("x-serverless-authenticated-user-id", "spiffe-via-agent-gateway"),
                    ),
                }
            )
            try:
                await asgi_app(scope, receive, send)
            finally:
                _CURRENT_TRACE_CONTEXT.reset(token)
        else:
            await asgi_app(scope, receive, send)

    return _wrapped


if __name__ == "__main__":
    # Exposes Streamable HTTP MCP endpoint at /mcp on PORT (default 8080) with W3C traceparent extraction
    try:
        import uvicorn

        asgi_app = mcp.streamable_http_app()
        uvicorn.run(
            _wrap_asgi_with_w3c_traceparent(asgi_app),
            host="0.0.0.0",
            port=PORT,
        )
    except Exception:
        if _IS_MCP_V2:
            mcp.run(transport="streamable-http", host="0.0.0.0", port=PORT)
        else:
            mcp.run(transport="streamable-http")
