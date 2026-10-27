"""Cloud Run 3LO Callback & Web Client (`secops-3lo-callback` in Project A).

Handles the complete 3-Legged OAuth (3LO) runtime handshake with Google Cloud
Auth Manager (`agentidentitycredentials.googleapis.com`) and Agent 1
(`secops_github_orchestrator` in Project A) WITHOUT requiring any local
`localhost:8501` server:
1. Deployed as a serverless Cloud Run service in Project A (`your-project-a-id`).
2. Exposes `/validateUserId` (configured as `--three-legged-oauth-default-continue-uri`)
   which receives `user_id_validation_state` and `auth_provider_name` after the
   user clicks Authorize on GitHub (whether triggered from the Cloud Console
   GEAP Playground UI where `user_id='vais-query-reasoning-engine'` OR from a
   custom frontend), calls `credentials:finalize`, and binds the user's GitHub
   OAuth token in Google Cloud Auth Manager's vault.
"""

import os
from typing import Any
from urllib.parse import urlencode
from fastapi import FastAPI, Query, Request as FastAPIRequest
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import google.auth
from google.auth.transport.requests import Request
import requests
import vertexai
from vertexai import agent_engines

PROJECT_A_ID = os.environ.get("PROJECT_A_ID", "your-project-a-id")
PROJECT_A_NUM = os.environ.get("PROJECT_A_NUM", "YOUR_PROJECT_A_NUM")
REGION = os.environ.get("REGION", "us-central1")
RE_1_ID = os.environ.get("RE_1_ID", "YOUR_REASONING_ENGINE_1_ID")
DEFAULT_USER_ID = os.environ.get("USER_ID", "secops-engineer-01")
PLAYGROUND_USER_ID = "vais-query-reasoning-engine"
GITHUB_OAUTH_SCOPES = ["repo"]

app = FastAPI(title="SecOps Incident-to-PR 3LO Cloud Run Callback Service")

# In-memory state tracking pending 3LO consent nonces and active user_id
_PENDING_CONSENT: dict[str, Any] = {
    "active_user_id": None,
    "by_user": {},
}


def _get_gcp_headers() -> dict[str, str]:
  credentials, _ = google.auth.default(
      scopes=["https://www.googleapis.com/auth/cloud-platform"]
  )
  credentials.refresh(Request())
  return {
      "Authorization": f"Bearer {credentials.token}",
      "Content-Type": "application/json",
      "X-Goog-User-Project": PROJECT_A_ID,
  }


def _lookup_active_consent_nonce(
    auth_provider_name: str, user_id: str, continue_uri: str
) -> str:
  """Queries Auth Manager RetrieveCredentials to fetch the active consentNonce for user_id."""
  retrieve_url = (
      f"https://agentidentitycredentials.googleapis.com/v1/"
      f"{auth_provider_name}/credentials:retrieve"
  )
  resp = requests.post(
      retrieve_url,
      headers=_get_gcp_headers(),
      json={
          "userId": user_id,
          "scopes": GITHUB_OAUTH_SCOPES,
          "continueUri": continue_uri,
      },
      timeout=30,
  )
  if resp.status_code == 200:
    data = resp.json()
    return data.get("uriConsentRequired", {}).get("consentNonce", "")
  return ""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
  return """
    <!DOCTYPE html>
    <html>
    <head>
      <title>SecOps Incident-to-PR (3LO + Agent Gateway)</title>
      <style>
        body { font-family: system-ui, sans-serif; max-width: 860px; margin: 2rem auto; padding: 0 1rem; }
        textarea { width: 100%; height: 85px; font-size: 1rem; padding: 0.5rem; margin-top: 0.5rem; }
        input[type="text"] { padding: 0.45rem; font-size: 0.95rem; width: 260px; }
        button { padding: 0.55rem 1.1rem; font-size: 0.95rem; cursor: pointer; margin-top: 0.5rem; margin-right: 0.5rem; }
        pre { background: #f4f6f8; padding: 1rem; border-radius: 6px; overflow-x: auto; white-space: pre-wrap; }
        .oauth-box { background: #fff8e1; border: 1px solid #ffca28; padding: 1rem; border-radius: 6px; margin-top: 1rem; }
        .user-row { background: #eef3f8; padding: 0.75rem; border-radius: 6px; margin-bottom: 0.75rem; }
      </style>
    </head>
    <body>
      <h2>SecOps Incident-to-PR Orchestrator (Agent 1 — 3LO Flow)</h2>
      <p><strong>Flow:</strong> Project B Private Cloud Run MCP (SPIFFE Agent Identity) + GitHub Copilot MCP (3LO Delegated User Authority via Auth Manager &amp; Agent Gateway)</p>
      <div class="user-row">
        <label><strong>End-User ID (Vault Key in Auth Manager):</strong></label>
        <input type="text" id="user_id" value="secops-engineer-01" />
        <button type="button" onclick="newUserId()">New User ID (Force Fresh 3LO Login Prompt)</button>
      </div>
      <textarea id="prompt">Check active CVE alerts and the internal deployment manifest for payment-service, then call get_github_authenticated_user to verify my GitHub user via 3LO OAuth.</textarea>
      <br/>
      <button onclick="sendPrompt()">Run SecOps Incident-to-PR</button>
      <div id="oauth-container"></div>
      <h3>Agent Response</h3>
      <pre id="output">Ready.</pre>
      <script>
        function newUserId() {
          const rand = Math.floor(1000 + Math.random() * 9000);
          document.getElementById('user_id').value = 'secops-user-' + rand;
        }
        async function sendPrompt() {
          document.getElementById('output').textContent = 'Querying Agent 1...';
          document.getElementById('oauth-container').innerHTML = '';
          const prompt = document.getElementById('prompt').value;
          const user_id = document.getElementById('user_id').value.trim() || 'secops-engineer-01';
          const res = await fetch('/chat', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({prompt, user_id})
          });
          const data = await res.json();
          if (data.authorization_uri) {
            document.getElementById('oauth-container').innerHTML = `
              <div class="oauth-box">
                <strong>3-Legged OAuth (3LO) Consent Required for <code>${data.user_id}</code>:</strong><br/>
                Google Cloud Auth Manager (<code>github-mcp-auth</code>) has no vaulted GitHub token for <code>${data.user_id}</code>.<br/><br/>
                <a href="${data.authorization_uri}" target="_blank">
                  <button type="button">1. Authorize with GitHub (3LO)</button>
                </a>
              </div>`;
          }
          document.getElementById('output').textContent = JSON.stringify(data, null, 2);
        }
      </script>
    </body>
    </html>
    """


