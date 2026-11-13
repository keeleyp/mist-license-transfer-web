"""
Mist license logic shared by the web app.

Copied from the Mist License Transfer CLI (keeleyp/mist-license-transfer,
v1.3.0) and kept behaviourally identical - config format, API calls, which
licenses/amendments are listed - but with the terminal I/O removed: every
user-facing failure is raised as TransferError and warnings go to `log`.
"""

import configparser
import json
import logging
from datetime import datetime
from pathlib import Path

import requests

REQUEST_TIMEOUT = 20

log = logging.getLogger("mist_license_transfer_web")


class TransferError(Exception):
    """Raised for any user-facing failure."""


# --------------------------------------------------------------------------
# Config (same .ini format as the CLI)
# --------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    if not path.exists():
        raise TransferError(f"Config file not found: {path.name}")

    parser = configparser.ConfigParser()
    try:
        parser.read(path)
    except configparser.Error as e:
        raise TransferError(f"{path.name} could not be parsed: {e}")

    required = {
        "mist": ["base_url", "api_token"],
        "source": ["org_id"],
        "destinations": ["org_ids"],
    }
    missing = []
    for section, keys in required.items():
        if section not in parser:
            missing.append(f"[{section}] section")
            continue
        for key in keys:
            if not parser[section].get(key, "").strip():
                missing.append(f"[{section}] {key}")
    if missing:
        raise TransferError(f"{path.name} is missing required values: " + ", ".join(missing))

    dest_org_ids = [
        org_id.strip()
        for org_id in parser["destinations"]["org_ids"].replace("\n", ",").split(",")
        if org_id.strip()
    ]
    if not dest_org_ids:
        raise TransferError(f"{path.name}: [destinations] org_ids must list at least one destination org ID.")

    verify, proxies = _parse_network_settings(parser, path)

    # Undocumented on purpose (not in the README or .ini.example): moves out to
    # orgs this API token can't read are listed for information only, unless
    # `allow_unknown_org_returns = true` is added to the [mist] section.
    try:
        allow_unknown = parser["mist"].getboolean("allow_unknown_org_returns", fallback=False)
    except ValueError:
        raise TransferError(f"{path.name}: [mist] allow_unknown_org_returns must be true or false.")

    return {
        "name": path.name,
        "allow_unknown_org_returns": allow_unknown,
        "base_url": parser["mist"]["base_url"].strip().rstrip("/"),
        "api_token": parser["mist"]["api_token"].strip(),
        "source_org_id": parser["source"]["org_id"].strip(),
        "dest_org_ids": dest_org_ids,
        "verify": verify,
        "proxies": proxies,
    }


def _parse_network_settings(parser: configparser.ConfigParser, path: Path):
    """Optional [network] section for TLS-inspecting proxies (e.g. Zscaler).
    With no [network] section, behaviour is unchanged (system CA store,
    env-var proxies)."""
    if "network" not in parser:
        return True, {}

    section = parser["network"]
    ca_bundle = section.get("ca_bundle", "").strip()
    try:
        verify_ssl = section.getboolean("verify_ssl", fallback=True)
    except ValueError:
        raise TransferError(f"{path.name}: [network] verify_ssl must be true or false.")

    if ca_bundle:
        ca_path = Path(ca_bundle).expanduser()
        if not ca_path.is_file():
            raise TransferError(
                f"{path.name}: [network] ca_bundle does not exist: {ca_path}. "
                f"This should be a PEM file containing your proxy's root CA certificate "
                f"(e.g. exported from Zscaler)."
            )
        verify = str(ca_path)
    elif not verify_ssl:
        verify = False
    else:
        verify = True

    proxies = {}
    http_proxy = section.get("http_proxy", "").strip()
    https_proxy = section.get("https_proxy", "").strip()
    if http_proxy:
        proxies["http"] = http_proxy
    if https_proxy:
        proxies["https"] = https_proxy
    return verify, proxies


def describe_config(path: Path) -> str:
    """Short label for the config picker: the source org ID from the file."""
    parser = configparser.ConfigParser()
    try:
        parser.read(path)
        org_id = parser.get("source", "org_id", fallback="").strip()
    except configparser.Error:
        return "(unreadable)"
    return f"source org {org_id}" if org_id else "(no [source] org_id)"


# --------------------------------------------------------------------------
# API helpers
# --------------------------------------------------------------------------

def build_session(cfg: dict) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Token {cfg['api_token']}",
        "Content-Type": "application/json",
    })
    session.verify = cfg["verify"]
    if cfg["proxies"]:
        session.proxies.update(cfg["proxies"])
    if cfg["verify"] is False:
        log.warning("%s: TLS certificate verification is DISABLED ([network] verify_ssl = false).", cfg["name"])
        requests.packages.urllib3.disable_warnings(requests.packages.urllib3.exceptions.InsecureRequestWarning)
    return session


def api_request(session: requests.Session, method: str, url: str, **kwargs):
    """session.request with consistent error handling. Returns parsed JSON."""
    try:
        response = session.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
        response.raise_for_status()
        if not response.content:
            return {}
        return response.json()
    except requests.exceptions.SSLError as e:
        raise TransferError(
            f"TLS certificate verification failed for {url}: {e}. "
            f"If you're behind a TLS-inspecting proxy (e.g. Zscaler), set [network] ca_bundle "
            f"in the config to the path of its root CA certificate (PEM format)."
        )
    except requests.exceptions.ProxyError as e:
        raise TransferError(f"Could not reach the proxy for {url}: {e}. Check [network] http_proxy/https_proxy in the config.")
    except requests.exceptions.Timeout:
        raise TransferError(f"Request to {url} timed out after {REQUEST_TIMEOUT}s.")
    except requests.exceptions.ConnectionError:
        raise TransferError(f"Could not connect to {url}. Check the network and the base_url in the config.")
    except requests.exceptions.HTTPError as e:
        detail = _extract_error_detail(e.response)
        raise TransferError(f"HTTP {e.response.status_code} from {url}{detail}")
    except (json.JSONDecodeError, ValueError):
        raise TransferError(f"Unexpected (non-JSON) response from {url}.")
    except requests.exceptions.RequestException as e:
        raise TransferError(f"Request to {url} failed: {e}")


