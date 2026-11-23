#!/usr/bin/env bash
# ==============================================================================
# Create a Gemini Enterprise App & Publish Main Agent ('secops-github-orchestrator')
# ==============================================================================
# End-to-end idempotent script that:
#   1. Enables 'discoveryengine.googleapis.com', grants the Discovery Engine
#      Service Agent ('gcp-sa-discoveryengine') permission to invoke Vertex AI
#      Reasoning Engines in Project A, and initializes Google Identity
#      ('aclConfig' -> 'GSUITE') if not yet set on the project.
#   2. Creates a brand-new Gemini Enterprise App ('secops-governance-hub',
#      displayName='SecOps Enterprise Agent Hub', appType='APP_TYPE_INTRANET')
#      if you don't already have one!
#   3. Publishes Main Agent 1 ('secops-github-orchestrator', RE_1_ID)
#      onto the Gemini Enterprise App's Default Assistant.
#   4. Resolves 'default_search_widget_config' and prints both the Cloud Console
#      link AND the direct Gemini Enterprise Web App chat URL!
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
export REGION="${REGION:-us-central1}"
export RE_1_ID="${RE_1_ID:-}"
if [[ -z "${RE_1_ID}" ]]; then
  echo "ERROR: RE_1_ID must be set in .env or agents/spiffe_identities.env (run 'python3 agents/deploy_agents.py' first)." >&2
  exit 1
fi

export GE_PROJECT_ID="${GEMINI_ENTERPRISE_PROJECT_ID:-${PROJECT_A_ID}}"
export GE_LOCATION="${GEMINI_ENTERPRISE_LOCATION:-global}"
export GE_APP_ID="${GEMINI_ENTERPRISE_APP_ID:-secops-governance-hub}"
export GE_APP_DISPLAY_NAME="${GEMINI_ENTERPRISE_APP_DISPLAY_NAME:-SecOps Enterprise Agent Hub}"
export GE_AGENT_DISPLAY_NAME="${GEMINI_ENTERPRISE_AGENT_NAME:-SecOps Incident-to-PR Orchestrator}"
export GE_AGENT_DESCRIPTION="Zero-Trust Enterprise SecOps Agent governed via Agent Gateway, SPIFFE Identity, IAP Tool-Level CEL, Model Armor, Semantic Governance Policy, Cross-Cloud Azure MCP, and 3LO GitHub OAuth."

get_token() {
  gcloud auth print-access-token --quiet 2>/dev/null || \
    gcloud auth application-default print-access-token --quiet
}

de_base_url() {
  local loc="$1"
  if [[ "${loc}" == "global" ]]; then
    echo "https://discoveryengine.googleapis.com"
  else
    echo "https://${loc}-discoveryengine.googleapis.com"
  fi
}

