#!/usr/bin/env python3
"""
Strip credentials, tokens and personal details out of a HAR capture
before sharing it anywhere.

This intentionally over-redacts: any JSON key that even loosely looks
identifying/secret has its value replaced, any header/cookie that could
carry a session or auth token is wiped, and any string that merely looks
like a JWT is wiped regardless of where it appears. What's left is the
*shape* of the API (URLs, methods, field names, numeric usage/date/cost
values) -- enough to design an integration against, without your
credentials, session tokens, or personal details in it.

Also drops non-API noise (JS/CSS/images/fonts/HTML navigation) and
truncates huge arrays/strings so the result stays reviewable.

Usage:
    python3 sanitize_har.py origin_traffic.har
    # -> writes origin_traffic.sanitized.json

Skim the output yourself before sending it anywhere -- this is a best
effort filter, not a guarantee.
"""
import base64
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

SENSITIVE_KEY_RE = re.compile(
    r"(pass|pwd|token|secret|auth|cookie|session|jwt|ssn|dob|birth|"
    r"medicare|card|cvv|email|phone|mobile|address|addr|suburb|"
    r"postcode|zip|name|nmi|mirn|serial|account|customer|contact|"
    r"user|holder|identity|licen[cs]e|\bid\b)",
    re.IGNORECASE,
)
SENSITIVE_HEADER_NAMES = {
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
    "x-csrf-token",
    "x-xsrf-token",
    "proxy-authorization",
}
JWT_RE = re.compile(r"^[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}$")
MAX_LIST_ITEMS = 5
MAX_STRING_LEN = 500


def redact_value(v):
    if isinstance(v, str) and JWT_RE.match(v):
        return "REDACTED_JWT"
    if isinstance(v, str) and len(v) > MAX_STRING_LEN:
        return v[:300] + f"...[truncated, {len(v)} chars total]"
    return v


GRAPHQL_TEXT_KEYS = {"query", "extensions"}


def scrub_json(obj, key=None):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in GRAPHQL_TEXT_KEYS and isinstance(v, str):
                # GraphQL query documents are just field-selection syntax --
                # the API shape we actually want -- not sensitive data.
                out[k] = v
            elif SENSITIVE_KEY_RE.search(k):
                out[k] = f"REDACTED_{type(v).__name__.upper()}"
            else:
                out[k] = scrub_json(v, k)
        return out
    if isinstance(obj, list):
        scrubbed = [scrub_json(item, key) for item in obj]
        if len(scrubbed) > MAX_LIST_ITEMS:
            head = scrubbed[:3]
            tail = scrubbed[-2:]
            omitted = len(scrubbed) - 5
            return head + [f"...[{omitted} items omitted]..."] + tail
        return scrubbed
    return redact_value(obj)


def scrub_json_text(text):
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    return json.dumps(scrub_json(parsed), indent=2)


def scrub_url(url):
    parts = urlsplit(url)
    q = parse_qsl(parts.query, keep_blank_values=True)
    new_q = [
        (k, "REDACTED" if SENSITIVE_KEY_RE.search(k) or JWT_RE.match(v) else v)
        for k, v in q
    ]
    return urlunsplit(parts._replace(query=urlencode(new_q)))


def scrub_headers(headers):
    out = []
    for h in headers or []:
        name, value = h.get("name", ""), h.get("value", "")
        if name.lower() in SENSITIVE_HEADER_NAMES or JWT_RE.match(value):
            value = "REDACTED"
        out.append({"name": name, "value": value})
    return out


def scrub_cookies(cookies):
    return [{"name": c.get("name", ""), "value": "REDACTED"} for c in cookies or []]


def scrub_content(content):
    if not content:
        return content
    text = content.get("text")
    if text is None:
        return {"mimeType": content.get("mimeType"), "size": content.get("size")}
    if content.get("encoding") == "base64":
        try:
            text = base64.b64decode(text).decode("utf-8", errors="replace")
        except Exception:
            return {"mimeType": content.get("mimeType"), "note": "binary content omitted"}
    scrubbed_json = scrub_json_text(text)
    if scrubbed_json is not None:
        return {"mimeType": content.get("mimeType"), "text": scrubbed_json}
    if len(text) > MAX_STRING_LEN:
        text = text[:300] + f"...[truncated, {len(text)} chars total]"
    return {"mimeType": content.get("mimeType"), "text": text}


AUTH_URL_HINTS = ("auth0", "authorize", "oauth", "/login", "/token", "/callback")


def is_api_entry(entry):
    req = entry["request"]
    res = entry["response"]
    mime = (res.get("content", {}) or {}).get("mimeType", "")
    url = req.get("url", "").lower()
    if "json" in mime or "graphql" in url or "/api" in url:
        return True
    if req.get("method") in ("POST", "PUT", "PATCH", "DELETE"):
        return True
    if any(hint in url for hint in AUTH_URL_HINTS):
        return True
    return False


def scrub_entry(entry):
    req, res = entry["request"], entry["response"]
    post_data = req.get("postData")
    scrubbed_post = None
    if post_data and post_data.get("text"):
        scrubbed_json = scrub_json_text(post_data["text"])
        scrubbed_post = scrubbed_json if scrubbed_json is not None else "[non-JSON body omitted]"

    return {
        "method": req.get("method"),
        "url": scrub_url(req.get("url", "")),
        "requestHeaders": scrub_headers(req.get("headers")),
        "requestCookies": scrub_cookies(req.get("cookies")),
        "postData": scrubbed_post,
        "status": res.get("status"),
        "responseHeaders": scrub_headers(res.get("headers")),
        "responseCookies": scrub_cookies(res.get("cookies")),
        "content": scrub_content(res.get("content")),
    }


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 sanitize_har.py <path-to.har>")
        sys.exit(1)

    src = Path(sys.argv[1])
    har = json.loads(src.read_text())
    entries = har["log"]["entries"]

    kept = [scrub_entry(e) for e in entries if is_api_entry(e)]

    out_path = src.with_suffix("").with_suffix(".sanitized.json")
    out_path.write_text(json.dumps(kept, indent=2))

    print(f"Kept {len(kept)} of {len(entries)} captured requests (API-like only).")
    print(f"Wrote {out_path}")
    print("\nQuick summary of what was captured:")
    seen = set()
    for e in kept:
        key = (e["method"], urlsplit(e["url"]).netloc, urlsplit(e["url"]).path)
        if key not in seen:
            seen.add(key)
            print(f"  {key[0]:6s} {key[1]}{key[2]}")

    print(
        "\nSkim the output file yourself before sharing it -- this is a best-effort "
        "filter, not a guarantee. Look especially for anything under fields this "
        "script wouldn't recognise as sensitive."
    )


if __name__ == "__main__":
    main()