def _extract_error_detail(response) -> str:
    if response is None:
        return ""
    try:
        return f"\n{json.dumps(response.json(), indent=2)}"
    except (json.JSONDecodeError, ValueError):
        text = response.text.strip()
        return f"\n{text}" if text else ""


def get_source_org_name(session: requests.Session, cfg: dict) -> str:
    """The source org must be readable - every listing and change goes through
    it - so fail straight away with a clear reason."""
    org_id = cfg["source_org_id"]
    try:
        data = api_request(session, "GET", f"{cfg['base_url']}/orgs/{org_id}")
    except TransferError as e:
        first_line = str(e).splitlines()[0]
        if "HTTP 401" in first_line:
            raise TransferError(
                f"The API token in {cfg['name']} was rejected as invalid ({first_line}). "
                f"It may have been deleted or expired - create a new one in Mist and update [mist] api_token."
            )
        if "HTTP 403" in first_line or "HTTP 404" in first_line:
            raise TransferError(
                f"The API token in {cfg['name']} can't access the source org {org_id} ({first_line}). "
                f"Check that [source] org_id is correct, and that the account behind the token "
                f"has a role on that org (directly, or through an MSP the org belongs to)."
            )
        raise
    return data.get("name") or "Unknown"


def resolve_org_names(session: requests.Session, base_url: str, org_ids, cache: dict) -> dict:
    """Fills `cache` with names for any org IDs not already in it. Orgs the
    token can't read are cached as None (shown by ID)."""
    for org_id in org_ids:
        if org_id in cache:
            continue
        try:
            cache[org_id] = api_request(session, "GET", f"{base_url}/orgs/{org_id}").get("name")
        except TransferError:
            cache[org_id] = None
    return cache


def get_licenses(session: requests.Session, cfg: dict) -> dict:
    return api_request(session, "GET", f"{cfg['base_url']}/orgs/{cfg['source_org_id']}/licenses") or {}


def amend(session: requests.Session, cfg: dict, subscription_id: str, dst_org_id: str, quantity: int):
    payload = {"op": "amend", "subscription_id": subscription_id, "dst_org_id": dst_org_id, "quantity": quantity}
    return api_request(session, "PUT", f"{cfg['base_url']}/orgs/{cfg['source_org_id']}/licenses", json=payload)


def unamend(session: requests.Session, cfg: dict, amendment_id: str):
    payload = {"op": "unamend", "amendment_id": amendment_id}
    return api_request(session, "PUT", f"{cfg['base_url']}/orgs/{cfg['source_org_id']}/licenses", json=payload)


# --------------------------------------------------------------------------
# Data shaping (identical rules to the CLI)
# --------------------------------------------------------------------------

def collect_valid_licenses(licenses_data: dict) -> list:
    """Unexpired subscriptions (active now, or starting in the future) that
    still have capacity left in the source org. `remaining_quantity` is what's
    left after earlier moves out; older responses without it fall back to
    quantity + the negative amendments for that subscription."""
    now = datetime.now().timestamp()

    moved_out = {}
    for amendment in licenses_data.get("amendments", []):
        qty = amendment.get("quantity", 0)
        if qty < 0:
            sub_id = amendment.get("subscription_id")
            moved_out[sub_id] = moved_out.get(sub_id, 0) + qty

    result = []
    for lic in licenses_data.get("licenses", []):
        start = lic.get("start_time", 0)
        end = lic.get("end_time", 0)
        sub_id = lic.get("subscription_id")
        if not sub_id or end < now:
            continue
        quantity = lic.get("quantity", 0)
        remaining = lic.get("remaining_quantity")
        if remaining is None:
            remaining = quantity + moved_out.get(sub_id, 0)
        if remaining <= 0:
            continue
        result.append({
            "subscription_id": sub_id,
            "type": lic.get("type", "unknown"),
            "quantity": quantity,
            "remaining": remaining,
            "start_time": start,
            "end_time": end,
            "status": "Active" if start <= now else "Not Started",
        })
    return result


def collect_returnable_amendments(licenses_data: dict) -> list:
    """Unexpired moves out of the source org (negative amendments) to any
    destination org - each can be reversed with an unamend."""
    now = datetime.now().timestamp()
    result = []
    for amendment in licenses_data.get("amendments", []):
        if (
            amendment.get("quantity", 0) < 0
            and amendment.get("dst_org_id")
            and amendment.get("end_time", 0) >= now
        ):
            result.append({
                "amendment_id": amendment.get("id"),
                "subscription_id": amendment.get("subscription_id"),
                "type": amendment.get("type", "unknown"),
                "quantity": amendment.get("quantity", 0),
                "dst_org_id": amendment.get("dst_org_id"),
                "start_time": amendment.get("start_time", 0),
                "end_time": amendment.get("end_time", 0),
            })
    return result


def fmt_date(ts) -> str:
    if not ts:
        return "N/A"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return "N/A"
