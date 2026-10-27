"""Deploy Agent 1 (Project A) & Agent 2 (Project B) with SPIFFE Agent Identity & Agent Gateway Config.

Provisions both agents on Vertex AI Agent Engine using:
  - identity_type=types.IdentityType.AGENT_IDENTITY
  - agent_gateway_config.agent_to_anywhere_config.agent_gateway
and writes their provisioned SPIFFE principals to `spiffe_identities.env`.
"""

import hashlib
import os
import shutil
import subprocess
import sys

try:
    if sys.version_info < (3, 10):
        raise ModuleNotFoundError("Python >= 3.10 required")
    import vertexai as _  # noqa: F401
except ModuleNotFoundError:
    _venv_candidates = [
        os.path.expanduser("~/.cache/agw-venv-py314/bin/python3"),
        os.path.expanduser("~/.cache/agw-venv-py313/bin/python3"),
        os.path.expanduser("~/.cache/agw-venv-py312/bin/python3"),
        os.path.expanduser("~/.cache/agw-venv-py311/bin/python3"),
        os.path.expanduser("~/.cache/agw-venv/bin/python3"),
        os.path.expanduser("~/agw-venv/bin/python3"),
        "/tmp/agw-venv/bin/python3",
    ]
    for _venv_py in _venv_candidates:
        if os.path.exists(_venv_py) and os.path.realpath(sys.executable) != os.path.realpath(_venv_py):
            try:
                subprocess.run(
                    [_venv_py, "-c", "import sys, vertexai; assert sys.version_info >= (3, 10)"],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                os.execv(_venv_py, [_venv_py, *sys.argv])
            except Exception:
                pass

    import glob

    _py_candidates = (
        [
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
        ]
        + glob.glob("/opt/homebrew/opt/python@3.*/bin/python3*")
        + glob.glob("/opt/homebrew/Cellar/python@3.*/*/bin/python3*")
    )
    _base_py = sys.executable if sys.version_info >= (3, 10) else None
    if not _base_py:
        for _cand in _py_candidates:
            if os.path.exists(_cand):
                try:
                    subprocess.run(
                        [_cand, "-c", "import sys; assert sys.version_info >= (3, 10)"],
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    _base_py = _cand
                    break
                except Exception:
                    pass
    if not _base_py:
        raise RuntimeError("Python >= 3.10 is required. Please run with /opt/homebrew/bin/python3")

    _py_ver = subprocess.check_output(
        [_base_py, "-c", "import sys; print(f'{sys.version_info.major}{sys.version_info.minor}')"],
        text=True,
    ).strip()
    _target_venv = os.path.expanduser(f"~/.cache/agw-venv-py{_py_ver}")
    _venv_py = os.path.join(_target_venv, "bin", "python3")
    if not os.path.exists(_venv_py):
        subprocess.run([_base_py, "-m", "venv", _target_venv], check=True)
    _wheel_pkgs = [
        "zipp<4.0.0",
        "google-api-core==2.35.0",
        "google-cloud-aiplatform==1.165.1",
        "mcp==1.30.0",
        "aiohttp>=3.9.0",
        "google-auth>=2.35.0",
        "httpx>=0.27.0",
        "cloudpickle>=3.0.0",
        "pydantic>=2.0.0",
        "anyio>=4.9.0",
        "authlib>=1.6.6",
        "click>=8.1.8",
        "fastapi>=0.133.0",
        "google-api-python-client>=2.157.0",
        "graphviz>=0.20.2",
        "jsonschema>=4.23.0",
        "opentelemetry-api==1.41.1",
        "opentelemetry-sdk==1.41.1",
        "opentelemetry-exporter-gcp-logging==1.12.0a0",
        "opentelemetry-exporter-gcp-monitoring>=1.9.0a0",
        "opentelemetry-exporter-gcp-trace>=1.9.0",
        "opentelemetry-exporter-otlp-proto-http>=1.36.0",
        "opentelemetry-resourcedetector-gcp>=1.9.0a0",
        "python-dateutil>=2.9.0.post0",
        "python-dotenv>=1.0.0",
        "pyyaml>=6.0.2",
        "requests>=2.32.4",
        "sqlalchemy>=2.0.0",
        "starlette>=1.3.1",
        "tenacity>=9.0.0",
        "tzlocal>=5.3",
        "uvicorn>=0.34.0",
        "websockets==16.1.1",
    ]
    print(f"Bootstrapping pure-wheel Python virtualenv at {_target_venv} using {_base_py} ...")
    subprocess.run(
        [_venv_py, "-m", "pip", "install", "--only-binary=:all:", *_wheel_pkgs],
        check=True,
    )
    subprocess.run(
        [_venv_py, "-m", "pip", "install", "--only-binary=:all:", "--no-deps", "google-adk==1.39.1"],
        check=True,
    )
    os.execv(_venv_py, [_venv_py, *sys.argv])

os.chdir("/tmp")
ORIG_AGENTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT_DIR = os.path.dirname(ORIG_AGENTS_DIR)
_dotenv_path = os.path.join(REPO_ROOT_DIR, ".env")
if os.path.isfile(_dotenv_path):
    with open(_dotenv_path, "r", encoding="utf-8") as _ef:
        for _line in _ef:
            _line = _line.strip()
            if not _line or _line.startswith("#"):
                continue
            if _line.startswith("export "):
                _line = _line[len("export "):].strip()
            if "=" in _line:
                _k, _v = _line.split("=", 1)
                _k = _k.strip()
                _v = _v.strip().strip('"').strip("'")
                os.environ.setdefault(_k, _v)

AGENTS_DIR = "/tmp/agw-cfg/agents_build"
os.makedirs(AGENTS_DIR, exist_ok=True)
for pkg_dir in ("agent1_github_secops", "agent2_internal_sre"):
    src_pkg = os.path.join(ORIG_AGENTS_DIR, pkg_dir)
    dst_pkg = os.path.join(AGENTS_DIR, pkg_dir)
    if os.path.exists(src_pkg):
        shutil.rmtree(dst_pkg, ignore_errors=True)
        shutil.copytree(src_pkg, dst_pkg)

if AGENTS_DIR not in sys.path:
    sys.path.insert(0, AGENTS_DIR)
os.chdir(AGENTS_DIR)

import time
import vertexai
from vertexai._genai import types

from agent1_github_secops.agent1 import app as agent1_app
from agent2_internal_sre.agent2 import app as agent2_app

def _resolve_project_number(project_id: str, env_var: str) -> str:
    val = os.environ.get(env_var, "").strip()
    if val:
        return val
    try:
        out = subprocess.check_output(
            ["gcloud", "projects", "describe", project_id, "--format=value(projectNumber)"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if out:
            return out
    except Exception:
        pass
    return "YOUR_PROJECT_NUM"


def _resolve_internal_mcp_url(project_b_id: str, region: str) -> str:
    env_url = os.environ.get("INTERNAL_MCP_URL", "").strip()
    if env_url:
        return env_url if env_url.endswith("/mcp") else f"{env_url.rstrip('/')}/mcp"
    try:
        out = subprocess.check_output(
            [
                "gcloud",
                "run",
                "services",
                "describe",
                "internal-mcp-server",
                f"--project={project_b_id}",
                f"--region={region}",
                "--format=value(status.url)",
            ],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if out:
            return f"{out.rstrip('/')}/mcp"
    except Exception:
        pass
    return "https://internal-mcp-server.a.run.app/mcp"


PROJECT_A_ID = os.environ.get("PROJECT_A_ID", "your-project-a-id")
PROJECT_B_ID = os.environ.get("PROJECT_B_ID", "your-project-b-id")
PROJECT_A_NUM = _resolve_project_number(PROJECT_A_ID, "PROJECT_A_NUM")
PROJECT_B_NUM = _resolve_project_number(PROJECT_B_ID, "PROJECT_B_NUM")
REGION = os.environ.get("REGION", "us-central1")
STAGING_BUCKET_A = os.environ.get(
    "STAGING_BUCKET_A", f"gs://{PROJECT_A_ID}-agent-staging"
)
STAGING_BUCKET_B = os.environ.get(
    "STAGING_BUCKET_B", f"gs://{PROJECT_B_ID}-agent-staging"
)
INTERNAL_MCP_URL = _resolve_internal_mcp_url(PROJECT_B_ID, REGION)


def _ensure_3lo_callback_service() -> None:
    client_3lo_dir = os.path.abspath(os.path.join(ORIG_AGENTS_DIR, "..", "client-3lo"))
    if not os.path.isdir(client_3lo_dir):
        return
    print(f"[0/3] Updating Cloud Run 3LO Callback Service (secops-3lo-callback) in {PROJECT_A_ID} with --no-invoker-iam-check...")
    subprocess.run(
        [
            "gcloud",
            "-q",
            "run",
            "deploy",
            "secops-3lo-callback",
            f"--source={client_3lo_dir}",
            f"--region={REGION}",
            "--no-invoker-iam-check",
            f"--set-env-vars=PROJECT_A_ID={PROJECT_A_ID},REGION={REGION},AUTH_PROVIDER_ID=github-mcp-auth",
            f"--project={PROJECT_A_ID}",
        ],
        check=False,
    )


def _resolve_continue_uri() -> str:
    env_uri = os.environ.get("OAUTH_CONTINUE_URI", "").strip()
    if env_uri and "localhost" not in env_uri and "127.0.0.1" not in env_uri:
        return env_uri
    try:
        out = subprocess.check_output(
            [
                "gcloud",
                "run",
                "services",
                "describe",
                "secops-3lo-callback",
                f"--project={PROJECT_A_ID}",
                f"--region={REGION}",
                "--format=value(status.url)",
            ],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if out:
            return f"{out.rstrip('/')}/validateUserId"
    except Exception:
        pass
    return env_uri or "http://localhost:8501/validateUserId"


CONTINUE_URI = _resolve_continue_uri()

AGW_URI = (
    f"projects/{PROJECT_A_ID}/locations/{REGION}/agentGateways/enterprise-egress-agw"
)


def _compute_pkg_hash(pkg_name: str, extra_salt: str = "") -> str:
    h = hashlib.sha256()
    pkg_path = os.path.join(AGENTS_DIR, pkg_name)
    for root, _, files in sorted(os.walk(pkg_path)):
        for fname in sorted(files):
            if fname.endswith(".py"):
                with open(os.path.join(root, fname), "rb") as f:
                    h.update(f.read())
    if extra_salt:
        h.update(extra_salt.encode("utf-8"))
    return h.hexdigest()[:10]


AGENT_1_CONFIG_VERSION = f"v6-{_compute_pkg_hash('agent1_github_secops', CONTINUE_URI)}"
AGENT_2_CONFIG_VERSION = f"v4-{_compute_pkg_hash('agent2_internal_sre')}"

AGENT_REQUIREMENTS = [
    "google-cloud-aiplatform[agent_engines,adk]>=1.112.0",
    "google-adk>=1.15.0",
    "mcp>=1.6.0,<2.0.0",
    "aiohttp>=3.9.0",
    "google-auth[aiohttp]>=2.35.0",
    "httpx>=0.27.0",
    "cloudpickle>=3.0.0",
    "pydantic>=2.0.0",
]


def _ensure_prerequisites() -> None:
    print("[0/3] Verifying Vertex AI & Agent Gateway Service Agent IAM bindings & GCS staging buckets...")
    cmds = [
        f"gcloud -q iam roles create ar_agw_cross_project_sa --project={PROJECT_A_ID} "
        "--permissions=networkservices.agentGateways.get,networkservices.agentGateways.use,networkservices.operations.get "
        f"|| gcloud -q iam roles update ar_agw_cross_project_sa --project={PROJECT_A_ID} "
        "--permissions=networkservices.agentGateways.get,networkservices.agentGateways.use,networkservices.operations.get",
        f"gcloud -q beta services identity create --service=aiplatform.googleapis.com --project={PROJECT_A_ID}",
        f"gcloud -q beta services identity create --service=aiplatform.googleapis.com --project={PROJECT_B_ID}",
        f"gcloud -q beta services identity create --service=networkservices.googleapis.com --project={PROJECT_A_ID}",
        f"gcloud -q beta services identity create --service=networkservices.googleapis.com --project={PROJECT_B_ID}",
        f"gcloud -q projects add-iam-policy-binding {PROJECT_A_ID} "
        f"--member=serviceAccount:service-{PROJECT_A_NUM}@gcp-sa-aiplatform.iam.gserviceaccount.com "
        f"--role=projects/{PROJECT_A_ID}/roles/ar_agw_cross_project_sa",
        f"gcloud -q projects add-iam-policy-binding {PROJECT_A_ID} "
        f"--member=serviceAccount:service-{PROJECT_B_NUM}@gcp-sa-aiplatform.iam.gserviceaccount.com "
        f"--role=projects/{PROJECT_A_ID}/roles/ar_agw_cross_project_sa",
        f"gcloud -q projects add-iam-policy-binding {PROJECT_A_ID} "
        f"--member=serviceAccount:service-{PROJECT_A_NUM}@gcp-sa-aiplatform-re.iam.gserviceaccount.com "
        f"--role=projects/{PROJECT_A_ID}/roles/ar_agw_cross_project_sa",
        f"gcloud -q projects add-iam-policy-binding {PROJECT_A_ID} "
        f"--member=serviceAccount:service-{PROJECT_B_NUM}@gcp-sa-aiplatform-re.iam.gserviceaccount.com "
        f"--role=projects/{PROJECT_A_ID}/roles/ar_agw_cross_project_sa",
        f"gcloud -q projects add-iam-policy-binding {PROJECT_A_ID} "
        f"--member=serviceAccount:service-{PROJECT_B_NUM}@gcp-sa-aiplatform-re.iam.gserviceaccount.com "
        "--role=roles/networkservices.viewer",
        f"gcloud -q projects add-iam-policy-binding {PROJECT_B_ID} "
        f"--member=serviceAccount:service-{PROJECT_A_NUM}@gcp-sa-dep.iam.gserviceaccount.com "
        "--role=roles/compute.networkUser",
        f"gcloud -q projects add-iam-policy-binding {PROJECT_B_ID} "
        f"--member=serviceAccount:service-{PROJECT_A_NUM}@gcp-sa-dep.iam.gserviceaccount.com "
        "--role=roles/dns.peer",
        f"gcloud -q storage buckets create {STAGING_BUCKET_A} --project={PROJECT_A_ID} --location={REGION}",
        f"gcloud -q storage buckets create {STAGING_BUCKET_B} --project={PROJECT_B_ID} --location={REGION}",
    ]
    for cmd in cmds:
        subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def _make_client(project_id: str) -> vertexai.Client:
    return vertexai.Client(
        project=project_id,
        location=REGION,
        http_options=dict(
            api_version="v1beta1",
            headers={"Connection": "close"},
        ),
    )


def _find_existing_agent(project_id: str, display_name: str):
    client = _make_client(project_id)
    for agent in client.agent_engines.list():
        res = agent.api_resource
        if getattr(res, "display_name", None) == display_name:
            return agent
    return None


def _wait_for_effective_identity(project_id: str, display_name: str):
    for poll in range(1, 40):
        try:
            agent = _find_existing_agent(project_id, display_name)
            if agent and getattr(agent.api_resource, "spec", None):
                eff_id = getattr(agent.api_resource.spec, "effective_identity", None)
                if eff_id:
                    return agent
            print(
                f"  [Poll {poll}/40] Waiting 15s for '{display_name}' provisioning to complete..."
            )
        except Exception as exc:
            print(f"  [Poll {poll}/40] Transient network error ({exc}); retrying in 15s...")
        time.sleep(15)
    raise RuntimeError(f"Timed out waiting for '{display_name}' in {project_id}")


def _has_env_var(agent, key: str, expected_val: str) -> bool:
    try:
        env_list = getattr(
            getattr(getattr(agent, "api_resource", None), "spec", None),
            "deployment_spec",
            None,
        )
        env_items = getattr(env_list, "env", None) or []
        for item in env_items:
            name = getattr(item, "name", None) or (item.get("name") if isinstance(item, dict) else None)
            val = getattr(item, "value", None) or (item.get("value") if isinstance(item, dict) else None)
            if name == key and val == expected_val:
                return True
    except Exception:
        pass
    return False


def _wait_for_agent_update(
    project_id: str, display_name: str, expected_project: str, expected_version: str
):
    for poll in range(1, 40):
        try:
            agent = _find_existing_agent(project_id, display_name)
            if (
                agent
                and _has_env_var(agent, "AGENT_PROJECT_ID", expected_project)
                and _has_env_var(agent, "AGENT_CONFIG_VERSION", expected_version)
            ):
                return agent
            print(
                f"  [Poll {poll}/40] Waiting 15s for '{display_name}' in-place update ({expected_version}) to complete..."
            )
        except Exception as exc:
            print(f"  [Poll {poll}/40] Transient network error ({exc}); retrying in 15s...")
        time.sleep(15)
    raise RuntimeError(f"Timed out waiting for '{display_name}' update in {project_id}")


def _get_or_create_agent(project_id: str, agent_app, config: dict):
    os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
    os.environ["GOOGLE_CLOUD_LOCATION"] = REGION
    vertexai.init(project=project_id, location=REGION)
    if hasattr(agent_app, "_tmpl_attrs"):
        agent_app._tmpl_attrs["project"] = project_id
        agent_app._tmpl_attrs["location"] = REGION
    display_name = config["display_name"]
    expected_version = config["env_vars"]["AGENT_CONFIG_VERSION"]
    existing = _find_existing_agent(project_id, display_name)
    if existing is not None:
        eff_id = getattr(getattr(existing.api_resource, "spec", None), "effective_identity", None)
        if not eff_id:
            print(f"  -> '{display_name}' is currently provisioning in {project_id}; waiting for completion...")
            existing = _wait_for_effective_identity(project_id, display_name)
        if (
            _has_env_var(existing, "AGENT_PROJECT_ID", project_id)
            and _has_env_var(existing, "AGENT_CONFIG_VERSION", expected_version)
        ):
            print(f"  -> Found up-to-date '{display_name}' ({expected_version}) in {project_id}, reusing it.")
            return existing
        print(
            f"  -> Updating existing '{display_name}' in {project_id} in-place to {expected_version} "
            f"(preserving Reasoning Engine ID & SPIFFE ID)..."
        )
        update_config = {
            "staging_bucket": config["staging_bucket"],
            "requirements": config["requirements"],
            "extra_packages": config["extra_packages"],
            "env_vars": config["env_vars"],
        }
        client = _make_client(project_id)
        try:
            return client.agent_engines.update(
                name=existing.api_resource.name,
                agent=agent_app,
                config=update_config,
            )
        except Exception as exc:
            err_str = str(exc)
            if any(
                k in err_str
                for k in ("ConnectionError", "ConnectionResetError", "Connection aborted", "ProtocolError", "reset by peer")
            ):
                print(
                    f"  -> Connection reset while polling update LRO for '{display_name}'. "
                    "Resuming status poll on server-side operation..."
                )
                return _wait_for_agent_update(project_id, display_name, project_id, expected_version)
            raise

    for attempt in range(1, 9):
        client = _make_client(project_id)
        try:
            return client.agent_engines.create(agent=agent_app, config=config)
        except Exception as exc:
            err_str = str(exc)
            if "Permission denied to get Agent Gateway" in err_str and attempt < 8:
                print(
                    f"  [Attempt {attempt}/8] Waiting 15s for Vertex AI Service Agent "
                    "IAM permission propagation on Agent Gateway..."
                )
                time.sleep(15)
            elif any(
                k in err_str
                for k in ("ConnectionError", "ConnectionResetError", "Connection aborted", "ProtocolError", "reset by peer")
            ):
                print(
                    f"  -> Connection reset while polling LRO for '{display_name}'. "
                    "Resuming status poll on server-side operation..."
                )
                return _wait_for_effective_identity(project_id, display_name)
            else:
                raise


def main() -> None:
    global CONTINUE_URI, AGENT_1_CONFIG_VERSION
    _ensure_3lo_callback_service()
    CONTINUE_URI = _resolve_continue_uri()
    AGENT_1_CONFIG_VERSION = f"v6-{_compute_pkg_hash('agent1_github_secops', CONTINUE_URI)}"
    _ensure_prerequisites()
    print(f"[1/3] Deploying/Updating Agent 1 (SecOps + GitHub 3LO) in {PROJECT_A_ID}...")
    remote_agent_1 = _get_or_create_agent(
        PROJECT_A_ID,
        agent1_app,
        {
            "display_name": "secops-github-orchestrator",
            "identity_type": types.IdentityType.AGENT_IDENTITY,
            "staging_bucket": STAGING_BUCKET_A,
            "agent_gateway_config": {
                "agent_to_anywhere_config": {
                    "agent_gateway": AGW_URI,
                }
            },
            "requirements": AGENT_REQUIREMENTS,
            "extra_packages": ["./agent1_github_secops"],
            "env_vars": {
                "AGENT_PROJECT_ID": PROJECT_A_ID,
                "AGENT_CONFIG_VERSION": AGENT_1_CONFIG_VERSION,
                "PROJECT_A_ID": PROJECT_A_ID,
                "REGION": REGION,
                "INTERNAL_MCP_URL": INTERNAL_MCP_URL,
                "OAUTH_CONTINUE_URI": CONTINUE_URI,
                "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY": "false",
                "GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES": "False",
            },
        },
    )
    agent_1_spiffe = f"principal://{remote_agent_1.api_resource.spec.effective_identity}"
    re_1_id = remote_agent_1.api_resource.name.split("/")[-1]
    print(f"  -> Agent 1 Resource: {remote_agent_1.api_resource.name}")
    print(f"  -> Agent 1 SPIFFE:   {agent_1_spiffe}")

    print(f"\n[2/3] Deploying/Updating Agent 2 (Internal SecOps Auditor) in {PROJECT_B_ID}...")
    remote_agent_2 = _get_or_create_agent(
        PROJECT_B_ID,
        agent2_app,
        {
            "display_name": "internal-secops-auditor",
            "identity_type": types.IdentityType.AGENT_IDENTITY,
            "staging_bucket": STAGING_BUCKET_B,
            "agent_gateway_config": {
                "agent_to_anywhere_config": {
                    "agent_gateway": AGW_URI,
                }
            },
            "requirements": AGENT_REQUIREMENTS,
            "extra_packages": ["./agent2_internal_sre"],
            "env_vars": {
                "AGENT_PROJECT_ID": PROJECT_B_ID,
                "AGENT_CONFIG_VERSION": AGENT_2_CONFIG_VERSION,
                "PROJECT_A_ID": PROJECT_A_ID,
                "PROJECT_B_ID": PROJECT_B_ID,
                "REGION": REGION,
                "INTERNAL_MCP_URL": INTERNAL_MCP_URL,
                "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY": "false",
                "GOOGLE_API_PREVENT_AGENT_TOKEN_SHARING_FOR_GCP_SERVICES": "False",
            },
        },
    )
    agent_2_spiffe = f"principal://{remote_agent_2.api_resource.spec.effective_identity}"
    re_2_id = remote_agent_2.api_resource.name.split("/")[-1]
    print(f"  -> Agent 2 Resource: {remote_agent_2.api_resource.name}")
    print(f"  -> Agent 2 SPIFFE:   {agent_2_spiffe}")

    env_contents = (
        f'export AGENT_1_SPIFFE="{agent_1_spiffe}"\n'
        f'export AGENT_2_SPIFFE="{agent_2_spiffe}"\n'
        f'export RE_1_ID="{re_1_id}"\n'
        f'export RE_2_ID="{re_2_id}"\n'
    )
    print("\n--- Copy & Export These SPIFFE Principals ---")
    print(env_contents.strip())

    for env_path in (
        "/tmp/spiffe_identities.env",
        os.path.join(ORIG_AGENTS_DIR, "spiffe_identities.env"),
    ):
        try:
            with open(env_path, "w", encoding="utf-8") as f:
                f.write(env_contents)
            print(f"Saved SPIFFE identities to {env_path}")
        except OSError:
            pass

    # Ensure IAP v2 Agent Registry parent policy only grants external MCP egress to Agent 1 (not wildcard)
    try:
        import json

        iap_parent_pol = {
            "bindings": [
                {
                    "role": "roles/iap.egressor",
                    "members": [agent_1_spiffe],
                }
            ]
        }
        with open("/tmp/iap_parent_policy.json", "w", encoding="utf-8") as f:
            json.dump(iap_parent_pol, f, indent=2)
        subprocess.run(
            [
                "gcloud",
                "-q",
                "alpha",
                "iap",
                "web",
                "set-iam-policy",
                "/tmp/iap_parent_policy.json",
                f"--project={PROJECT_A_ID}",
                f"--region={REGION}",
                "--resource-type=agent-registry",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:
        pass

    _verify_live_agents_and_logs(re_1_id, re_2_id)


def _verify_live_agents_and_logs(re_1_id: str, re_2_id: str) -> None:
    from vertexai import agent_engines

    print("\n==================================================================")
    print("[3/3] Running Live Smoke Test on Agent 1 & Agent 2 + Verifying Cloud Logs...")
    print("==================================================================")

    # 1. Enable allServices DATA_READ / DATA_WRITE audit logs on Project A so Auth Manager logs to cloudaudit.googleapis.com/data_access
    try:
        import json

        pol = json.loads(
            subprocess.check_output(
                ["gcloud", "projects", "get-iam-policy", PROJECT_A_ID, "--format=json"],
                stderr=subprocess.DEVNULL,
                text=True,
            )
        )
        cfgs = [c for c in pol.get("auditConfigs", []) if c.get("service") != "allServices"]
        cfgs.append(
            {
                "service": "allServices",
                "auditLogConfigs": [
                    {"logType": "ADMIN_READ"},
                    {"logType": "DATA_READ"},
                    {"logType": "DATA_WRITE"},
                ],
            }
        )
        pol["auditConfigs"] = cfgs
        with open("/tmp/audit_policy.json", "w", encoding="utf-8") as f:
            json.dump(pol, f, indent=2)
        subprocess.run(
            ["gcloud", "projects", "set-iam-policy", PROJECT_A_ID, "/tmp/audit_policy.json", "--quiet"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:
        pass

    # 2. Smoke-test Agent 1 (Internal SecOps MCP + 3LO GitHub Auth Manager + Playground Session ID handling)
    print(f"\n-> Testing Agent 1 ({re_1_id}) stream_query (Internal MCP + 3LO Auth Manager + Playground Session ID)...")
    vertexai.init(project=PROJECT_A_ID, location=REGION)
    a1 = agent_engines.get(f"projects/{PROJECT_A_NUM}/locations/{REGION}/reasoningEngines/{re_1_id}")
    for ev in a1.stream_query(
        user_id="vais-query-reasoning-engine",
        session_id="playground-session-verify-1",
        message="Check active CVE alerts for payment-service and call get_github_authenticated_user to check my GitHub profile.",
    ):
        content = ev.get("content", {}) if isinstance(ev, dict) else {}
        for part in content.get("parts", []):
            if "function_call" in part:
                print(f"   [Agent 1 Tool Call]: {part['function_call'].get('name')}({part['function_call'].get('args')})")
            if "function_response" in part:
                print(f"   [Agent 1 Tool Resp]: {part['function_response'].get('name')} -> {str(part['function_response'].get('response'))[:240]}")
            if "text" in part:
                print(f"   [Agent 1 Reply]:     {part['text'].strip()}")

    # 3. Smoke-test Agent 2 (IAP v2 403 block on GitHub MCP + Agent Gateway 403 block on example.com)
    print(f"\n-> Testing Agent 2 ({re_2_id}) stream_query (IAP v2 403 DENIED + Default-Deny 403)...")
    vertexai.init(project=PROJECT_B_ID, location=REGION)
    a2 = agent_engines.get(f"projects/{PROJECT_B_NUM}/locations/{REGION}/reasoningEngines/{re_2_id}")
    for ev in a2.stream_query(
        user_id="secops-auditor-test",
        session_id="playground-session-verify-2",
        message="Call test_egress_policy_check on https://api.githubcopilot.com/mcp/ and on https://example.com and report the HTTP status codes.",
    ):
        content = ev.get("content", {}) if isinstance(ev, dict) else {}
        for part in content.get("parts", []):
            if "function_call" in part:
                print(f"   [Agent 2 Tool Call]: {part['function_call'].get('name')}({part['function_call'].get('args')})")
            if "function_response" in part:
                print(f"   [Agent 2 Tool Resp]: {part['function_response'].get('name')} -> {part['function_response'].get('response')}")
            if "text" in part:
                print(f"   [Agent 2 Reply]:     {part['text'].strip()}")

    time.sleep(5)
    print("\n-> Fetching Recent Agent 1 [AUTH_MANAGER_3LO] & [INTERNAL_MCP] Logs from Cloud Logging:")
    subprocess.run(
        [
            "gcloud",
            "logging",
            "read",
            f'resource.type="aiplatform.googleapis.com/ReasoningEngine" AND resource.labels.reasoning_engine_id="{re_1_id}" AND (textPayload:"[AUTH_MANAGER_3LO]" OR textPayload:"[INTERNAL_MCP]")',
            f"--project={PROJECT_A_ID}",
            "--limit=8",
            "--format=table(timestamp,textPayload)",
        ],
        check=False,
    )

    print("\n-> Fetching Recent Agent Gateway + IAP v2 Logs (ALLOWED & DENIED) from Cloud Logging:")
    subprocess.run(
        [
            "gcloud",
            "logging",
            "read",
            'resource.type="networkservices.googleapis.com/Gateway" AND resource.labels.gateway_name="enterprise-egress-agw"',
            f"--project={PROJECT_A_ID}",
            "--limit=10",
            "--format=table(timestamp,httpRequest.status,jsonPayload.authzPolicyInfo.result:label=IAP_AUTHZ,jsonPayload.agentGatewayInfo.mcpInfo.method:label=MCP_METHOD,jsonPayload.agentGatewayInfo.mcpInfo.parameter:label=TOOL_NAME,httpRequest.serverIp,httpRequest.requestUrl)",
        ],
        check=False,
    )


if __name__ == "__main__":
    main()
