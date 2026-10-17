"""Run one bounded snapshot evaluation against a remote HTTP adapter."""

from dataclasses import asdict
from http.client import HTTPConnection, HTTPSConnection
import json
from math import isfinite
from time import monotonic
from urllib.parse import urlsplit, urlunsplit

from .core import Event, Probe
from .execution import InvalidAdapterResponse, Recall
from .process_adapter import MAX_REQUEST_BYTES, MAX_RESPONSE_BYTES
from .strictjson import decode_object


class HttpSnapshotSession:
    """One stateless HTTPS request per scenario with optional bearer authentication."""

    def __init__(self, endpoint: str, token: str | None = None):
        self.endpoint = normalize_endpoint(endpoint)
        if token is not None and (not isinstance(token, str) or not token):
            raise ValueError("adapter bearer token must be a nonempty string")
        self.token = token
        self.events: list[Event] = []

    def observe(self, event: Event, deadline: float) -> int:
        if monotonic() > deadline:
            raise TimeoutError("adapter deadline expired")
        self.events.append(event)
        return 0

    def recall(self, probe: Probe, deadline: float) -> Recall:
        remaining = deadline - monotonic()
        if not isfinite(remaining) or remaining <= 0:
            raise TimeoutError("adapter deadline expired")
        payload = json.dumps(
            {"version": 1, "events": [asdict(event) for event in self.events], "probe": asdict(probe)},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > MAX_REQUEST_BYTES:
            raise InvalidAdapterResponse("request exceeds protocol limit")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token is not None:
            headers["Authorization"] = f"Bearer {self.token}"
        parsed = urlsplit(self.endpoint)
        connection_type = HTTPSConnection if parsed.scheme == "https" else HTTPConnection
        connection = connection_type(parsed.hostname, parsed.port, timeout=remaining)
        raw = bytearray()
        try:
            path = parsed.path or "/"
            connection.request("POST", path, body=payload, headers=headers)
            response = connection.getresponse()
            if response.status != 200:
                raise OSError(f"adapter returned HTTP status {response.status}")
            while len(raw) <= MAX_RESPONSE_BYTES:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError("adapter deadline expired")
                if connection.sock is not None:
                    connection.sock.settimeout(remaining)
                chunk = response.read1(min(4096, MAX_RESPONSE_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
        finally:
            connection.close()
        if len(raw) > MAX_RESPONSE_BYTES:
            raise InvalidAdapterResponse("adapter response exceeds protocol limit")
        try:
            document = decode_object(bytes(raw), "adapter response")
        except ValueError as error:
            raise InvalidAdapterResponse("adapter response is not valid JSON") from error
        if not isinstance(document, dict) or set(document) != {"version", "value", "cost_microusd"}:
            raise InvalidAdapterResponse("adapter response fields are invalid")
        if type(document["version"]) is not int or document["version"] != 1:
            raise InvalidAdapterResponse("adapter response version is invalid")
        value = document["value"]
        cost = document["cost_microusd"]
        if value is not None and not isinstance(value, str):
            raise InvalidAdapterResponse("adapter response value is invalid")
        if type(cost) is not int or cost < 0:
            raise InvalidAdapterResponse("adapter response cost is invalid")
        return Recall(value, cost)

    def close(self) -> None:
        self.events.clear()
        self.token = None


def normalize_endpoint(endpoint: str) -> str:
    if not isinstance(endpoint, str) or not endpoint:
        raise ValueError("adapter endpoint must be a nonempty URL")
    parsed = urlsplit(endpoint)
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("adapter endpoint must not contain credentials, a query, or a fragment")
    if not parsed.hostname or parsed.scheme not in {"http", "https"}:
        raise ValueError("adapter endpoint must be an HTTP URL")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("nonlocal adapter endpoints must use HTTPS")
    path = parsed.path or "/"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