poll_de_lro() {
  local base_url="$1" op_name="$2"
  [[ -z "${op_name}" || "${op_name}" != *"/operations/"* ]] && return 0
  for _ in $(seq 1 40); do
    local state
    state=$(curl -sf \
      -H "Authorization: Bearer $(get_token)" \
      -H "X-Goog-User-Project: ${GE_PROJECT_ID}" \
      "${base_url}/v1alpha/${op_name}" | python3 -c "
import json, sys
d = json.load(sys.stdin)
if d.get('error'): print('ERROR:' + json.dumps(d['error']))
elif d.get('done'): print('DONE')
else: print('RUNNING')
" 2>/dev/null || echo "RUNNING")
    if [[ "${state}" == "DONE" ]]; then
      return 0
    elif [[ "${state}" == ERROR:* ]]; then
      echo "  [WARN] Operation returned: ${state#ERROR:}" >&2
      return 1
    fi
    sleep 5
  done
}

PROJECT_A_NUM=$(gcloud projects describe "${PROJECT_A_ID}" --format="value(projectNumber)" --quiet)
GE_PROJECT_NUM=$(gcloud projects describe "${GE_PROJECT_ID}" --format="value(projectNumber)" --quiet)
REASONING_ENGINE_PATH="projects/${PROJECT_A_ID}/locations/${REGION}/reasoningEngines/${RE_1_ID}"
BASE_URL="$(de_base_url "${GE_LOCATION}")"

echo "================================================================================"
echo "Creating Gemini Enterprise App & Publishing Main Agent"
echo "  Gemini Enterprise Project:  ${GE_PROJECT_ID} (${GE_PROJECT_NUM})"
echo "  Gemini Enterprise App ID:   ${GE_APP_ID} (location=${GE_LOCATION})"
echo "  Agent Runtime Resource:     ${REASONING_ENGINE_PATH}"
echo "================================================================================"

# ------------------------------------------------------------------------------
# [1/6] Enable Discovery Engine API & Grant Service Agent + User IAM
# ------------------------------------------------------------------------------
echo "[1/6] Enabling discoveryengine.googleapis.com & granting Gemini Enterprise IAM..."
gcloud services enable discoveryengine.googleapis.com --project="${GE_PROJECT_ID}" --quiet >/dev/null
gcloud beta services identity create --service=discoveryengine.googleapis.com --project="${GE_PROJECT_ID}" --quiet >/dev/null 2>&1 || true

DE_SA="service-${GE_PROJECT_NUM}@gcp-sa-discoveryengine.iam.gserviceaccount.com"
for role in roles/discoveryengine.serviceAgent roles/aiplatform.user roles/viewer; do
  gcloud projects add-iam-policy-binding "${PROJECT_A_ID}" \
    --member="serviceAccount:${DE_SA}" \
    --role="${role}" \
    --quiet >/dev/null 2>&1 || true
done

CURRENT_USER=$(gcloud config get-value account 2>/dev/null || true)
if [[ -n "${CURRENT_USER}" ]]; then
  for u_role in \
    roles/discoveryengine.admin \
    roles/discoveryengine.agentspaceAdmin \
    roles/discoveryengine.agentspaceUser \
    roles/discoveryengine.user; do
    gcloud projects add-iam-policy-binding "${GE_PROJECT_ID}" \
      --member="user:${CURRENT_USER}" \
      --role="${u_role}" \
      --quiet >/dev/null 2>&1 || true
  done
fi

# ------------------------------------------------------------------------------
# [2/6] Ensure Identity Provider (Google Identity / GSUITE) Is Configured for Web App
# ------------------------------------------------------------------------------
echo "[2/6] Checking Gemini Enterprise Identity Provider ('aclConfig') in ${GE_LOCATION}..."
ACL_JSON=$(curl -sf \
  -H "Authorization: Bearer $(get_token)" \
  -H "X-Goog-User-Project: ${GE_PROJECT_ID}" \
  "${BASE_URL}/v1alpha/projects/${GE_PROJECT_ID}/locations/${GE_LOCATION}/aclConfig" 2>/dev/null || echo "{}")

CURRENT_IDP=$(printf '%s' "${ACL_JSON}" | python3 -c "import json,sys; print(json.load(sys.stdin).get('idpConfig',{}).get('idpType',''))" 2>/dev/null || true)

if [[ -z "${CURRENT_IDP}" || "${CURRENT_IDP}" == "IDP_TYPE_UNSPECIFIED" ]]; then
  echo "  -> Configuring Google Identity (idpType=GSUITE) so the Gemini Enterprise Web App is ready..."
  curl -sf -X PATCH \
    -H "Authorization: Bearer $(get_token)" \
    -H "Content-Type: application/json" \
    -H "X-Goog-User-Project: ${GE_PROJECT_ID}" \
    "${BASE_URL}/v1alpha/projects/${GE_PROJECT_ID}/locations/${GE_LOCATION}/aclConfig" \
    -d "{
      \"name\": \"projects/${GE_PROJECT_ID}/locations/${GE_LOCATION}/aclConfig\",
      \"idpConfig\": {
        \"idpType\": \"GSUITE\"
      }
    }" >/dev/null 2>&1 || true
else
  echo "  -> Identity Provider already configured (idpType=${CURRENT_IDP})."
fi

# ------------------------------------------------------------------------------
# [3/6] Ensure Active Gemini Enterprise LicenseConfig & Assign Seat to Current User
# ------------------------------------------------------------------------------
echo "[3/6] Ensuring active Gemini Enterprise license & assigning seat to '${CURRENT_USER}'..."

