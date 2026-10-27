terraform {
  required_version = ">= 1.5.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.30.0"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = ">= 5.30.0"
    }
  }
}

provider "google" {
  region = var.region
}

provider "google-beta" {
  region = var.region
}

# ============================================================================
# 1. CREATE TWO GCP PROJECTS FROM SCRATCH
# ============================================================================

resource "google_project" "project_a" {
  name            = "Agent Gateway Governance Hub"
  project_id      = var.project_a_id
  org_id          = var.org_id
  billing_account = var.billing_account
}

resource "google_project" "project_b" {
  name            = "Agent & Cloud Run MCP Spoke"
  project_id      = var.project_b_id
  org_id          = var.org_id
  billing_account = var.billing_account
}

locals {
  project_a_apis = [
    "aiplatform.googleapis.com",
    "agentgateway.googleapis.com",
    "agentregistry.googleapis.com",
    "agentidentity.googleapis.com",
    "agentidentitycredentials.googleapis.com",
    "iap.googleapis.com",
    "networkservices.googleapis.com",
    "accesscontextmanager.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "serviceusage.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "cloudtrace.googleapis.com"
  ]

  project_b_apis = [
    "aiplatform.googleapis.com",
    "run.googleapis.com",
    "compute.googleapis.com",
    "dns.googleapis.com",
    "networkservices.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "cloudtrace.googleapis.com"
  ]
}

resource "google_project_service" "project_a_services" {
  for_each           = toset(local.project_a_apis)
  project            = google_project.project_a.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_project_service" "project_b_services" {
  for_each           = toset(local.project_b_apis)
  project            = google_project.project_b.project_id
  service            = each.value
  disable_on_destroy = false
}

# ============================================================================
# 2. PROJECT B: ISOLATED VPC, SUBNET, CLOUD ROUTER + NAT, RESTRICTED DNS
# ============================================================================

resource "google_compute_network" "vpc_agw_egress" {
  project                 = google_project.project_b.project_id
  name                    = "vpc-agw-egress"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.project_b_services]
}

resource "google_compute_subnetwork" "subnet_agw_egress" {
  project                  = google_project.project_b.project_id
  name                     = "subnet-agw-egress"
  ip_cidr_range            = "10.10.0.0/24"
  region                   = var.region
  network                  = google_compute_network.vpc_agw_egress.id
  private_ip_google_access = true
}

resource "google_compute_router" "router_agw_nat" {
  project = google_project.project_b.project_id
  name    = "router-agw-nat"
  region  = var.region
  network = google_compute_network.vpc_agw_egress.id
}

resource "google_compute_router_nat" "nat_agw_external_mcp" {
  project                            = google_project.project_b.project_id
  name                               = "nat-agw-external-mcp"
  router                             = google_compute_router.router_agw_nat.name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  log_config {
    enable = true
    filter = "ALL"
  }
}

resource "google_dns_managed_zone" "googleapis_restricted" {
  project    = google_project.project_b.project_id
  name       = "googleapis-restricted"
  dns_name   = "googleapis.com."
  visibility = "private"

  private_visibility_config {
    networks {
      network_url = google_compute_network.vpc_agw_egress.id
    }
  }
  depends_on = [google_project_service.project_b_services]
}

resource "google_dns_record_set" "restricted_a" {
  project      = google_project.project_b.project_id
  managed_zone = google_dns_managed_zone.googleapis_restricted.name
  name         = "restricted.googleapis.com."
  type         = "A"
  ttl          = 300
  rrdatas      = ["199.36.153.4", "199.36.153.5", "199.36.153.6", "199.36.153.7"]
}

resource "google_dns_record_set" "googleapis_wildcard_cname" {
  project      = google_project.project_b.project_id
  managed_zone = google_dns_managed_zone.googleapis_restricted.name
  name         = "*.googleapis.com."
  type         = "CNAME"
  ttl          = 300
  rrdatas      = ["restricted.googleapis.com."]
}

resource "google_dns_managed_zone" "run_app_restricted" {
  project    = google_project.project_b.project_id
  name       = "run-app-restricted"
  dns_name   = "run.app."
  visibility = "private"

  private_visibility_config {
    networks {
      network_url = google_compute_network.vpc_agw_egress.id
    }
  }
  depends_on = [google_project_service.project_b_services]
}

resource "google_dns_record_set" "run_app_a" {
  project      = google_project.project_b.project_id
  managed_zone = google_dns_managed_zone.run_app_restricted.name
  name         = "run.app."
  type         = "A"
  ttl          = 300
  rrdatas      = ["199.36.153.4", "199.36.153.5", "199.36.153.6", "199.36.153.7"]
}

resource "google_dns_record_set" "run_app_wildcard_cname" {
  project      = google_project.project_b.project_id
  managed_zone = google_dns_managed_zone.run_app_restricted.name
  name         = "*.run.app."
  type         = "CNAME"
  ttl          = 300
  rrdatas      = ["run.app."]
}

resource "google_compute_network_attachment" "agw_net_attachment" {
  provider              = google-beta
  project               = google_project.project_b.project_id
  name                  = "agw-net-attachment"
  region                = var.region
  connection_preference = "ACCEPT_MANUAL"
  subnetworks           = [google_compute_subnetwork.subnet_agw_egress.id]

  producer_accept_lists = [
    google_project.project_a.project_id
  ]
}

# ============================================================================
# 3. PROJECT B: PRIVATE TOKENLESS SECOPS CLOUD RUN MCP SERVER
# ============================================================================

