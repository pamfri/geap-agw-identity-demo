#!/usr/bin/env bash
# ==============================================================================
# Cloud Run MCP ('internal-cloudrun-mcp') IAP with Tool-Level CEL Condition
# ==============================================================================
# Configures and verifies fine-grained Identity-Aware Proxy (IAP) CEL governance
# on our private Cloud Run MCP server ('internal-cloudrun-mcp') via Agent Gateway:
#   - Binding 1 (Agent 1 - SecOps Orchestrator in Project A):
#       Unconditional 'roles/iap.egressor' -> Allowed to call ALL 3 tools:
#       'get_active_cve_alerts', 'get_internal_deployment_manifest', AND
#       mutating 'record_remediation_ticket'.
#   - Binding 2 (Agent 2 - SecOps Auditor in Project B):
#       Conditional 'roles/iap.egressor' with IAP CEL expression on
#       'iap.googleapis.com/mcp.toolName' -> Allowed ONLY on read-only tools
#       ('get_active_cve_alerts', 'get_internal_deployment_manifest') and MCP
#       handshake (''), and DENIED (HTTP 403) when attempting to call the
#       mutating 'record_remediation_ticket' tool on the EXACT SAME MCP server!
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "${SCRIPT_DIR}")"
if [[ -f "${ROOT_DIR}/.env" ]]; then
  source "${ROOT_DIR}/.env"
fi
if [[ -f "${ROOT_DIR}/agents/spiffe_identities.env" ]]; then
  source "${ROOT_DIR}/agents/spiffe_identities.env"
fi

export PROJECT_A_ID="${PROJECT_A_ID:-your-project-a-id}"
export PROJECT_B_ID="${PROJECT_B_ID:-your-project-b-id}"
export REGION="${REGION:-us-central1}"
export RE_1_ID="${RE_1_ID:-}"
export RE_2_ID="${RE_2_ID:-}"

if [[ -z "${RE_1_ID}" || -z "${RE_2_ID}" ]]; then
  echo "ERROR: RE_1_ID and RE_2_ID must be set in .env or agents/spiffe_identities.env (run 'python3 agents/deploy_agents.py' first)." >&2
  exit 1
fi

PROJECT_A_NUM=$(gcloud projects describe "${PROJECT_A_ID}" --format="value(projectNumber)" --quiet)
PROJECT_B_NUM=$(gcloud projects describe "${PROJECT_B_ID}" --format="value(projectNumber)" --quiet)
ORG_ID="${ORG_ID:-$(gcloud projects get-ancestors "${PROJECT_A_ID}" --format="value(id)" --quiet | tail -n 1)}"

AGENT_1_SPIFFE="${AGENT_1_SPIFFE:-principal://agents.global.org-${ORG_ID}.system.id.goog/resources/aiplatform/projects/${PROJECT_A_NUM}/locations/${REGION}/reasoningEngines/${RE_1_ID}}"
AGENT_2_SPIFFE="${AGENT_2_SPIFFE:-principal://agents.global.org-${ORG_ID}.system.id.goog/resources/aiplatform/projects/${PROJECT_B_NUM}/locations/${REGION}/reasoningEngines/${RE_2_ID}}"
PRINCIPAL_SET_A="principalSet://agents.global.org-${ORG_ID}.system.id.goog/attribute.platformContainer/aiplatform/projects/${PROJECT_A_NUM}"
PRINCIPAL_SET_B="principalSet://agents.global.org-${ORG_ID}.system.id.goog/attribute.platformContainer/aiplatform/projects/${PROJECT_B_NUM}"

echo "================================================================================"
echo "Configuring IAP Tool-Level CEL Condition on Cloud Run MCP ('internal-cloudrun-mcp')"
echo "================================================================================"

