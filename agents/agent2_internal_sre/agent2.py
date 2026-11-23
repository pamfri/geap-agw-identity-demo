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
from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.sessions.base_session_service import BaseSessionService
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.genai import types
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
1. Internal SecOps Read-Only Auditing (ALLOWED by IAP Tool-Level CEL Condition on `internal-cloudrun-mcp`):
   - Use `get_active_cve_alerts(service_name)` to inspect open CVEs across internal services ('payment-service', 'checkout-worker', or 'all').
   - Use `get_internal_deployment_manifest(service_name)` to inspect production image digests, owning on-call teams, and change-freeze windows.
2. Fine-Grained Tool-Level CEL Condition Block Verification (`record_remediation_ticket`):
   - If the user asks to record or log a remediation ticket (or test the fine-grained IAP Tool-Level CEL condition on `internal-cloudrun-mcp`), call `record_remediation_ticket(cve_id, service_name, github_pr_or_issue_url, remediated_by_github_user)`.
   - Explain clearly that while Agent 2 is allowed to call read-only tools (`get_active_cve_alerts`, `get_internal_deployment_manifest`) on `internal-cloudrun-mcp`, the IAP CEL condition (`api.getAttribute('iap.googleapis.com/mcp.toolName', '')`) blocks Agent 2 from calling the mutating `record_remediation_ticket` tool on that same MCP server with HTTP 403 (`Egress request is not authorized`).
3. Live Egress & IAP Policy Block Verification (`test_egress_policy_check`):
   - If the user asks to test or showcase an active **IAP v2 block** (`403` / `authzPolicyInfo.result: DENIED`) or **Agent Gateway default-deny block**, call `test_egress_policy_check(target_url)`:
     * Passing `target_url="https://api.githubcopilot.com/mcp/"` attempts a JSON-RPC MCP call to the External GitHub Copilot MCP server (which is registered in Agent Registry, but its IAP v2 policy only allows Agent 1, so IAP v2 blocks Agent 2 with HTTP 403 `DENIED`).
     * Passing `target_url="https://example.com"` attempts an outbound request to an unregistered external domain (which Agent Gateway blocks via its `default_denied` rule with HTTP 403).
"""


class HybridVertexSessionService(BaseSessionService):
  """Durable Vertex AI Agent Engine session service with in-memory fallback."""

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
        or PROJECT_B_ID
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
            app_name=app_name, user_id=user_id, state=state, **kwargs
        )
      except Exception:  # pylint: disable=broad-except
        pass
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
      except Exception:  # pylint: disable=broad-except
        pass
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
      except Exception:  # pylint: disable=broad-except
        pass
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


async def _call_internal_mcp_jsonrpc(
    tool_name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
  """Calls the Internal SecOps Cloud Run MCP Server over Streamable HTTP JSON-RPC 2.0."""
  mcp_url = os.environ.get("INTERNAL_MCP_URL", INTERNAL_MCP_URL).rstrip("/")
  if not mcp_url.endswith("/mcp"):
    mcp_url = f"{mcp_url}/mcp"
  project_b_num = (
      os.environ.get("PROJECT_B_NUM")
      or os.environ.get("AGENT_PROJECT_NUM")
      or "YOUR_PROJECT_B_NUM"
  )
  region = os.environ.get("REGION", REGION)
  dest_uri = (
      f"//run.googleapis.com/projects/{project_b_num}"
      f"/locations/{region}/services/internal-mcp-server"
  )
  span_attrs = {
      "gcp.resource.destination.id": dest_uri,
      "gcp.mcp.server.destination.id": dest_uri,
      "rpc.system": "jsonrpc",
      "rpc.service": "internal-cloudrun-mcp",
      "rpc.method": tool_name,
      "url.full": mcp_url,
  }
  try:
    from opentelemetry import trace
    cur_span = trace.get_current_span()
    if cur_span and cur_span.is_recording():
      for k, v in span_attrs.items():
        cur_span.set_attribute(k, v)
  except Exception:
    pass
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
          "blocked_by_iap_cel_condition": resp.status_code == 403,
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


async def record_remediation_ticket(
    cve_id: str = "CVE-2026-41092",
    service_name: str = "payment-service",
    github_pr_or_issue_url: str = "https://github.com/org/repo/issues/99",
    remediated_by_github_user: str = "secops-auditor",
) -> dict[str, Any]:
  """Attempt to record a remediation ticket into the internal SecOps compliance ledger on `internal-cloudrun-mcp`.

  Demonstrates fine-grained IAP Tool-Level CEL condition enforcement on `internal-cloudrun-mcp`:
  Agent 2 is allowed to call read-only tools (`get_active_cve_alerts`, `get_internal_deployment_manifest`),
  but blocked with HTTP 403 (`Egress request is not authorized`) when attempting to call this mutating tool.

  Args:
    cve_id: The CVE identifier being remediated (e.g., 'CVE-2026-41092').
    service_name: The affected internal service name (e.g., 'payment-service').
    github_pr_or_issue_url: Full URL of the GitHub Pull Request or Issue.
    remediated_by_github_user: Username attempting to record the ticket.
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
    from opentelemetry.propagate import inject
    inject(headers)
  except Exception:
    pass
  try:
    async with httpx.AsyncClient(timeout=15.0) as client:
      # When probing an unregistered domain like example.com, also send a non-CONNECT HTTP request
      # so the Agent Gateway Observability 'Unregistered Outbound Blocks' table (which filters
      # http_request.request_method != 'CONNECT') records the blocked GET/POST entry.
      if "example.com" in target_url:
        try:
          await client.get("http://example.com/", headers=headers)
        except Exception:
          pass
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