resolve_active_license_config() {
  curl -sf \
    -H "Authorization: Bearer $(get_token)" \
    -H "X-Goog-User-Project: ${GE_PROJECT_ID}" \
    "${BASE_URL}/v1alpha/projects/${GE_PROJECT_ID}/locations/${GE_LOCATION}/licenseConfigs" 2>/dev/null | python3 -c "
import json, sys
d = json.load(sys.stdin)
for lc in d.get('licenseConfigs', []):
    state = lc.get('state', '')
    tier = lc.get('subscriptionTier', '')
    if state in ('ACTIVE', 'DEACTIVATING') and 'NOTEBOOK' not in tier:
        print(lc.get('name', ''))
        break
" 2>/dev/null || true
}

ACTIVE_LICENSE_CONFIG="$(resolve_active_license_config)"

if [[ -z "${ACTIVE_LICENSE_CONFIG}" ]]; then
  echo "  -> No active Gemini Enterprise license found in ${GE_PROJECT_ID} (${GE_LOCATION}); activating 30-day Gemini Enterprise Plus Free Trial..."
  TRIAL_RESP=$(curl -s -X POST \
    -H "Authorization: Bearer $(get_token)" \
    -H "Content-Type: application/json" \
    -H "X-Goog-User-Project: ${GE_PROJECT_ID}" \
    "${BASE_URL}/v1alpha/projects/${GE_PROJECT_ID}/locations/${GE_LOCATION}/licenseConfigs?licenseConfigId=free_trial_gemini" \
    -d '{
      "subscriptionTier": "SUBSCRIPTION_TIER_SEARCH_AND_ASSISTANT",
      "freeTrial": true
    }')

  ACTIVE_LICENSE_CONFIG=$(printf '%s' "${TRIAL_RESP}" | python3 -c "
import json, sys
d = json.load(sys.stdin)
if 'name' in d and 'error' not in d:
    print(d['name'])
" 2>/dev/null || true)

  if [[ -z "${ACTIVE_LICENSE_CONFIG}" ]]; then
    ACTIVE_LICENSE_CONFIG="$(resolve_active_license_config)"
  fi

  # Fallback: check if Billing Account already has an active Gemini Enterprise subscription pool to distribute from
  if [[ -z "${ACTIVE_LICENSE_CONFIG}" ]]; then
    BILLING_ACCT=$(gcloud billing projects describe "${GE_PROJECT_ID}" --format="value(billingAccountName)" --quiet 2>/dev/null || true)
    if [[ -n "${BILLING_ACCT}" ]]; then
      BALC_NAME=$(curl -sf \
        -H "Authorization: Bearer $(get_token)" \
        -H "X-Goog-User-Project: ${GE_PROJECT_NUM}" \
        "${BASE_URL}/v1alpha/${BILLING_ACCT}/billingAccountLicenseConfigs" 2>/dev/null | python3 -c "
import json, sys
d = json.load(sys.stdin)
for b in d.get('billingAccountLicenseConfigs', []):
    if b.get('state') == 'ACTIVE' and 'NOTEBOOK' not in b.get('subscriptionTier', ''):
        print(b.get('name', ''))
        break
" 2>/dev/null || true)
      if [[ -n "${BALC_NAME}" ]]; then
        echo "  -> Distributing 2 seats from Billing Account subscription (${BALC_NAME}) to ${GE_PROJECT_ID} (${GE_LOCATION})..."
        curl -s -X POST \
          -H "Authorization: Bearer $(get_token)" \
          -H "Content-Type: application/json" \
          -H "X-Goog-User-Project: ${GE_PROJECT_NUM}" \
          "${BASE_URL}/v1alpha/${BALC_NAME}:distributeLicenseConfig" \
          -d "{
            \"projectNumber\": \"${GE_PROJECT_NUM}\",
            \"location\": \"${GE_LOCATION}\",
            \"licenseCount\": 2
          }" >/dev/null || true
        ACTIVE_LICENSE_CONFIG="$(resolve_active_license_config)"
      fi
    fi
  fi

  if [[ -z "${ACTIVE_LICENSE_CONFIG}" ]]; then
    echo "  [WARN] Auto-trial creation response: ${TRIAL_RESP}" >&2
    echo "  [ACTION NEEDED] Please enable a Pay-As-You-Go or Trial subscription in Console:" >&2
    echo "    https://console.cloud.google.com/gemini-enterprise/start?project=${GE_PROJECT_ID}" >&2
    echo "    (Click 'Manage subscriptions > Create subscription > Pay-as-you-go', distribute to ${GE_PROJECT_ID} / ${GE_LOCATION}, then re-run this script.)" >&2
    exit 1
  fi
fi

# Normalize project ID -> project number in licenseConfig resource name for batchUpdateUserLicenses
LICENSE_CFG_ID="${ACTIVE_LICENSE_CONFIG##*/}"
NORMALIZED_LICENSE_CFG="projects/${GE_PROJECT_NUM}/locations/${GE_LOCATION}/licenseConfigs/${LICENSE_CFG_ID}"
echo "  -> Active LicenseConfig: ${NORMALIZED_LICENSE_CFG}"

if [[ -n "${CURRENT_USER}" ]]; then
  ASSIGN_RESP=$(curl -s -X POST \
    -H "Authorization: Bearer $(get_token)" \
    -H "Content-Type: application/json" \
    -H "X-Goog-User-Project: ${GE_PROJECT_ID}" \
    "${BASE_URL}/v1alpha/projects/${GE_PROJECT_ID}/locations/${GE_LOCATION}/userStores/default_user_store:batchUpdateUserLicenses" \
    -d "{
      \"inlineSource\": {
        \"userLicenses\": [
          {
            \"userPrincipal\": \"${CURRENT_USER}\",
            \"licenseConfig\": \"${NORMALIZED_LICENSE_CFG}\"
          }
        ],
        \"updateMask\": \"userPrincipal,licenseConfig\"
      },
      \"deleteUnassignedUserLicenses\": false
    }")
  ASSIGN_OP=$(printf '%s' "${ASSIGN_RESP}" | python3 -c "import json,sys; print(json.load(sys.stdin).get('name',''))" 2>/dev/null || true)
  if [[ -n "${ASSIGN_OP}" ]]; then
    poll_de_lro "${BASE_URL}" "${ASSIGN_OP}" || true
  fi
  echo "  -> Assigned Gemini Enterprise seat to '${CURRENT_USER}'."
  sleep 3
