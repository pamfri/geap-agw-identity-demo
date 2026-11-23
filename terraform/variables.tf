variable "org_id" {
  description = "GCP Organization ID (numeric)"
  type        = string
}

variable "billing_account" {
  description = "GCP Billing Account ID (e.g., 012345-6789AB-CDEF01)"
  type        = string
}

variable "access_policy_id" {
  description = "Organization Access Context Manager Policy ID (numeric)"
  type        = string
}

variable "project_a_id" {
  description = "Project A ID (Agent Gateway, IAP v2, Auth Manager 3LO, Agent 1 - SecOps GitHub Orchestrator)"
  type        = string
  default     = "your-project-a-id"
}

variable "project_b_id" {
  description = "Project B ID (Agent 2 - Internal SecOps Auditor, Cloud Run MCP, Isolated VPC + NAT)"
  type        = string
  default     = "your-project-b-id"
}

variable "region" {
  description = "GCP Region for all regional resources"
  type        = string
  default     = "us-central1"
}

variable "github_oauth_client_id" {
  description = "GitHub OAuth App Client ID for 3-Legged OAuth (3LO) in Auth Manager"
  type        = string
  sensitive   = true
}

variable "github_oauth_client_secret" {
  description = "GitHub OAuth App Client Secret for 3-Legged OAuth (3LO) in Auth Manager"
  type        = string
  sensitive   = true
}

variable "oauth_default_continue_uri" {
  description = "Optional override for 3LO user ID validation continue URI (defaults to Cloud Run secops-3lo-callback in Project A)"
  type        = string
  default     = ""
}

variable "mcp_container_image" {
  description = "Container image for the private SecOps Cloud Run MCP Server in Project B"
  type        = string
  default     = "us-central1-docker.pkg.dev/your-project-b-id/mcp-repo/internal-mcp-server:latest"
}
