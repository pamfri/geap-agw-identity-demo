# Zero-Trust Multi-Project Agent Gateway, SPIFFE Identity, IAP v2 & 3LO Auth Manager

End-to-end reference implementation demonstrating **per-agent cryptographic workload identity (SPIFFE)**, **centralized multi-project egress governance (Agent Gateway + IAP v2)**, **private cross-project VPC routing (Private Service Connect + Private DNS)**, and **user-delegated 3-Legged OAuth (3LO) credential brokering (Google Cloud Managed Auth Manager)** on Vertex AI Agent Engine (GEAP).

---

## 1. Architecture Overview

```text
+---------------------------------------------------------------------------------------------------+
| PROJECT A: your-project-a-id (Central Governance & External Egress Hub)                           |
|                                                                                                   |
|  [Agent 1: secops-github-orchestrator]  <-- SPIFFE ID: .../reasoningEngines/<RE_1_ID>             |
|     |                                                                                             |
|     | (1) RetrieveCredentials (authenticated via Agent 1's SPIFFE Identity)                       |
|     v                                                                                             |
|  [Google Cloud Managed Auth Manager (github-mcp-auth)] <---> [Cloud Run: secops-3lo-callback]     |
|     |                                                                                             |
|     | (2) All outbound traffic forced through Central Agent Gateway                               |
|     v                                                                                             |
|  [Central Agent Gateway: enterprise-egress-agw] + [IAP v2 Policy Engine] + [Agent Registry]       |
|     |                                                   |                                         |
|     | Route A: External Internet (IAP v2 checks SPIFFE) | Route B: Private PSC Tunnel (240.0.0.10)|
+-----|---------------------------------------------------|-----------------------------------------+
      |                                                   |
      v (Allowed ONLY for Agent 1's SPIFFE ID)            v (Allowed for Agent 1 & Agent 2)
+-----------------------------------+       +-------------------------------------------------------+
| External GitHub Copilot MCP       |       | PROJECT B: your-project-b-id (Isolated SecOps VPC)    |
| https://api.githubcopilot.com/mcp |       |                                                       |
+-----------------------------------+       |  [Private Cloud Run: internal-mcp-server]             |
                                            |   (INTERNAL_ONLY ingress - no public internet access) |
                                            |                                                       |
                                            |  [Agent 2: internal-secops-auditor]                   |
                                            |   <-- SPIFFE ID: .../reasoningEngines/<RE_2_ID>       |
                                            |   (Routes its outbound traffic up to Project A's AGW) |
                                            +-------------------------------------------------------+
```

### The 4 Security Pillars

| Pillar | Google Cloud Service | What It Solves |
| :--- | :--- | :--- |
| **1. Per-Agent Identity (SPIFFE)** | Vertex AI Agent Engine (`identity_type=AGENT_IDENTITY`) | Mints a unique cryptographic SPIFFE ID (`principal://agents.global.org-<ORG_ID>.system.id.goog/...`) per agent instance instead of sharing a project-wide service account. |
| **2. Private Cross-Project VPC Egress** | Agent Gateway (`enterprise-egress-agw`) + PSC Network Attachment + Private DNS (`240.0.0.10`) | Allows Agent 1 (in Project A) and Agent 2 (in Project B) to reach an `--ingress=internal` Cloud Run MCP server in Project B privately over Private Service Connect. |
| **3. Managed 3LO OAuth Broker** | Google Cloud Managed Auth Manager (`agentidentitycredentials.googleapis.com`) + `github-mcp-auth` | Brokers 3-Legged OAuth (3LO) user consent for GitHub without exposing the OAuth `client_secret` to the agent. Caches short-lived user tokens and releases them only to Agent 1's SPIFFE ID. |
| **4. Zero-Trust Egress Firewall** | Agent Registry + IAP v2 (`enterprise-egress-agw-authz-policy`) | Enforces per-agent SPIFFE access control at the proxy layer: allows Agent 1 to call `api.githubcopilot.com/mcp/`, blocks Agent 2 (`DENIED`), and default-denies unapproved domains (`example.com` -> `403 Forbidden`). |

---

## 2. Repository Structure

