"""Minimal client for the avtonalogi.ru /api/req endpoint.

Flow:
    1. POST   /api/req            -> create a subscription (returns id + tokenReq)
    2. GET    /api/req?id=<id>    -> poll status and get taxes/fines list
    3. DELETE /api/req?id=<id>    -> remove the subscription

All calls (except the first POST) require the `tokenReq` header returned by the
server on the first request. The server may also rotate the token and return a
new `tokenReq` in the JSON body; the client always uses the latest one.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

import requests

BASE_URL = "https://avtonalogi.ru/api"

REQ_TYPE_INN = "inn"
REQ_TYPE_UIN = "uin"

STATUS_VIEW = "view"
STATUS_LOADING = "loading"
STATUS_DELETING = "deleting"

DEFAULT_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Origin": "https://avtonalogi.ru",
    "Referer": "https://avtonalogi.ru/",
}


class AvtonalogiClient:
    def __init__(self, base_url: str = BASE_URL, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        headers = dict(kwargs.pop("headers", {}) or {})
        if self.token:
            headers["tokenReq"] = self.token

        resp = self.session.request(
            method,
            f"{self.base_url}{path}",
            headers=headers,
            timeout=30,
            **kwargs,
        )
        resp.raise_for_status()
        body = resp.json()

        if isinstance(body, dict) and body.get("tokenReq"):
            self.token = body["tokenReq"]

        return body

    def create_req(self, req_type: str, value: str, mail: str) -> dict:
        """POST /api/req — create subscription for an INN or UIN."""
        if req_type not in (REQ_TYPE_INN, REQ_TYPE_UIN):
            raise ValueError(f"req_type must be 'inn' or 'uin', got {req_type!r}")
        return self._request(
            "POST",
            "/req",
            json={"type": req_type, "value": value, "mail": mail},
        )

    def get_req(self, req_id: str | int) -> dict:
        """GET /api/req?id=<id> — fetch current state of a subscription."""
        return self._request("GET", "/req", params={"id": req_id})

    def delete_req(self, req_id: str | int) -> dict:
        """DELETE /api/req?id=<id> — remove the subscription."""
        return self._request("DELETE", "/req", params={"id": req_id})

    def wait_until_ready(
        self,
        req_id: str | int,
        interval: float = 3.0,
        timeout: float = 120.0,
    ) -> dict:
        """Poll GET /api/req until its status leaves `loading`."""
        deadline = time.monotonic() + timeout
        while True:
            data = self.get_req(req_id)
            status = data.get("status")
            if status != STATUS_LOADING:
                return data
            if time.monotonic() >= deadline:
                raise TimeoutError(f"req {req_id} still loading after {timeout}s")
            time.sleep(interval)


def _dump(label: str, data: Any) -> None:
    print(f"\n=== {label} ===")
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main() -> int:
    p = argparse.ArgumentParser(description="avtonalogi.ru /api/req demo client")
    p.add_argument("--type", choices=[REQ_TYPE_INN, REQ_TYPE_UIN], default=REQ_TYPE_INN)
    p.add_argument("--value", required=True, help="INN (12 digits) or UIN (20 digits)")
    p.add_argument("--mail", required=True, help="Email to attach to the subscription")
    p.add_argument("--delete", action="store_true", help="Delete the req after polling")
    p.add_argument("--poll-timeout", type=float, default=120.0)
    args = p.parse_args()

    client = AvtonalogiClient()

    created = client.create_req(args.type, args.value, args.mail)
    _dump("POST /api/req", created)

    req_id = created.get("id") or created.get("req_id")
    if not req_id:
        print("Server did not return a req id; nothing to poll.", file=sys.stderr)
        return 1

    ready = client.wait_until_ready(req_id, timeout=args.poll_timeout)
    _dump(f"GET /api/req?id={req_id}", ready)

    if args.delete:
        removed = client.delete_req(req_id)
        _dump(f"DELETE /api/req?id={req_id}", removed)

    return 0


if __name__ == "__main__":
    sys.exit(main())