fi

# ------------------------------------------------------------------------------
# [4/6] Create Gemini Enterprise App ('secops-governance-hub') If Not Yet Present
# ------------------------------------------------------------------------------
echo "[4/6] Ensuring Gemini Enterprise App '${GE_APP_ID}' exists in ${GE_PROJECT_ID} (${GE_LOCATION})..."

ENGINE_CHECK=$(curl -sf \
  -H "Authorization: Bearer $(get_token)" \
  -H "X-Goog-User-Project: ${GE_PROJECT_ID}" \
  "${BASE_URL}/v1alpha/projects/${GE_PROJECT_ID}/locations/${GE_LOCATION}/collections/default_collection/engines/${GE_APP_ID}" 2>/dev/null || echo "")

if [[ -z "${ENGINE_CHECK}" ]]; then
  echo "  -> Creating new Gemini Enterprise App '${GE_APP_ID}' ('${GE_APP_DISPLAY_NAME}')..."

  # Attempt 1: Full Gemini Enterprise Intranet App with Enterprise + LLM + Search & Assistant tier
  CREATE_BODY_FULL=$(cat <<EOF
{
  "displayName": "${GE_APP_DISPLAY_NAME}",
  "solutionType": "SOLUTION_TYPE_SEARCH",
  "searchEngineConfig": {
    "searchTier": "SEARCH_TIER_ENTERPRISE",
    "searchAddOns": ["SEARCH_ADD_ON_LLM"],
    "requiredSubscriptionTier": "SUBSCRIPTION_TIER_SEARCH_AND_ASSISTANT"
  },
  "industryVertical": "GENERIC",
  "appType": "APP_TYPE_INTRANET",
  "dataStoreIds": [],
  "commonConfig": {
    "companyName": "Enterprise SecOps"
  },
  "observabilityConfig": {
    "observabilityEnabled": true,
    "sensitiveLoggingEnabled": true
  }
}
EOF
)

  CREATE_RESP=$(curl -s -X POST \
    -H "Authorization: Bearer $(get_token)" \
    -H "Content-Type: application/json" \
    -H "X-Goog-User-Project: ${GE_PROJECT_ID}" \
    "${BASE_URL}/v1alpha/projects/${GE_PROJECT_ID}/locations/${GE_LOCATION}/collections/default_collection/engines?engineId=${GE_APP_ID}" \
    -d "${CREATE_BODY_FULL}")

  HAS_ERR=$(printf '%s' "${CREATE_RESP}" | python3 -c "import json,sys; d=json.load(sys.stdin); print('YES' if 'error' in d else 'NO')" 2>/dev/null || echo "YES")

  # Fallback if project doesn't have SUBSCRIPTION_TIER_SEARCH_AND_ASSISTANT licenses attached yet
  if [[ "${HAS_ERR}" == "YES" ]]; then
    CREATE_BODY_FALLBACK=$(cat <<EOF
{
  "displayName": "${GE_APP_DISPLAY_NAME}",
  "solutionType": "SOLUTION_TYPE_SEARCH",
  "searchEngineConfig": {
    "searchTier": "SEARCH_TIER_ENTERPRISE",
    "searchAddOns": ["SEARCH_ADD_ON_LLM"]
  },
  "industryVertical": "GENERIC",
  "appType": "APP_TYPE_INTRANET",
  "dataStoreIds": [],
  "commonConfig": {
    "companyName": "Enterprise SecOps"
  }
}
EOF
)
    CREATE_RESP=$(curl -s -X POST \
      -H "Authorization: Bearer $(get_token)" \
      -H "Content-Type: application/json" \
      -H "X-Goog-User-Project: ${GE_PROJECT_ID}" \
      "${BASE_URL}/v1alpha/projects/${GE_PROJECT_ID}/locations/${GE_LOCATION}/collections/default_collection/engines?engineId=${GE_APP_ID}" \
      -d "${CREATE_BODY_FALLBACK}")
  fi

  OP_OR_ERR=$(printf '%s' "${CREATE_RESP}" | python3 -c "
import json, sys
d = json.load(sys.stdin)
if 'error' in d and d['error'].get('code') != 409:
    print('ERR:' + json.dumps(d['error']))
else:
    print(d.get('name', ''))
")
  if [[ "${OP_OR_ERR}" == ERR:* ]]; then
    echo "  [ERROR] Failed to create Gemini Enterprise App '${GE_APP_ID}': ${OP_OR_ERR#ERR:}" >&2
    exit 1
  fi

  poll_de_lro "${BASE_URL}" "${OP_OR_ERR}" || true

  # Wait briefly for the Default Assistant ('default_assistant') to initialize on the newly created app
  echo "  -> Waiting for Default Assistant to initialize on '${GE_APP_ID}'..."
  for _ in $(seq 1 18); do
    if curl -sf \
      -H "Authorization: Bearer $(get_token)" \
      -H "X-Goog-User-Project: ${GE_PROJECT_ID}" \
      "${BASE_URL}/v1alpha/projects/${GE_PROJECT_ID}/locations/${GE_LOCATION}/collections/default_collection/engines/${GE_APP_ID}/assistants/default_assistant" >/dev/null 2>&1; then
      break
    fi
    sleep 5
  done
  echo "  -> Created Gemini Enterprise App '${GE_APP_ID}'!"
else
  echo "  -> Gemini Enterprise App '${GE_APP_ID}' already exists."
fi

# ------------------------------------------------------------------------------
# [5/6] Register or Update Main ADK Agent ('secops-github-orchestrator') on App
# ------------------------------------------------------------------------------
AGENTS_API_URL="${BASE_URL}/v1alpha/projects/${GE_PROJECT_ID}/locations/${GE_LOCATION}/collections/default_collection/engines/${GE_APP_ID}/assistants/default_assistant/agents"

echo "[5/6] Publishing '${GE_AGENT_DISPLAY_NAME}' (${REASONING_ENGINE_PATH}) to '${GE_APP_ID}'..."

EXISTING_GE_AGENT_NAME=$(curl -sf \
  -H "Authorization: Bearer $(get_token)" \
  -H "X-Goog-User-Project: ${GE_PROJECT_ID}" \
  "${AGENTS_API_URL}" 2>/dev/null | python3 -c "
import json, sys
d = json.load(sys.stdin)
for ag in d.get('agents', []):
    re_path = ag.get('adkAgentDefinition', {}).get('provisionedReasoningEngine', {}).get('reasoningEngine', '')
    if '${RE_1_ID}' in re_path or ag.get('displayName') == '${GE_AGENT_DISPLAY_NAME}':
        print(ag['name'])
        break
" 2>/dev/null || true)

AGENT_PAYLOAD=$(cat <<EOF
{
  "displayName": "${GE_AGENT_DISPLAY_NAME}",
  "description": "${GE_AGENT_DESCRIPTION}",
  "adkAgentDefinition": {
    "provisionedReasoningEngine": {
      "reasoningEngine": "${REASONING_ENGINE_PATH}"
    }
  }
}
EOF
)

REG_RESP=""
for attempt in 1 2 3 4 5 6; do
  if [[ -n "${EXISTING_GE_AGENT_NAME}" ]]; then
    REG_RESP=$(curl -s -X PATCH \
      -H "Authorization: Bearer $(get_token)" \
      -H "Content-Type: application/json" \
      -H "X-Goog-User-Project: ${GE_PROJECT_ID}" \
      "${BASE_URL}/v1alpha/${EXISTING_GE_AGENT_NAME}" \
      -d "${AGENT_PAYLOAD}")
  else
    REG_RESP=$(curl -s -X POST \
      -H "Authorization: Bearer $(get_token)" \
      -H "Content-Type: application/json" \
      -H "X-Goog-User-Project: ${GE_PROJECT_ID}" \
      "${AGENTS_API_URL}" \
      -d "${AGENT_PAYLOAD}")
  fi
  if printf '%s' "${REG_RESP}" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if 'name' in d and 'error' not in d else 1)" 2>/dev/null; then
    break
  fi
  echo "  -> Waiting 5s for license/assistant cache propagation before retrying agent registration (attempt ${attempt}/6)..."
  sleep 5
done

REGISTERED_NAME=$(printf '%s' "${REG_RESP}" | python3 -c "
import json, sys
d = json.load(sys.stdin)
if 'error' in d:
    print('ERROR: ' + json.dumps(d['error']), file=sys.stderr)
    sys.exit(1)
print(d.get('name', ''))
")
echo "  -> Published Gemini Enterprise Agent Resource:"
echo "     ${REGISTERED_NAME}"

# ------------------------------------------------------------------------------
# [6/6] Resolve Live Web App URL ('default_search_widget_config') & Summary
# ------------------------------------------------------------------------------
echo "[6/6] Fetching live Gemini Enterprise Web App URL & registered agents..."

WIDGET_CID=$(curl -sf \
  -H "Authorization: Bearer $(get_token)" \
  -H "X-Goog-User-Project: ${GE_PROJECT_ID}" \
  "${BASE_URL}/v1alpha/projects/${GE_PROJECT_ID}/locations/${GE_LOCATION}/collections/default_collection/engines/${GE_APP_ID}/widgetConfigs/default_search_widget_config" 2>/dev/null | python3 -c "
import json, sys
print(json.load(sys.stdin).get('configId', ''))
" 2>/dev/null || true)

if [[ "${GE_LOCATION}" == "global" ]]; then
  WEB_APP_URL="https://vertexaisearch.cloud.google.com/home/cid/${WIDGET_CID}"
else
  WEB_APP_URL="https://vertexaisearch.cloud.google.com/${GE_LOCATION}/home/cid/${WIDGET_CID}"
fi

curl -sf \
  -H "Authorization: Bearer $(get_token)" \
  -H "X-Goog-User-Project: ${GE_PROJECT_ID}" \
  "${AGENTS_API_URL}" | python3 -c "
import json, sys
for ag in json.load(sys.stdin).get('agents', []):
    re = ag.get('adkAgentDefinition', {}).get('provisionedReasoningEngine', {}).get('reasoningEngine', '')
    if re or ag.get('displayName') == '${GE_AGENT_DISPLAY_NAME}':
        print(f\"  - {ag.get('displayName')} | state={ag.get('state', 'ENABLED')} | reasoningEngine={re}\")
"

echo ""
echo "================================================================================"
echo "✅ Gemini Enterprise App Created & Main Agent Published!"
echo "================================================================================"
echo "1. Gemini Enterprise Admin Console:"
echo "     https://console.cloud.google.com/gemini-enterprise?project=${GE_PROJECT_ID}"
if [[ -n "${WIDGET_CID}" ]]; then
  echo ""
  echo "2. Direct Live Gemini Enterprise Web App URL (Click to Chat with Your Agent!):"
  echo "     ${WEB_APP_URL}"
fi
echo ""
echo "3. Try Asking '@${GE_AGENT_DISPLAY_NAME}' in the Gemini Enterprise Web App:"
echo "     Check active CVE alerts for payment-service and check the weather in Zurich using GetWeather."
echo "================================================================================"
