"""Agent 2 (Project B: your-project-b-id) — Internal SecOps SRE Auditor.

Identity:
- Runs on Vertex AI Agent Engine in Project B with identity_type=AGENT_IDENTITY (SPIFFE).
- Zero service account keys, zero static tokens.

Governed Toolsets via Project A's Central Agent Gateway (enterprise-egress-agw):
1. internal-cloudrun-mcp (Project B private Cloud Run MCP server):
   - Allowed by IAP v2 policy on `internal-cloudrun-mcp`.
   - Routed privately via Agent Gateway -> PSC Network Attachment ->
     Private Cloud DNS (*.run.app -> 240.0.0.10) -> PSC Endpoint (psc2gapis).
2. Blocked from External GitHub Copilot MCP & Unregistered Domains:
   - IAP v2 policy on `github-copilot-mcp` explicitly grants `roles/iap.egressor`
     ONLY to Agent 1, excluding Agent 2's SPIFFE ID.
   - Includes `test_egress_policy_check(target_url)` so you can showcase live
     IAP v2 `DENIED` (`403`) blocks on `https://api.githubcopilot.com/mcp/` and
     Agent Gateway `default_denied` (`403`) blocks on unregistered domains
     (e.g. `https://example.com`) directly from the Playground UI without
     changing any IAM bindings.
"""

import json
import os
import sys
import traceback
from typing import Any
from google.adk.agents import LlmAgent
from google.adk.sessions.in_memory_session_service import InMemorySessionService
import httpx
import vertexai
from vertexai.agent_engines import AdkApp

PROJECT_A_ID = os.environ.get("PROJECT_A_ID", "your-project-a-id")
PROJECT_B_ID = os.environ.get("PROJECT_B_ID", "your-project-b-id")
REGION = os.environ.get("REGION", "us-central1")
os.environ["GOOGLE_CLOUD_PROJECT"] = PROJECT_B_ID
os.environ["GOOGLE_CLOUD_LOCATION"] = REGION
vertexai.init(project=PROJECT_B_ID, location=REGION)
_raw_mcp_url = os.environ.get(
    "INTERNAL_MCP_URL",
    "https://internal-mcp-server.a.run.app/mcp",
).rstrip("/")
INTERNAL_MCP_URL = (
    _raw_mcp_url if _raw_mcp_url.endswith("/mcp") else f"{_raw_mcp_url}/mcp"
)

INTERNAL_AUDITOR_INSTRUCTION = """You are the Internal SecOps SRE Auditor (Agent 2) running in Project B.

Your role is internal compliance, vulnerability triage, and egress policy verification inside the isolated VPC perimeter:
1. Internal SecOps Auditing (ALLOWED by IAP v2):
   - Use `get_active_cve_alerts(service_name)` to inspect open CVEs across internal services ('payment-service', 'checkout-worker', or 'all').
   - Use `get_internal_deployment_manifest(service_name)` to inspect production image digests, owning on-call teams, and change-freeze windows.
2. Live Egress & IAP Policy Block Verification (`test_egress_policy_check`):
   - If the user asks to test or showcase an active **IAP v2 block** (`403` / `authzPolicyInfo.result: DENIED`) or **Agent Gateway default-deny block**, call `test_egress_policy_check(target_url)`:
     * Passing `target_url="https://api.githubcopilot.com/mcp/"` attempts a JSON-RPC MCP call to the External GitHub Copilot MCP server (which is registered in Agent Registry, but its IAP v2 policy only allows Agent 1, so IAP v2 blocks Agent 2 with HTTP 403 `DENIED`).
     * Passing `target_url="https://example.com"` attempts an outbound request to an unregistered external domain (which Agent Gateway blocks via its `default_denied` rule with HTTP 403).
"""


class AutoCreatingInMemorySessionService(InMemorySessionService):
  """In-memory session service that auto-creates missing sessions on get_session."""

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
  try:
    async with httpx.AsyncClient(timeout=30.0) as client:
      resp = await client.post(mcp_url, headers=headers, json=payload)
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
    return {
        "status": "ERROR",
        "mcp_endpoint": mcp_url,
        "tool_called": tool_name,
        "error": str(exc),
    }


async def get_active_cve_alerts(
    service_name: str = "all",
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


async def test_egress_policy_check(
    target_url: str = "https://api.githubcopilot.com/mcp/",
) -> dict[str, Any]:
  """Probes a target URL through the Central Agent Gateway to showcase IAP v2 or Agent Gateway egress blocking.

  Args:
    target_url: URL to probe (e.g., 'https://api.githubcopilot.com/mcp/' to
      showcase IAP v2 per-agent block, or 'https://example.com' to showcase
      Agent Gateway unregistered-domain default-deny block).
  """
  payload = {
      "jsonrpc": "2.0",
      "id": 1,
      "method": "tools/list",
      "params": {},
  }
  headers = {
      "Content-Type": "application/json",
      "Accept": "application/json, text/event-stream",
  }
  try:
    async with httpx.AsyncClient(timeout=15.0) as client:
      resp = await client.post(target_url, headers=headers, json=payload)
      return {
          "target_url": target_url,
          "http_status": resp.status_code,
          "blocked_by_gateway_or_iap": resp.status_code == 403,
          "response_preview": resp.text[:500],
      }
  except Exception as exc:  # pylint: disable=broad-except
    return {
        "target_url": target_url,
        "blocked_by_gateway_or_iap": True,
        "error": str(exc),
    }


def _build_root_agent() -> LlmAgent:
  return LlmAgent(
      model="gemini-2.5-flash",
      name="internal_secops_auditor",
      instruction=INTERNAL_AUDITOR_INSTRUCTION,
      tools=[
          get_active_cve_alerts,
          get_internal_deployment_manifest,
          test_egress_policy_check,
      ],
  )


class ProjectBBoundAdkApp(AdkApp):

  def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
    self._tmpl_attrs["project"] = PROJECT_B_ID
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
    target_project = os.environ.get("AGENT_PROJECT_ID", PROJECT_B_ID)
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

app = ProjectBBoundAdkApp(
    agent=root_agent,
    session_service_builder=AutoCreatingInMemorySessionService,
)