@app.post("/chat")
def chat(payload: dict[str, str]) -> JSONResponse:
  prompt = payload.get("prompt", "")
  user_id = payload.get("user_id", DEFAULT_USER_ID).strip() or DEFAULT_USER_ID
  _PENDING_CONSENT["active_user_id"] = user_id

  vertexai.init(project=PROJECT_A_ID, location=REGION)
  remote_agent = agent_engines.get(
      f"projects/{PROJECT_A_NUM}/locations/{REGION}/reasoningEngines/{RE_1_ID}"
  )

  events = []
  auth_uri = None
  for event in remote_agent.stream_query(
      user_id=user_id,
      message=prompt,
  ):
    events.append(event)
    content = event.get("content", {}) if isinstance(event, dict) else {}
    for part in content.get("parts", []):
      fn_call = part.get("function_call") or {}
      if fn_call.get("name") == "adk_request_credential":
        args = fn_call.get("args", {})
        auth_cfg = args.get("auth_config", {})
        exchanged = auth_cfg.get("exchanged_auth_credential", {})
        oauth2 = exchanged.get("oauth2", {})
        auth_uri = oauth2.get("auth_uri") or auth_uri
        consent_nonce = oauth2.get("nonce")
        _PENDING_CONSENT["by_user"][user_id] = {
            "consent_nonce": consent_nonce,
            "function_call_id": fn_call.get("id"),
            "auth_config": auth_cfg,
        }
      fn_resp = part.get("function_response") or {}
      resp_data = fn_resp.get("response") or {}
      if (
          isinstance(resp_data, dict)
          and resp_data.get("status") == "URI_CONSENT_REQUIRED"
      ):
        auth_uri = resp_data.get("authorization_uri") or auth_uri
        consent_nonce = resp_data.get("consent_nonce")
        _PENDING_CONSENT["by_user"][user_id] = {
            "consent_nonce": consent_nonce,
        }

  return JSONResponse({
      "user_id": user_id,
      "authorization_uri": auth_uri,
      "events": events,
  })


@app.post("/registerNonce")
def register_nonce(payload: dict[str, str]) -> JSONResponse:
  """Receives the active (user_id, consent_nonce, authorization_uri) from Agent 1 during 3LO initiation."""
  user_id = payload.get("user_id", "").strip()
  consent_nonce = payload.get("consent_nonce", "").strip()
  authorization_uri = payload.get("authorization_uri", "").strip()
  if user_id and consent_nonce:
    _PENDING_CONSENT["active_user_id"] = user_id
    _PENDING_CONSENT["by_user"][user_id] = {
        "consent_nonce": consent_nonce,
        "authorization_uri": authorization_uri,
    }
  return JSONResponse({"status": "ok", "user_id": user_id})