INTERNAL_MCP_UID=$(gcloud alpha agent-registry mcp-servers list \
  --project="${PROJECT_A_ID}" \
  --location="${REGION}" \
  --filter="displayName='Internal SecOps Cloud Run MCP Server'" \
  --limit=5 \
  --format="value(name)" \
  --quiet | awk -F'/' '{print $NF}')

if [[ -z "${INTERNAL_MCP_UID}" ]]; then
  echo "ERROR: 'Internal SecOps Cloud Run MCP Server' not found in ${PROJECT_A_ID} Agent Registry." >&2
  exit 1
fi
echo "[1/3] Resolved Cloud Run MCP Registry UID: ${INTERNAL_MCP_UID}"

mkdir -p /tmp/agw-cfg
cat > /tmp/agw-cfg/iap-internal-mcp-cel.json <<EOF
{
  "bindings": [
    {
      "role": "roles/iap.egressor",
      "members": [
        "${AGENT_1_SPIFFE}",
        "${PRINCIPAL_SET_A}",
        "serviceAccount:service-${PROJECT_A_NUM}@gcp-sa-aiplatform-re.iam.gserviceaccount.com",
        "serviceAccount:service-${PROJECT_A_NUM}@gcp-sa-aiplatform.iam.gserviceaccount.com"
      ]
    },
    {
      "role": "roles/iap.egressor",
      "members": [
        "${AGENT_2_SPIFFE}",
        "${PRINCIPAL_SET_B}",
        "serviceAccount:service-${PROJECT_B_NUM}@gcp-sa-aiplatform-re.iam.gserviceaccount.com",
        "serviceAccount:service-${PROJECT_B_NUM}@gcp-sa-aiplatform.iam.gserviceaccount.com"
      ],
      "condition": {
        "title": "allow_agent2_readonly_secops_tools",
        "description": "Allow Agent 2 (Project B Auditor) only read-only CVE & manifest tools on internal-cloudrun-mcp; deny mutating record_remediation_ticket",
        "expression": "api.getAttribute('iap.googleapis.com/mcp.toolName', '') == '' || api.getAttribute('iap.googleapis.com/mcp.toolName', '') == 'get_active_cve_alerts' || api.getAttribute('iap.googleapis.com/mcp.toolName', '') == 'get_internal_deployment_manifest'"
      }
    }
  ]
}
EOF

echo "[2/3] Applying conditional IAP policy on 'internal-cloudrun-mcp'..."
gcloud alpha iap web set-iam-policy /tmp/agw-cfg/iap-internal-mcp-cel.json \
  --project="${PROJECT_A_ID}" \
  --region="${REGION}" \
  --resource-type=agent-registry \
  --mcp-server="${INTERNAL_MCP_UID}" \
  --quiet >/dev/null

echo "[3/3] Verifying live IAP + CEL IAM Policy on 'internal-cloudrun-mcp':"
gcloud alpha iap web get-iam-policy \
  --project="${PROJECT_A_ID}" \
  --region="${REGION}" \
  --resource-type=agent-registry \
  --mcp-server="${INTERNAL_MCP_UID}" \
  --format=json

echo ""
echo "================================================================================"
echo "✅ Cloud Run MCP IAP Tool-Level CEL Condition Is Live!"
echo "================================================================================"
echo "How to Demo in Agent 2 Playground (internal-secops-auditor in ${PROJECT_B_ID}):"
echo "  https://console.cloud.google.com/vertex-ai/agents/agent-engines/locations/${REGION}/agent-engines/${RE_2_ID}/playground?project=${PROJECT_B_ID}"
echo ""
echo "  1. Read-Only Tool -> ALLOWED (HTTP 200 OK):"
echo "       Check active CVE alerts for payment-service."
echo ""
echo "  2. Mutating Tool on SAME Cloud Run MCP -> BLOCKED by CEL (HTTP 403 Forbidden):"
echo "       Try to record a remediation ticket for CVE-2026-41092 on payment-service with URL https://github.com/org/repo/issues/99 by secops-auditor."
echo "================================================================================"