async def _before_model_guardrail_check(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> LlmResponse | None:
  """Ensures span content capture is active and forwards adversarial prompts through Agent Gateway CONTENT_AUTHZ."""
  del callback_context
  os.environ["ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"] = "true"
  user_text = _extract_latest_user_text(llm_request)
  if not user_text:
    return None
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
  return None


async def _after_model_ensure_visible_content(
    callback_context: CallbackContext, llm_response: LlmResponse
) -> LlmResponse | None:
  """Ensures blocked or empty model responses render a visible message in Cloud Console Playground."""
  del callback_context
  if llm_response.content and getattr(llm_response.content, "parts", None):
    return None
  if llm_response.error_code or (
      llm_response.finish_reason
      and llm_response.finish_reason != types.FinishReason.STOP
  ):
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


async def _on_model_error_ensure_visible_content(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
    error: Exception,
) -> LlmResponse | None:
  """Converts model/guardrail API errors into a visible Playground response."""
  del callback_context, llm_request
  return LlmResponse(
      content=types.Content(
          role="model",
          parts=[
              types.Part(
                  text=(
                      "Request blocked by Model Armor / Vertex AI Security"
                      f" Policy: {error}"
                  )
              )
          ],
      )
  )


def _build_root_agent() -> LlmAgent:
  return LlmAgent(
      model="gemini-2.5-flash",
      name="internal_secops_auditor",
      instruction=INTERNAL_AUDITOR_INSTRUCTION,
      before_model_callback=_before_model_guardrail_check,
      after_model_callback=_after_model_ensure_visible_content,
      on_model_error_callback=_on_model_error_ensure_visible_content,
      tools=[
          get_active_cve_alerts,
          get_internal_deployment_manifest,
          record_remediation_ticket,
          test_egress_policy_check,
      ],
  )


class ProjectBBoundAdkApp(AdkApp):

  def __init__(self, *args, **kwargs):
    kwargs.setdefault("enable_tracing", True)
    super().__init__(*args, **kwargs)
    self._tmpl_attrs["project"] = PROJECT_B_ID
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
    self._tmpl_attrs["session_service_builder"] = (
        AutoCreatingInMemorySessionService
    )

  def set_up(self):
    target_project = os.environ.get("AGENT_PROJECT_ID", PROJECT_B_ID)
    target_project_num = os.environ.get("AGENT_PROJECT_NUM", "")
    target_location = os.environ.get("REGION", REGION)
    engine_id = os.environ.get(
        "GOOGLE_CLOUD_AGENT_ENGINE_ID", "internal-secops-auditor"
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
      agent_urn = f"urn:agent:{target_project}:internal-secops-auditor"
      fallback_re_id = os.environ.get("RE_ENGINE_ID", "internal-secops-auditor")
      cloud_resource_id = (
          f"//aiplatform.googleapis.com/projects/{target_project_num or 'YOUR_PROJECT_B_NUM'}"
          f"/locations/{target_location}/reasoningEngines/{fallback_re_id}"
      )
    os.environ["OTEL_RESOURCE_ATTRIBUTES"] = (
        f"cloud.provider=gcp,cloud.region={target_location},"
        f"cloud.resource.id={cloud_resource_id},"
        f"gcp.project_id={target_project},gcp.agent.id={agent_urn},"
        "service.name=internal-secops-auditor"
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

  def stream_query(self, *args, **kwargs):
    try:
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
          "author": "internal_secops_auditor",
          "content": {
              "role": "model",
              "parts": [{
                  "text": (
                      "🛡️ [Google Cloud Model Armor / Agent Gateway Guardrail]: "
                      "This request was intercepted and blocked by security policy "
                      f"(Prompt Injection / Jailbreak / Unsafe Content filter): {exc}"
                  )
              }],
          },
      }

  async def async_stream_query(self, *args, **kwargs):
    try:
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
          "author": "internal_secops_auditor",
          "content": {
              "role": "model",
              "parts": [{
                  "text": (
                      "🛡️ [Google Cloud Model Armor / Agent Gateway Guardrail]: "
                      "This request was intercepted and blocked by security policy "
                      f"(Prompt Injection / Jailbreak / Unsafe Content filter): {exc}"
                  )
              }],
          },
      }

  async def streaming_agent_run_with_events(self, *args, **kwargs):
    try:
      async for chunk in super().streaming_agent_run_with_events(*args, **kwargs):
        yield chunk
    except Exception as exc:  # pylint: disable=broad-except
      tb_one_line = traceback.format_exc().replace("\n", " | ")
      print(
          f"[AGENT_RUNTIME_ERROR] streaming_agent_run_with_events failed: {exc} || {tb_one_line}",
          file=sys.stderr,
          flush=True,
      )
      yield {
          "author": "internal_secops_auditor",
          "content": {
              "role": "model",
              "parts": [{
                  "text": (
                      "🛡️ [Google Cloud Model Armor / Agent Gateway Guardrail]: "
                      "This request was intercepted and blocked by security policy "
                      f"(Prompt Injection / Jailbreak / Unsafe Content filter): {exc}"
                  )
              }],
          },
      }


root_agent = _build_root_agent()

app = ProjectBBoundAdkApp(
    agent=root_agent,
    enable_tracing=True,
    session_service_builder=AutoCreatingInMemorySessionService,
)
