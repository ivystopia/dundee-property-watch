#!/usr/bin/env python3
"""Save a public page as private, inspectable evidence for the property watch."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup


def public_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Expected a public HTTP(S) URL without credentials")
    if parsed.port not in {None, 80, 443}:
        raise ValueError("Unsupported URL port")
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("Private network URLs are not property evidence")
    return url


def fetch(url: str, output: Path, *, tor: bool = False) -> dict:
    output.parent.mkdir(parents=True, exist_ok=True)
    result = {"url": url, "retrieved_at": datetime.now(timezone.utc).isoformat(), "status": 0,
              "title": "", "text": "", "links": [], "structured_data": [], "error": ""}
    try:
        public_url(url)
        with tempfile.TemporaryDirectory() as tmp:
            body_path = Path(tmp) / "body"
            # Follow redirects one at a time to validate each destination before requesting it.
            current = url
            for _ in range(6):
                public_url(current)
                command = ["curl", "--silent", "--show-error", "--compressed", "--max-time", "25",
                           "--connect-timeout", "10", "--max-filesize", "5000000", "--proto", "=http,https",
                           "--user-agent", "Mozilla/5.0 (compatible; DundeePropertyWatch/1.0)",
                           "--output", str(body_path), "--write-out", "%{json}", current]
                if tor:
                    proxy = os.environ.get("DUNDEE_TOR_PROXY")
                    if not proxy:
                        raise ValueError("DUNDEE_TOR_PROXY is not configured for the optional SOCKS retry")
                    command[1:1] = ["--proxy", proxy]
                response = subprocess.run(command, capture_output=True, text=True, timeout=30)
                metadata = json.loads(response.stdout or "{}")
                result["status"] = metadata.get("http_code", 0)
                if response.returncode:
                    raise RuntimeError(f"curl failed ({response.returncode})")
                redirect = metadata.get("redirect_url")
                if redirect and result["status"] in {301, 302, 303, 307, 308}:
                    current = urljoin(current, redirect)
                    continue
                break
            else:
                raise ValueError("Too many redirects")
            result["final_url"] = current
            result["via_tor"] = tor
            body = body_path.read_bytes().decode("utf-8", errors="replace")
            soup = BeautifulSoup(body, "html.parser")
            result["title"] = soup.title.get_text(" ", strip=True) if soup.title else ""
            result["structured_data"] = [s.get_text() for s in soup.select('script[type="application/ld+json"]')]
            seen = set()
            for a in soup.select("a[href]"):
                link = urljoin(current, a["href"])
                if urlsplit(link).scheme in {"https", "http"} and link not in seen:
                    seen.add(link)
                    result["links"].append({"url": link, "text": a.get_text(" ", strip=True)[:300]})
            for element in soup(["script", "style", "noscript", "svg"]):
                element.decompose()
            result["text"] = soup.get_text(" ", strip=True)
            if result["status"] != 200:
                result["error"] = f"HTTP {result['status']}"
            elif any(x in result["title"].lower() for x in ["just a moment", "access denied", "attention required"]):
                result["error"] = "Access challenge"
    except Exception as exc:
        result["error"] = str(exc)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("output", type=Path)
    parser.add_argument("--tor", action="store_true", help="One explicit retry through the configured SOCKS proxy")
    args = parser.parse_args()
    result = fetch(args.url, args.output, tor=args.tor)
    print(json.dumps({k: result[k] for k in ("url", "status", "title", "error")}))