@app.get("/authorize")
def authorize(
    request: FastAPIRequest,
    uid: str = Query(PLAYGROUND_USER_ID),
):
  """LLM-proof 3LO redirect endpoint.

  Calls Auth Manager RetrieveCredentials directly and HTTP 302 redirects the
  browser to GitHub's authorizationUri so an LLM in the Playground never
  corrupts the 350-character encrypted `state=` parameter.
  """
  user_id = uid.strip() or PLAYGROUND_USER_ID
  auth_provider_name = (
      f"projects/{PROJECT_A_ID}/locations/{REGION}/authProviders/github-mcp-auth"
  )
  base_continue_uri = (
      str(request.base_url).rstrip("/").replace("http://", "https://")
      + "/validateUserId"
  )
  continue_uri_with_uid = f"{base_continue_uri}?{urlencode({'uid': user_id})}"

  retrieve_url = (
      f"https://agentidentitycredentials.googleapis.com/v1/"
      f"{auth_provider_name}/credentials:retrieve"
  )
  resp = requests.post(
      retrieve_url,
      headers=_get_gcp_headers(),
      json={
          "userId": user_id,
          "scopes": GITHUB_OAUTH_SCOPES,
          "continueUri": continue_uri_with_uid,
      },
      timeout=30,
  )
  resp.raise_for_status()
  data = resp.json()

  if "success" in data:
    return HTMLResponse(f"""
      <html>
        <body style="font-family: system-ui, sans-serif; max-width: 650px; margin: 3rem auto;">
          <h2>GitHub 3LO Token Already Active!</h2>
          <p>User <code>{user_id}</code> already has an active vaulted GitHub OAuth token in Google Cloud Auth Manager.</p>
          <p>Return to the <strong>Cloud Console GEAP Playground</strong> and ask Agent 1 to call <code>get_github_authenticated_user</code>.</p>
        </body>
      </html>
    """)

  consent = data.get("uriConsentRequired", {})
  auth_uri = consent.get("authorizationUri", "")
  consent_nonce = consent.get("consentNonce", "")
  if not auth_uri:
    return JSONResponse(
        {"error": "No authorizationUri returned by Auth Manager", "response": data},
        status_code=400,
    )

  _PENDING_CONSENT["active_user_id"] = user_id
  _PENDING_CONSENT["by_user"][user_id] = {
      "consent_nonce": consent_nonce,
      "authorization_uri": auth_uri,
  }
  redirect_resp = RedirectResponse(url=auth_uri, status_code=302)
  if consent_nonce:
    redirect_resp.set_cookie(
        key="agw_3lo_nonce",
        value=consent_nonce,
        max_age=1800,
        httponly=True,
        secure=True,
        samesite="lax",
    )
  return redirect_resp


@app.get("/validateUserId", response_class=HTMLResponse)
def validate_user_id(
    request: FastAPIRequest,
    user_id_validation_state: str = Query(...),
    auth_provider_name: str = Query(...),
    uid: str | None = Query(None),
    nonce: str | None = Query(None),
) -> str:
  """Called by Google Cloud Auth Manager after GitHub redirects to /oauthcallback."""
  user_id = (
      uid
      or _PENDING_CONSENT.get("active_user_id")
      or PLAYGROUND_USER_ID
  )
  base_continue_uri = str(request.base_url).rstrip("/").replace("http://", "https://") + "/validateUserId"
  continue_uri_with_uid = f"{base_continue_uri}?{urlencode({'uid': user_id})}"

  pending = _PENDING_CONSENT["by_user"].get(user_id, {})
  cookie_nonce = request.cookies.get("agw_3lo_nonce")
  consent_nonce = (
      nonce
      or cookie_nonce
      or pending.get("consent_nonce")
      or _lookup_active_consent_nonce(auth_provider_name, user_id, continue_uri_with_uid)
  )

  finalize_url = (
      f"https://agentidentitycredentials.googleapis.com/v1/"
      f"{auth_provider_name}/credentials:finalize"
  )
  body = {
      "userId": user_id,
      "userIdValidationState": user_id_validation_state,
      "consentNonce": consent_nonce,
  }
  resp = requests.post(
      finalize_url,
      headers=_get_gcp_headers(),
      json=body,
      timeout=30,
  )
  resp.raise_for_status()

  return f"""
    <html>
      <body style="font-family: system-ui, sans-serif; max-width: 650px; margin: 3rem auto;">
        <h2>GitHub 3LO Authorization Finalized on Cloud Run!</h2>
        <p>Google Cloud Auth Manager bound your GitHub OAuth token to user_id
           <code>{user_id}</code> via <code>credentials:finalize</code>.</p>
        <p>You can now return to the <strong>Cloud Console GEAP Playground</strong> (or the
           <a href="/">SecOps Incident-to-PR UI</a>) and run
           <code>Call get_github_authenticated_user to check my GitHub user profile.</code>
           again to execute the GitHub MCP tool call using your vaulted token!</p>
      </body>
    </html>
    """


if __name__ == "__main__":
  import uvicorn

  port = int(os.environ.get("PORT", "8501"))
  host = "0.0.0.0" if "K_SERVICE" in os.environ else "127.0.0.1"
  print(f"Starting SecOps Incident-to-PR 3LO Callback Service on http://{host}:{port} ...")
  uvicorn.run(app, host=host, port=port)
