"""Private Cloud Run MCP Server (Project B: your-project-b-id).

Use Case: SecOps Incident-to-PR Orchestrator
Exposes private enterprise vulnerability scanner alerts, internal deployment
manifests, and remediation audit logging over Streamable HTTP (/mcp).

Security Posture:
- Deployed with --ingress=internal and --no-allow-unauthenticated
- Invoked via Project A's Agent Gateway over PSC Endpoint (240.0.0.10) after
  IAP v2 verifies the calling agent's SPIFFE identity.
"""

from datetime import datetime, timezone
import os
from typing import Any

PORT = int(os.environ.get("PORT", "8080"))

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
    if service_name.lower() == "all":
        return {
            "source": "project-b-internal-secops-scanner",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "services": _CVE_DATABASE,
        }
    alerts = _CVE_DATABASE.get(service_name, [])
    return {
        "source": "project-b-internal-secops-scanner",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "service_name": service_name,
        "alert_count": len(alerts),
        "alerts": alerts,
    }


@mcp.tool()
def get_internal_deployment_manifest(service_name: str) -> dict[str, Any]:
    """Fetch the internal production deployment metadata and policy requirements for a service.

    Args:
        service_name: Name of the service (e.g., 'payment-service').
    """
    manifest = _DEPLOYMENT_MANIFESTS.get(service_name)
    if not manifest:
        return {
            "error": f"Service '{service_name}' not found in internal manifest registry.",
            "available_services": list(_DEPLOYMENT_MANIFESTS.keys()),
        }
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

    return {
        "message": "Remediation recorded in Project B internal SecOps ledger.",
        "ledger_entry": entry,
    }


if __name__ == "__main__":
    # Exposes Streamable HTTP MCP endpoint at /mcp on PORT (default 8080)
    if _IS_MCP_V2:
        mcp.run(transport="streamable-http", host="0.0.0.0", port=PORT)
    else:
        mcp.run(transport="streamable-http")
