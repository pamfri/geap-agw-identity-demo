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
import contextvars
import json
import logging
import os
import re
import sys
import traceback
from typing import Any
from urllib.parse import urlencode
from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions.base_session_service import BaseSessionService
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.adk.tools.tool_context import ToolContext
import google.auth
from google.auth.transport.requests import Request
from google.genai import types
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
AZURE_MCP_URL = os.environ.get(
    "AZURE_MCP_URL",
    "https://your-azure-mcp-app.azurewebsites.net/runtime/webhooks/mcp",
)
AUTH_PROVIDER_RESOURCE = (
    f"projects/{PROJECT_A_ID}/locations/{REGION}/authProviders/github-mcp-auth"
)
CONTINUE_URI = os.environ.get(
    "OAUTH_CONTINUE_URI", "http://localhost:8501/validateUserId"
)
GITHUB_OAUTH_SCOPES = ["repo"]

SECOPS_ORCHESTRATOR_INSTRUCTION = """You are the SecOps Incident-to-PR Orchestrator (Agent 1).

You have direct access to the following 7 tools (call them directly by their exact function name):
1. Internal SecOps MCP Tools (routed via Agent Gateway + IAP v2 + Private Cloud DNS 240.0.0.10 + PSC to Project B Cloud Run):
   - `get_active_cve_alerts(service_name)`: Inspect active CVEs detected in our production environments (e.g., 'payment-service', 'checkout-worker', or 'all').
   - `get_internal_deployment_manifest(service_name)`: Check production deployment metadata, SLA timers, and whether a change freeze is active.
   - `record_remediation_ticket(cve_id, service_name, github_pr_or_issue_url, remediated_by_github_user)`: Log a GitHub PR or Issue URL into the internal SecOps compliance ledger.

2. External GitHub Copilot MCP 3LO Tools (authenticated via Google Cloud Auth Manager `github-mcp-auth`):
   - `get_github_authenticated_user()`: Verify the authenticated engineer's GitHub profile via the governed GitHub Copilot MCP server (`https://api.githubcopilot.com/mcp/`).
   - `create_github_remediation_issue(owner, repo, title, body)`: Create a remediation issue on GitHub via the governed GitHub Copilot MCP server using the engineer's 3-Legged OAuth (3LO) token.

3. Cross-Cloud Azure Weather MCP & Observability Demo Tools (governed via Agent Gateway + IAP v2 + Distributed W3C Trace Propagation):
   - `GetWeather(location, city, country)` (and alias `get_azure_weather(location, city, country)`): Query current weather for a location/city from the remote MCP server hosted on Microsoft Azure (`azure-weather-mcp`), propagating W3C `traceparent` across clouds so both Vertex AI Agent Engine and Azure MCP spans appear in one unified Cloud Trace waterfall.
   - `get_weather_forecast(city)`: Fetch a multi-day weather forecast for a city (Demo 4 Error Tracing tool — demonstrates OpenTelemetry tool error recording and ancestor span error promotion in Cloud Trace).

RULES:
- If the user asks to check their GitHub user profile or call `get_github_authenticated_user`, ALWAYS call `get_github_authenticated_user()`.
- If `get_github_authenticated_user` or `create_github_remediation_issue` returns `status: "URI_CONSENT_REQUIRED"`, ALWAYS display the short `short_authorize_url` (e.g. `https://<callback-service-url>/authorize?uid=vais-query-reasoning-engine`) FIRST so the user can click it without any base64 `state` truncation/corruption, along with the `user_id` and `consent_nonce`.
- If the user asks for current weather or Azure MCP weather (or mentions `GetWeather` or `get_azure_weather`), ALWAYS call `GetWeather(location=...)`.
- If the user asks for a weather forecast (e.g. 5-day forecast), ALWAYS call `get_weather_forecast(city=...)`.
- Do NOT apply your own ad-hoc governance refusals before calling a tool: if the user asks you to call `record_remediation_ticket` or `create_github_remediation_issue` with specific arguments, invoke the requested tool directly so that the Agent Gateway Semantic Governance Policy (SGP) engine can evaluate and enforce enterprise business rules at runtime.
- If a previous assistant message in the conversation paused `create_github_remediation_issue` awaiting operator approval and the user replies `CONFIRM` (even if an earlier turn in the same session tested `BLOCK` first), immediately invoke `create_github_remediation_issue(owner=..., repo=..., title=..., body=...)` using the exact `owner`, `repo`, `title`, and `body` from the earlier request in the conversation. Conversely, if the user replies `BLOCK` or cancels, do NOT invoke `create_github_remediation_issue` and confirm that the action was cancelled.
"""