```text
secops-agw-spiffe/
├── .env.example                            # Template for environment variables (Project IDs & GitHub OAuth Client ID/Secret)
├── .gitignore                              # Excludes __pycache__, *.env, spiffe_identities.env, and Terraform state
├── README.md                               # Architecture, deployment, Playground demo & Cloud Logging guide
├── agents/
│   ├── deploy_agents.py                    # Deploys/updates Agent 1 & Agent 2 with AGENT_IDENTITY + Agent Gateway & runs smoke tests
│   ├── agent1_github_secops/
│   │   └── agent1.py                       # Agent 1 (Project A): Internal SecOps MCP + External GitHub Copilot MCP (3LO)
│   └── agent2_internal_sre/
│       └── agent2.py                       # Agent 2 (Project B): Internal SecOps MCP + Egress Policy Verification Probe
├── client-3lo/
│   ├── Dockerfile                          # Container definition for the Cloud Run 3LO callback service
│   ├── main.py                             # FastAPI 3LO callback service (/authorize 302 redirect & /validateUserId finalizeCredentials)
│   └── requirements.txt                    # FastAPI, Uvicorn, Google Auth & Vertex AI SDK dependencies
├── mcp-server/
│   ├── Dockerfile                          # Container definition for the private Project B Cloud Run MCP server
│   ├── requirements.txt                    # FastMCP & Starlette dependencies
│   └── server.py                           # Stateless JSON-RPC FastMCP server (CVE scanner, deployment manifest & remediation ledger)
├── scripts/
│   └── deploy.sh                           # Idempotent CLI deployment script (VPC, PSC, DNS, Cloud Run, Auth Manager, AGW & IAP v2)
└── terraform/
    ├── main.tf                             # Equivalent Terraform IaC configuration
    ├── outputs.tf                          # Terraform outputs
    ├── terraform.tfvars.example            # Example Terraform variables file
    └── variables.tf                        # Terraform input variables
```

---

## 3. End-to-End Deployment Steps

### Step 1: Configure Environment Variables & Provision Infrastructure
Copy `.env.example` to `.env` (which is ignored by `.gitignore`), fill in your project IDs and GitHub OAuth App credentials, and run `scripts/deploy.sh`:

```bash
cp .env.example .env
# Edit .env with your PROJECT_A_ID, PROJECT_B_ID, GITHUB_OAUTH_CLIENT_ID, and GITHUB_OAUTH_CLIENT_SECRET
source .env

./scripts/deploy.sh
```

> **Note on GitHub OAuth App Redirect URI**: In your GitHub OAuth App settings (`Settings -> Developer settings -> OAuth Apps`), set the **Authorization callback URL** to Google Cloud Managed Auth Manager's callback endpoint:
> `https://agentidentitycredentials.googleapis.com/v1/projects/${PROJECT_A_ID}/locations/${REGION}/authProviders/github-mcp-auth/oauthcallback`

### Step 2: Deploy Agent 1 & Agent 2 (Provisions SPIFFE Identities)
Run `agents/deploy_agents.py` (which automatically reads `.env` at the repo root) to provision both agents on Vertex AI Agent Engine with `IdentityType.AGENT_IDENTITY` and `agent_gateway_config`, write their SPIFFE principals to `agents/spiffe_identities.env`, and execute live smoke tests:

```bash
cd /tmp && /opt/homebrew/bin/python3 /path/to/secops-agw-spiffe/agents/deploy_agents.py
```

### Step 3: Bind Provisioned SPIFFE Principals to IAP v2 & Auth Manager
Re-run `scripts/deploy.sh` once `spiffe_identities.env` is generated so step 8 binds Agent 1's and Agent 2's SPIFFE IDs to the Agent Registry IAP policies and `github-mcp-auth`:

```bash
./scripts/deploy.sh
```

---

## 4. Live Demo Guide: GEAP Playground

### Scene 1: Agent 1 — Private Cross-Project MCP + 3LO GitHub OAuth Consent

1. **Open Agent 1 (`secops-github-orchestrator`) in the GEAP Playground (`PROJECT_A_ID`)**:
   `https://console.cloud.google.com/vertex-ai/agents/agent-engines/locations/us-central1/agent-engines/<RE_1_ID>/playground?project=<PROJECT_A_ID>`
2. **Paste Prompt 1A into the Playground**:
   ```text
   1. Check the active CVE alerts and internal deployment manifest for payment-service.
   2. Check my authenticated GitHub user profile using get_github_authenticated_user.
   ```
3. **What Happens Live on Screen**:
   - **Internal MCP Calls Succeed (`200 OK`)**: Agent 1 calls `get_active_cve_alerts("payment-service")` and `get_internal_deployment_manifest("payment-service")` through the Central Agent Gateway over Private Service Connect (`240.0.0.10:443`) into Project B's private Cloud Run service (`internal-mcp-server`).
   - **3LO GitHub Call**:
     - If not yet authorized, Agent 1 calls Auth Manager (`RetrieveCredentials`) and displays the short authorization link:
       `https://<CALLBACK_SERVICE_URL>/authorize?uid=vais-query-reasoning-engine`
     - Click that link -> click **Authorize** on GitHub -> see **"GitHub 3LO Authorization Finalized on Cloud Run!"**.
     - Once authorized, Auth Manager returns the cached GitHub OAuth token (`token_ready`) and Agent 1 returns your live GitHub username and profile without prompting again.
4. **Paste Prompt 1B (After Authorizing GitHub)**:
   ```text
   Now that GitHub is authorized, check my GitHub user profile with get_github_authenticated_user and record an internal remediation ticket for CVE-2026-41092 on payment-service.
   ```

