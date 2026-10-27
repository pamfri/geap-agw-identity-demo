"""Agent 1 (Project A: your-project-a-id) — SecOps Incident-to-PR Orchestrator.

Identity:
- Runs on Vertex AI Agent Engine with identity_type=AGENT_IDENTITY (SPIFFE).
- Zero service account keys, zero static tokens, zero env secrets.

Governed Toolsets via Central Agent Gateway (enterprise-egress-agw):
1. Internal SecOps Cloud Run MCP Server (Project B private Cloud Run MCP server):
   - Exposes get_active_cve_alerts, get_internal_deployment_manifest, and
     record_remediation_ticket.
   - Routed privately via Agent Gateway -> PSC Network Attachment ->
     Private Cloud DNS (*.run.app -> 240.0.0.10) -> PSC Endpoint (psc2gapis).
2. External GitHub Copilot MCP Server (https://api.githubcopilot.com/mcp/):
   - Invoked via 3-Legged OAuth (3LO) managed by Google Cloud Auth Manager
     (authProviders/github-mcp-auth) and routed through Agent Gateway + IAP v2 +
     Project B VPC + Cloud NAT.
"""

import asyncio
import json
import logging
import os
import sys
import traceback
from typing import Any
from urllib.parse import urlencode
from google.adk.agents import LlmAgent
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.tools.tool_context import ToolContext
import google.auth
from google.auth.transport.requests import Request
import httpx
import vertexai
from vertexai.agent_engines import AdkApp

logger = logging.getLogger(__name__)

PROJECT_A_ID = os.environ.get("PROJECT_A_ID", "your-project-a-id")
REGION = os.environ.get("REGION", "us-central1")
os.environ["GOOGLE_CLOUD_PROJECT"] = PROJECT_A_ID
os.environ["GOOGLE_CLOUD_LOCATION"] = REGION
vertexai.init(project=PROJECT_A_ID, location=REGION)
_raw_mcp_url = os.environ.get(
    "INTERNAL_MCP_URL",
    "https://internal-mcp-server.a.run.app/mcp",
).rstrip("/")
INTERNAL_MCP_URL = (
    _raw_mcp_url if _raw_mcp_url.endswith("/mcp") else f"{_raw_mcp_url}/mcp"
)
GITHUB_MCP_URL = os.environ.get(
    "GITHUB_MCP_URL",
    "https://api.githubcopilot.com/mcp/",
)
AUTH_PROVIDER_RESOURCE = (
    f"projects/{PROJECT_A_ID}/locations/{REGION}/authProviders/github-mcp-auth"
)
CONTINUE_URI = os.environ.get(
    "OAUTH_CONTINUE_URI", "http://localhost:8501/validateUserId"
)
GITHUB_OAUTH_SCOPES = ["repo"]

SECOPS_ORCHESTRATOR_INSTRUCTION = """You are the SecOps Incident-to-PR Orchestrator (Agent 1).

You have direct access to the following 5 tools (call them directly by their exact function name):
1. Internal SecOps MCP Tools (routed via Agent Gateway + IAP v2 + Private Cloud DNS 240.0.0.10 + PSC to Project B Cloud Run):
   - `get_active_cve_alerts(service_name)`: Inspect active CVEs detected in our production environments (e.g., 'payment-service', 'checkout-worker', or 'all').
   - `get_internal_deployment_manifest(service_name)`: Check production deployment metadata, SLA timers, and whether a change freeze is active.
   - `record_remediation_ticket(cve_id, service_name, github_pr_or_issue_url, remediated_by_github_user)`: Log a GitHub PR or Issue URL into the internal SecOps compliance ledger.

2. External GitHub Copilot MCP 3LO Tools (authenticated via Google Cloud Auth Manager `github-mcp-auth`):
   - `get_github_authenticated_user()`: Verify the authenticated engineer's GitHub profile via the governed GitHub Copilot MCP server (`https://api.githubcopilot.com/mcp/`).
   - `create_github_remediation_issue(owner, repo, title, body)`: Create a remediation issue on GitHub via the governed GitHub Copilot MCP server using the engineer's 3-Legged OAuth (3LO) token.

RULES:
- If the user asks to check their GitHub user profile or call `get_github_authenticated_user`, ALWAYS call `get_github_authenticated_user()`.
- If `get_github_authenticated_user` or `create_github_remediation_issue` returns `status: "URI_CONSENT_REQUIRED"`, ALWAYS display the short `short_authorize_url` (e.g. `https://<callback-service-url>/authorize?uid=vais-query-reasoning-engine`) FIRST so the user can click it without any base64 `state` truncation/corruption, along with the `user_id` and `consent_nonce`.
"""