resource "google_cloud_run_v2_service" "internal_mcp_server" {
  project  = google_project.project_b.project_id
  name     = "internal-mcp-server"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  template {
    containers {
      image = var.mcp_container_image
      ports {
        container_port = 8080
      }
    }
  }
  depends_on = [google_project_service.project_b_services]
}

# ============================================================================
# 4. VPC SERVICE CONTROLS (VPC-SC) PERIMETER SPANNING PROJECT A & PROJECT B
# ============================================================================

resource "google_access_context_manager_service_perimeter" "agw_perimeter" {
  parent = "accessPolicies/${var.access_policy_id}"
  name   = "accessPolicies/${var.access_policy_id}/servicePerimeters/sp_agw_mcp_perimeter"
  title  = "Agent Gateway & Cloud Run MCP Perimeter"

  status {
    resources = [
      "projects/${google_project.project_a.number}",
      "projects/${google_project.project_b.number}"
    ]

    restricted_services = [
      "run.googleapis.com",
      "aiplatform.googleapis.com",
      "agentgateway.googleapis.com",
      "agentregistry.googleapis.com",
      "agentidentity.googleapis.com"
    ]

    vpc_accessible_services {
      enable_restriction = true
      allowed_services   = ["RESTRICTED-SERVICES"]
    }
  }
}

# ============================================================================
# 5. PROJECT A: AUTH MANAGER (3LO), IAP V2, AGENT GATEWAY & REGISTRY
# ============================================================================

resource "terraform_data" "agw_3lo_and_spiffe_setup" {
  depends_on = [
    google_project_service.project_a_services,
    google_project_service.project_b_services,
    google_compute_network_attachment.agw_net_attachment,
    google_cloud_run_v2_service.internal_mcp_server
  ]

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    environment = {
      PROJECT_A_ID               = google_project.project_a.project_id
      PROJECT_A_NUM              = google_project.project_a.number
      PROJECT_B_ID               = google_project.project_b.project_id
      PROJECT_B_NUM              = google_project.project_b.number
      REGION                     = var.region
      MCP_URL                    = google_cloud_run_v2_service.internal_mcp_server.uri
      GITHUB_OAUTH_CLIENT_ID     = var.github_oauth_client_id
      GITHUB_OAUTH_CLIENT_SECRET = var.github_oauth_client_secret
      OAUTH_CONTINUE_URI         = var.oauth_default_continue_uri
    }
    command = <<-EOT
      set -euo pipefail

      # 1. Provision Agent Gateway P4SA in Project A and authorize on Project B subnet
      gcloud beta services identity create \
        --service=agentgateway.googleapis.com \
        --project="$PROJECT_A_ID"

      AGW_P4SA="service-$${PROJECT_A_NUM}@gcp-sa-agentgateway.iam.gserviceaccount.com"

      gcloud compute networks subnets add-iam-policy-binding subnet-agw-egress \
        --project="$PROJECT_B_ID" \
        --region="$REGION" \
        --member="serviceAccount:$${AGW_P4SA}" \
        --role="roles/compute.networkUser"

      # 2. Create 3-Legged OAuth (3LO) Auth Provider in Auth Manager for GitHub MCP
      gcloud alpha agent-identity auth-providers create github-mcp-auth \
        --project="$PROJECT_A_ID" \
        --location="$REGION" \
        --display-name="GitHub Copilot MCP 3LO Auth Provider" \
        --three-legged-oauth-client-id="$GITHUB_OAUTH_CLIENT_ID" \
        --three-legged-oauth-client-secret="$GITHUB_OAUTH_CLIENT_SECRET" \
        --three-legged-oauth-authorization-url="https://github.com/login/oauth/authorize?scope=repo%20read:org%20read:user" \
        --three-legged-oauth-token-url="https://github.com/login/oauth/access_token" \
        --three-legged-oauth-default-continue-uri="$OAUTH_CONTINUE_URI"

      # 3. Create Central Agent Gateway in Project A (Egress + IAP v2 + VPC-SC ALL_TRAFFIC)
      curl -s -X POST \
        -H "Authorization: Bearer $(gcloud auth print-access-token)" \
        -H "Content-Type: application/json" \
        -H "X-Goog-User-Project: $${PROJECT_A_ID}" \
        "https://agentgateway.googleapis.com/v1alpha/projects/$${PROJECT_A_ID}/locations/$${REGION}/agentGateways?agentGatewayId=enterprise-egress-agw" \
        -d "{
          \"protocols\": [\"MCP\"],
          \"googleManaged\": {
            \"governedAccessPath\": \"AGENT_TO_ANYWHERE\"
          },
          \"networkConfig\": {
            \"egress\": {
              \"networkAttachment\": \"projects/$${PROJECT_B_ID}/regions/$${REGION}/networkAttachments/agw-net-attachment\",
              \"vpcEgress\": \"ALL_TRAFFIC\"
            }
          },
          \"registryWithIapConfig\": {
            \"iapPolicyVersion\": \"V2\"
          }
        }"

      echo "Waiting 60s for Agent Gateway provisioning..."
      sleep 60

      AGW_SA=$(curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" \
        "https://agentgateway.googleapis.com/v1alpha/projects/$${PROJECT_A_ID}/locations/$${REGION}/agentGateways/enterprise-egress-agw" \
        | jq -r '.agentGatewayServiceAccount')

      # 4. Grant Agent Gateway Service Identity Tokenless Invoker on Project B Cloud Run MCP
      gcloud run services add-iam-policy-binding internal-mcp-server \
        --project="$PROJECT_B_ID" \
        --region="$REGION" \
        --member="serviceAccount:$${AGW_SA}" \
        --role="roles/run.invoker"
    EOT
  }
}
