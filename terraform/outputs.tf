output "project_a_id" {
  value       = google_project.project_a.project_id
  description = "Project A (Governance Hub) ID"
}

output "project_b_id" {
  value       = google_project.project_b.project_id
  description = "Project B (Agent & Cloud Run MCP Spoke) ID"
}

output "internal_mcp_server_url" {
  value       = google_cloud_run_v2_service.internal_mcp_server.uri
  description = "Private SecOps Cloud Run MCP Server URL in Project B"
}

output "github_oauth_app_redirect_uri" {
  value       = "https://agentidentitycredentials.googleapis.com/v1/projects/${google_project.project_a.project_id}/locations/${var.region}/authProviders/github-mcp-auth/oauthcallback"
  description = "Exact Redirect URI to configure in your GitHub OAuth App settings"
}
