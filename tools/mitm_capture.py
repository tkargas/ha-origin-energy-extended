"""
mitmproxy addon: capture originenergy.com.au / auth0 traffic to a HAR
file, full fidelity, with no browser devtools involved.

Origin's bot defense appears to detect an open devtools panel and fail
login silently, so this sidesteps devtools entirely: your browser runs
completely normally (no panel, no automation flags) and just has its
network traffic routed through a local proxy that mitmproxy uses to
inspect it.

Setup:
    pip install mitmproxy
    mitmdump -s mitm_capture.py -p 8080

Then, in a normal (non-automated) browser:
  1. Set the browser's (or OS) proxy to 127.0.0.1:8080.
  2. Visit http://mitm.it while proxied and install/trust the mitmproxy
     CA certificate for your OS/browser (one-time setup).
  3. Browse to originenergy.com.au and log in / view usage as normal.
     Use a browser window with nothing else sensitive open -- everything
     that browser does while proxied passes through this script, though
     only originenergy.com.au/auth0 traffic is written to disk.
  4. Ctrl+C the mitmdump process. This writes origin_mitm.har here.

Then run:
    python3 sanitize_har.py origin_mitm.har

Afterwards, turn the browser's proxy setting back off.
"""
import json
from pathlib import Path

from mitmproxy import http

OUT = Path(__file__).parent / "origin_mitm.har"
ONLY_HOSTS = ("originenergy.com.au", "auth0.com")

entries = []


def _headers(h):
    return [{"name": k, "value": v} for k, v in h.items(multi=True)]


def _body(message):
    try:
        text = message.get_text(strict=False)
    except Exception:
        text = None
    if not text:
        return None
    return {"mimeType": message.headers.get("content-type", ""), "text": text}


def response(flow: http.HTTPFlow) -> None:
    if flow.response is None:
        return
    host = flow.request.pretty_host
    if not any(h in host for h in ONLY_HOSTS):
        return

    entries.append(
        {
            "request": {
                "method": flow.request.method,
                "url": flow.request.pretty_url,
                "headers": _headers(flow.request.headers),
                "postData": _body(flow.request),
            },
            "response": {
                "status": flow.response.status_code,
                "headers": _headers(flow.response.headers),
                "content": _body(flow.response),
            },
        }
    )


def done():
    har = {"log": {"version": "1.2", "entries": entries}}
    OUT.write_text(json.dumps(har))
    print(f"\nWrote {len(entries)} entries to {OUT}")
