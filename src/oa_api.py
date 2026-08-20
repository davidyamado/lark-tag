"""Internal OA platform API — fetches per-user OpenRouter keys."""
import json
import logging
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

_OA_BASE = "https://aq.yostar.net"


def get_user_personal_key(email: str, oa_api_key: str) -> str:
    """
    Fetch the first active OpenRouter key (plaintext) for the given email.

    Returns the plaintext key string, or "" if none found / any error.
    Single round-trip using include_key=true.
    """
    if not email or not oa_api_key:
        return ""
    url = f"{_OA_BASE}/api/v2/openrouter/oa/user-keys?{urllib.parse.urlencode({'email': email, 'include_key': 'true'})}"
    req = urllib.request.Request(url, headers={"X-OA-Api-Key": oa_api_key})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    if data.get("code") != 0:
        raise RuntimeError(f"OA API error: {data}")
    for k in data.get("data", {}).get("keys", []):
        if k.get("status") == "active" and k.get("key"):
            return k["key"]
    return ""