---

### Scene 2: Agent 2 — Cross-Project Internal Access (`ALLOWED`) vs. External Egress (`DENIED`)

1. **Open Agent 2 (`internal-secops-auditor`) in the GEAP Playground (`PROJECT_B_ID`)**:
   `https://console.cloud.google.com/vertex-ai/agents/agent-engines/locations/us-central1/agent-engines/<RE_2_ID>/playground?project=<PROJECT_B_ID>`
2. **Paste Prompt 2A into the Playground**:
   ```text
   1. Get the active CVE alerts and internal deployment manifest for checkout-worker.
   2. Run test_egress_policy_check on https://api.githubcopilot.com/mcp/ and on https://example.com and report the exact HTTP status codes and policy results.
   ```
3. **What Happens Live on Screen**:
   - **Internal SecOps MCP Call (`ALLOWED — HTTP 200`)**: Agent 2 routes its request through Project A's Central Agent Gateway over PSC to `internal-mcp-server` and retrieves `CVE-2026-39104` for `checkout-worker`.
   - **External GitHub Copilot MCP Probe (`DENIED — HTTP 403 / 401`)**: Blocked because Agent 2's SPIFFE ID is not granted `roles/iap.egressor` on `github-copilot-mcp`.
   - **Unapproved Internet Probe (`DENIED — HTTP 403 Forbidden`)**: Blocked by the Central Agent Gateway's default-deny policy on `https://example.com`.

---

## 5. Live Demo Guide: Cloud Logging (Logs Explorer) Queries

### Query 1: Google Cloud Managed Auth Manager (`RetrieveCredentials` & `FinalizeCredentials`)
- **Console Project**: **Project A (`PROJECT_A_ID`)**
- **Filter**:
  ```text
  logName="projects/<PROJECT_A_ID>/logs/cloudaudit.googleapis.com%2Fdata_access"
  protoPayload.serviceName="agentidentitycredentials.googleapis.com"
  ```
- **What It Shows**:
  - `protoPayload.serviceName: "agentidentitycredentials.googleapis.com"` (Google's managed Auth Manager service).
  - `protoPayload.authenticationInfo.principalSubject`: Agent 1's cryptographic **SPIFFE identity** (`principal://agents.global.org-<ORG_ID>.../reasoningEngines/<RE_1_ID>`).
  - `protoPayload.authorizationInfo[0].permission: "agentidentity.authProviders.retrieveCredentials"`, `"granted": true`.

---

### Query 2: Central Agent Gateway — Deep MCP Inspection & Private PSC Routing (`HTTP 200`)
- **Console Project**: **Project A (`PROJECT_A_ID`)**
- **Filter**:
  ```text
  resource.type="networkservices.googleapis.com/Gateway"
  resource.labels.gateway_name="enterprise-egress-agw"
  httpRequest.status=200
  ```
- **What It Shows**:
  - `httpRequest.serverIp: "240.0.0.10:443"` on `internal-mcp-server` calls (proving Private Cloud DNS + Private Service Connect tunneling into Project B's VPC).
  - `jsonPayload.agentGatewayInfo.mcpInfo`: `{"method": "tools/call", "parameter": "get_active_cve_alerts"}` and `{"method": "tools/call", "parameter": "get_me"}` (proving L7 MCP JSON-RPC protocol inspection by Agent Gateway).
  - `jsonPayload.authzPolicyInfo.result: "ALLOWED"`.

---

### Query 3: Central Agent Gateway — Zero-Trust Egress Denials (`DENIED` / `HTTP 403`)
- **Console Project**: **Project A (`PROJECT_A_ID`)**
- **Filter**:
  ```text
  resource.type="networkservices.googleapis.com/Gateway"
  resource.labels.gateway_name="enterprise-egress-agw"
  (httpRequest.status=403 OR httpRequest.status=401 OR jsonPayload.authzPolicyInfo.result="DENIED")
  ```
- **What It Shows**:
  - `enforcedGatewaySecurityPolicy.requestWasTlsIntercepted: true`
  - `authzPolicyInfo.result: "DENIED"` (`enterprise-egress-agw-authz-policy`)
  - `httpRequest.status: 403` when Agent 2 attempts to reach `https://example.com/` or `https://api.githubcopilot.com/mcp/`.

---

### Query 4: Project B Private Cloud Run MCP Server Audit Logs
- **Console Project**: **Switch Console Project Picker to Project B (`PROJECT_B_ID`)**
- **Filter**:
  ```text
  resource.type="cloud_run_revision"
  resource.labels.service_name="internal-mcp-server"
  jsonPayload.component="internal-secops-mcp-server"
  ```
- **What It Shows**:
  - `jsonPayload.caller_spiffe_principal`: Logs the exact calling agent's SPIFFE ID alongside the invoked tool and service name.
