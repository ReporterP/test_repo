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
        interval: float = 8.0,
        max_wait_attempts: int = 10,
        max_empty_attempts: int = 5,
        on_attempt: "callable | None" = None,
    ) -> dict:
        """Poll GET /api/req mirroring the front-end loop.

        The front end retries every 8s, allowing up to 10 `wait:true` responses
        and 5 empty/`findFail` responses before giving up. Returns the final
        payload as soon as it has neither `wait` nor `findFail` flags.
        """
        wait_attempts = 0
        empty_attempts = 0
        attempt = 0
        while True:
            attempt += 1
            data = self.get_req(req_id)
            kind = classify_find_response(data)
            if on_attempt:
                on_attempt(attempt, kind, data)

            if kind == "done":
                return data
            if kind == "noParams":
                raise RuntimeError("server returned 'noParams' for req")
            if kind == "reload":
                raise RuntimeError("server requested reload")
            if kind == "wait":
                wait_attempts += 1
                if wait_attempts > 9:
                    raise TimeoutError(
                        f"server kept returning wait=true after {wait_attempts} attempts"
                    )
            else:  # 'findFail' or 'empty'
                empty_attempts += 1
                if empty_attempts > 4:
                    raise RuntimeError(
                        f"server kept returning empty/findFail after {empty_attempts} attempts"
                    )
            time.sleep(interval)


def _dump(label: str, data: Any, elapsed: float | None = None) -> None:
    suffix = f"  [+{elapsed:.2f}s]" if elapsed is not None else ""
    print(f"\n=== {label}{suffix} ===")
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.2f}s"
    m, s = divmod(seconds, 60)
    return f"{int(m)}m {s:.2f}s"


def extract_req_id(payload: dict) -> int | str | None:
    """Find the req id inside a /api/req response.

    The server may return it at different paths depending on whether the req
    was just created, found by subscription, or merged with an existing one.
    """
    for key in ("id", "reqId", "req_id"):
        if payload.get(key):
            return payload[key]
    create = payload.get("create")
    if isinstance(create, dict) and create.get("reqId"):
        return create["reqId"]
    for key in ("subscribeFail", "subscribeOk", "subscribe"):
        v = payload.get(key)
        if isinstance(v, (int, str)) and v:
            return v
    return None


def classify_find_response(payload: Any) -> str:
    """Reproduce the front-end branching for GET /api/req responses.

    Returns one of: 'noParams', 'reload', 'wait', 'findFail', 'done', 'empty'.
    """
    if payload is None or payload == "":
        return "empty"
    if payload == "noParams":
        return "noParams"
    if not isinstance(payload, dict):
        return "done"
    if payload.get("reload"):
        return "reload"
    if payload.get("wait"):
        return "wait"
    if payload.get("findFail"):
        return "findFail"
    return "done"


def main() -> int:
    p = argparse.ArgumentParser(description="avtonalogi.ru /api/req demo client")
    p.add_argument("--type", choices=[REQ_TYPE_INN, REQ_TYPE_UIN], default=REQ_TYPE_INN)
    p.add_argument("--value", required=True, help="INN (12 digits) or UIN (20 digits)")
    p.add_argument("--mail", required=True, help="Email to attach to the subscription")
    p.add_argument("--delete", action="store_true", help="Delete the req after polling")
    p.add_argument("--poll-interval", type=float, default=8.0,
                   help="seconds between GET /api/req calls (front-end uses 8)")
    p.add_argument("--max-wait", type=int, default=10,
                   help="max consecutive wait=true responses (front-end uses 10)")
    p.add_argument("--max-empty", type=int, default=5,
                   help="max consecutive empty/findFail responses (front-end uses 5)")
    args = p.parse_args()

    client = AvtonalogiClient()
    started_at = time.monotonic()
    started_wall = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"-> started at {started_wall}", file=sys.stderr)
    exit_code = 0
    try:
        created = client.create_req(args.type, args.value, args.mail)
        _dump("POST /api/req", created, elapsed=time.monotonic() - started_at)

        req_id = extract_req_id(created)
        if not req_id:
            print("Server did not return a req id; nothing to poll.", file=sys.stderr)
            return 1
        print(f"\n-> polling req id {req_id} ...", file=sys.stderr)

        last_seen: dict[str, Any] = {}

        def trace(attempt: int, kind: str, data: Any) -> None:
            elapsed = time.monotonic() - started_at
            print(
                f"   [attempt {attempt}] kind={kind} (+{elapsed:.2f}s)",
                file=sys.stderr,
            )
            last_seen["attempt"] = attempt
            last_seen["kind"] = kind
            last_seen["data"] = data

        try:
            ready = client.wait_until_ready(
                req_id,
                interval=args.poll_interval,
                max_wait_attempts=args.max_wait,
                max_empty_attempts=args.max_empty,
                on_attempt=trace,
            )
        except (TimeoutError, RuntimeError) as exc:
            elapsed = time.monotonic() - started_at
            _dump(
                f"GET /api/req?id={req_id} (gave up, last response)",
                last_seen.get("data"),
                elapsed=elapsed,
            )
            print(f"Polling stopped: {exc}", file=sys.stderr)
            return 2

        elapsed = time.monotonic() - started_at
        _dump(
            f"GET /api/req?id={req_id} (attempt {last_seen.get('attempt')})",
            ready,
            elapsed=elapsed,
        )

        if args.delete:
            removed = client.delete_req(req_id)
            _dump(
                f"DELETE /api/req?id={req_id}",
                removed,
                elapsed=time.monotonic() - started_at,
            )

        return exit_code
    finally:
        total = time.monotonic() - started_at
        print(
            f"\n-> finished in {_format_duration(total)} (started {started_wall})",
            file=sys.stderr,
        )


if __name__ == "__main__":
    sys.exit(main())
