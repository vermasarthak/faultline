"""Run a trusted local memory executable through a bounded JSON protocol."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic

from .core import Event, Probe
from .execution import InvalidAdapterResponse, Recall
from .strictjson import decode_object


MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 16 * 1024


class ProcessSession:
    """One fresh executable invocation per scenario, never a shared memory state."""

    def __init__(self, program: str | Path):
        self.program = str(Path(program).resolve())
        self.events: list[Event] = []

    def observe(self, event: Event, deadline: float) -> int:
        if monotonic() > deadline:
            raise TimeoutError("adapter deadline expired")
        self.events.append(event)
        return 0

    def recall(self, probe: Probe, deadline: float) -> Recall:
        request = json.dumps(
            {"version": 1, "events": [asdict(event) for event in self.events], "probe": asdict(probe)},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        if len(request) > MAX_REQUEST_BYTES:
            raise InvalidAdapterResponse("request exceeds protocol limit")
        raw = _exchange([self.program], request, deadline)
        try:
            response = decode_object(raw, "adapter response")
        except ValueError as error:
            raise InvalidAdapterResponse("adapter response is not valid JSON") from error
        if not isinstance(response, dict) or set(response) != {"version", "value", "cost_microusd"}:
            raise InvalidAdapterResponse("adapter response fields are invalid")
        if type(response["version"]) is not int or response["version"] != 1:
            raise InvalidAdapterResponse("adapter response version is invalid")
        value = response["value"]
        cost = response["cost_microusd"]
        if value is not None and not isinstance(value, str):
            raise InvalidAdapterResponse("adapter response value is invalid")
        if type(cost) is not int or cost < 0:
            raise InvalidAdapterResponse("adapter response cost is invalid")
        return Recall(value, cost)

    def close(self) -> None:
        self.events.clear()


class ContinuitySession:
    """One process per operation, with only a trial scoped directory shared across them."""

    def __init__(self, program: str | Path):
        self.program = str(Path(program).resolve())
        self.state = TemporaryDirectory(prefix="faultline-continuity-")

    def observe(self, event: Event, deadline: float) -> int:
        response = self._request({"version": 2, "operation": "observe", "event": asdict(event)}, deadline)
        if set(response) != {"version", "cost_microusd"}:
            raise InvalidAdapterResponse("adapter response fields are invalid")
        return _response_cost(response)

    def recall(self, probe: Probe, deadline: float) -> Recall:
        response = self._request({"version": 2, "operation": "recall", "probe": asdict(probe)}, deadline)
        if set(response) != {"version", "value", "cost_microusd"}:
            raise InvalidAdapterResponse("adapter response fields are invalid")
        value = response["value"]
        if value is not None and not isinstance(value, str):
            raise InvalidAdapterResponse("adapter response value is invalid")
        return Recall(value, _response_cost(response))

    def _request(self, payload: dict, deadline: float) -> dict:
        request = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(request) > MAX_REQUEST_BYTES:
            raise InvalidAdapterResponse("request exceeds protocol limit")
        raw = _exchange([self.program, self.state.name], request, deadline)
        try:
            response = decode_object(raw, "adapter response")
        except ValueError as error:
            raise InvalidAdapterResponse("adapter response is not valid JSON") from error
        if not isinstance(response, dict):
            raise InvalidAdapterResponse("adapter response must be an object")
        return response

    def close(self) -> None:
        self.state.cleanup()


def _response_cost(response: dict) -> int:
    if type(response["version"]) is not int or response["version"] != 2:
        raise InvalidAdapterResponse("adapter response version is invalid")
    cost = response["cost_microusd"]
    if type(cost) is not int or cost < 0:
        raise InvalidAdapterResponse("adapter response cost is invalid")
    return cost


def _exchange(command: list[str], request: bytes, deadline: float) -> bytes:
    if monotonic() >= deadline:
        raise TimeoutError("adapter deadline expired")
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    selector = selectors.DefaultSelector()
    output = bytearray()
    written = 0
    try:
        assert process.stdin is not None and process.stdout is not None
        os.set_blocking(process.stdin.fileno(), False)
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdin, selectors.EVENT_WRITE)
        selector.register(process.stdout, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("adapter deadline expired")
            for key, _ in selector.select(remaining):
                if key.fileobj is process.stdin:
                    try:
                        written += os.write(process.stdin.fileno(), request[written:written + 4096])
                    except BrokenPipeError:
                        written = len(request)
                    if written == len(request):
                        selector.unregister(process.stdin)
                        process.stdin.close()
                else:
                    chunk = os.read(
                        process.stdout.fileno(),
                        min(4096, MAX_RESPONSE_BYTES + 1 - len(output)),
                    )
                    if not chunk:
                        selector.unregister(process.stdout)
                        process.stdout.close()
                    else:
                        output.extend(chunk)
                        if len(output) > MAX_RESPONSE_BYTES:
                            raise InvalidAdapterResponse("adapter response exceeds protocol limit")
        try:
            exit_code = process.wait(timeout=max(0, deadline - monotonic()))
        except subprocess.TimeoutExpired as error:
            raise TimeoutError("adapter deadline expired") from error
        if exit_code != 0:
            raise OSError("adapter process exited unsuccessfully")
        return bytes(output)
    finally:
        selector.close()
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        if process.stdout is not None and not process.stdout.closed:
            process.stdout.close()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
