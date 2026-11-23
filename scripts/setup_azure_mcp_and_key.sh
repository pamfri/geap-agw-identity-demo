#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# Cross-Cloud Azure MCP Integration & Cloud Trace Writer SA Provisioning
# ==============================================================================
# 1. Creates a dedicated least-privilege Service Account ('sa-azure-mcp-traces')
#    in Project A with ONLY Cloud Trace / OTLP write roles:
#      - roles/cloudtrace.agent
#      - roles/telemetry.tracesWriter
#    and generates a JSON key outside the git working tree ('/tmp/azure-mcp-trace-writer-key.json')
#    for the external Azure MCP Server (`AZURE_MCP_URL`).
# 2. Registers 'azure-weather-mcp' ("Cross-Cloud Azure Weather MCP Server") in
#    Project A's Agent Registry (tool: GetWeather).
# 3. Binds IAP v2 (roles/iap.egressor) on 'azure-weather-mcp' so Agent 1
#    (RE_1_ID) is ALLOWED and Agent 2 (RE_2_ID) is DENIED (403).
# 4. Updates 'enterprise-egress-agw-ma-policy' (CONTENT_AUTHZ) so Model Armor
#    inspects both '.run.app' and '.azurewebsites.net' MCP calls inline.
# ==============================================================================

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
export AZURE_MCP_URL="${AZURE_MCP_URL:-https://your-azure-mcp-app.azurewebsites.net/runtime/webhooks/mcp}"

PROJECT_A_NUM=$(gcloud projects describe "${PROJECT_A_ID}" --format="value(projectNumber)" --quiet)
PROJECT_B_NUM=$(gcloud projects describe "${PROJECT_B_ID}" --format="value(projectNumber)" --quiet)
ORG_ID="${ORG_ID:-$(gcloud projects get-ancestors "${PROJECT_A_ID}" --format="value(id)" --quiet | tail -n 1)}"
RE_1_ID="${RE_1_ID:-}"
RE_2_ID="${RE_2_ID:-}"
if [[ -z "${RE_1_ID}" || -z "${RE_2_ID}" ]]; then
  echo "ERROR: RE_1_ID and RE_2_ID must be set in .env or agents/spiffe_identities.env (run 'python3 agents/deploy_agents.py' first)." >&2
  exit 1
fi
AGENT_1_SPIFFE="${AGENT_1_SPIFFE:-principal://agents.global.org-${ORG_ID}.system.id.goog/resources/aiplatform/projects/${PROJECT_A_NUM}/locations/${REGION}/reasoningEngines/${RE_1_ID}}"
AGENT_2_SPIFFE="${AGENT_2_SPIFFE:-principal://agents.global.org-${ORG_ID}.system.id.goog/resources/aiplatform/projects/${PROJECT_B_NUM}/locations/${REGION}/reasoningEngines/${RE_2_ID}}"
PRINCIPAL_SET_A="principalSet://agents.global.org-${ORG_ID}.system.id.goog/attribute.platformContainer/aiplatform/projects/${PROJECT_A_NUM}"
PRINCIPAL_SET_B="principalSet://agents.global.org-${ORG_ID}.system.id.goog/attribute.platformContainer/aiplatform/projects/${PROJECT_B_NUM}"

AZURE_SA_NAME="sa-azure-mcp-traces"
AZURE_SA_EMAIL="${AZURE_SA_NAME}@${PROJECT_A_ID}.iam.gserviceaccount.com"
KEY_OUT_PATH="${KEY_OUT_PATH:-/tmp/azure-mcp-trace-writer-key.json}"

echo "[1/4] Creating least-privilege Cloud Trace Writer Service Account (${AZURE_SA_EMAIL})..."
if ! gcloud iam service-accounts describe "${AZURE_SA_EMAIL}" --project="${PROJECT_A_ID}" --quiet >/dev/null 2>&1; then
  gcloud iam service-accounts create "${AZURE_SA_NAME}" \
    --display-name="Cross-Cloud Azure MCP Server Cloud Trace Writer SA" \
    --description="Least-privilege SA for Cross-Cloud Azure MCP Server to export OTLP/Cloud Trace spans to ${PROJECT_A_ID}" \
    --project="${PROJECT_A_ID}" \
    --quiet
