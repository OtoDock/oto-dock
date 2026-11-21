"""A person's API key: the public routes under ``/v1/api/`` and the ``otodock``
verbs, as an outside program uses them. A key drives a headless chat turn, a
run-when-asked task, files and notifications, and nothing else."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .person import Http, Person


def mint(person: Person, *, name: str, scopes: list[str], days: int = 7, run_without_asking: bool = True,
         agents: list[str] | None = None) -> str:
    """A key minted the way User Settings does, the password asked again."""
    body = {"name": name, "permissions": scopes, "agents": agents or [], "expires_in_days": days,
            "run_without_asking": run_without_asking, "password": person.password}
    return person.ok("POST", "/v1/user-api-keys", body)["key"]


class Key(Http):
    def __init__(self, base: str, key: str):
        super().__init__(base, token=key)
        self.key = key

    def verb(self, *args: str, timeout: float = 300.0) -> subprocess.CompletedProcess:
        """The ``otodock`` command, downloaded from the install as a person gets it."""
        status, data, _ = self.raw("GET", "/v1/api/otodock.py")
        if status != 200:
            raise OSError(f"the otodock command answered {status}")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "otodock.py"
            path.write_bytes(data)
            env = {**os.environ, "OTODOCK_URL": self.base, "OTODOCK_API_KEY": self.key, "HOME": folder}
            env.pop("OTO_SESSION_ID", None)
            env.pop("PROXY_API_KEY", None)
            return subprocess.run([sys.executable, "-I", str(path), *args], env=env, capture_output=True,
                                  text=True, timeout=timeout, check=False)

    def send_and_wait(self, chat_id: str, text: str, *, wait: int = 120) -> dict:
        return self.ok("POST", f"/v1/api/chats/{chat_id}/messages", {"text": text, "wait_seconds": wait},
                       timeout=wait + 30)

    def read_text(self, agent: str, path: str) -> str:
        status, data = self.call("GET", f"/v1/api/agents/{agent}/files/{path}")
        if status != 200:
            raise OSError(f"reading {path} answered {status}")
        return data.get("content", "") if isinstance(data, dict) else json.dumps(data)


def files_key(ctx) -> Key:
    """The owner's key for the probes' files, minted once per install."""
    if "files_key" not in ctx.state:
        ctx.state["files_key"] = mint(ctx.owner, name=f"rig-files-{uuid.uuid4().hex[:8]}", scopes=["files"],
                                      run_without_asking=False)
    return Key(ctx.base, ctx.state["files_key"])


class Planted:
    """A file the owner's key writes in the owner's workspace, which a step then
    removes: whether it is still there says whether the step ran. ``step_path`` is
    the path a session sees from its own folder, on the host and on a machine."""

    def __init__(self, ctx, label: str):
        self.key = files_key(ctx)
        self.agent = ctx.agent
        name = f"rig-{label}-{uuid.uuid4().hex[:10]}.txt"
        self.step_path = f"workspace/{name}"
        self.key_path = f"users/{ctx.owner.username}/workspace/{name}"
        self.key.ok("PUT", f"/v1/api/agents/{self.agent}/files/{self.key_path}", {"content": "planted"})

    def present(self) -> bool:
        status, _ = self.key.call("GET", f"/v1/api/agents/{self.agent}/files/{self.key_path}")
        if status not in (200, 404):
            raise OSError(f"reading {self.key_path} answered {status}")
        return status == 200

    def wait(self, *, present: bool, timeout: float = 45.0) -> bool:
        """Whether the file reaches that state in time (a machine syncs at the turn's end)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.present() == present:
                return True
            time.sleep(2)
        return self.present() == present
