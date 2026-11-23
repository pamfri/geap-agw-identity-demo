#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# 0. ENVIRONMENT VARIABLES (Loads .env if present; defaults to placeholders)
# ==============================================================================
SCRIPT_PATH="${BASH_SOURCE[0]}"
if [[ "${SCRIPT_PATH}" = /* ]]; then
  SCRIPT_DIR="$(dirname "${SCRIPT_PATH}")"
else
  SCRIPT_DIR="$(cd "$(dirname "${SCRIPT_PATH}")" && pwd)"
fi
ROOT_DIR="$(dirname "${SCRIPT_DIR}")"
if [[ -f "${ROOT_DIR}/.env" ]]; then
  source "${ROOT_DIR}/.env"
fi
cd "${TMPDIR:-/tmp}"

export REGION="${REGION:-us-central1}"
export PROJECT_A_ID="${PROJECT_A_ID:-your-project-a-id}"
export PROJECT_B_ID="${PROJECT_B_ID:-your-project-b-id}"

# GitHub OAuth App Client ID & Secret (required only when creating 'github-mcp-auth' for the first time;
# configure Redirect URI: https://agentidentitycredentials.googleapis.com/v1/projects/${PROJECT_A_ID}/locations/${REGION}/authProviders/github-mcp-auth/oauthcallback)
export GITHUB_OAUTH_CLIENT_ID="${GITHUB_OAUTH_CLIENT_ID:-}"
export GITHUB_OAUTH_CLIENT_SECRET="${GITHUB_OAUTH_CLIENT_SECRET:-}"
export OAUTH_CONTINUE_URI="${OAUTH_CONTINUE_URI:-http://localhost:8501/validateUserId}"
export AZURE_MCP_URL="${AZURE_MCP_URL:-https://your-azure-mcp-app.azurewebsites.net/runtime/webhooks/mcp}"

mkdir -p /tmp/agw-cfg

# ==============================================================================
# 1. VERIFY PROJECTS A & B, AUTO-DETECT ORG_ID, AND ENABLE APIS (BATCHED <= 20)
# ==============================================================================
gcloud auth print-access-token >/dev/null

if ! gcloud projects describe "${PROJECT_A_ID}" >/dev/null 2>&1; then
  echo "Creating Project A (${PROJECT_A_ID})..."
  gcloud projects create "${PROJECT_A_ID}" \
    ${ORG_ID:+--organization="${ORG_ID}"} \
    --name="Agent Gateway Governance Hub"
  if [[ -n "${BILLING_ACCOUNT:-}" ]]; then
    gcloud billing projects link "${PROJECT_A_ID}" --billing-account="${BILLING_ACCOUNT}"
  fi
else
  echo "Using existing Project A (${PROJECT_A_ID})."
fi

if ! gcloud projects describe "${PROJECT_B_ID}" >/dev/null 2>&1; then
  echo "Creating Project B (${PROJECT_B_ID})..."
  gcloud projects create "${PROJECT_B_ID}" \
    ${ORG_ID:+--organization="${ORG_ID}"} \
    --name="Agent & Cloud Run MCP Spoke"
  if [[ -n "${BILLING_ACCOUNT:-}" ]]; then
    gcloud billing projects link "${PROJECT_B_ID}" --billing-account="${BILLING_ACCOUNT}"
  fi
else
  echo "Using existing Project B (${PROJECT_B_ID})."
fi

export PROJECT_A_NUM=$(gcloud projects describe "${PROJECT_A_ID}" --format="value(projectNumber)")
export PROJECT_B_NUM=$(gcloud projects describe "${PROJECT_B_ID}" --format="value(projectNumber)")
export ORG_ID="${ORG_ID:-$(gcloud projects get-ancestors "${PROJECT_A_ID}" --format="value(id)" | tail -n 1)}"

echo "Resolved IDs:"
echo "  PROJECT_A_ID  = ${PROJECT_A_ID} (${PROJECT_A_NUM})"
echo "  PROJECT_B_ID  = ${PROJECT_B_ID} (${PROJECT_B_NUM})"
echo "  ORG_ID        = ${ORG_ID}"

if ! gcloud network-services agent-gateways describe enterprise-egress-agw --project="${PROJECT_A_ID}" --location="${REGION}" >/dev/null 2>&1; then
  echo "Enabling APIs in Project A (${PROJECT_A_ID}) — Batch 1/2..."
  gcloud services enable \
    agentregistry.googleapis.com \
    aiplatform.googleapis.com \
    apphub.googleapis.com \
    apptopology.googleapis.com \
    cloudasset.googleapis.com \
    cloudresourcemanager.googleapis.com \
    cloudtrace.googleapis.com \
    compute.googleapis.com \
    dns.googleapis.com \
    iam.googleapis.com \
    iamcredentials.googleapis.com \
    --project="${PROJECT_A_ID}"

  echo "Enabling APIs in Project A (${PROJECT_A_ID}) — Batch 2/2..."
  gcloud services enable \
    iap.googleapis.com \
    logging.googleapis.com \
    modelarmor.googleapis.com \
    monitoring.googleapis.com \
    networksecurity.googleapis.com \
    networkservices.googleapis.com \
    observability.googleapis.com \
    securitycenter.googleapis.com \
    serviceextensions.googleapis.com \
    serviceusage.googleapis.com \
    storage.googleapis.com \
    telemetry.googleapis.com \
    --project="${PROJECT_A_ID}"

  echo "Enabling Agent Identity API (agentidentity.googleapis.com) in Project A..."
  gcloud services enable agentidentity.googleapis.com --project="${PROJECT_A_ID}" || true

  echo "Upgrading Project A & Project B _Default log buckets to Log Analytics (required for Agent Gateway Observability & Anomaly Detection UI)..."
  gcloud logging buckets update _Default \
    --location=global \
    --enable-analytics \
    --async \
    --project="${PROJECT_A_ID}" 2>/dev/null || true
  gcloud logging buckets update _Default \
    --location=global \
    --enable-analytics \
    --async \
    --project="${PROJECT_B_ID}" 2>/dev/null || true

  echo "Enabling APIs in Project B (${PROJECT_B_ID}) — Batch 1/2..."
  gcloud services enable \
    agentregistry.googleapis.com \
    aiplatform.googleapis.com \
    apphub.googleapis.com \
    apptopology.googleapis.com \
    artifactregistry.googleapis.com \
    cloudasset.googleapis.com \
    cloudbuild.googleapis.com \
    cloudresourcemanager.googleapis.com \
    cloudtrace.googleapis.com \
    compute.googleapis.com \
    dns.googleapis.com \
    --project="${PROJECT_B_ID}"

  echo "Enabling APIs in Project B (${PROJECT_B_ID}) — Batch 2/2..."
  gcloud services enable \
    iam.googleapis.com \
    iamcredentials.googleapis.com \
    logging.googleapis.com \
    modelarmor.googleapis.com \
    monitoring.googleapis.com \
    networkservices.googleapis.com \
    observability.googleapis.com \
    run.googleapis.com \
    securitycenter.googleapis.com \
    storage.googleapis.com \
    telemetry.googleapis.com \
    --project="${PROJECT_B_ID}"
else
  echo "APIs and Agent Gateway already initialized; ensuring Model Armor, App Topology & SCC APIs are enabled..."
  gcloud services enable \
    apphub.googleapis.com \
    apptopology.googleapis.com \
    cloudasset.googleapis.com \
    modelarmor.googleapis.com \
    observability.googleapis.com \
    securitycenter.googleapis.com \
    --project="${PROJECT_A_ID}" >/dev/null 2>&1 || true
  gcloud services enable \
    apphub.googleapis.com \
    apptopology.googleapis.com \
    cloudasset.googleapis.com \
    modelarmor.googleapis.com \
    observability.googleapis.com \
    securitycenter.googleapis.com \
    --project="${PROJECT_B_ID}" >/dev/null 2>&1 || true
fi

# ==============================================================================
# 2. PROJECT B: ISOLATED VPC + PSC ENDPOINT FOR GOOGLE APIS (240.0.0.10) + NAT
# ==============================================================================
echo "Configuring Isolated VPC, PSC Endpoint, Private DNS & Cloud NAT in Project B (${PROJECT_B_ID})..."

gcloud compute networks describe vpc-agw-egress --project="${PROJECT_B_ID}" >/dev/null 2>&1 || \
  gcloud compute networks create vpc-agw-egress \
    --project="${PROJECT_B_ID}" \
    --subnet-mode=custom

gcloud compute networks subnets describe subnet-agw-egress --project="${PROJECT_B_ID}" --region="${REGION}" >/dev/null 2>&1 || \
  gcloud compute networks subnets create subnet-agw-egress \
    --project="${PROJECT_B_ID}" \
    --network=vpc-agw-egress \
    --region="${REGION}" \
    --range="10.10.0.0/24" \
    --enable-private-ip-google-access

gcloud compute routers describe router-agw-nat --project="${PROJECT_B_ID}" --region="${REGION}" >/dev/null 2>&1 || \
  gcloud compute routers create router-agw-nat \
    --project="${PROJECT_B_ID}" \
    --network=vpc-agw-egress \
    --asn=16550 \
    --region="${REGION}"

gcloud compute routers nats describe nat-agw-external-mcp --router=router-agw-nat --project="${PROJECT_B_ID}" --region="${REGION}" >/dev/null 2>&1 || \
  gcloud compute routers nats create nat-agw-external-mcp \
    --project="${PROJECT_B_ID}" \
    --router=router-agw-nat \
    --region="${REGION}" \
    --auto-allocate-nat-external-ips \
    --nat-all-subnet-ip-ranges \
    --enable-logging \
    --log-filter=ALL

export PSC_EP_IP="240.0.0.10"

gcloud compute addresses describe ip-psc2gapis --project="${PROJECT_B_ID}" --global >/dev/null 2>&1 || \
  gcloud compute addresses create ip-psc2gapis \
    --project="${PROJECT_B_ID}" \
    --global \
    --purpose=PRIVATE_SERVICE_CONNECT \
    --addresses="${PSC_EP_IP}" \
    --network=vpc-agw-egress

gcloud compute forwarding-rules describe psc2gapis --project="${PROJECT_B_ID}" --global >/dev/null 2>&1 || \
  gcloud compute forwarding-rules create psc2gapis \
    --project="${PROJECT_B_ID}" \
    --global \
    --network=vpc-agw-egress \
    --address=ip-psc2gapis \
    --target-google-apis-bundle=all-apis

gcloud dns managed-zones describe run-app-restricted --project="${PROJECT_B_ID}" >/dev/null 2>&1 || \
  gcloud dns managed-zones create run-app-restricted \
    --project="${PROJECT_B_ID}" \
    --dns-name="run.app." \
    --description="Route Cloud Run URLs to internal PSC endpoint" \
    --visibility=private \
    --networks=vpc-agw-egress

gcloud dns record-sets describe "*.run.app." --type=A --zone=run-app-restricted --project="${PROJECT_B_ID}" >/dev/null 2>&1 || \
  gcloud dns record-sets create "*.run.app." \
    --project="${PROJECT_B_ID}" \
    --zone=run-app-restricted \
    --type=A \
    --ttl=300 \
    --rrdatas="${PSC_EP_IP}"

gcloud compute network-attachments describe agw-net-attachment --project="${PROJECT_B_ID}" --region="${REGION}" >/dev/null 2>&1 || \
  gcloud compute network-attachments create agw-net-attachment \
    --project="${PROJECT_B_ID}" \
    --region="${REGION}" \
    --subnets=subnet-agw-egress \
    --connection-preference=ACCEPT_AUTOMATIC

export PSC_NA_URI="projects/${PROJECT_B_ID}/regions/${REGION}/networkAttachments/agw-net-attachment"

# ==============================================================================
# 3. PROJECT B: DEPLOY PRIVATE TOKENLESS SECOPS CLOUD RUN MCP SERVER
# ==============================================================================
echo "Checking Private SecOps Cloud Run MCP Server in Project B (${PROJECT_B_ID})..."
CURRENT_MCP_MODE=$(gcloud run services describe internal-mcp-server \
  --project="${PROJECT_B_ID}" \
  --region="${REGION}" \
  --format="value(spec.template.spec.containers[0].env)" 2>/dev/null || true)
CURRENT_MCP_FUNC_TYPE=$(gcloud run services describe internal-mcp-server \
  --project="${PROJECT_B_ID}" \
  --region="${REGION}" \
  --format="value(metadata.annotations['apphub.cloud.google.com/functional-type'])" 2>/dev/null || true)

if [[ "${CURRENT_MCP_MODE}" != *"stateless-json-v2"* || "${CURRENT_MCP_FUNC_TYPE}" != "mcp-server" ]]; then
  echo "Deploying/Updating Private SecOps Cloud Run MCP Server (--functional-type=mcp-server, stateless JSON HTTP) in Project B (${PROJECT_B_ID})..."
  gcloud projects add-iam-policy-binding "${PROJECT_B_ID}" \
    --member="serviceAccount:${PROJECT_B_NUM}-compute@developer.gserviceaccount.com" \
    --role="roles/run.builder" --quiet >/dev/null

  rm -rf /tmp/agw-cfg/mcp-server
  cp -R "${ROOT_DIR}/mcp-server" /tmp/agw-cfg/mcp-server

  if [[ -n "${CURRENT_MCP_MODE}" && -z "${CURRENT_MCP_FUNC_TYPE}" ]]; then
    echo "  -> Recreating 'internal-mcp-server' once to set immutable --functional-type=mcp-server annotation for Agent Registry auto-discovery..."
    gcloud -q run services delete internal-mcp-server \
      --project="${PROJECT_B_ID}" \
      --region="${REGION}" >/dev/null 2>&1 || true
  fi

  gcloud -q beta run deploy internal-mcp-server \
    --project="${PROJECT_B_ID}" \
    --region="${REGION}" \
    --source="/tmp/agw-cfg/mcp-server" \
    --ingress=internal \
    --no-invoker-iam-check \
    --functional-type=mcp-server \
    --min-instances=1 \
    --set-env-vars="MCP_TRANSPORT_MODE=stateless-json-v2,PROJECT_A_ID=${PROJECT_A_ID},PROJECT_B_ID=${PROJECT_B_ID}" || \
  gcloud -q run deploy internal-mcp-server \
    --project="${PROJECT_B_ID}" \
    --region="${REGION}" \
    --source="/tmp/agw-cfg/mcp-server" \
    --ingress=internal \
    --no-invoker-iam-check \
    --update-annotations="apphub.cloud.google.com/functional-type=mcp-server" \
    --min-instances=1 \
    --set-env-vars="MCP_TRANSPORT_MODE=stateless-json-v2,PROJECT_A_ID=${PROJECT_A_ID},PROJECT_B_ID=${PROJECT_B_ID}"
else
  echo "  -> Cloud Run service 'internal-mcp-server' (stateless-json-v2, functional-type=mcp-server) is already deployed and serving."
  gcloud -q run services update internal-mcp-server \
    --project="${PROJECT_B_ID}" \
    --region="${REGION}" \
    --no-invoker-iam-check \
    --min-instances=1 \
    --update-env-vars="PROJECT_A_ID=${PROJECT_A_ID},PROJECT_B_ID=${PROJECT_B_ID}" >/dev/null 2>&1 || true
fi

export INTERNAL_MCP_URL=$(gcloud run services describe internal-mcp-server \
  --project="${PROJECT_B_ID}" \
  --region="${REGION}" \
  --format="value(status.url)")
export INTERNAL_MCP_REGIONAL_URL="https://internal-mcp-server-${PROJECT_B_NUM}.${REGION}.run.app"
echo "  -> Internal Cloud Run MCP URL: ${INTERNAL_MCP_URL}/mcp"
echo "  -> Regional Cloud Run MCP URL: ${INTERNAL_MCP_REGIONAL_URL}/mcp"

# ==============================================================================
# 4. PROJECT A: DEPLOY CLOUD RUN 3LO CALLBACK SERVICE & 3LO AUTH PROVIDER
# ==============================================================================
echo "Checking Production 3LO Callback Service ('secops-3lo-callback') on Cloud Run in Project A (${PROJECT_A_ID})..."
CURRENT_3LO_URL=$(gcloud run services describe secops-3lo-callback \
  --project="${PROJECT_A_ID}" \
  --region="${REGION}" \
  --format="value(status.url)" 2>/dev/null || true)

if [[ -z "${CURRENT_3LO_URL}" || "${FORCE_REBUILD_3LO:-false}" == "true" ]]; then
  echo "Deploying Production 3LO Callback Service ('secops-3lo-callback') on Cloud Run in Project A (${PROJECT_A_ID})..."
  gcloud services enable compute.googleapis.com run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
    --project="${PROJECT_A_ID}" >/dev/null 2>&1 || true

  for build_svc in compute.googleapis.com run.googleapis.com cloudbuild.googleapis.com; do
    gcloud -q beta services identity create --service="${build_svc}" --project="${PROJECT_A_ID}" >/dev/null 2>&1 || true
  done

  for cb_role in roles/run.builder roles/cloudbuild.builds.builder roles/storage.objectAdmin roles/artifactregistry.writer roles/logging.logWriter roles/serviceusage.serviceUsageConsumer; do
    gcloud projects add-iam-policy-binding "${PROJECT_A_ID}" \
      --member="serviceAccount:${PROJECT_A_NUM}-compute@developer.gserviceaccount.com" \
      --role="${cb_role}" --quiet >/dev/null
  done

  # Create dedicated least-privilege runtime service account for secops-3lo-callback
  # so it does NOT run as the Default Compute Service Account in Security Command Center
  CALLBACK_SA_NAME="sa-3lo-callback"
  CALLBACK_SA_EMAIL="${CALLBACK_SA_NAME}@${PROJECT_A_ID}.iam.gserviceaccount.com"
  gcloud iam service-accounts describe "${CALLBACK_SA_EMAIL}" --project="${PROJECT_A_ID}" >/dev/null 2>&1 || \
    gcloud iam service-accounts create "${CALLBACK_SA_NAME}" \
      --display-name="Least-Privilege 3LO OAuth Callback Runtime SA" \
      --project="${PROJECT_A_ID}" >/dev/null 2>&1 || true

  for runtime_role in roles/agentidentity.user roles/logging.logWriter; do
    gcloud projects add-iam-policy-binding "${PROJECT_A_ID}" \
      --member="serviceAccount:${CALLBACK_SA_EMAIL}" \
      --role="${runtime_role}" --quiet >/dev/null
  done

  rm -rf /tmp/agw-cfg/client-3lo
  cp -R "${ROOT_DIR}/client-3lo" /tmp/agw-cfg/client-3lo

  gcloud -q run deploy secops-3lo-callback \
    --project="${PROJECT_A_ID}" \
    --region="${REGION}" \
    --source="/tmp/agw-cfg/client-3lo" \
    --service-account="${CALLBACK_SA_EMAIL}" \
    --ingress=all \
    --no-invoker-iam-check \
    --min-instances=1 \
    --max-instances=1 \
    --set-env-vars="PROJECT_A_ID=${PROJECT_A_ID},PROJECT_A_NUM=${PROJECT_A_NUM},REGION=${REGION}"
else
  echo "  -> Cloud Run service 'secops-3lo-callback' is already deployed (${CURRENT_3LO_URL})."
fi

export CALLBACK_SERVICE_URL=$(gcloud run services describe secops-3lo-callback \
  --project="${PROJECT_A_ID}" \
  --region="${REGION}" \
  --format="value(status.url)")
export OAUTH_CONTINUE_URI="${CALLBACK_SERVICE_URL}/validateUserId"
echo "  -> Cloud Run 3LO Callback URL (OAUTH_CONTINUE_URI): ${OAUTH_CONTINUE_URI}"

echo "Checking 3-Legged OAuth (3LO) Auth Provider 'github-mcp-auth' in Project A (${PROJECT_A_ID})..."
if gcloud alpha agent-identity auth-providers describe github-mcp-auth \
  --project="${PROJECT_A_ID}" --location="${REGION}" >/dev/null 2>&1; then
  gcloud -q alpha agent-identity auth-providers update github-mcp-auth \
    --project="${PROJECT_A_ID}" \
    --location="${REGION}" \
    --three-legged-oauth-default-continue-uri="${OAUTH_CONTINUE_URI}" \
    --allowed-scopes="repo" >/dev/null 2>&1 || true
else
  if [[ -z "${GITHUB_OAUTH_CLIENT_ID}" || -z "${GITHUB_OAUTH_CLIENT_SECRET}" ]]; then
    echo "ERROR: Auth Provider 'github-mcp-auth' does not exist yet. Please export GITHUB_OAUTH_CLIENT_ID and GITHUB_OAUTH_CLIENT_SECRET before running deploy.sh." >&2
    exit 1
  fi
  gcloud -q alpha agent-identity auth-providers create github-mcp-auth \
    --project="${PROJECT_A_ID}" \
    --location="${REGION}" \
    --description="GitHub Copilot MCP 3LO Auth Provider" \
    --three-legged-oauth-client-id="${GITHUB_OAUTH_CLIENT_ID}" \
    --three-legged-oauth-client-secret="${GITHUB_OAUTH_CLIENT_SECRET}" \
    --three-legged-oauth-authorization-url="https://github.com/login/oauth/authorize" \
    --three-legged-oauth-token-url="https://github.com/login/oauth/access_token" \
    --three-legged-oauth-default-continue-uri="${OAUTH_CONTINUE_URI}" \
    --allowed-scopes="repo" || true
fi

gcloud -q alpha agent-identity auth-providers add-iam-policy-binding github-mcp-auth \
  --project="${PROJECT_A_ID}" \
  --location="${REGION}" \
  --role="roles/agentidentity.user" \
  --member="serviceAccount:${PROJECT_A_NUM}-compute@developer.gserviceaccount.com" >/dev/null 2>&1 || true

# ==============================================================================
# 5. PROJECT A: CREATE AGENT GATEWAY + IAP V2 AUTHZ EXTENSION & POLICY
# ==============================================================================
echo "Minting Network Services, Agent Gateway, Data Egress (DEP) & Vertex AI Service Identities..."
for svc in networkservices.googleapis.com aiplatform.googleapis.com; do
  gcloud -q beta services identity create --service="${svc}" --project="${PROJECT_A_ID}" || true
  gcloud -q beta services identity create --service="${svc}" --project="${PROJECT_B_ID}" || true
done

# Grant Agent Runtime Service Agents (Project A & Project B) permission to get/use Project A's Agent Gateway
gcloud iam roles describe ar_agw_cross_project_sa --project="${PROJECT_A_ID}" >/dev/null 2>&1 && \
  gcloud -q iam roles update ar_agw_cross_project_sa \
    --project="${PROJECT_A_ID}" \
    --permissions="networkservices.agentGateways.get,networkservices.agentGateways.use,networkservices.operations.get" >/dev/null 2>&1 || \
  gcloud -q iam roles create ar_agw_cross_project_sa \
    --project="${PROJECT_A_ID}" \
    --title="Agent Runtime Agent Gateway Cross-Project SA" \
    --description="Allows Project A and Project B Agent Runtime Service Agents to bind to Project A Agent Gateway" \
    --permissions="networkservices.agentGateways.get,networkservices.agentGateways.use,networkservices.operations.get"

SERVICE_AGENT_NETWORK_MEMBERS=(
  "serviceAccount:service-${PROJECT_A_NUM}@gcp-sa-networkservices.iam.gserviceaccount.com"
  "serviceAccount:service-${PROJECT_A_NUM}@gcp-sa-agentgateway.iam.gserviceaccount.com"
  "serviceAccount:service-${PROJECT_A_NUM}@gcp-sa-dep.iam.gserviceaccount.com"
  "serviceAccount:service-${PROJECT_A_NUM}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
  "serviceAccount:service-${PROJECT_B_NUM}@gcp-sa-networkservices.iam.gserviceaccount.com"
  "serviceAccount:service-${PROJECT_B_NUM}@gcp-sa-agentgateway.iam.gserviceaccount.com"
  "serviceAccount:service-${PROJECT_B_NUM}@gcp-sa-dep.iam.gserviceaccount.com"
  "serviceAccount:service-${PROJECT_B_NUM}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"
)

echo "Binding cross-project VPC, DNS Peering & Data Egress (gcp-sa-dep / gcp-sa-aiplatform-re) IAM roles..."
for sa in "${SERVICE_AGENT_NETWORK_MEMBERS[@]}"; do
  gcloud compute networks subnets add-iam-policy-binding subnet-agw-egress \
    --project="${PROJECT_B_ID}" \
    --region="${REGION}" \
    --member="${sa}" \
    --role="roles/compute.networkUser" --quiet >/dev/null 2>&1 || true

  for net_role in roles/compute.networkUser roles/compute.networkAdmin roles/dns.peer roles/run.invoker; do
    gcloud projects add-iam-policy-binding "${PROJECT_B_ID}" \
      --member="${sa}" \
      --role="${net_role}" --quiet >/dev/null 2>&1 || true
  done
done

for sa_num in "${PROJECT_A_NUM}" "${PROJECT_B_NUM}"; do
  # Agent Gateway P4SA role
  for target_proj in "${PROJECT_A_ID}" "${PROJECT_B_ID}"; do
    gcloud projects add-iam-policy-binding "${target_proj}" \
      --member="serviceAccount:service-${sa_num}@gcp-sa-agentgateway.iam.gserviceaccount.com" \
      --role="roles/agentgateway.serviceAgent" --quiet >/dev/null 2>&1 || true

    gcloud projects add-iam-policy-binding "${target_proj}" \
      --member="serviceAccount:service-${sa_num}@gcp-sa-dep.iam.gserviceaccount.com" \
      --role="roles/serviceusage.serviceUsageConsumer" --quiet >/dev/null 2>&1 || true
  done

  gcloud projects add-iam-policy-binding "${PROJECT_A_ID}" \
    --member="serviceAccount:service-${sa_num}@gcp-sa-dep.iam.gserviceaccount.com" \
    --role="roles/iam.workloadIdentityUser" --quiet >/dev/null 2>&1 || true

  # Both aiplatform and aiplatform-re P4SAs need cross-project Agent Gateway get/use & viewer on Project A
  for aip_sa in \
    "serviceAccount:service-${sa_num}@gcp-sa-aiplatform.iam.gserviceaccount.com" \
    "serviceAccount:service-${sa_num}@gcp-sa-aiplatform-re.iam.gserviceaccount.com"; do
    gcloud projects add-iam-policy-binding "${PROJECT_A_ID}" \
      --member="${aip_sa}" \
      --role="projects/${PROJECT_A_ID}/roles/ar_agw_cross_project_sa" --quiet >/dev/null 2>&1 || true
    gcloud projects add-iam-policy-binding "${PROJECT_A_ID}" \
      --member="${aip_sa}" \
      --role="roles/networkservices.viewer" --quiet >/dev/null 2>&1 || true
  done
done

cat > /tmp/agw-cfg/enterprise-egress-agw.yaml <<EOF
name: enterprise-egress-agw
protocols:
  - MCP
googleManaged:
  governedAccessPath: AGENT_TO_ANYWHERE
registries:
  - "//agentregistry.googleapis.com/projects/${PROJECT_A_ID}/locations/${REGION}"
networkConfig:
  egress:
    networkAttachment: ${PSC_NA_URI}
  dnsPeeringConfig:
    domains:
      - run.app.
    targetProject: ${PROJECT_B_ID}
    targetNetwork: projects/${PROJECT_B_ID}/global/networks/vpc-agw-egress
EOF

echo "Checking Agent Gateway 'enterprise-egress-agw' in Project A (${PROJECT_A_ID})..."
gcloud network-services agent-gateways describe enterprise-egress-agw \
  --project="${PROJECT_A_ID}" --location="${REGION}" >/dev/null 2>&1 || \
gcloud -q network-services agent-gateways import enterprise-egress-agw \
  --project="${PROJECT_A_ID}" \
  --location="${REGION}" \
  --source="/tmp/agw-cfg/enterprise-egress-agw.yaml"

cat > /tmp/agw-cfg/iap-v2-authz-ext.yaml <<EOF
name: enterprise-egress-agw-iap-v2-ext
service: iap.googleapis.com
failOpen: false
timeout: 1s
metadata:
  iapPolicyVersion: "V1"
  iamEnforcementMode: "DRY_RUN"
EOF

echo "Checking IAP Authz Extension 'enterprise-egress-agw-iap-v2-ext'..."
gcloud service-extensions authz-extensions describe enterprise-egress-agw-iap-v2-ext \
  --project="${PROJECT_A_ID}" --location="${REGION}" >/dev/null 2>&1 || \
gcloud -q service-extensions authz-extensions import enterprise-egress-agw-iap-v2-ext \
  --project="${PROJECT_A_ID}" \
  --location="${REGION}" \
  --source="/tmp/agw-cfg/iap-v2-authz-ext.yaml"

cat > /tmp/agw-cfg/iap-v2-authz-policy.yaml <<EOF
name: enterprise-egress-agw-authz-policy
target:
  resources:
    - "projects/${PROJECT_A_ID}/locations/${REGION}/agentGateways/enterprise-egress-agw"
policyProfile: REQUEST_AUTHZ
action: CUSTOM
customProvider:
  authzExtension:
    resources:
      - "projects/${PROJECT_A_ID}/locations/${REGION}/authzExtensions/enterprise-egress-agw-iap-v2-ext"
EOF

echo "Checking Authz Policy 'enterprise-egress-agw-authz-policy'..."
gcloud network-security authz-policies describe enterprise-egress-agw-authz-policy \
  --project="${PROJECT_A_ID}" --location="${REGION}" >/dev/null 2>&1 || \
gcloud -q network-security authz-policies import enterprise-egress-agw-authz-policy \
  --project="${PROJECT_A_ID}" \
  --location="${REGION}" \
  --source="/tmp/agw-cfg/iap-v2-authz-policy.yaml"

# ==============================================================================
# 5B. PROJECT A & B: MODEL ARMOR INLINE GUARDRAILS (CONTENT_AUTHZ + FLOOR SETTINGS) & TELEMETRY POLICY
# ==============================================================================
echo "Configuring Model Armor Inline Guardrails ('secops-agent-guardrail'), Vertex AI Floor Settings & Cloud Trace TelemetryPolicy..."
for ma_proj in "${PROJECT_A_ID}" "${PROJECT_B_ID}"; do
  gcloud -q beta services identity create --service=modelarmor.googleapis.com --project="${ma_proj}" >/dev/null 2>&1 || true
done

for dep_sa_num in "${PROJECT_A_NUM}" "${PROJECT_B_NUM}"; do
  for ma_sa in \
    "serviceAccount:service-${dep_sa_num}@gcp-sa-aiplatform.iam.gserviceaccount.com" \
    "serviceAccount:service-${dep_sa_num}@gcp-sa-dep.iam.gserviceaccount.com" \
    "serviceAccount:service-${dep_sa_num}@gcp-sa-aiplatform-re.iam.gserviceaccount.com" \
    "serviceAccount:service-${dep_sa_num}@gcp-sa-agentgateway.iam.gserviceaccount.com"; do
    for ma_role in roles/modelarmor.calloutUser roles/modelarmor.user roles/serviceusage.serviceUsageConsumer; do
      gcloud projects add-iam-policy-binding "${PROJECT_A_ID}" \
        --member="${ma_sa}" \
        --role="${ma_role}" --quiet >/dev/null 2>&1 || true
      gcloud projects add-iam-policy-binding "${PROJECT_B_ID}" \
        --member="${ma_sa}" \
        --role="${ma_role}" --quiet >/dev/null 2>&1 || true
    done
  done
done

for ma_proj in "${PROJECT_A_ID}" "${PROJECT_B_ID}"; do
  # Enable Model Armor Floor Settings FIRST so template update conforms with floorSetting
  curl -s -X PATCH \
    -H "Authorization: Bearer $(gcloud auth print-access-token)" \
    -H "Content-Type: application/json" \
    -H "X-Goog-User-Project: ${ma_proj}" \
    "https://modelarmor.googleapis.com/v1/projects/${ma_proj}/locations/global/floorSetting?updateMask=filterConfig,enableFloorSettingEnforcement,integratedServices,aiPlatformFloorSetting" \
    -d '{
      "filterConfig": {
        "piAndJailbreakFilterSettings": {
          "filterEnforcement": "ENABLED",
          "confidenceLevel": "MEDIUM_AND_ABOVE"
        },
        "maliciousUriFilterSettings": {
          "filterEnforcement": "ENABLED"
        },
        "raiSettings": {
          "raiFilters": [
            {"filterType": "HATE_SPEECH", "confidenceLevel": "HIGH"},
            {"filterType": "HARASSMENT", "confidenceLevel": "HIGH"},
            {"filterType": "SEXUALLY_EXPLICIT", "confidenceLevel": "HIGH"}
          ]
        }
      },
      "enableFloorSettingEnforcement": true,
      "integratedServices": ["AI_PLATFORM"],
      "aiPlatformFloorSetting": {
        "inspectOnly": true,
        "enableCloudLogging": true
      }
    }' >/dev/null 2>&1 || true

  if CLOUDSDK_API_ENDPOINT_OVERRIDES_MODELARMOR="https://modelarmor.${REGION}.rep.googleapis.com/" \
     gcloud model-armor templates describe secops-agent-guardrail \
    --project="${ma_proj}" --location="${REGION}" >/dev/null 2>&1; then
    CLOUDSDK_API_ENDPOINT_OVERRIDES_MODELARMOR="https://modelarmor.${REGION}.rep.googleapis.com/" \
    gcloud -q model-armor templates update secops-agent-guardrail \
      --project="${ma_proj}" \
      --location="${REGION}" \
      --pi-and-jailbreak-filter-settings-enforcement=enabled \
      --pi-and-jailbreak-filter-settings-confidence-level=medium-and-above \
      --malicious-uri-filter-settings-enforcement=enabled \
      --rai-settings-filters='[{"filterType":"HATE_SPEECH","confidenceLevel":"HIGH"},{"filterType":"HARASSMENT","confidenceLevel":"HIGH"},{"filterType":"SEXUALLY_EXPLICIT","confidenceLevel":"HIGH"}]' \
      --template-metadata-log-operations \
      --template-metadata-log-sanitize-operations >/dev/null 2>&1 || true
  else
    CLOUDSDK_API_ENDPOINT_OVERRIDES_MODELARMOR="https://modelarmor.${REGION}.rep.googleapis.com/" \
    gcloud -q model-armor templates create secops-agent-guardrail \
      --project="${ma_proj}" \
      --location="${REGION}" \
      --pi-and-jailbreak-filter-settings-enforcement=enabled \
      --pi-and-jailbreak-filter-settings-confidence-level=medium-and-above \
      --malicious-uri-filter-settings-enforcement=enabled \
      --rai-settings-filters='[{"filterType":"HATE_SPEECH","confidenceLevel":"HIGH"},{"filterType":"HARASSMENT","confidenceLevel":"HIGH"},{"filterType":"SEXUALLY_EXPLICIT","confidenceLevel":"HIGH"}]' \
      --template-metadata-log-operations \
      --template-metadata-log-sanitize-operations || true
  fi
done

cat > /tmp/agw-cfg/ma-authz-ext.yaml <<EOF
name: enterprise-egress-agw-ma-ext
service: modelarmor.${REGION}.rep.googleapis.com
failOpen: false
timeout: 1s
metadata:
  model_armor_settings: '[{"request_template_id":"projects/${PROJECT_A_ID}/locations/${REGION}/templates/secops-agent-guardrail","response_template_id":"projects/${PROJECT_A_ID}/locations/${REGION}/templates/secops-agent-guardrail"}]'
EOF

echo "Importing Model Armor Authz Extension 'enterprise-egress-agw-ma-ext'..."
gcloud -q service-extensions authz-extensions import enterprise-egress-agw-ma-ext \
  --project="${PROJECT_A_ID}" \
  --location="${REGION}" \
  --source="/tmp/agw-cfg/ma-authz-ext.yaml" || true

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

echo "Importing Model Armor Content Authz Policy 'enterprise-egress-agw-ma-policy'..."
gcloud -q network-security authz-policies import enterprise-egress-agw-ma-policy \
  --project="${PROJECT_A_ID}" \
  --location="${REGION}" \
  --source="/tmp/agw-cfg/ma-authz-policy.yaml" || true

cat > /tmp/agw-cfg/agw-telemetry-policy.yaml <<EOF
name: enterprise-egress-agw-telemetry
telemetryTarget:
  resources:
    - "//networkservices.googleapis.com/projects/${PROJECT_A_ID}/locations/${REGION}/agentGateways/enterprise-egress-agw"
tracingConfiguration:
  samplingRate: 1.0
  parentBasedSampling:
    enabled: true
EOF

echo "Checking Agent Gateway Distributed Trace TelemetryPolicy 'enterprise-egress-agw-telemetry'..."
gcloud beta network-services telemetry-policies import enterprise-egress-agw-telemetry \
  --source="/tmp/agw-cfg/agw-telemetry-policy.yaml" \
  --location="${REGION}" \
  --project="${PROJECT_A_ID}" \
  --quiet || true

# ==============================================================================
# 6. REGISTER MCP SERVERS & CORE GOOGLE APIS IN PROJECT A'S AGENT REGISTRY
# ==============================================================================
echo "Checking Core Google APIs, Internal Cloud Run MCP & External GitHub Copilot MCP in Project A Registry..."

GAPI_INTERFACES=(
  "--interfaces=protocolBinding=JSONRPC,url=https://telemetry.googleapis.com"
  "--interfaces=protocolBinding=JSONRPC,url=https://telemetry.googleapis.com:443"
  "--interfaces=protocolBinding=JSONRPC,url=https://telemetry.mtls.googleapis.com"
  "--interfaces=protocolBinding=JSONRPC,url=https://telemetry.mtls.googleapis.com:443"
  "--interfaces=protocolBinding=JSONRPC,url=https://cloudtrace.googleapis.com"
  "--interfaces=protocolBinding=JSONRPC,url=https://cloudtrace.mtls.googleapis.com"
  "--interfaces=protocolBinding=JSONRPC,url=https://${REGION}-aiplatform.googleapis.com"
  "--interfaces=protocolBinding=JSONRPC,url=https://${REGION}-aiplatform.googleapis.com:443"
  "--interfaces=protocolBinding=JSONRPC,url=https://${REGION}-aiplatform.mtls.googleapis.com"
  "--interfaces=protocolBinding=JSONRPC,url=https://${REGION}-aiplatform.mtls.googleapis.com:443"
  "--interfaces=protocolBinding=JSONRPC,url=https://aiplatform.googleapis.com"
  "--interfaces=protocolBinding=JSONRPC,url=https://aiplatform.googleapis.com:443"
  "--interfaces=protocolBinding=JSONRPC,url=https://aiplatform.mtls.googleapis.com"
  "--interfaces=protocolBinding=JSONRPC,url=https://aiplatform.mtls.googleapis.com:443"
  "--interfaces=protocolBinding=JSONRPC,url=https://cloudresourcemanager.googleapis.com"
  "--interfaces=protocolBinding=JSONRPC,url=https://cloudresourcemanager.mtls.googleapis.com"
  "--interfaces=protocolBinding=JSONRPC,url=https://iamcredentials.googleapis.com"
  "--interfaces=protocolBinding=JSONRPC,url=https://iamcredentials.mtls.googleapis.com"
  "--interfaces=protocolBinding=JSONRPC,url=https://agentregistry.googleapis.com"
  "--interfaces=protocolBinding=JSONRPC,url=https://agentregistry.mtls.googleapis.com"
  "--interfaces=protocolBinding=JSONRPC,url=https://agentregistry.googleapis.com:443"
  "--interfaces=protocolBinding=JSONRPC,url=https://agentregistry.mtls.googleapis.com:443"
  "--interfaces=protocolBinding=JSONRPC,url=https://agentidentity.googleapis.com"
  "--interfaces=protocolBinding=JSONRPC,url=https://agentidentitycredentials.googleapis.com"
  "--interfaces=protocolBinding=JSONRPC,url=https://logging.googleapis.com"
  "--interfaces=protocolBinding=JSONRPC,url=https://logging.mtls.googleapis.com"
  "--interfaces=protocolBinding=JSONRPC,url=https://monitoring.googleapis.com"
  "--interfaces=protocolBinding=JSONRPC,url=https://monitoring.mtls.googleapis.com"
  "--interfaces=protocolBinding=JSONRPC,url=https://modelarmor.${REGION}.rep.googleapis.com"
  "--interfaces=protocolBinding=JSONRPC,url=https://modelarmor.${REGION}.rep.googleapis.com:443"
  "--interfaces=protocolBinding=JSONRPC,url=${CALLBACK_SERVICE_URL}"
  "--interfaces=protocolBinding=JSONRPC,url=${CALLBACK_SERVICE_URL}/registerNonce"
)

if gcloud alpha agent-registry services describe core-gapi-services --project="${PROJECT_A_ID}" --location="${REGION}" >/dev/null 2>&1; then
  gcloud -q alpha agent-registry services update core-gapi-services \
    --project="${PROJECT_A_ID}" \
    --location="${REGION}" \
    --display-name="gapi.core.services" \
    --description="Core Google APIs and Services for Agent Runtime" \
    --endpoint-spec-type=no-spec \
    "${GAPI_INTERFACES[@]}" >/dev/null
else
  gcloud -q alpha agent-registry services create core-gapi-services \
    --project="${PROJECT_A_ID}" \
    --location="${REGION}" \
    --display-name="gapi.core.services" \
    --description="Core Google APIs and Services for Agent Runtime" \
    --endpoint-spec-type=no-spec \
    "${GAPI_INTERFACES[@]}"
fi

cat > /tmp/agw-cfg/secops-toolspec.json <<EOF
{
  "tools": [
    {
      "name": "get_active_cve_alerts",
      "description": "Retrieve active production CVE alerts from the internal SecOps scanner.",
      "inputSchema": {
        "type": "object",
        "properties": {
          "service_name": {
            "type": "string",
            "description": "Name of the internal service (e.g. payment-service, checkout-worker, or all)."
          }
        }
      }
    },
    {
      "name": "get_internal_deployment_manifest",
      "description": "Fetch the internal production deployment metadata and policy requirements for a service.",
      "inputSchema": {
        "type": "object",
        "properties": {
          "service_name": {
            "type": "string",
            "description": "Name of the service (e.g. payment-service)."
          }
        },
        "required": ["service_name"]
      }
    },
    {
      "name": "record_remediation_ticket",
      "description": "Record a GitHub remediation PR or Issue URL into the internal SecOps compliance ledger.",
      "inputSchema": {
        "type": "object",
        "properties": {
          "cve_id": {"type": "string"},
          "service_name": {"type": "string"},
          "github_pr_or_issue_url": {"type": "string"},
          "remediated_by_github_user": {"type": "string"}
        },
        "required": ["cve_id", "service_name", "github_pr_or_issue_url", "remediated_by_github_user"]
      }
    }
  ]
}
EOF

if gcloud alpha agent-registry services describe internal-cloudrun-mcp --project="${PROJECT_A_ID}" --location="${REGION}" >/dev/null 2>&1; then
  gcloud -q alpha agent-registry services update internal-cloudrun-mcp \
    --project="${PROJECT_A_ID}" \
    --location="${REGION}" \
    --display-name="Internal SecOps Cloud Run MCP Server" \
    --description="Private Cloud Run SecOps MCP in Project B" \
    --mcp-server-spec-type=tool-spec \
    --mcp-server-spec-content=/tmp/agw-cfg/secops-toolspec.json \
    --interfaces="url=${INTERNAL_MCP_URL}/mcp,protocolBinding=JSONRPC" \
    --interfaces="url=${INTERNAL_MCP_REGIONAL_URL}/mcp,protocolBinding=JSONRPC" >/dev/null
else
  gcloud -q alpha agent-registry services create internal-cloudrun-mcp \
    --project="${PROJECT_A_ID}" \
    --location="${REGION}" \
    --display-name="Internal SecOps Cloud Run MCP Server" \
    --description="Private Cloud Run SecOps MCP in Project B" \
    --mcp-server-spec-type=tool-spec \
    --mcp-server-spec-content=/tmp/agw-cfg/secops-toolspec.json \
    --interfaces="url=${INTERNAL_MCP_URL}/mcp,protocolBinding=JSONRPC" \
    --interfaces="url=${INTERNAL_MCP_REGIONAL_URL}/mcp,protocolBinding=JSONRPC"
fi

cat > /tmp/agw-cfg/github-toolspec.json <<EOF
{
  "tools": [
    {
      "name": "get_me",
      "description": "Get details of the authenticated GitHub user.",
      "inputSchema": {
        "type": "object",
        "properties": {}
      }
    },
    {
      "name": "search_repositories",
      "description": "Search for GitHub repositories.",
      "inputSchema": {
        "type": "object",
        "properties": {
          "query": {"type": "string"}
        },
        "required": ["query"]
      }
    },
    {
      "name": "issue_write",
      "description": "Create a new or update an existing issue in a GitHub repository.",
      "inputSchema": {
        "type": "object",
        "properties": {
          "method": {"type": "string"},
          "owner": {"type": "string"},
          "repo": {"type": "string"},
          "title": {"type": "string"},
          "body": {"type": "string"}
        },
        "required": ["method", "owner", "repo", "title"]
      }
    },
    {
      "name": "create_issue",
      "description": "Create a new GitHub issue in a repository.",
      "inputSchema": {
        "type": "object",
        "properties": {
          "owner": {"type": "string"},
          "repo": {"type": "string"},
          "title": {"type": "string"},
          "body": {"type": "string"}
        },
        "required": ["owner", "repo", "title"]
      }
    },
    {
      "name": "create_github_remediation_issue",
      "description": "Create a GitHub remediation issue via 3LO OAuth attributing the ticket to the authenticated human engineer.",
      "inputSchema": {
        "type": "object",
        "properties": {
          "owner": {"type": "string"},
          "repo": {"type": "string"},
          "title": {"type": "string"},
          "body": {"type": "string"}
        },
        "required": ["owner", "repo", "title", "body"]
      }
    }
  ]
}
EOF

if gcloud alpha agent-registry services describe github-copilot-mcp --project="${PROJECT_A_ID}" --location="${REGION}" >/dev/null 2>&1; then
  gcloud -q alpha agent-registry services update github-copilot-mcp \
    --project="${PROJECT_A_ID}" \
    --location="${REGION}" \
    --display-name="External GitHub Copilot MCP Server" \
    --description="External GitHub Copilot MCP governed via 3LO" \
    --mcp-server-spec-type=tool-spec \
    --mcp-server-spec-content=/tmp/agw-cfg/github-toolspec.json \
    --interfaces="url=https://api.githubcopilot.com/mcp/,protocolBinding=JSONRPC" >/dev/null
else
  gcloud -q alpha agent-registry services create github-copilot-mcp \
    --project="${PROJECT_A_ID}" \
    --location="${REGION}" \
    --display-name="External GitHub Copilot MCP Server" \
    --description="External GitHub Copilot MCP governed via 3LO" \
    --mcp-server-spec-type=tool-spec \
    --mcp-server-spec-content=/tmp/agw-cfg/github-toolspec.json \
    --interfaces="url=https://api.githubcopilot.com/mcp/,protocolBinding=JSONRPC"
fi

cat > /tmp/agw-cfg/azure-weather-toolspec.json <<EOF
{
  "tools": [
    {
      "name": "GetWeather",
      "description": "Retrieve current weather for a location/city from the Cross-Cloud Azure MCP Server.",
      "inputSchema": {
        "type": "object",
        "properties": {
          "location": {"type": "string", "description": "Location or city name (e.g. Zurich, Seattle, London)."},
          "city": {"type": "string", "description": "City name (e.g. Zurich, Seattle, London)."},
          "country": {"type": "string", "description": "Optional country code or name."}
        }
      }
    },
    {
      "name": "get_weather",
      "description": "Retrieve current weather for a location/city from the Cross-Cloud Azure MCP Server.",
      "inputSchema": {
        "type": "object",
        "properties": {
          "location": {"type": "string", "description": "Location or city name (e.g. Zurich, Seattle, London)."},
          "city": {"type": "string", "description": "City name (e.g. Zurich, Seattle, London)."},
          "country": {"type": "string", "description": "Optional country code or name."}
        }
      }
    }
  ]
}
EOF

if gcloud alpha agent-registry services describe azure-weather-mcp --project="${PROJECT_A_ID}" --location="${REGION}" >/dev/null 2>&1; then
  gcloud -q alpha agent-registry services update azure-weather-mcp \
    --project="${PROJECT_A_ID}" \
    --location="${REGION}" \
    --display-name="Cross-Cloud Azure Weather MCP Server" \
    --description="Cross-Cloud Azure Functions MCP Server (GetWeather) with W3C OTLP Trace Propagation" \
    --mcp-server-spec-type=tool-spec \
    --mcp-server-spec-content=/tmp/agw-cfg/azure-weather-toolspec.json \
    --interfaces="url=${AZURE_MCP_URL},protocolBinding=JSONRPC" >/dev/null
else
  gcloud -q alpha agent-registry services create azure-weather-mcp \
    --project="${PROJECT_A_ID}" \
    --location="${REGION}" \
    --display-name="Cross-Cloud Azure Weather MCP Server" \
    --description="Cross-Cloud Azure Functions MCP Server (GetWeather) with W3C OTLP Trace Propagation" \
    --mcp-server-spec-type=tool-spec \
    --mcp-server-spec-content=/tmp/agw-cfg/azure-weather-toolspec.json \
    --interfaces="url=${AZURE_MCP_URL},protocolBinding=JSONRPC"
fi

# ==============================================================================
# 7. STAGING BUCKETS & SPIFFE PRINCIPALSET RUNTIME PERMISSIONS
# ==============================================================================
echo "Ensuring GCS staging buckets & SPIFFE PrincipalSet runtime roles..."
gcloud -q storage buckets create "gs://${PROJECT_A_ID}-agent-staging" \
  --project="${PROJECT_A_ID}" \
  --location="${REGION}" 2>/dev/null || true
gcloud -q storage buckets create "gs://${PROJECT_B_ID}-agent-staging" \
  --project="${PROJECT_B_ID}" \
  --location="${REGION}" 2>/dev/null || true

PRINCIPAL_SET_A="principalSet://agents.global.org-${ORG_ID}.system.id.goog/attribute.platformContainer/aiplatform/projects/${PROJECT_A_NUM}"
PRINCIPAL_SET_B="principalSet://agents.global.org-${ORG_ID}.system.id.goog/attribute.platformContainer/aiplatform/projects/${PROJECT_B_NUM}"

if ! gcloud alpha agent-registry bindings describe github-mcp-3lo-binding --project="${PROJECT_A_ID}" --location="${REGION}" >/dev/null 2>&1; then
  for role in roles/aiplatform.user roles/agentregistry.viewer roles/logging.logWriter roles/monitoring.metricWriter roles/cloudtrace.agent roles/telemetry.tracesWriter roles/browser; do
    gcloud -q projects add-iam-policy-binding "${PROJECT_A_ID}" \
      --member="${PRINCIPAL_SET_A}" \
      --role="${role}" >/dev/null 2>&1 || true
    gcloud -q projects add-iam-policy-binding "${PROJECT_B_ID}" \
      --member="${PRINCIPAL_SET_B}" \
      --role="${role}" >/dev/null 2>&1 || true
  done
fi

for trace_role in roles/cloudtrace.agent roles/telemetry.tracesWriter roles/logging.logWriter; do
  gcloud -q projects add-iam-policy-binding "${PROJECT_A_ID}" \
    --member="${PRINCIPAL_SET_A}" \
    --role="${trace_role}" >/dev/null 2>&1 || true
  gcloud -q projects add-iam-policy-binding "${PROJECT_B_ID}" \
    --member="${PRINCIPAL_SET_B}" \
    --role="${trace_role}" >/dev/null 2>&1 || true
  # Cross-project OTLP & Log correlation: allow Project B agents & Cloud Run MCP server to write spans/logs to Project A
  gcloud -q projects add-iam-policy-binding "${PROJECT_A_ID}" \
    --member="${PRINCIPAL_SET_B}" \
    --role="${trace_role}" >/dev/null 2>&1 || true
  gcloud -q projects add-iam-policy-binding "${PROJECT_A_ID}" \
    --member="serviceAccount:${PROJECT_B_NUM}-compute@developer.gserviceaccount.com" \
    --role="${trace_role}" >/dev/null 2>&1 || true
done

echo "Configuring Cross-Project Log Sink ('project-b-to-project-a-agw-logs') from Project B into Project A's _Default Log Analytics bucket..."
gcloud logging sinks describe project-b-to-project-a-agw-logs --project="${PROJECT_B_ID}" >/dev/null 2>&1 || \
gcloud -q logging sinks create project-b-to-project-a-agw-logs \
  "logging.googleapis.com/projects/${PROJECT_A_ID}/locations/global/buckets/_Default" \
  --project="${PROJECT_B_ID}" \
  --log-filter='resource.type="aiplatform.googleapis.com/ReasoningEngine" OR (resource.type="cloud_run_revision" AND resource.labels.service_name="internal-mcp-server")' >/dev/null 2>&1 || true

SINK_WRITER_IDENTITY=$(gcloud logging sinks describe project-b-to-project-a-agw-logs \
  --project="${PROJECT_B_ID}" --format="value(writerIdentity)" 2>/dev/null || true)
if [[ -n "${SINK_WRITER_IDENTITY}" ]]; then
  gcloud -q projects add-iam-policy-binding "${PROJECT_A_ID}" \
    --member="${SINK_WRITER_IDENTITY}" \
    --role="roles/logging.bucketWriter" >/dev/null 2>&1 || true
fi

for cross_role in roles/aiplatform.user roles/agentregistry.viewer; do
  gcloud -q projects add-iam-policy-binding "${PROJECT_A_ID}" \
    --member="${PRINCIPAL_SET_B}" \
    --role="${cross_role}" >/dev/null
done

for invoker_member in "${PRINCIPAL_SET_A}" "${PRINCIPAL_SET_B}"; do
  gcloud -q projects add-iam-policy-binding "${PROJECT_B_ID}" \
    --member="${invoker_member}" \
    --role="roles/run.invoker" >/dev/null 2>&1 || true
done

# ==============================================================================
# 8. IF AGENTS ARE DEPLOYED: BIND SPIFFE IDS TO AUTH MANAGER (3LO) & IAP
# ==============================================================================
if [[ -f /tmp/spiffe_identities.env ]]; then
  source /tmp/spiffe_identities.env
elif [[ -f "${ROOT_DIR}/agents/spiffe_identities.env" ]]; then
  source "${ROOT_DIR}/agents/spiffe_identities.env"
fi

if [[ -n "${AGENT_1_SPIFFE:-}" && -n "${AGENT_2_SPIFFE:-}" ]]; then
  echo "Binding provisioned SPIFFE identities:"
  echo "  AGENT_1_SPIFFE = ${AGENT_1_SPIFFE}"
  echo "  AGENT_2_SPIFFE = ${AGENT_2_SPIFFE}"

  # 8a. Bind Agent 1 SPIFFE workload identity & roles/agentidentity.user to GitHub 3LO Auth Provider
  echo "  -> [8a] Binding Agent 1 SPIFFE & roles/agentidentity.user to Auth Provider 'github-mcp-auth'..."
  gcloud -q alpha agent-identity auth-providers update github-mcp-auth \
    --project="${PROJECT_A_ID}" \
    --location="${REGION}" \
    --workload-ids="${AGENT_1_SPIFFE}" >/dev/null

  gcloud -q alpha agent-identity auth-providers add-iam-policy-binding github-mcp-auth \
    --project="${PROJECT_A_ID}" \
    --location="${REGION}" \
    --role="roles/agentidentity.user" \
    --member="${AGENT_1_SPIFFE}" >/dev/null 2>&1 || true

  CURRENT_USER_EMAIL=$(gcloud config get-value account 2>/dev/null || true)
  if [[ -n "${CURRENT_USER_EMAIL}" ]]; then
    gcloud -q alpha agent-identity auth-providers add-iam-policy-binding github-mcp-auth \
      --project="${PROJECT_A_ID}" \
      --location="${REGION}" \
      --role="roles/agentidentity.user" \
      --member="user:${CURRENT_USER_EMAIL}" >/dev/null 2>&1 || true
  fi

  for a1_role in roles/agentidentity.user roles/serviceusage.serviceUsageConsumer; do
    gcloud -q projects add-iam-policy-binding "${PROJECT_A_ID}" \
      --member="${PRINCIPAL_SET_A}" \
      --role="${a1_role}" >/dev/null 2>&1 || true
    gcloud -q projects add-iam-policy-binding "${PROJECT_A_ID}" \
      --member="${AGENT_1_SPIFFE}" \
      --role="${a1_role}" >/dev/null 2>&1 || true
  done

  # 8b. Create Agent Registry Auth Binding linking Agent 1 URN -> GitHub MCP URN -> github-mcp-auth
  echo "  -> [8b] Creating Agent Registry 3LO Binding 'github-mcp-3lo-binding'..."
  AGENT_1_SHORT_ID="${RE_1_ID:-${AGENT_1_SPIFFE##*/}}"
  AGENT_1_URN="urn:agent:projects-${PROJECT_A_NUM}:projects:${PROJECT_A_NUM}:locations:${REGION}:reasoningEngines:${AGENT_1_SHORT_ID}"
  GITHUB_MCP_URN=$(gcloud alpha agent-registry mcp-servers list \
    --project="${PROJECT_A_ID}" --location="${REGION}" \
    --filter="displayName='External GitHub Copilot MCP Server'" \
    --format="value(mcpServerId)")

  if gcloud alpha agent-registry bindings describe github-mcp-3lo-binding \
    --project="${PROJECT_A_ID}" --location="${REGION}" >/dev/null 2>&1; then
    gcloud -q alpha agent-registry bindings update github-mcp-3lo-binding \
      --project="${PROJECT_A_ID}" \
      --location="${REGION}" \
      --auth-provider-binding="projects/${PROJECT_A_ID}/locations/${REGION}/authProviders/github-mcp-auth" \
      --auth-provider-binding-continue-uri="${OAUTH_CONTINUE_URI}" \
      --auth-provider-binding-scopes="repo" >/dev/null 2>&1 || true
  else
    gcloud -q alpha agent-registry bindings create github-mcp-3lo-binding \
      --project="${PROJECT_A_ID}" \
      --location="${REGION}" \
      --display-name="Agent 1 to GitHub Copilot 3LO Binding" \
      --source-identifier="${AGENT_1_URN}" \
      --target-identifier="${GITHUB_MCP_URN}" \
      --auth-provider-binding="projects/${PROJECT_A_ID}/locations/${REGION}/authProviders/github-mcp-auth" \
      --auth-provider-binding-continue-uri="${OAUTH_CONTINUE_URI}" \
      --auth-provider-binding-scopes="repo"
  fi

  # 8b-2. Register per-agent runtime mTLS endpoints in Project A's Agent Registry
  AGENT_2_SHORT_ID="${RE_2_ID:-${AGENT_2_SPIFFE##*/}}"
  echo "  -> [8b-2] Registering Agent 1 (${AGENT_1_SHORT_ID}) and Agent 2 (${AGENT_2_SHORT_ID}) runtime mTLS endpoints in Project A's Agent Registry..."
  gcloud alpha agent-registry services describe runtime-agent-1 \
    --project="${PROJECT_A_ID}" --location="${REGION}" >/dev/null 2>&1 || \
  gcloud -q alpha agent-registry services create runtime-agent-1 \
    --project="${PROJECT_A_ID}" \
    --location="${REGION}" \
    --display-name="runtime.agent.1" \
    --description="Agent 1 (secops-github-orchestrator) runtime mTLS endpoint in Project A" \
    --endpoint-spec-type=no-spec \
    --interfaces="protocolBinding=JSONRPC,url=https://${REGION}-aiplatform.mtls.googleapis.com/v1/projects/${PROJECT_A_NUM}/locations/${REGION}/reasoningEngines/${AGENT_1_SHORT_ID}"

  gcloud alpha agent-registry services describe runtime-agent-2 \
    --project="${PROJECT_A_ID}" --location="${REGION}" >/dev/null 2>&1 || \
  gcloud -q alpha agent-registry services create runtime-agent-2 \
    --project="${PROJECT_A_ID}" \
    --location="${REGION}" \
    --display-name="runtime.agent.2" \
    --description="Agent 2 (internal-secops-auditor) cross-project runtime mTLS endpoint in Project B" \
    --endpoint-spec-type=no-spec \
    --interfaces="protocolBinding=JSONRPC,url=https://${REGION}-aiplatform.mtls.googleapis.com/v1/projects/${PROJECT_B_NUM}/locations/${REGION}/reasoningEngines/${AGENT_2_SHORT_ID}"

  # 8c. Resolve auto-created Agent Registry resource IDs and bind IAP (roles/iap.egressor) policies
  echo "  -> [8c] Binding IAP (roles/iap.egressor) policies on Agent Registry Endpoints & MCP Servers..."
  GAPI_MCP_UID=$(gcloud alpha agent-registry endpoints list \
    --project="${PROJECT_A_ID}" --location="${REGION}" \
    --filter="displayName=gapi.core.services" \
    --format="value(name)" | awk -F'/' '{print $NF}')

  RUNTIME_A1_UID=$(gcloud alpha agent-registry endpoints list \
    --project="${PROJECT_A_ID}" --location="${REGION}" \
    --filter="displayName=runtime.agent.1" \
    --format="value(name)" | awk -F'/' '{print $NF}')

  RUNTIME_A2_UID=$(gcloud alpha agent-registry endpoints list \
    --project="${PROJECT_A_ID}" --location="${REGION}" \
    --filter="displayName=runtime.agent.2" \
    --format="value(name)" | awk -F'/' '{print $NF}')

  INTERNAL_MCP_UID=$(gcloud alpha agent-registry mcp-servers list \
    --project="${PROJECT_A_ID}" --location="${REGION}" \
    --filter="displayName='Internal SecOps Cloud Run MCP Server'" \
    --format="value(name)" | awk -F'/' '{print $NF}')

  GITHUB_MCP_UID=$(gcloud alpha agent-registry mcp-servers list \
    --project="${PROJECT_A_ID}" --location="${REGION}" \
    --filter="displayName='External GitHub Copilot MCP Server'" \
    --format="value(name)" | awk -F'/' '{print $NF}')

  AZURE_MCP_UID=$(gcloud alpha agent-registry mcp-servers list \
    --project="${PROJECT_A_ID}" --location="${REGION}" \
    --filter="displayName='Cross-Cloud Azure Weather MCP Server'" \
    --format="value(name)" | awk -F'/' '{print $NF}')

  cat > /tmp/agw-cfg/iap-both-agents.json <<EOF
{
  "bindings": [
    {
      "role": "roles/iap.egressor",
      "members": [
        "${AGENT_1_SPIFFE}",
        "${AGENT_2_SPIFFE}",
        "${PRINCIPAL_SET_A}",
        "${PRINCIPAL_SET_B}",
        "serviceAccount:service-${PROJECT_A_NUM}@gcp-sa-aiplatform-re.iam.gserviceaccount.com",
        "serviceAccount:service-${PROJECT_B_NUM}@gcp-sa-aiplatform-re.iam.gserviceaccount.com",
        "serviceAccount:service-${PROJECT_A_NUM}@gcp-sa-aiplatform.iam.gserviceaccount.com",
        "serviceAccount:service-${PROJECT_B_NUM}@gcp-sa-aiplatform.iam.gserviceaccount.com"
      ]
    }
  ]
}
EOF

  cat > /tmp/agw-cfg/iap-agent1-only.json <<EOF
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

  gcloud -q alpha iap web set-iam-policy /tmp/agw-cfg/iap-agent1-only.json \
    --project="${PROJECT_A_ID}" \
    --region="${REGION}" \
    --resource-type=agent-registry >/dev/null 2>&1 || true

  for ep_uid in "${GAPI_MCP_UID}" "${RUNTIME_A1_UID}" "${RUNTIME_A2_UID}"; do
    if [[ -n "${ep_uid}" ]]; then
      gcloud -q alpha iap web set-iam-policy /tmp/agw-cfg/iap-both-agents.json \
        --project="${PROJECT_A_ID}" \
        --region="${REGION}" \
        --resource-type=agent-registry \
        --endpoint="${ep_uid}" >/dev/null
    fi
  done

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
    gcloud -q alpha iap web set-iam-policy /tmp/agw-cfg/iap-internal-mcp-cel.json \
      --project="${PROJECT_A_ID}" \
      --region="${REGION}" \
      --resource-type=agent-registry \
      --mcp-server="${INTERNAL_MCP_UID}" >/dev/null
  fi

  if [[ -n "${GITHUB_MCP_UID}" ]]; then
    gcloud -q alpha iap web set-iam-policy /tmp/agw-cfg/iap-agent1-only.json \
      --project="${PROJECT_A_ID}" \
      --region="${REGION}" \
      --resource-type=agent-registry \
      --mcp-server="${GITHUB_MCP_UID}" >/dev/null
  fi

  if [[ -n "${AZURE_MCP_UID}" ]]; then
    gcloud -q alpha iap web set-iam-policy /tmp/agw-cfg/iap-agent1-only.json \
      --project="${PROJECT_A_ID}" \
      --region="${REGION}" \
      --resource-type=agent-registry \
      --mcp-server="${AZURE_MCP_UID}" >/dev/null
  fi

  # 8d. Switch IAP Authz Extension to enforced V1 mode (matches per-resource Agent Registry IAP bindings)
  echo "  -> [8d] Ensuring IAP Authz Extension 'enterprise-egress-agw-iap-v2-ext' is in ENFORCE mode (V1)..."
  cat > /tmp/agw-cfg/iap-v2-authz-ext-enforce.yaml <<EOF
name: enterprise-egress-agw-iap-v2-ext
service: iap.googleapis.com
failOpen: false
timeout: 1s
metadata:
  iapPolicyVersion: "V1"
EOF
  gcloud -q service-extensions authz-extensions import enterprise-egress-agw-iap-v2-ext \
    --project="${PROJECT_A_ID}" \
    --location="${REGION}" \
    --source="/tmp/agw-cfg/iap-v2-authz-ext-enforce.yaml"

  echo "=================================================================="
  echo "End-to-End SPIFFE + Agent Gateway + IAP (ENFORCE) Configured!"
  echo "=================================================================="
else
  echo "=================================================================="
  echo "Infrastructure, Log Analytics, Agent Gateway & Registry Verified!"
  echo "Next: Run 'python3 agents/deploy_agents.py', then re-run deploy.sh"
  echo "=================================================================="
fi