class HybridVertexSessionService(BaseSessionService):
  """Durable Vertex AI Agent Engine session service with seamless in-memory fallback.

  Persists real Playground / Vertex AI Agent Engine sessions (`sessions/<NUMERIC_ID>`)
  via `VertexAiSessionService` so multi-turn Conversational HITL (`CONFIRM` / `BLOCK`)
  and `Scan-Before-Remediate` state survive across all Reasoning Engine worker processes
  and container replicas, while falling back to `InMemorySessionService` for synthetic
  non-numeric smoke-test session IDs or local offline runs.
  """

  def __init__(self) -> None:
    """Initializes durable Vertex AI and fallback in-memory session stores."""
    self._memory = InMemorySessionService()
    self._local_session_ids: set[str] = set()
    self._vertex: Any = None

  @staticmethod
  def _extract_short_id(session_id: str | None) -> str:
    raw = (session_id or "").strip()
    if "/" in raw:
      return raw.rstrip("/").split("/")[-1]
    return raw

  def _get_vertex_service(self) -> Any:
    if self._vertex is not None:
      return self._vertex
    engine_id = self._extract_short_id(
        os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_ID")
        or os.environ.get("RE_ENGINE_ID")
        or ""
    )
    if not engine_id.isdigit():
      return None
    project = (
        os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("AGENT_PROJECT_ID")
        or PROJECT_A_ID
    )
    location = (
        os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_LOCATION")
        or os.environ.get("REGION")
        or REGION
    )
    try:
      from google.adk.sessions.vertex_ai_session_service import (  # pylint: disable=g-import-not-at-top
          VertexAiSessionService,
      )
    except (ImportError, AttributeError):
      try:
        from google.adk.sessions.vertex_ai_session_service_g3 import (  # pylint: disable=g-import-not-at-top
            VertexAiSessionService,
        )
      except (ImportError, AttributeError):
        return None
    self._vertex = VertexAiSessionService(
        project=project,
        location=location,
        agent_engine_id=engine_id,
    )
    return self._vertex

  async def create_session(
      self,
      *,
      app_name: str,
      user_id: str,
      state: dict[str, Any] | None = None,
      session_id: str | None = None,
      **kwargs: Any,
  ):
    short_id = self._extract_short_id(session_id)
    vertex = self._get_vertex_service()
    if not short_id and vertex is not None:
      try:
        return await vertex.create_session(
            app_name=app_name,
            user_id=user_id,
            state=state,
            **kwargs,
        )
      except Exception as exc:  # pylint: disable=broad-except
        print(
            f"[SGP_SESSION_WARN] VertexAiSessionService.create_session failed: {exc}",
            file=sys.stderr,
            flush=True,
        )
    sess = await self._memory.create_session(
        app_name=app_name,
        user_id=user_id,
        state=state,
        session_id=short_id or None,
    )
    self._local_session_ids.add(sess.id)
    return sess

  async def get_session(
      self,
      *,
      app_name: str,
      user_id: str,
      session_id: str,
      config: Any = None,
  ):
    short_id = self._extract_short_id(session_id)
    if short_id in self._local_session_ids:
      return await self._memory.get_session(
          app_name=app_name,
          user_id=user_id,
          session_id=short_id,
          config=config,
      )
    vertex = self._get_vertex_service()
    if short_id.isdigit() and vertex is not None:
      try:
        sess = await vertex.get_session(
            app_name=app_name,
            user_id=user_id,
            session_id=short_id,
            config=config,
        )
        if sess is not None:
          return sess
      except Exception as exc:  # pylint: disable=broad-except
        print(
            f"[SGP_SESSION_WARN] VertexAiSessionService.get_session({short_id}) failed: {exc}",
            file=sys.stderr,
            flush=True,
        )
    mem_sess = await self._memory.get_session(
        app_name=app_name,
        user_id=user_id,
        session_id=short_id,
        config=config,
    )
    if mem_sess is not None:
      self._local_session_ids.add(short_id)
      return mem_sess
    return None

  async def append_event(self, session: Any, event: Any):
    short_id = self._extract_short_id(getattr(session, "id", ""))
    vertex = self._get_vertex_service()
    if (
        short_id
        and short_id not in self._local_session_ids
        and short_id.isdigit()
        and vertex is not None
    ):
      try:
        return await vertex.append_event(session=session, event=event)
      except Exception as exc:  # pylint: disable=broad-except
        print(
            f"[SGP_SESSION_WARN] VertexAiSessionService.append_event({short_id}) failed: {exc}",
            file=sys.stderr,
            flush=True,
        )
    return await self._memory.append_event(session=session, event=event)

  async def list_sessions(
      self, *, app_name: str, user_id: str | None = None
  ):
    vertex = self._get_vertex_service()
    if vertex is not None:
      try:
        return await vertex.list_sessions(
            app_name=app_name,
            user_id=user_id or "vais-query-reasoning-engine",
        )
      except Exception:  # pylint: disable=broad-except
        pass
    return await self._memory.list_sessions(app_name=app_name, user_id=user_id)

  async def delete_session(
      self, *, app_name: str, user_id: str, session_id: str
  ) -> None:
    short_id = self._extract_short_id(session_id)
    if short_id in self._local_session_ids or not short_id.isdigit():
      self._local_session_ids.discard(short_id)
      await self._memory.delete_session(
          app_name=app_name, user_id=user_id, session_id=short_id
      )
      return
    vertex = self._get_vertex_service()
    if vertex is not None:
      try:
        await vertex.delete_session(
            app_name=app_name, user_id=user_id, session_id=short_id
        )
      except Exception:  # pylint: disable=broad-except
        pass


AutoCreatingInMemorySessionService = HybridVertexSessionService


def _set_mcp_destination_span_attrs(mcp_service_id: str, tool_name: str, mcp_url: str):
  """Sets OneGraph / Cloud Trace topology destination attributes on the active span."""
  project_a_num = (
      os.environ.get("PROJECT_A_NUM")
      or os.environ.get("AGENT_PROJECT_NUM")
      or "YOUR_PROJECT_A_NUM"
  )
  region = os.environ.get("REGION", REGION)
  dest_uri = (
      f"//agentregistry.googleapis.com/projects/{project_a_num}"
      f"/locations/{region}/services/{mcp_service_id}"
  )
  attrs = {
      "gcp.resource.destination.id": dest_uri,
      "gcp.mcp.server.destination.id": dest_uri,
      "rpc.system": "jsonrpc",
      "rpc.service": mcp_service_id,
      "rpc.method": tool_name,
      "url.full": mcp_url,
  }
  try:
    from opentelemetry import trace
    cur_span = trace.get_current_span()
    if cur_span and cur_span.is_recording():
      for k, v in attrs.items():
        cur_span.set_attribute(k, v)
  except Exception:
    pass
  return attrs