fi

echo "  -> Granting roles/cloudtrace.agent and roles/telemetry.tracesWriter on ${PROJECT_A_ID}..."
for trace_role in roles/cloudtrace.agent roles/telemetry.tracesWriter; do
  gcloud projects add-iam-policy-binding "${PROJECT_A_ID}" \
    --member="serviceAccount:${AZURE_SA_EMAIL}" \
    --role="${trace_role}" \
    --quiet >/dev/null
done

if [[ ! -f "${KEY_OUT_PATH}" ]]; then
  echo "  -> Generating JSON key at ${KEY_OUT_PATH}..."
  if gcloud iam service-accounts keys create "${KEY_OUT_PATH}" \
    --iam-account="${AZURE_SA_EMAIL}" \
    --project="${PROJECT_A_ID}" \
    --quiet; then
    chmod 600 "${KEY_OUT_PATH}"
    echo "  -> Created key file: ${KEY_OUT_PATH}"
  else
    echo "  [WARN] Key creation blocked by org policy (iam.disableServiceAccountKeyCreation)."
    echo "         Temporarily exempt ${PROJECT_A_ID} or use Workload Identity Federation."
  fi
else
  echo "  -> Key file already exists at ${KEY_OUT_PATH} (skipping new key creation)."
fi

mkdir -p /tmp/agw-cfg
cat > /tmp/agw-cfg/azure-weather-toolspec.json << 'EOF'
{
  "tools": [
    {
      "name": "GetWeather",
      "description": "Retrieve current live weather conditions for a location/city from the cross-cloud Azure MCP server.",
      "inputSchema": {
        "type": "object",
        "properties": {
          "location": {
            "type": "string",
            "description": "Location or city name (e.g., Zurich, Vienna, London, San Francisco)."
          },
          "city": {
            "type": "string",
            "description": "City name (e.g., Zurich, Vienna, London, San Francisco)."
          },
          "country": {
            "type": "string",
            "description": "Optional country name or code (e.g., AT, CH, UK, US)."
          }
        }
      }
    },
    {
      "name": "get_weather",
      "description": "Retrieve current live weather conditions for a location/city from the cross-cloud Azure MCP server.",
      "inputSchema": {
        "type": "object",
        "properties": {
          "location": {
            "type": "string",
            "description": "Location or city name (e.g., Zurich, Vienna, London, San Francisco)."
          },
          "city": {
            "type": "string",
            "description": "City name (e.g., Zurich, Vienna, London, San Francisco)."
          },
          "country": {
            "type": "string",
            "description": "Optional country name or code (e.g., AT, CH, UK, US)."
          }
        }
      }
    }
  ]
}
EOF

echo "[2/4] Registering 'azure-weather-mcp' (${AZURE_MCP_URL}) in Project A Agent Registry..."
if gcloud alpha agent-registry services describe azure-weather-mcp \
  --project="${PROJECT_A_ID}" --location="${REGION}" --quiet >/dev/null 2>&1; then
  gcloud alpha agent-registry services update azure-weather-mcp \
    --project="${PROJECT_A_ID}" \
    --location="${REGION}" \
    --display-name="Cross-Cloud Azure Weather MCP Server" \
    --description="External MCP Server hosted on Azure App Service/Functions exposing GetWeather" \
    --mcp-server-spec-type=tool-spec \
    --mcp-server-spec-content=/tmp/agw-cfg/azure-weather-toolspec.json \
    --interfaces="url=${AZURE_MCP_URL},protocolBinding=JSONRPC" \
    --quiet >/dev/null
else
  gcloud alpha agent-registry services create azure-weather-mcp \
    --project="${PROJECT_A_ID}" \
    --location="${REGION}" \
    --display-name="Cross-Cloud Azure Weather MCP Server" \
    --description="External MCP Server hosted on Azure App Service/Functions exposing GetWeather" \
    --mcp-server-spec-type=tool-spec \
    --mcp-server-spec-content=/tmp/agw-cfg/azure-weather-toolspec.json \
    --interfaces="url=${AZURE_MCP_URL},protocolBinding=JSONRPC" \
    --quiet
fi