class AutoCreatingInMemorySessionService(InMemorySessionService):
  """In-memory session service that auto-creates missing sessions on get_session.

  Prevents SessionNotFoundError when the Cloud Console Playground UI sends a
  session_id created prior to a container update or via the Vertex AI Sessions API.
  """

  async def get_session(
      self,
      *,
      app_name: str,
      user_id: str,
      session_id: str,
      config: Any = None,
  ):
    existing = self._get_session_impl(
        app_name=app_name,
        user_id=user_id,
        session_id=session_id,
        config=config,
    )
    if existing is None and session_id:
      return self._create_session_impl(
          app_name=app_name,
          user_id=user_id,
          session_id=session_id,
      )
    return existing

  def get_session_sync(
      self,
      *,
      app_name: str,
      user_id: str,
      session_id: str,
      config: Any = None,
  ):
    existing = self._get_session_impl(
        app_name=app_name,
        user_id=user_id,
        session_id=session_id,
        config=config,
    )
    if existing is None and session_id:
      return self._create_session_impl(
          app_name=app_name,
          user_id=user_id,
          session_id=session_id,
      )
    return existing


async def _call_internal_mcp_jsonrpc(
    tool_name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
  """Calls the Internal SecOps Cloud Run MCP Server over Streamable HTTP JSON-RPC 2.0."""
  mcp_url = os.environ.get("INTERNAL_MCP_URL", INTERNAL_MCP_URL).rstrip("/")
  if not mcp_url.endswith("/mcp"):
    mcp_url = f"{mcp_url}/mcp"
  headers = {
      "Content-Type": "application/json",
      "Accept": "application/json, text/event-stream",
  }
  payload = {
      "jsonrpc": "2.0",
      "id": 1,
      "method": "tools/call",
      "params": {
          "name": tool_name,
          "arguments": arguments,
      },
  }
  print(
      f"[INTERNAL_MCP] Calling {mcp_url} tool='{tool_name}' args={arguments}",
      file=sys.stderr,
      flush=True,
  )
  try:
    async with httpx.AsyncClient(timeout=30.0) as client:
      resp = await client.post(mcp_url, headers=headers, json=payload)
    print(
        f"[INTERNAL_MCP] Response from {mcp_url} tool='{tool_name}': HTTP {resp.status_code}",
        file=sys.stderr,
        flush=True,
    )
    try:
      data = resp.json()
      content_items = data.get("result", {}).get("content", [])
      if content_items and isinstance(content_items, list):
        text_val = content_items[0].get("text")
        if text_val:
          try:
            return json.loads(text_val)
          except Exception:
            return {"status_code": resp.status_code, "result": text_val}
      return data
    except Exception:
      return {
          "status_code": resp.status_code,
          "mcp_endpoint": mcp_url,
          "tool_called": tool_name,
          "response": resp.text[:2000],
      }
  except Exception as exc:  # pylint: disable=broad-except
    print(
        f"[INTERNAL_MCP] Error calling {mcp_url} tool='{tool_name}': {exc}",
        file=sys.stderr,
        flush=True,
    )
    return {
        "status": "ERROR",
        "mcp_endpoint": mcp_url,
        "tool_called": tool_name,
        "error": str(exc),
    }


async def get_active_cve_alerts(
    service_name: str = "payment-service",
) -> dict[str, Any]:
  """Retrieve active production CVE alerts from the internal SecOps scanner in Project B.

  Args:
    service_name: Name of the internal service ('payment-service',
      'checkout-worker', or 'all').
  """
  return await _call_internal_mcp_jsonrpc(
      "get_active_cve_alerts", {"service_name": service_name}
  )


async def get_internal_deployment_manifest(
    service_name: str = "payment-service",
) -> dict[str, Any]:
  """Fetch the internal production deployment metadata and policy requirements for a service.

  Args:
    service_name: Name of the internal service (e.g., 'payment-service' or
      'checkout-worker').
  """
  return await _call_internal_mcp_jsonrpc(
      "get_internal_deployment_manifest", {"service_name": service_name}
  )


async def record_remediation_ticket(
    cve_id: str,
    service_name: str,
    github_pr_or_issue_url: str,
    remediated_by_github_user: str,
) -> dict[str, Any]:
  """Record a GitHub remediation PR or Issue URL into the internal SecOps compliance ledger.

  Args:
    cve_id: The CVE identifier being remediated (e.g., 'CVE-2026-41092').
    service_name: The affected internal service name (e.g., 'payment-service').
    github_pr_or_issue_url: Full URL of the GitHub Pull Request or Issue.
    remediated_by_github_user: The GitHub username of the engineer who
      authorized via 3LO.
  """
  return await _call_internal_mcp_jsonrpc(
      "record_remediation_ticket",
      {
          "cve_id": cve_id,
          "service_name": service_name,
          "github_pr_or_issue_url": github_pr_or_issue_url,
          "remediated_by_github_user": remediated_by_github_user,
      },
  )


def _get_spiffe_adc_headers() -> dict[str, str]:
  """Fetches Agent 1's own SPIFFE identity token via Application Default Credentials."""
  credentials, _ = google.auth.default(
      scopes=["https://www.googleapis.com/auth/cloud-platform"]
  )
  credentials.refresh(Request())
  return {
      "Authorization": f"Bearer {credentials.token}",
      "Content-Type": "application/json",
  }


async def _retrieve_3lo_github_credential(
    tool_context: ToolContext | None,
) -> dict[str, Any]:
  """Calls Google Cloud Auth Manager (`credentials:retrieve`) using Agent 1's SPIFFE identity."""
  user_id = (
      getattr(tool_context, "user_id", None) if tool_context else None
  ) or "playground-user"
  project_a = os.environ.get("PROJECT_A_ID", PROJECT_A_ID)
  region = os.environ.get("REGION", REGION)
  auth_provider = (
      f"projects/{project_a}/locations/{region}/authProviders/github-mcp-auth"
  )
  continue_uri = os.environ.get("OAUTH_CONTINUE_URI", CONTINUE_URI)
  retrieve_url = (
      f"https://agentidentitycredentials.googleapis.com/v1/"
      f"{auth_provider}/credentials:retrieve"
  )
  continue_uri_with_uid = f"{continue_uri}?{urlencode({'uid': user_id})}"
  req_body = {
      "userId": user_id,
      "scopes": GITHUB_OAUTH_SCOPES,
      "continueUri": continue_uri_with_uid,
  }
  print(
      "[AUTH_MANAGER_3LO] Calling Auth Manager RetrieveCredentials:"
      f" POST {retrieve_url} for user_id='{user_id}'",
      file=sys.stderr,
      flush=True,
  )
  headers = await asyncio.to_thread(_get_spiffe_adc_headers)
  async with httpx.AsyncClient(timeout=30.0) as client:
    resp = await client.post(retrieve_url, headers=headers, json=req_body)

  if resp.status_code != 200:
    print(
        "[AUTH_MANAGER_3LO] RetrieveCredentials HTTP error"
        f" {resp.status_code}: {resp.text}",
        file=sys.stderr,
        flush=True,
    )
    return {
        "status": "ERROR",
        "http_status": resp.status_code,
        "auth_provider": auth_provider,
        "user_id": user_id,
        "error": resp.text,
    }

  data = resp.json()
  if "uriConsentRequired" in data:
    consent = data["uriConsentRequired"]
    auth_uri = consent.get("authorizationUri", "")
    nonce = consent.get("consentNonce", "")
    continue_uri_with_nonce = (
        f"{continue_uri}?{urlencode({'uid': user_id, 'prev_nonce': nonce})}"
    )
    print(
        "[AUTH_MANAGER_3LO] Auth Manager returned uri_consent_required for"
        f" user_id='{user_id}'! authorization_uri='{auth_uri}',"
        f" consent_nonce='{nonce}'",
        file=sys.stderr,
        flush=True,
    )
    register_url = continue_uri.replace("/validateUserId", "/registerNonce")
    short_authorize_url = continue_uri.replace(
        "/validateUserId", f"/authorize?{urlencode({'uid': user_id})}"
    )
    try:
      async with httpx.AsyncClient(timeout=5.0) as reg_client:
        await reg_client.post(
            register_url,
            json={
                "user_id": user_id,
                "consent_nonce": nonce,
                "authorization_uri": auth_uri,
            },
        )
    except Exception:
      pass
    return {
        "status": "URI_CONSENT_REQUIRED",
        "auth_provider": auth_provider,
        "user_id": user_id,
        "short_authorize_url": short_authorize_url,
        "authorization_uri": auth_uri,
        "consent_nonce": nonce,
        "continue_uri": continue_uri_with_nonce,
        "message": (
            "Google Cloud Auth Manager requires 3-Legged OAuth (3LO) user"
            f" consent for user_id='{user_id}'. Click {short_authorize_url}"
            " to sign in to GitHub and delegate access."
        ),
    }

  if "success" in data:
    token = data["success"].get("token", "")
    print(
        "[AUTH_MANAGER_3LO] Auth Manager returned vaulted 3LO GitHub Bearer"
        f" token for user_id='{user_id}'!",
        file=sys.stderr,
        flush=True,
    )
    return {
        "status": "SUCCESS",
        "user_id": user_id,
        "token": token,
    }

  print(
      f"[AUTH_MANAGER_3LO] Auth Manager returned state: {data}",
      file=sys.stderr,
      flush=True,
  )
  return {
      "status": "PENDING_OR_OTHER",
      "auth_provider": auth_provider,
      "user_id": user_id,
      "response": data,
  }


async def _call_github_mcp_jsonrpc(
    token: str, tool_name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
  """Calls the External GitHub Copilot MCP Server through Agent Gateway with the 3LO token."""
  print(
      f"[AUTH_MANAGER_3LO] Calling {GITHUB_MCP_URL} tool='{tool_name}' with"
      " user-delegated 3LO GitHub Bearer token via Agent Gateway!",
      file=sys.stderr,
      flush=True,
  )
  headers = {
      "Authorization": f"Bearer {token}",
      "Content-Type": "application/json",
      "Accept": "application/json, text/event-stream",
  }
  payload = {
      "jsonrpc": "2.0",
      "id": 1,
      "method": "tools/call",
      "params": {
          "name": tool_name,
          "arguments": arguments,
      },
  }
  async with httpx.AsyncClient(timeout=30.0) as client:
    resp = await client.post(GITHUB_MCP_URL, headers=headers, json=payload)
    return {
        "status_code": resp.status_code,
        "mcp_endpoint": GITHUB_MCP_URL,
        "tool_called": tool_name,
        "response": resp.text[:2000],
    }


async def get_github_authenticated_user(
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
  """Get details of the authenticated GitHub user via 3-Legged OAuth (3LO) on GitHub Copilot MCP."""
  try:
    cred_result = await _retrieve_3lo_github_credential(tool_context)
    if cred_result.get("status") != "SUCCESS":
      return cred_result
    return await _call_github_mcp_jsonrpc(cred_result["token"], "get_me", {})
  except Exception as exc:  # pylint: disable=broad-except
    print(
        f"[AUTH_MANAGER_3LO] Exception in get_github_authenticated_user: {exc}",
        file=sys.stderr,
        flush=True,
    )
    return {"status": "ERROR", "error": str(exc)}


async def create_github_remediation_issue(
    owner: str,
    repo: str,
    title: str,
    body: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
  """Create a remediation GitHub issue in owner/repo via 3-Legged OAuth (3LO) on GitHub Copilot MCP."""
  try:
    cred_result = await _retrieve_3lo_github_credential(tool_context)
    if cred_result.get("status") != "SUCCESS":
      return cred_result
    return await _call_github_mcp_jsonrpc(
        cred_result["token"],
        "create_issue",
        {"owner": owner, "repo": repo, "title": title, "body": body},
    )
  except Exception as exc:  # pylint: disable=broad-except
    print(
        f"[AUTH_MANAGER_3LO] Exception in create_github_remediation_issue: {exc}",
        file=sys.stderr,
        flush=True,
    )
    return {"status": "ERROR", "error": str(exc)}


def _build_root_agent() -> LlmAgent:
  return LlmAgent(
      model="gemini-2.5-flash",
      name="secops_github_orchestrator",
      instruction=SECOPS_ORCHESTRATOR_INSTRUCTION,
      tools=[
          get_active_cve_alerts,
          get_internal_deployment_manifest,
          record_remediation_ticket,
          get_github_authenticated_user,
          create_github_remediation_issue,
      ],
  )


class ProjectABoundAdkApp(AdkApp):

  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
    self._tmpl_attrs["project"] = PROJECT_A_ID
    self._tmpl_attrs["location"] = REGION

  def __getstate__(self):
    state = dict(self.__dict__)
    tmpl = dict(state.get("_tmpl_attrs", {}))
    tmpl["agent"] = None
    tmpl["runner"] = None
    tmpl["in_memory_runner"] = None
    tmpl["session_service"] = None
    tmpl["in_memory_session_service"] = None
    state["_tmpl_attrs"] = tmpl
    return state

  def __setstate__(self, state):
    self.__dict__.update(state)
    self._tmpl_attrs["agent"] = _build_root_agent()
    self._tmpl_attrs["session_service_builder"] = (
        AutoCreatingInMemorySessionService
    )

  def set_up(self):
    target_project = os.environ.get("AGENT_PROJECT_ID", PROJECT_A_ID)
    target_location = os.environ.get("REGION", REGION)
    self._tmpl_attrs["project"] = target_project
    self._tmpl_attrs["location"] = target_location
    self._tmpl_attrs["agent"] = _build_root_agent()
    self._tmpl_attrs["session_service_builder"] = (
        AutoCreatingInMemorySessionService
    )
    os.environ["GOOGLE_CLOUD_PROJECT"] = target_project
    os.environ["GOOGLE_CLOUD_LOCATION"] = target_location
    super().set_up()
    for r_key in ("runner", "in_memory_runner"):
      runner_obj = self._tmpl_attrs.get(r_key)
      if runner_obj is not None and hasattr(runner_obj, "auto_create_session"):
        runner_obj.auto_create_session = True

  async def async_stream_query(self, *args, **kwargs):
    try:
      async for chunk in super().async_stream_query(*args, **kwargs):
        yield chunk
    except Exception as exc:
      tb_one_line = traceback.format_exc().replace("\n", " | ")
      print(
          f"[AGENT_RUNTIME_ERROR] async_stream_query failed: {exc} || {tb_one_line}",
          file=sys.stderr,
          flush=True,
      )
      raise

  async def streaming_agent_run_with_events(self, *args, **kwargs):
    try:
      async for chunk in super().streaming_agent_run_with_events(*args, **kwargs):
        yield chunk
    except Exception as exc:
      tb_one_line = traceback.format_exc().replace("\n", " | ")
      print(
          f"[AGENT_RUNTIME_ERROR] streaming_agent_run_with_events failed: {exc} || {tb_one_line}",
          file=sys.stderr,
          flush=True,
      )
      raise


root_agent = _build_root_agent()

app = ProjectABoundAdkApp(
    agent=root_agent,
    session_service_builder=AutoCreatingInMemorySessionService,
)