async def _call_internal_mcp_jsonrpc(
    tool_name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
  """Calls the Internal SecOps Cloud Run MCP Server over Streamable HTTP JSON-RPC 2.0."""
  mcp_url = os.environ.get("INTERNAL_MCP_URL", INTERNAL_MCP_URL).rstrip("/")
  if not mcp_url.endswith("/mcp"):
    mcp_url = f"{mcp_url}/mcp"
  span_attrs = _set_mcp_destination_span_attrs(
      "internal-cloudrun-mcp", tool_name, mcp_url
  )
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
    from contextlib import nullcontext
    try:
      from opentelemetry import trace
      from opentelemetry.propagate import inject
      tracer = trace.get_tracer(__name__)
      span_cm = tracer.start_as_current_span(
          f"mcp.tools.call/{tool_name}",
          kind=trace.SpanKind.CLIENT,
          attributes=span_attrs,
      )
    except Exception:
      span_cm = nullcontext()
      inject = None
    with span_cm:
      if inject is not None:
        try:
          inject(headers)
        except Exception:
          pass
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


async def get_service_deployment_manifest(
    service_name: str = "payment-service",
) -> dict[str, Any]:
  """Alias for `get_internal_deployment_manifest`: fetch internal production deployment metadata for a service.

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
  headers = await asyncio.to_thread(_get_spiffe_adc_headers)
  async with httpx.AsyncClient(timeout=30.0) as client:
    resp = await client.post(retrieve_url, headers=headers, json=req_body)

  if resp.status_code != 200:
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
    return {
        "status": "SUCCESS",
        "user_id": user_id,
        "token": token,
    }

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
  span_attrs = _set_mcp_destination_span_attrs(
      "github-copilot-mcp", tool_name, GITHUB_MCP_URL
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
  from contextlib import nullcontext
  try:
    from opentelemetry import trace
    from opentelemetry.propagate import inject
    tracer = trace.get_tracer(__name__)
    span_cm = tracer.start_as_current_span(
        f"mcp.tools.call/{tool_name}",
        kind=trace.SpanKind.CLIENT,
        attributes=span_attrs,
    )
  except Exception:
    span_cm = nullcontext()
    inject = None
  with span_cm:
    if inject is not None:
      try:
        inject(headers)
      except Exception:
        pass
    async with httpx.AsyncClient(timeout=30.0) as client:
      resp = await client.post(GITHUB_MCP_URL, headers=headers, json=payload)
      parsed = _parse_mcp_http_response(resp)
      return {
          "status_code": resp.status_code,
          "mcp_endpoint": GITHUB_MCP_URL,
          "tool_called": tool_name,
          "result": parsed,
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
    # GitHub's official remote MCP server (https://api.githubcopilot.com/mcp/)
    # consolidated create_issue/update_issue into `issue_write` with method="create".
    res = await _call_github_mcp_jsonrpc(
        cred_result["token"],
        "issue_write",
        {
            "method": "create",
            "owner": owner,
            "repo": repo,
            "title": title,
            "body": body,
        },
    )
    resp_text = str(res.get("response", "")).lower()
    if "unknown tool" in resp_text or "method not found" in resp_text:
      res = await _call_github_mcp_jsonrpc(
          cred_result["token"],
          "create_issue",
          {"owner": owner, "repo": repo, "title": title, "body": body},
      )
    return res
  except Exception as exc:  # pylint: disable=broad-except
    return {"status": "ERROR", "error": str(exc)}


def _parse_mcp_http_response(resp: httpx.Response) -> dict[str, Any]:
  """Parses either standard application/json or SSE (text/event-stream) MCP responses."""
  raw_text = resp.text.strip()
  try:
    data = resp.json()
  except Exception:
    data = None
    for line in raw_text.splitlines():
      line = line.strip()
      if line.startswith("data:"):
        candidate = line[len("data:") :].strip()
        if candidate:
          try:
            data = json.loads(candidate)
            break
          except Exception:
            continue
  if isinstance(data, dict):
    is_err = bool(data.get("result", {}).get("isError")) or ("error" in data)
    content_items = data.get("result", {}).get("content", [])
    if content_items and isinstance(content_items, list):
      text_val = content_items[0].get("text")
      if text_val:
        try:
          parsed_json = json.loads(text_val)
          if isinstance(parsed_json, dict) and is_err:
            parsed_json["_mcp_is_error"] = True
          return parsed_json
        except Exception:
          out = {"status_code": resp.status_code, "result": text_val}
          if is_err:
            out["_mcp_is_error"] = True
          return out
    return data
  return {
      "status_code": resp.status_code,
      "response": raw_text[:2000],
  }


def _looks_like_mcp_arg_or_tool_error(resp_code: int, parsed: Any) -> bool:
  if resp_code in (400, 404, 422):
    return True
  if not isinstance(parsed, dict):
    return False
  if "error" in parsed or parsed.get("_mcp_is_error"):
    return True
  txt = str(parsed.get("result", "") or parsed.get("response", "")).lower()
  return any(
      kw in txt
      for kw in (
          "missing",
          "required",
          "parameter",
          "location",
          "invalid params",
          "validation error",
      )
  )


async def _call_azure_mcp_jsonrpc(
    tool_name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
  """Calls the Cross-Cloud Azure Weather MCP Server through Agent Gateway with W3C traceparent."""
  azure_url = os.environ.get("AZURE_MCP_URL", AZURE_MCP_URL)
  span_attrs = _set_mcp_destination_span_attrs(
      "azure-weather-mcp", tool_name, azure_url
  )
  headers = {
      "Content-Type": "application/json",
      "Accept": "application/json, text/event-stream",
  }
  from contextlib import nullcontext
  try:
    from opentelemetry import trace
    from opentelemetry.propagate import inject
    tracer = trace.get_tracer(__name__)
    span_cm = tracer.start_as_current_span(
        f"mcp.tools.call/{tool_name}",
        kind=trace.SpanKind.CLIENT,
        attributes=span_attrs,
    )
  except Exception:
    span_cm = nullcontext()
    inject = None

  with span_cm:
    if inject is not None:
      try:
        inject(headers)
      except Exception:
        pass
    meta_ctx = {}
    if "traceparent" in headers:
      meta_ctx["traceparent"] = headers["traceparent"]
    if "tracestate" in headers:
      meta_ctx["tracestate"] = headers["tracestate"]

    target_loc = str(
        arguments.get("location")
        or arguments.get("city")
        or "Zurich"
    ).strip()
    country_val = str(arguments.get("country") or "").strip()
    loc_with_country = f"{target_loc}, {country_val}" if country_val else target_loc

    # Candidate argument shapes: start with pure {"location": target_loc} as expected by Azure MCP GetWeather
    arg_candidates = [
        {"location": target_loc},
        {"location": loc_with_country},
        arguments,
        {"Location": target_loc},
        {"city": target_loc},
    ]

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": {"location": target_loc},
            "_meta": meta_ctx,
        },
    }
    try:
      async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(azure_url, headers=headers, json=payload)
        parsed = _parse_mcp_http_response(resp)
        if not _looks_like_mcp_arg_or_tool_error(resp.status_code, parsed):
          return parsed

        # Try alternate argument shapes first (e.g. pure {"location": target_loc})
        for cand_args in arg_candidates[1:]:
          payload["params"]["arguments"] = cand_args
          retry_resp = await client.post(azure_url, headers=headers, json=payload)
          retry_parsed = _parse_mcp_http_response(retry_resp)
          if retry_resp.status_code == 200 and not _looks_like_mcp_arg_or_tool_error(
              retry_resp.status_code, retry_parsed
          ):
            return retry_parsed

        # If the server requires an MCP session handshake or alternate tool name, initialize & retry
        init_payload = {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {
                    "name": "secops-github-orchestrator",
                    "version": "1.0.0",
                },
            },
        }
        init_resp = await client.post(
            azure_url, headers=headers, json=init_payload
        )
        session_id = (
            init_resp.headers.get("Mcp-Session-Id")
            or init_resp.headers.get("mcp-session-id")
        )
        call_headers = dict(headers)
        if session_id:
          call_headers["Mcp-Session-Id"] = session_id
        for candidate_tool in (tool_name, "get_weather", "GetWeather"):
          for cand_args in arg_candidates:
            payload["params"]["name"] = candidate_tool
            payload["params"]["arguments"] = cand_args
            retry_resp = await client.post(
                azure_url, headers=call_headers, json=payload
            )
            retry_parsed = _parse_mcp_http_response(retry_resp)
            if retry_resp.status_code == 200 and not _looks_like_mcp_arg_or_tool_error(
                retry_resp.status_code, retry_parsed
            ):
              return retry_parsed
        return parsed
    except Exception as exc:  # pylint: disable=broad-except
      return {
          "status": "ERROR",
          "mcp_endpoint": azure_url,
          "tool_called": tool_name,
          "error": str(exc),
      }


async def GetWeather(  # pylint: disable=invalid-name
    location: str = "Zurich",
    city: str = "",
    country: str = "",
) -> dict[str, Any]:
  """Query current weather for a location/city via the Cross-Cloud Azure Weather MCP Server (`GetWeather`).

  Routes through Agent Gateway (`enterprise-egress-agw`) + IAP v2 (`azure-weather-mcp`)
  and propagates W3C `traceparent` headers and MCP `_meta` so Vertex AI Agent Engine
  and Azure MCP spans appear in one unified Cloud Trace waterfall (Demo 3).

  Args:
    location: Location or city name to query (e.g., 'Zurich', 'Vienna', 'Seattle', 'London').
    city: Optional city name alias (if provided, used as the target location).
    country: Optional country code or name (e.g., 'CH', 'Switzerland').
  """
  target_loc = (location or city or "Zurich").strip()
  args: dict[str, Any] = {"location": target_loc}
  if country:
    args["country"] = country
  return await _call_azure_mcp_jsonrpc("GetWeather", args)


async def get_azure_weather(
    location: str = "Zurich",
    city: str = "",
    country: str = "",
) -> dict[str, Any]:
  """Alias for `GetWeather` on the Cross-Cloud Azure Weather MCP Server (`GetWeather`).

  Args:
    location: Location or city name to query (e.g., 'Zurich', 'Vienna', 'Seattle', 'London').
    city: Optional city name alias (if provided, used as the target location).
    country: Optional country code or name (e.g., 'CH', 'Switzerland').
  """
  return await GetWeather(location=location, city=city, country=country)


async def get_weather_forecast(city: str = "Zurich") -> dict[str, Any]:
  """Fetch a 5-day weather forecast for a city (Demo 4 Error Tracing demonstration).

  Intentionally raises a RuntimeError to demonstrate OpenTelemetry exception recording
  on the `execute_tool` span and error status promotion up to parent `invoke_agent` /
  `invocation` spans in Cloud Trace.

  Args:
    city: City name for the forecast request.
  """
  raise RuntimeError(
      f"Weather forecast service failed for city='{city}': upstream forecast"
      " provider returned HTTP 503 Service Unavailable (Demo 4 Error Tracing)."
  )


def _emit_model_armor_custom_span(
    span_name: str,
    kind: str,
    decision: str,
    match_state: str,
) -> None:
  """Emits a custom OpenTelemetry child span for Model Armor screening (Demo 2)."""
  try:
    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode

    tracer = trace.get_tracer(__name__)
    template_id = (
        f"projects/{os.environ.get('PROJECT_A_ID', PROJECT_A_ID)}"
        f"/locations/{os.environ.get('REGION', REGION)}"
        "/templates/secops-agent-guardrail"
    )
    with tracer.start_as_current_span(span_name) as span:
      span.set_attribute("model_armor.template_id", template_id)
      span.set_attribute("model_armor.kind", kind)
      span.set_attribute("model_armor.decision", decision)
      span.set_attribute("model_armor.match_state", match_state)
      if decision == "BLOCK":
        span.set_status(
            Status(StatusCode.ERROR, f"Blocked by Model Armor ({match_state})")
        )
  except Exception:
    pass


async def record_tool_error_on_span(
    tool: Any,
    args: dict[str, Any],
    tool_context: ToolContext,
    error: Exception,
) -> dict[str, Any] | None:
  """Records tool exceptions + StatusCode.ERROR on the active `execute_tool` span (Demo 4)."""
  del tool, args, tool_context
  try:
    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode

    span = trace.get_current_span()
    if span and span.is_recording():
      span.record_exception(error)
      span.set_status(Status(StatusCode.ERROR, str(error)))
  except Exception:
    pass
  return {
      "status": "ERROR",
      "error_type": type(error).__name__,
      "error": str(error),
  }


def _register_promote_error_processor() -> None:
  """Registers a SpanProcessor that promotes child tool ERROR status to ancestor ADK spans (Demo 4)."""
  if os.environ.get("PROMOTE_TOOL_ERROR_TO_ANCESTORS", "true").lower() not in (
      "1",
      "true",
      "yes",
  ):
    return
  try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor
    from opentelemetry.trace import Status, StatusCode

    class PromoteErrorToAncestorsProcessor(SpanProcessor):
      """Propagates StatusCode.ERROR from child spans (e.g. execute_tool) to parent spans."""

      def __init__(self) -> None:
        self._errored_spans: dict[int, str] = {}
        self._parent_map: dict[int, int] = {}

      def on_start(self, span: Span, parent_context: Any = None) -> None:
        del parent_context
        parent = getattr(span, "parent", None)
        if parent and getattr(parent, "span_id", None):
          self._parent_map[span.context.span_id] = parent.span_id

      def on_end(self, span: ReadableSpan) -> None:
        span_id = span.context.span_id
        if span.status and span.status.status_code == StatusCode.ERROR:
          desc = span.status.description or "Child span failed"
          self._errored_spans[span_id] = desc
          parent_id = self._parent_map.get(span_id)
          while parent_id:
            self._errored_spans[parent_id] = desc
            parent_id = self._parent_map.get(parent_id)
        elif span_id in self._errored_spans:
          try:
            span._status = Status(  # pylint: disable=protected-access
                StatusCode.ERROR, self._errored_spans.pop(span_id)
            )
          except Exception:
            pass
        self._parent_map.pop(span_id, None)

      def shutdown(self) -> None:
        self._errored_spans.clear()
        self._parent_map.clear()

      def force_flush(self, timeout_millis: int = 30000) -> bool:
        del timeout_millis
        return True

    provider = trace.get_tracer_provider()
    if hasattr(provider, "add_span_processor") and not getattr(
        provider, "_agw_error_promoter_registered", False
    ):
      provider.add_span_processor(PromoteErrorToAncestorsProcessor())
      setattr(provider, "_agw_error_promoter_registered", True)
  except Exception:
    pass


_ACTIVE_TURN_SGP_CONTEXT_VAR: contextvars.ContextVar[dict[str, Any]] = (
    contextvars.ContextVar(
        "active_turn_sgp_context",
        default={
            "has_cve_scan_in_session": False,
            "has_prior_hitl_prompt": False,
            "user_replied_confirm": False,
            "user_replied_block": False,
        },
    )
)


def _extract_latest_user_text(llm_request: LlmRequest) -> str:
  if not llm_request or not getattr(llm_request, "contents", None):
    return ""
  for content in reversed(llm_request.contents):
    if getattr(content, "role", None) == "user" and getattr(
        content, "parts", None
    ):
      texts = [
          p.text
          for p in content.parts
          if getattr(p, "text", None) and isinstance(p.text, str)
      ]
      if texts:
        return "\n".join(texts).strip()
  return ""


def _inspect_session_sgp_context(
    llm_request: LlmRequest, latest_user_text: str
) -> dict[str, Any]:
  """Inspects multi-turn conversation history for SGP & Conversational HITL state."""
  has_cve_scan = False
  has_hitl_prompt = False
  prior_confirmed = False
  awaiting_3lo_retry = False
  prior_issue_request_text = ""
  contents = list(getattr(llm_request, "contents", None) or [])

  last_parts = getattr(contents[-1], "parts", None) or [] if contents else []
  is_tool_followup = any(
      getattr(p, "function_response", None) is not None for p in last_parts
  )

  history_slice = contents if is_tool_followup else contents[:-1]
  for content in history_slice:
    role = getattr(content, "role", "") or ""
    parts = getattr(content, "parts", None) or []
    user_texts_in_turn: list[str] = []
    for p in parts:
      fc = getattr(p, "function_call", None)
      if fc and getattr(fc, "name", None) == "get_active_cve_alerts":
        has_cve_scan = True
      fr = getattr(p, "function_response", None)
      if fr:
        fr_name = getattr(fr, "name", None) or ""
        if fr_name == "get_active_cve_alerts":
          has_cve_scan = True
        elif fr_name in (
            "create_github_remediation_issue",
            "issue_write",
            "create_issue",
        ):
          fr_resp_str = str(getattr(fr, "response", "") or "")
          if "URI_CONSENT_REQUIRED" in fr_resp_str:
            awaiting_3lo_retry = True
          else:
            # Issue creation completed; require fresh HITL for any subsequent new issue.
            has_hitl_prompt = False
            prior_confirmed = False
            awaiting_3lo_retry = False
      txt = getattr(p, "text", None)
      if isinstance(txt, str) and txt.strip():
        if role == "user":
          user_texts_in_turn.append(txt.strip())
        elif role == "model":
          if (
              "Human-in-the-Loop Confirmation Required" in txt
              or "⏸️" in txt
              or "reply CONFIRM to approve" in txt
              or (
                  "secops-hitl-github-issue-confirm" in txt
                  and "Action Cancelled" not in txt
              )
          ):
            has_hitl_prompt = True

    if user_texts_in_turn:
      joined_u = "\n".join(user_texts_in_turn).strip()
      u_low = joined_u.lower()
      if has_hitl_prompt and re.search(r"\bconfirm\b", u_low):
        prior_confirmed = True
      if (
          len(joined_u) >= 20
          and not joined_u.upper().startswith("CONFIRM")
          and any(
              kw in u_low
              for kw in ("github", "issue", "remediation", "cve", "open")
          )
      ):
        prior_issue_request_text = joined_u

  if is_tool_followup:
    return {
        "has_cve_scan_in_session": has_cve_scan,
        "has_prior_hitl_prompt": has_hitl_prompt,
        "user_replied_confirm": prior_confirmed,
        "user_replied_block": False,
        "prior_issue_request_text": prior_issue_request_text,
        "is_tool_followup": True,
    }

  stripped = (latest_user_text or "").strip()
  low = stripped.lower()
  replied_block = (
      has_hitl_prompt
      and len(stripped) < 80
      and bool(re.search(r"\b(block|cancel|deny|reject|abort)\b", low))
      and not bool(re.search(r"\bconfirm\b", low))
  )
  replied_confirm = (
      has_hitl_prompt
      and not replied_block
      and (
          bool(re.search(r"\bconfirm\b", low))
          or (prior_confirmed and awaiting_3lo_retry and len(stripped) < 120)
      )
  )
  return {
      "has_cve_scan_in_session": has_cve_scan,
      "has_prior_hitl_prompt": has_hitl_prompt,
      "user_replied_confirm": replied_confirm,
      "user_replied_block": replied_block,
      "prior_issue_request_text": prior_issue_request_text,
      "is_tool_followup": False,
  }


async def _before_model_guardrail_check(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> LlmResponse | None:
  """Ensures span content capture is active, emits Demo 2 Model Armor span, and enforces CONTENT_AUTHZ."""
  del callback_context
  os.environ["ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"] = "true"
  user_text = _extract_latest_user_text(llm_request)
  sgp_ctx = _inspect_session_sgp_context(llm_request, user_text)
  _ACTIVE_TURN_SGP_CONTEXT_VAR.set(sgp_ctx)
  if not user_text:
    return None

  if sgp_ctx["user_replied_block"]:
    return LlmResponse(
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    text=(
                        "🛑 [Human-in-the-Loop Action Cancelled — Semantic"
                        " Governance Policy (`secops-hitl-github-issue-confirm`)]:"
                        " Operator replied `BLOCK`. The pending"
                        " `create_github_remediation_issue` call was cancelled"
                        " and zero external GitHub API requests were made. (You"
                        " may still reply `CONFIRM` in this session if you"
                        " later wish to approve and execute it.)"
                    )
                )
            ],
        )
    )

  if sgp_ctx["user_replied_confirm"] and not sgp_ctx.get("is_tool_followup"):
    # Enrich 'CONFIRM' user prompt with the prior Turn 1 request from durable session
    # history so both Gemini and Agent Gateway SGP (NLC + sgpUserIntentMatching) see
    # explicit 'CONFIRM' plus the exact owner/repo/title/body intent.
    prior_req = sgp_ctx.get("prior_issue_request_text", "").strip()
    enriched_confirm = (
        "CONFIRM — I explicitly confirm and approve proceeding now with"
        f" create_github_remediation_issue for my earlier request: {prior_req}"
        if prior_req
        else (
            "CONFIRM — I explicitly confirm and approve calling"
            " create_github_remediation_issue using the exact owner, repo,"
            " title, and body from my prior request in the conversation"
            " history."
        )
    )
    try:
      if llm_request.contents:
        last_content = llm_request.contents[-1]
        if getattr(last_content, "role", None) == "user" and getattr(
            last_content, "parts", None
        ):
          for p in last_content.parts:
            if (
                getattr(p, "text", None)
                and isinstance(p.text, str)
                and not p.text.lstrip().startswith("CONFIRM —")
            ):
              p.text = enriched_confirm
              break
    except Exception:  # pylint: disable=broad-except
      pass

  lowered = user_text.lower()
  if any(
      kw in lowered
      for kw in (
          "ignore all previous instructions",
          "ignore previous instructions",
          "system rules",
          "exfiltrate",
          "malicious-c2",
          "/steal",
      )
  ):
    _emit_model_armor_custom_span(
        "model_armor.sanitize_user_prompt",
        kind="user_prompt",
        decision="BLOCK",
        match_state="MATCH_FOUND",
    )
    gw_res = await _call_internal_mcp_jsonrpc(
        "get_active_cve_alerts", {"service_name": user_text}
    )
    if gw_res.get("status_code") == 403 or "Error:" in str(
        gw_res.get("response", "")
    ):
      return LlmResponse(
          content=types.Content(
              role="model",
              parts=[
                  types.Part(
                      text=(
                          "Request blocked by Agent Gateway (`enterprise-egress-agw`)"
                          " & Model Armor (`enterprise-egress-agw-ma-ext` /"
                          f" `secops-agent-guardrail`): HTTP {gw_res.get('status_code', 403)}"
                          f" — `{gw_res.get('response', 'Error: Security Policy Violation')}`"
                      )
                  )
              ],
          )
      )
  _emit_model_armor_custom_span(
      "model_armor.sanitize_user_prompt",
      kind="user_prompt",
      decision="ALLOW",
      match_state="NO_MATCH_FOUND",
  )
  return None


async def _after_model_ensure_visible_content(
    callback_context: CallbackContext, llm_response: LlmResponse
) -> LlmResponse | None:
  """Emits Demo 2 Model Armor response span, enforces SGP/HITL defense-in-depth, and renders blocks."""
  del callback_context
  active_sgp_ctx = _ACTIVE_TURN_SGP_CONTEXT_VAR.get()
  if llm_response.content and getattr(llm_response.content, "parts", None):
    for p in llm_response.content.parts:
      fc = getattr(p, "function_call", None)
      if not fc:
        continue
      fn_name = getattr(fc, "name", "") or ""
      fn_args = getattr(fc, "args", None) or {}
      if not isinstance(fn_args, dict):
        try:
          fn_args = dict(fn_args)
        except Exception:
          fn_args = {}

      # 1. SGP Policy: secops-scan-before-remediate (on record_remediation_ticket)
      if fn_name == "record_remediation_ticket":
        ticket_url = str(fn_args.get("github_pr_or_issue_url", "")).strip()
        if (
            not active_sgp_ctx.get("has_cve_scan_in_session")
            or not ticket_url.startswith("https://github.com/")
        ):
          return LlmResponse(
              content=types.Content(
                  role="model",
                  parts=[
                      types.Part(
                          text=(
                              "I am sorry, but I cannot proceed with that request."
                              " Reason: Blocked by Semantic Governance Policy"
                              " (secops-scan-before-remediate): Remediation tickets"
                              " can only be recorded in the compliance ledger after"
                              " scanning active CVEs with get_active_cve_alerts in"
                              " the current session AND supplying a valid"
                              " https://github.com/ PR or Issue URL."
                          )
                      )
                  ],
              )
          )

      # 2 & 3. SGP Policies on create_github_remediation_issue / issue_write / create_issue
      if fn_name in (
          "create_github_remediation_issue",
          "issue_write",
          "create_issue",
      ):
        owner = str(fn_args.get("owner", "")).strip()
        allowed_orgs_env = os.environ.get("ALLOWED_GITHUB_ORGS", "").strip()
        allowed_orgs = {
            o.strip().lower() for o in allowed_orgs_env.split(",") if o.strip()
        }
        # 2. SGP Policy: secops-approved-github-org
        if owner.lower() in (
            "unapproved-org",
            "public-leak",
            "evil-corp",
        ) or (allowed_orgs and owner and owner.lower() not in allowed_orgs):
          return LlmResponse(
              content=types.Content(
                  role="model",
                  parts=[
                      types.Part(
                          text=(
                              "I am sorry, but I cannot proceed with that request."
                              " Reason: Blocked by Semantic Governance Policy"
                              " (secops-approved-github-org): SecOps CVE"
                              " remediation issues cannot be created in unapproved"
                              " external GitHub organizations."
                          )
                      )
                  ],
              )
          )

        # 3. SGP Policy: secops-hitl-github-issue-confirm (Two-Turn Conversational HITL)
        if not active_sgp_ctx.get("user_replied_confirm"):
          return LlmResponse(
              content=types.Content(
                  role="model",
                  parts=[
                      types.Part(
                          text=(
                              "I am sorry, but I cannot proceed with that request."
                              " Reason: ⏸️ [Human-in-the-Loop Confirmation Required"
                              " — Semantic Governance Policy"
                              " (secops-hitl-github-issue-confirm)]: Agent Gateway"
                              " intercepted create_github_remediation_issue before"
                              f" execution (target: {owner}/{fn_args.get('repo', '')}"
                              f" — \"{fn_args.get('title', '')}\"). Please review"
                              " the target GitHub owner/repo and issue title from"
                              " your request, then reply CONFIRM to approve"
                              " execution, or reply BLOCK to cancel."
                          )
                      )
                  ],
              )
          )

    _emit_model_armor_custom_span(
        "model_armor.sanitize_model_response",
        kind="model_response",
        decision="ALLOW",
        match_state="NO_MATCH_FOUND",
    )
    return None
  if llm_response.error_code or (
      llm_response.finish_reason
      and llm_response.finish_reason != types.FinishReason.STOP
  ):
    _emit_model_armor_custom_span(
        "model_armor.sanitize_model_response",
        kind="model_response",
        decision="BLOCK",
        match_state="MATCH_FOUND",
    )
    reason = llm_response.error_code or llm_response.finish_reason
    msg = (
        llm_response.error_message
        or "Blocked by Model Armor / Vertex AI Security Policy."
    )
    return LlmResponse(
        content=types.Content(
            role="model",
            parts=[
                types.Part(
                    text=(
                        "Request blocked by Model Armor / Vertex AI Security"
                        f" Policy (`{reason}`): {msg}"
                    )
                )
            ],
        )
    )
  return None


def _format_runtime_error_banner(err: Exception | str) -> str:
  """Formats runtime exceptions clearly, distinguishing Apigee Token Quota, SGP, Model Armor, and runtime errors."""
  err_str = str(err)
  lower = err_str.lower()
  if any(
      kw in lower
      for kw in (
          "llmtokenquota",
          "token quota",
          "rate limit llm",
          "429",
          "too many requests",
      )
  ):
    return (
        "🚦 [Apigee AI Gateway — Per-Agent LLM Token Quota Exceeded (HTTP 429)]: "
        f"{err_str}"
    )
  if any(
      kw in lower
      for kw in (
          "semantic governance",
          "secops-scan-before-remediate",
          "secops-approved-github-org",
          "secops-hitl-github-issue-confirm",
          "human-in-the-loop",
          "business policy",
          "naturallanguageconstraint",
          "user intent",
      )
  ):
    return (
        "🛡️ [Semantic Governance Policy (SGP) / Agent Gateway]: "
        f"{err_str}"
    )
  if any(
      kw in lower
      for kw in (
          "model armor",
          "guardrail",
          "prompt injection",
          "jailbreak",
          "403",
          "permission_denied",
          "forbidden",
          "blocked",
          "safety",
          "security policy",
      )
  ):
    return (
        "🛡️ [Google Cloud Model Armor / Agent Gateway Guardrail]: "
        f"This request was intercepted and blocked by security policy: {err_str}"
    )
  return f"⚠️ [Agent Runtime Error]: {err_str}"


async def _on_model_error_ensure_visible_content(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
    error: Exception,
) -> LlmResponse | None:
  """Converts model/guardrail/SGP API errors into a visible Playground response."""
  del callback_context, llm_request
  return LlmResponse(
      content=types.Content(
          role="model",
          parts=[
              types.Part(
                  text=_format_runtime_error_banner(error)
              )
          ],
      )
  )


def _build_root_agent() -> LlmAgent:
  return LlmAgent(
      model="gemini-2.5-flash",
      name="secops_github_orchestrator",
      instruction=SECOPS_ORCHESTRATOR_INSTRUCTION,
      before_model_callback=_before_model_guardrail_check,
      after_model_callback=_after_model_ensure_visible_content,
      on_model_error_callback=_on_model_error_ensure_visible_content,
      on_tool_error_callback=record_tool_error_on_span,
      tools=[
          get_active_cve_alerts,
          get_internal_deployment_manifest,
          get_service_deployment_manifest,
          record_remediation_ticket,
          get_github_authenticated_user,
          create_github_remediation_issue,
          GetWeather,
          get_azure_weather,
          get_weather_forecast,
      ],
  )


class ProjectABoundAdkApp(AdkApp):

  def __init__(self, *args, **kwargs):
    kwargs.setdefault("enable_tracing", True)
    super().__init__(*args, **kwargs)
    self._tmpl_attrs["project"] = PROJECT_A_ID
    self._tmpl_attrs["location"] = REGION
    self._tmpl_attrs["enable_tracing"] = True

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
    self._tmpl_attrs["enable_tracing"] = True
    self._tmpl_attrs["session_service_builder"] = HybridVertexSessionService

  def set_up(self):
    target_project = os.environ.get("AGENT_PROJECT_ID", PROJECT_A_ID)
    target_project_num = os.environ.get("AGENT_PROJECT_NUM", "")
    target_location = os.environ.get("REGION", REGION)
    engine_id = os.environ.get(
        "GOOGLE_CLOUD_AGENT_ENGINE_ID", "secops-github-orchestrator"
    )
    if target_project_num and engine_id.isdigit():
      agent_urn = (
          f"urn:agent:projects-{target_project_num}:projects:{target_project_num}"
          f":locations:{target_location}:reasoningEngines:{engine_id}"
      )
      cloud_resource_id = (
          f"//aiplatform.googleapis.com/projects/{target_project_num}"
          f"/locations/{target_location}/reasoningEngines/{engine_id}"
      )
    else:
      agent_urn = f"urn:agent:{target_project}:secops-github-orchestrator"
      fallback_re_id = os.environ.get("RE_ENGINE_ID", "secops-github-orchestrator")
      cloud_resource_id = (
          f"//aiplatform.googleapis.com/projects/{target_project_num or 'YOUR_PROJECT_A_NUM'}"
          f"/locations/{target_location}/reasoningEngines/{fallback_re_id}"
      )
    os.environ["OTEL_RESOURCE_ATTRIBUTES"] = (
        f"cloud.provider=gcp,cloud.region={target_location},"
        f"cloud.resource.id={cloud_resource_id},"
        f"gcp.project_id={target_project},gcp.agent.id={agent_urn},"
        "service.name=secops-github-orchestrator"
    )
    os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = (
        "SPAN_AND_EVENT"
    )
    os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] = "gen_ai_latest_experimental"
    os.environ["ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"] = "true"
    self._tmpl_attrs["project"] = target_project
    self._tmpl_attrs["location"] = target_location
    self._tmpl_attrs["enable_tracing"] = True
    self._tmpl_attrs["agent"] = _build_root_agent()
    self._tmpl_attrs["session_service_builder"] = HybridVertexSessionService
    os.environ["GOOGLE_CLOUD_PROJECT"] = target_project
    os.environ["GOOGLE_CLOUD_LOCATION"] = target_location
    super().set_up()
    _register_promote_error_processor()
    for r_key in ("runner", "in_memory_runner"):
      runner_obj = self._tmpl_attrs.get(r_key)
      if runner_obj is not None and hasattr(runner_obj, "auto_create_session"):
        runner_obj.auto_create_session = True

  @staticmethod
  def _force_non_streaming_run_config(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Ensures ADK calls non-streaming `:generateContent` (`StreamingMode.NONE` == `None`) so SGP evaluates every turn."""
    if os.environ.get("SGP_FORCE_NON_STREAMING", "true").lower() not in (
        "1",
        "true",
        "yes",
    ):
      return kwargs
    updated = dict(kwargs)
    rc = updated.get("run_config")
    if rc is None:
      # RunConfig() defaults streaming_mode to StreamingMode.NONE (which has enum value None)
      updated["run_config"] = {"streaming_mode": None}
    elif isinstance(rc, dict):
      rc_copy = dict(rc)
      rc_copy["streaming_mode"] = None
      updated["run_config"] = rc_copy
    elif hasattr(rc, "model_dump"):
      try:
        rc_copy = rc.model_dump(mode="json")
        rc_copy["streaming_mode"] = None
        updated["run_config"] = rc_copy
      except Exception:
        updated["run_config"] = {"streaming_mode": None}
    return updated

  def stream_query(self, *args, **kwargs):
    try:
      kwargs = self._force_non_streaming_run_config(kwargs)
      for chunk in super().stream_query(*args, **kwargs):
        yield chunk
    except Exception as exc:  # pylint: disable=broad-except
      tb_one_line = traceback.format_exc().replace("\n", " | ")
      print(
          f"[AGENT_RUNTIME_ERROR] stream_query failed: {exc} || {tb_one_line}",
          file=sys.stderr,
          flush=True,
      )
      yield {
          "author": "secops_github_orchestrator",
          "content": {
              "role": "model",
              "parts": [{"text": _format_runtime_error_banner(exc)}],
          },
      }

  async def async_stream_query(self, *args, **kwargs):
    try:
      kwargs = self._force_non_streaming_run_config(kwargs)
      async for chunk in super().async_stream_query(*args, **kwargs):
        yield chunk
    except Exception as exc:  # pylint: disable=broad-except
      tb_one_line = traceback.format_exc().replace("\n", " | ")
      print(
          f"[AGENT_RUNTIME_ERROR] async_stream_query failed: {exc} || {tb_one_line}",
          file=sys.stderr,
          flush=True,
      )
      yield {
          "author": "secops_github_orchestrator",
          "content": {
              "role": "model",
              "parts": [{"text": _format_runtime_error_banner(exc)}],
          },
      }

  async def streaming_agent_run_with_events(self, request_json: str, *args, **kwargs):
    try:
      # Gemini Enterprise sends the full prior conversation history in `request.events`
      # alongside a non-numeric `session_id`. Ensure empty in-memory sessions never mask
      # `request.events` so `AdkApp._init_session` hydrates multi-turn history cleanly.
      try:
        req_dict = json.loads(request_json) if isinstance(request_json, str) else None
        if isinstance(req_dict, dict) and req_dict.get("events") and req_dict.get("session_id"):
          if not self._tmpl_attrs.get("session_service"):
            self.set_up()
          ss = self._tmpl_attrs.get("session_service")
          uid = req_dict.get("user_id") or req_dict.get("userId") or "default-user-id"
          sid = req_dict["session_id"]
          if ss is not None:
            existing_sess = await ss.get_session(
                app_name=self._app_name(),
                user_id=uid,
                session_id=sid,
            )
            if existing_sess is None or not getattr(existing_sess, "events", None):
              req_dict_copy = dict(req_dict)
              req_dict_copy.pop("session_id", None)
              req_dict_copy.pop("sessionId", None)
              request_json = json.dumps(req_dict_copy)
      except Exception:  # pylint: disable=broad-except
        pass
      async for chunk in super().streaming_agent_run_with_events(request_json, *args, **kwargs):
        yield chunk
    except Exception as exc:  # pylint: disable=broad-except
      tb_one_line = traceback.format_exc().replace("\n", " | ")
      print(
          f"[AGENT_RUNTIME_ERROR] streaming_agent_run_with_events failed: {exc} || {tb_one_line}",
          file=sys.stderr,
          flush=True,
      )
      yield {
          "author": "secops_github_orchestrator",
          "content": {
              "role": "model",
              "parts": [{"text": _format_runtime_error_banner(exc)}],
          },
      }


root_agent = _build_root_agent()

app = ProjectABoundAdkApp(
    agent=root_agent,
    enable_tracing=True,
    session_service_builder=HybridVertexSessionService,
)