echo "[3/4] Binding IAP v2 (roles/iap.egressor) on 'azure-weather-mcp' (Allowing Agent 1, Denying Agent 2)..."
AZURE_MCP_UID=$(gcloud alpha agent-registry mcp-servers list \
  --project="${PROJECT_A_ID}" \
  --location="${REGION}" \
  --filter="displayName='Cross-Cloud Azure Weather MCP Server'" \
  --limit=5 \
  --format="value(name)" \
  --quiet | awk -F'/' '{print $NF}')

cat > /tmp/agw-cfg/iap-azure-agent1-only.json <<EOF
{
  "bindings": [
    {
      "role": "roles/iap.egressor",
      "members": [
        "${AGENT_1_SPIFFE}"
      ]
    }
  ]
}
EOF

if [[ -n "${AZURE_MCP_UID}" ]]; then
  gcloud alpha iap web set-iam-policy /tmp/agw-cfg/iap-azure-agent1-only.json \
    --project="${PROJECT_A_ID}" \
    --region="${REGION}" \
    --resource-type=agent-registry \
    --mcp-server="${AZURE_MCP_UID}" \
    --quiet >/dev/null
  echo "  -> Bound roles/iap.egressor on MCP server UID '${AZURE_MCP_UID}' to Agent 1 (${RE_1_ID}) ONLY."
fi

echo "[3b/4] Binding Tool-Level CEL condition on 'internal-cloudrun-mcp' (Option A: Agent 1 = All Tools, Agent 2 = Read-Only Tools ONLY)..."
INTERNAL_MCP_UID=$(gcloud alpha agent-registry mcp-servers list \
  --project="${PROJECT_A_ID}" \
  --location="${REGION}" \
  --filter="displayName='Internal SecOps Cloud Run MCP Server'" \
  --limit=5 \
  --format="value(name)" \
  --quiet | awk -F'/' '{print $NF}')

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

if [[ -n "${INTERNAL_MCP_UID}" ]]; then
  gcloud alpha iap web set-iam-policy /tmp/agw-cfg/iap-internal-mcp-cel.json \
    --project="${PROJECT_A_ID}" \
    --region="${REGION}" \
    --resource-type=agent-registry \
    --mcp-server="${INTERNAL_MCP_UID}" \
    --quiet >/dev/null
  echo "  -> Bound Tool-Level CEL condition on 'internal-cloudrun-mcp' ('${INTERNAL_MCP_UID}'): Agent 1 = All Tools, Agent 2 = Read-Only Tools ONLY."
fi

echo "[4/4] Updating Model Armor CONTENT_AUTHZ policy to inspect both .run.app and .azurewebsites.net MCP calls..."
cat > /tmp/agw-cfg/ma-authz-policy.yaml <<EOF
name: enterprise-egress-agw-ma-policy
target:
  resources:
    - "projects/${PROJECT_A_ID}/locations/${REGION}/agentGateways/enterprise-egress-agw"
policyProfile: CONTENT_AUTHZ
action: CUSTOM
httpRules:
  - to:
      operations:
        - hosts:
            - suffix: ".run.app"
            - suffix: ".azurewebsites.net"
customProvider:
  authzExtension:
    resources:
      - "projects/${PROJECT_A_ID}/locations/${REGION}/authzExtensions/enterprise-egress-agw-ma-ext"
EOF

gcloud network-security authz-policies import enterprise-egress-agw-ma-policy \
  --project="${PROJECT_A_ID}" \
  --location="${REGION}" \
  --source="/tmp/agw-cfg/ma-authz-policy.yaml" \
  --quiet

echo ""
echo "================================================================================"
echo "✅ Azure MCP Server Registered & Cloud Trace Writer Key Ready!"
echo "================================================================================"
echo "1. Send this JSON key file to your colleague:"
echo "     ${KEY_OUT_PATH}"
echo ""
echo "2. Ask your colleague to set these environment variables on the Azure MCP Server:"
echo "     GOOGLE_CLOUD_PROJECT=${PROJECT_A_ID}"
echo "     GOOGLE_APPLICATION_CREDENTIALS=/path/to/azure-mcp-trace-writer-key.json"
echo "     TRACE_EXPORTER=cloud_trace"
echo "     MCP_OTEL_SERVICE_NAME=azure-weather-mcp"
echo "================================================================================"
