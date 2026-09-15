"""WorkBuddy App Service backend — reuse the local WorkBuddy login quota as the LLM.

Instead of an API key + base URL, this backend spawns a local ``codebuddy --serve``
gateway (bundled with the WorkBuddy desktop app) and talks the ACP protocol
(JSON-RPC over SSE) to it. No third-party API key is required — requests are
billed to the logged-in WorkBuddy account.

Protocol reference (read only): D:/CyberBoss/src/adapters/runtime/codebuddy
Implementation lineage: tools/acp-bridge/acp_bridge.py (sidecar bridge), ported
here as an in-process provider (one fewer process/port to manage).

Key pitfalls baked in (see workbuddy-acp-bridge skill):
  - Gateway password MUST be passed via env CODEBUDDY_GATEWAY_PASSWORD
    (the settings.json overlay field is only a fallback and is ignored).
  - ``acp-connection-id`` must be set before the first RPC.
  - ``session/new`` uses ``cwd`` (v2.115+), not ``workingDirectory``.
  - ``--tools ""`` disables all gateway tools so it behaves as a pure text model.
  - ACP has no function-calling pass-through → text-mode tool emulation.
  - One gateway process is pinned to one ``--model``; switching models restarts it.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

import httpx

from .base import AbstractSummarizer
from .prompts import MEMORY_CONSOLE_PROMPT
from ..utils.llm_logger import log_llm_interaction

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

SUPPORTED_MODELS = [
    "auto", "hy4-preview", "hy4-preview-x", "hy3", "hy3-x",
    "glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-5.1", "glm-5v-turbo",
    "minimax-m3", "kimi-k3-1", "kimi-k2.7", "kimi-k2.6",
    "deepseek-v4-flash", "deepseek-v4-pro",
]

DEFAULT_MODEL = "auto"
DEFAULT_NODE = r"C:\Users\23159\.workbuddy\binaries\node\versions\22.22.2-3\node.exe"
DEFAULT_CLI = (
    r"C:\Users\23159\AppData\Local\Programs\WorkBuddy"
    r"\resources\app.asar.unpacked\cli\bin\codebuddy"
)

ROUTE_HEALTH = "/api/v1/health"
ROUTE_CONNECT = "/api/v1/acp/connect"
ROUTE_ACP = "/api/v1/acp"

STARTUP_TIMEOUT_SECONDS = float(os.environ.get("WORKBUDDY_STARTUP_TIMEOUT", "90"))
PROMPT_TIMEOUT_SECONDS = float(os.environ.get("WORKBUDDY_PROMPT_TIMEOUT", "600"))
IDLE_SECONDS = float(os.environ.get("WORKBUDDY_IDLE_SECONDS", "900"))
TOOL_EMULATION = os.environ.get("WORKBUDDY_TOOL_EMULATION", "1").strip().lower() not in (
    "0", "false", "no", "off", "",
)

# Injected into the REAL system prompt via `--append-system-prompt` when tool
# emulation is on. Prompt-level instructions lose to the CLI's own system
# prompt (the model replies "I'm CodeBuddy, not a tool backend"); only a
# system-prompt-level injection sticks. Rules reference the per-request
# <available_tools> manifest emitted by build_tool_instruction().
TOOL_SYSTEM_PROMPT = (
    "You are also operating as a structured tool-calling backend for an "
    "application. When a user prompt contains an <available_tools> block, "
    "those tools are real and callable in this session regardless of what "
    "your built-in tool list says. To call one, reply with a single raw JSON "
    'object: {"tool_calls": [{"name": "<tool name>", "arguments": {<args>}}]} '
    "and nothing else — multiple array entries mean parallel calls. If no "
    "tool is needed, or you already have the required information, reply "
    "with the final answer as plain text. Never mix JSON and prose in one "
    "reply, and never use markdown code fences around tool-call JSON. Use "
    "only tool names declared in <available_tools>. If the prompt contains "
    "no <available_tools> block, never emit a tool_calls envelope."
)

# Conservative token budget: ai_chat uses it for context-compression thresholds.
# WorkBuddy models are typically 128K-context class.
token_budget_default = 128_000


# --------------------------------------------------------------------------- #
# CLI discovery
# --------------------------------------------------------------------------- #

_discovery_cache: dict[str, str] = {}


def _discover_node() -> str:
    explicit = os.environ.get("WORKBUDDY_NODE", "").strip()
    if explicit and Path(explicit).exists():
        return explicit
    if Path(DEFAULT_NODE).exists():
        return DEFAULT_NODE
    found = shutil.which("node") or shutil.which("node.exe")
    if not found:
        raise RuntimeError("未找到 Node.js 运行时，无法启动 WorkBuddy CLI。")
    return found


def _discover_cli() -> str:
    explicit = os.environ.get("WORKBUDDY_CLI", "").strip()
    if explicit and Path(explicit).exists():
        return explicit
    if Path(DEFAULT_CLI).exists():
        return DEFAULT_CLI
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        base = Path(local_app_data) / "Programs" / "WorkBuddy" / "resources"
        for candidate in (
            base / "app.asar.unpacked" / "cli" / "bin" / "codebuddy",
            base / "codebuddy" / "cli.js",
        ):
            if candidate.exists():
                return str(candidate)
    raise RuntimeError(
        "未找到 WorkBuddy CLI。请确认已安装并登录 WorkBuddy 桌面端，"
        "或通过 WORKBUDDY_CLI 环境变量指定路径。"
    )


def discover() -> dict[str, Any]:
    """Light discovery probe (cached per process; never spawns anything)."""
    try:
        node = _discovery_cache.get("node") or _discover_node()
        cli = _discovery_cache.get("cli") or _discover_cli()
        _discovery_cache["node"], _discovery_cache["cli"] = node, cli
        return {"ok": True, "node": node, "cli": cli}
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}


def rescan() -> dict[str, Any]:
    _discovery_cache.clear()
    return discover()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# --------------------------------------------------------------------------- #
# SSE parsing
# --------------------------------------------------------------------------- #

class SSEParser:
    """Incremental Server-Sent Events parser for ACP responses."""

    def __init__(self, on_message) -> None:
        self._buffer = ""
        self._on_message = on_message

    def push(self, text: str) -> None:
        self._buffer += text
        while True:
            marker = self._buffer.find("\n\n")
            if marker == -1:
                break
            block, self._buffer = self._buffer[:marker], self._buffer[marker + 2:]
            self._handle(block.replace("\r\n", "\n"))

    def finish(self) -> None:
        if self._buffer.strip():
            block, self._buffer = self._buffer, ""
            self._handle(block.replace("\r\n", "\n"))

    def _handle(self, block: str) -> None:
        for line in block.split("\n"):
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                self._on_message(json.loads(payload))
            except json.JSONDecodeError:
                continue


def _message_text(message: dict) -> str:
    """Pull assistant text out of an ACP ``session/update`` notification."""
    if message.get("method") != "session/update":
        return ""
    update = (message.get("params") or {}).get("update") or {}
    if update.get("sessionUpdate") != "agent_message_chunk":
        return ""
    content = update.get("content")
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    if isinstance(content, list):
        parts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        return "".join(parts)
    if isinstance(update.get("text"), str):
        return update["text"]
    return ""


# --------------------------------------------------------------------------- #
# Text-mode tool emulation (ACP has no function-calling pass-through)
# --------------------------------------------------------------------------- #

def render_tool_schemas(tools: list[dict[str, Any]]) -> str:
    blocks: list[str] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        if not isinstance(fn, dict):
            continue
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        desc = str(fn.get("description") or "").strip()
        params = fn.get("parameters")
        try:
            params_text = json.dumps(params, ensure_ascii=False, indent=2) if params else "{}"
        except Exception:
            params_text = "{}"
        blocks.append(f"### {name}\n{desc}\nparameters (JSON Schema):\n{params_text}")
    return "\n\n".join(blocks)


def build_tool_instruction(tools: list[dict[str, Any]]) -> str:
    """Per-request tool manifest.

    The *protocol rules* live in TOOL_SYSTEM_PROMPT (injected via the CLI's
    ``--append-system-prompt``), because prompt-level instructions are weaker
    than the CLI's own system prompt — the model would reply "I'm CodeBuddy,
    not a tool backend". Here we only declare which tools exist THIS turn.
    """
    return (
        "<available_tools>\n"
        f"{render_tool_schemas(tools)}\n"
        "</available_tools>"
    )


def strip_fences(text: str) -> str:
    stripped = (text or "").strip()
    if not stripped.startswith("```"):
        return text or ""
    first_newline = stripped.find("\n")
    if first_newline == -1:
        return text or ""
    body = stripped[first_newline + 1:]
    if body.rstrip().endswith("```"):
        body = body.rstrip()[: -3].rstrip("\n")
    return body or ""


def parse_tool_calls(text: str, allowed: set[str]) -> list[dict[str, Any]] | None:
    """Read a tool-call JSON envelope out of the model reply.

    Returns OpenAI-shaped tool_calls, or None when the reply is a final answer.
    Anything malformed or referencing unknown tools degrades to plain text.
    """
    if not text or not allowed:
        return None
    stripped = strip_fences(text).strip()
    candidates = [stripped]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        candidates.append(stripped[start:end + 1])

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue

        raw: list[Any] | None = None
        for key in ("tool_calls", "tool_call", "calls"):
            value = data.get(key)
            if isinstance(value, list) and value:
                raw = value
                break
            if isinstance(value, dict):
                raw = [value]
                break
        if raw is None and "name" in data and ("arguments" in data or "args" in data):
            raw = [data]
        if not raw:
            continue

        calls: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            fn = item.get("function") if isinstance(item.get("function"), dict) else item
            name = str(fn.get("name") or "").strip()
            if name not in allowed:
                continue
            args = fn.get("arguments", fn.get("args", {}))
            if isinstance(args, str):
                args_text = args or "{}"
            else:
                try:
                    args_text = json.dumps(args or {}, ensure_ascii=False)
                except Exception:
                    args_text = "{}"
            calls.append({
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {"name": name, "arguments": args_text},
            })
        if calls:
            return calls
    return None


def collect_tool_names(tools: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        if isinstance(fn, dict) and fn.get("name"):
            names.add(str(fn["name"]))
    return names


def build_prompt(messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]] | None = None) -> str:
    """Flatten an OpenAI messages array into a single ACP prompt."""
    lines: list[str] = []
    tool_names_by_id: dict[str, str] = {}

    for message in messages:
        role = str(message.get("role") or "user").lower()
        content = message.get("content")
        if isinstance(content, list):
            parts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
            content = "\n".join(parts)
        if content is None:
            content = ""
        content = str(content)

        # An assistant turn carrying tool_calls must survive flattening,
        # otherwise the ReAct loop loses its own history and retries forever.
        tool_calls = message.get("tool_calls") if role == "assistant" else None
        if role == "assistant" and tool_calls:
            rendered: list[str] = []
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                name = str(fn.get("name") or "?")
                args = str(fn.get("arguments") or "{}")
                tc_id = str(tc.get("id") or "")
                if tc_id:
                    tool_names_by_id[tc_id] = name
                rendered.append(f"{name}({args})")
            block = "[tool call] " + "; ".join(rendered)
            if content.strip():
                block = f"{content}\n{block}"
            lines.append(f"<assistant>\n{block}\n</assistant>")
            continue

        if not content.strip():
            continue
        if role == "system":
            lines.append(f"<system>\n{content}\n</system>")
        elif role == "assistant":
            lines.append(f"<assistant>\n{content}\n</assistant>")
        elif role in ("tool", "function"):
            tc_id = str(message.get("tool_call_id") or "")
            name = tool_names_by_id.get(tc_id) or message.get("name") or "tool"
            lines.append(f'<tool_result name="{name}">\n{content}\n</tool_result>')
        else:
            lines.append(f"<user>\n{content}\n</user>")

    if tools and TOOL_EMULATION:
        # Must come FIRST — the CLI injects its own system prompt and a trailing
        # tool block gets overridden by it (verified: model claims "no tools").
        lines.insert(0, build_tool_instruction(tools))
    return "\n\n".join(lines)


# --------------------------------------------------------------------------- #
# Gateway process + ACP connection (in-process, sync)
# --------------------------------------------------------------------------- #

class WorkBuddyGateway:
    """Owns the ``codebuddy --serve`` gateway subprocess and ACP connection.

    One process is pinned to one ``--model``; ensure_ready(model) restarts it
    when the requested model differs. All ACP calls are serialized with a lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._client: httpx.Client | None = None
        self._proc: subprocess.Popen | None = None
        self._endpoint = ""
        self._connection_id = ""
        self._model = ""
        self._password = secrets.token_urlsafe(32)
        self._overlay_dir = Path(tempfile.gettempdir()) / "wx-assist-acp" / uuid.uuid4().hex
        self._workdir = Path(tempfile.gettempdir()) / "wx-assist-acp-workspace"
        self._workdir.mkdir(parents=True, exist_ok=True)
        self._last_used = time.monotonic()
        self._session_count = 0
        self._prompt_count = 0
        self._restart_count = 0
        self._reaper_started = False

    # -- lifecycle ---------------------------------------------------------- #

    def _start_reaper(self) -> None:
        if self._reaper_started:
            return
        self._reaper_started = True

        def _reap() -> None:
            while True:
                time.sleep(30)
                try:
                    if self._proc is not None and self._proc.poll() is None:
                        if time.monotonic() - self._last_used > IDLE_SECONDS:
                            logger.info("[WorkBuddy] gateway idle %.0fs — stopping", IDLE_SECONDS)
                            with self._lock:
                                self._stop_locked()
                except Exception:
                    logger.exception("[WorkBuddy] idle reaper error")

        threading.Thread(target=_reap, daemon=True, name="workbuddy-idle-reaper").start()

    def _write_overlays(self) -> tuple[str, str]:
        self._overlay_dir.mkdir(parents=True, exist_ok=True)
        settings_path = self._overlay_dir / "settings.json"
        mcp_path = self._overlay_dir / "mcp.json"
        settings_path.write_text(
            json.dumps({"gateway": {"auth": "password", "password": self._password}}),
            encoding="utf-8",
        )
        mcp_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        return str(settings_path), str(mcp_path)

    def _start(self, model: str) -> None:
        info = discover()
        if not info.get("ok"):
            raise RuntimeError(info.get("error") or "未找到 WorkBuddy CLI")
        settings_path, mcp_path = self._write_overlays()
        port = _free_port()
        endpoint = f"http://127.0.0.1:{port}"

        args = [
            info["node"], info["cli"],
            "--serve",
            "--host", "127.0.0.1",
            "--port", str(port),
            "--settings", settings_path,
            "--strict-mcp-config",
            "--mcp-config", mcp_path,
            "--no-session-persistence",
            # Pure text-model mode: no gateway tools → no file reads / commands.
            "--tools", "",
            "--permission-mode", "default",
        ]
        if model and model != "auto":
            args += ["--model", model]
        if TOOL_EMULATION:
            args += ["--append-system-prompt", TOOL_SYSTEM_PROMPT]

        env = dict(os.environ)
        env["CODEBUDDY_GATEWAY_AUTH"] = "password"
        # The password MUST come from this env var — the settings.json overlay
        # value is only a fallback and is ignored by GatewayAuth.setup().
        env["CODEBUDDY_GATEWAY_PASSWORD"] = self._password

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self._proc = subprocess.Popen(
            args, cwd=str(self._workdir), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        logger.info("[WorkBuddy] gateway starting on %s (model=%s, pid=%d)",
                    endpoint, model or "auto", self._proc.pid)

        try:
            self._wait_health(endpoint)
        except Exception:
            self._stop_locked()
            raise

        self._endpoint = endpoint
        self._model = model
        self._connection_id = ""
        self._start_reaper()

    def _wait_health(self, endpoint: str) -> None:
        assert self._client is not None
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        last = "no attempt"
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise RuntimeError(f"WorkBuddy 网关进程提前退出 (rc={self._proc.poll()})")
            try:
                response = self._client.get(
                    endpoint + ROUTE_HEALTH, headers=self._headers(), timeout=5.0,
                )
                if response.status_code == 200:
                    return
                last = f"HTTP {response.status_code}"
            except Exception as exc:
                last = repr(exc)
            time.sleep(0.4)
        raise RuntimeError(f"WorkBuddy 网关 {STARTUP_TIMEOUT_SECONDS:.0f}s 内未就绪: {last}")

    def _stop_locked(self) -> None:
        self._connection_id = ""
        proc, self._proc = self._proc, None
        self._endpoint = ""
        self._model = ""
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        logger.info("[WorkBuddy] gateway stopped")

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def restart(self) -> None:
        with self._lock:
            self._stop_locked()

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._password}",
            "X-CodeBuddy-Request": "1",
        }
        if extra:
            headers.update(extra)
        return headers

    # -- ACP ---------------------------------------------------------------- #

    def _ensure_ready(self, model: str) -> None:
        if self._client is None:
            self._client = httpx.Client(timeout=None, trust_env=False)
        if self._proc is None or self._proc.poll() is not None:
            self._start(model)
        elif model and self._model != model:
            # One process is pinned to one model — restart to switch.
            logger.info("[WorkBuddy] switching model %s -> %s (restart)", self._model, model)
            self._stop_locked()
            self._restart_count += 1
            self._start(model)
        if not self._connection_id:
            self._connect()

    def _connect(self) -> None:
        assert self._client is not None
        response = self._client.post(
            self._endpoint + ROUTE_CONNECT, headers=self._headers(), timeout=30.0,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        connection_id = data.get("connectionId") or payload.get("connectionId")
        if not connection_id:
            raise RuntimeError(f"ACP connect 未返回 connectionId: {payload}")

        # Must be set before the first RPC: every request carries this header
        # and an empty value is rejected with 400.
        self._connection_id = connection_id

        self._rpc("initialize", {
            "protocolVersion": 1,
            "clientInfo": {"name": "wx-assist", "version": "1.0.0"},
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False},
                "terminal": False,
            },
        }, timeout=30.0)
        logger.info("[WorkBuddy] ACP connected (%s…)", connection_id[:8])

    def _rpc(self, method: str, params: dict[str, Any],
             timeout: float = PROMPT_TIMEOUT_SECONDS, on_text=None) -> tuple[dict, list[dict]]:
        """Send one JSON-RPC request over ACP and collect the SSE reply."""
        assert self._client is not None
        request_id = str(uuid.uuid4())
        body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        messages: list[dict] = []

        def collect(message: dict) -> None:
            messages.append(message)
            text = _message_text(message)
            if text and on_text is not None:
                on_text(text)

        parser = SSEParser(collect)

        try:
            with self._client.stream(
                "POST",
                self._endpoint + ROUTE_ACP,
                headers=self._headers({
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    "acp-connection-id": self._connection_id,
                }),
                json=body,
                timeout=timeout,
            ) as response:
                if response.status_code in (401, 403):
                    raise RuntimeError("ACP 拒绝了网关凭据（密码校验失败）")
                if response.status_code >= 400:
                    raw = response.read()
                    raise RuntimeError(
                        f"ACP {method} HTTP {response.status_code}: {raw[:800]!r}"
                    )
                for chunk in response.iter_text():
                    parser.push(chunk)
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError) as exc:
            raise RuntimeError(f"ACP {method} 传输失败: {exc!r}") from exc
        parser.finish()

        reply = next((m for m in messages if str(m.get("id", "")) == request_id), None)
        if reply is None:
            raise RuntimeError(f"ACP {method}: 未找到关联响应（共 {len(messages)} 条消息）")
        if reply.get("error"):
            raise RuntimeError(f"ACP {method} 错误: {reply['error']}")
        return reply.get("result") or {}, messages

    # -- public ------------------------------------------------------------- #

    def complete(self, prompt: str, model: str = DEFAULT_MODEL,
                 on_text=None, timeout: float = PROMPT_TIMEOUT_SECONDS) -> str:
        """Run one isolated prompt and return the assistant text."""
        with self._lock:
            self._last_used = time.monotonic()
            last_error: Exception | None = None

            for attempt in (1, 2):
                try:
                    self._ensure_ready(model)
                    session, _ = self._rpc(
                        "session/new", {"cwd": str(self._workdir), "mcpServers": []},
                        timeout=60.0,
                    )
                    session_id = session.get("sessionId")
                    if not session_id:
                        raise RuntimeError("session/new 未返回 sessionId")
                    self._session_count += 1

                    result, acp_messages = self._rpc(
                        "session/prompt",
                        {
                            "sessionId": session_id,
                            "prompt": [{"type": "text", "text": prompt}],
                        },
                        timeout=timeout,
                        on_text=on_text,
                    )
                    self._prompt_count += 1
                    self._last_used = time.monotonic()
                    stop_reason = (result or {}).get("stopReason")
                    if stop_reason and stop_reason not in ("end_turn", "cancelled"):
                        logger.warning("[WorkBuddy] unexpected stopReason=%s", stop_reason)
                    # Authoritative text source: the collected session/update
                    # notifications (on_text alone is only a streaming hook and
                    # may be None for non-streaming callers).
                    return "".join(filter(None, (_message_text(m) for m in acp_messages)))
                except Exception as exc:
                    last_error = exc
                    logger.warning("[WorkBuddy] ACP attempt %d failed: %r", attempt, exc)
                    self._connection_id = ""
                    if attempt == 1:
                        # Drop the gateway too — a stale process is the usual cause.
                        self._stop_locked()
                        self._restart_count += 1
                        continue
            raise RuntimeError(f"WorkBuddy ACP 请求失败（已重试）: {last_error!r}") from last_error

    def list_models(self, model: str = DEFAULT_MODEL) -> dict[str, Any]:
        """Open a session to fetch the gateway's available model list."""
        with self._lock:
            self._ensure_ready(model)
            session, _ = self._rpc(
                "session/new", {"cwd": str(self._workdir), "mcpServers": []}, timeout=60.0,
            )
            models = session.get("models") or {}
            available = [
                {"id": str(m.get("modelId") or ""), "name": str(m.get("name") or "")}
                for m in (models.get("availableModels") or [])
                if isinstance(m, dict) and m.get("modelId")
            ]
            return {
                "current": str(models.get("currentModelId") or self._model),
                "available": available or [{"id": m, "name": m} for m in SUPPORTED_MODELS],
            }

    def test_connection(self, model: str = DEFAULT_MODEL) -> dict[str, Any]:
        """Full probe: start gateway, open session, run a tiny prompt."""
        started = time.monotonic()
        collector = _TextCollector()
        text = self.complete(
            "Respond with exactly TEST_OK and nothing else.",
            model=model, on_text=collector, timeout=120.0,
        ).strip()
        elapsed = int((time.monotonic() - started) * 1000)
        ok = text == "TEST_OK"
        return {
            "ok": ok,
            "reply": text,
            "model": model,
            "elapsed_ms": elapsed,
            "gateway": self.status(),
            "error": "" if ok else f"模型回复异常: {text[:100]}",
        }

    def status(self) -> dict[str, Any]:
        running = self._proc is not None and self._proc.poll() is None
        return {
            "gateway_running": running,
            "endpoint": self._endpoint,
            "acp_connected": bool(self._connection_id),
            "model": self._model,
            "pid": self._proc.pid if running else 0,
            "sessions": self._session_count,
            "prompts": self._prompt_count,
            "restarts": self._restart_count,
            "idle_seconds": round(time.monotonic() - self._last_used, 1),
        }


class _TextCollector:
    """Accumulates streamed assistant text so callers can read it afterwards."""

    def __init__(self) -> None:
        self.chunks: list[str] = []

    def __call__(self, text: str) -> None:
        self.chunks.append(text)


_gateway: WorkBuddyGateway | None = None
_gateway_lock = threading.Lock()


def get_gateway() -> WorkBuddyGateway:
    global _gateway
    with _gateway_lock:
        if _gateway is None:
            _gateway = WorkBuddyGateway()
            atexit.register(_shutdown_gateway)
        return _gateway


def _shutdown_gateway() -> None:
    global _gateway
    if _gateway is not None:
        try:
            _gateway.stop()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Summarizer backend
# --------------------------------------------------------------------------- #

class WorkBuddySummarizer(AbstractSummarizer):
    """LLM backend backed by the local WorkBuddy App Service (ACP gateway).

    No API key / base URL involved — model quota comes from the logged-in
    WorkBuddy desktop account.
    """

    token_budget = token_budget_default
    _backend_name = "workbuddy"
    retry_exceptions = ()  # complete() already retries internally

    def __init__(self, model: str = DEFAULT_MODEL, chunk_size: int = 400):
        self.model = model or DEFAULT_MODEL
        self.chunk_size = chunk_size
        # Fail fast at construction if the CLI is missing, so the caller gets a
        # clear error instead of a mid-chat surprise.
        discover()

    # -- internal ----------------------------------------------------------- #

    def _complete(self, system_prompt: str, messages: list[dict],
                  tools: list[dict] | None = None,
                  on_text=None, timeout: float = PROMPT_TIMEOUT_SECONDS) -> str:
        api_messages = [{"role": "system", "content": system_prompt}] + messages
        prompt = build_prompt(api_messages, tools=tools)
        text = strip_fences(get_gateway().complete(prompt, model=self.model,
                                                   on_text=on_text, timeout=timeout))
        if tools and TOOL_EMULATION:
            calls = parse_tool_calls(text, collect_tool_names(tools))
            if calls:
                # Caller (agent_chat) handles the envelope; return raw text.
                return text
        return text

    # -- AbstractSummarizer contract ---------------------------------------- #

    def _call_chat_api(self, system_prompt: str, messages: list[dict]) -> str:
        content = self._complete(system_prompt, messages)
        if not content:
            logger.warning("[WorkBuddy] chat returned empty content (model=%s)", self.model)
            return "..."
        return content

    def _call_digest_api(self, system_prompt: str, messages: list[dict],
                         timeout: float | None = None) -> str:
        content = self._complete(system_prompt, messages, timeout=timeout or PROMPT_TIMEOUT_SECONDS)
        if not content:
            logger.warning("[WorkBuddy] digest returned empty content (model=%s)", self.model)
            return "..."
        return content

    def _call_long_api(self, system_prompt: str, messages: list[dict],
                       max_tokens: int = 2000, temperature: float = 0.3,
                       timeout: float | None = None) -> str:
        # max_tokens/temperature have no ACP equivalent; ignored.
        content = self._complete(system_prompt, messages, timeout=timeout or PROMPT_TIMEOUT_SECONDS)
        if not content:
            logger.warning("[WorkBuddy] long-form returned empty content (model=%s)", self.model)
            return "..."
        return content

    def _call_chat_api_stream(self, system_prompt: str, messages: list[dict],
                              max_tokens: int = 2000,
                              extra_body: dict | None = None) -> Iterator[str]:
        """Stream chat response by pushing ACP chunks through a queue.

        complete() runs in a worker thread (it blocks until the turn ends);
        on_text fires per SSE chunk, so the frontend gets real increments.
        """
        api_messages = [{"role": "system", "content": system_prompt}] + messages
        prompt = build_prompt(api_messages)
        q: queue.Queue = queue.Queue()
        collector = _TextCollector()

        def _on_text(text: str) -> None:
            collector(text)
            q.put(("chunk", text))

        def _worker() -> None:
            try:
                get_gateway().complete(prompt, model=self.model, on_text=_on_text)
                q.put(("done", None))
            except Exception as exc:  # noqa: BLE001
                q.put(("error", exc))

        threading.Thread(target=_worker, daemon=True, name="workbuddy-stream").start()

        got_any = False
        while True:
            kind, payload = q.get()
            if kind == "chunk":
                got_any = True
                yield payload
            elif kind == "done":
                return
            else:  # error
                if got_any:
                    logger.warning("[WorkBuddy] stream ended with error after %d chunks: %r",
                                   len(collector.chunks), payload)
                    return
                raise RuntimeError(f"WorkBuddy 流式请求失败: {payload!r}")

    def agent_chat(self, system_prompt: str, messages: list[dict],
                   tools: list[dict]) -> tuple[str, list[dict] | None, str]:
        """ReAct agent chat with text-mode tool-call emulation."""
        started = time.monotonic()
        raw_tools = tools if TOOL_EMULATION else None
        text = self._complete(system_prompt, messages, tools=raw_tools)
        latency = (time.monotonic() - started) * 1000

        tool_calls = None
        content = text
        if raw_tools:
            tool_calls = parse_tool_calls(text, collect_tool_names(raw_tools))
            if tool_calls:
                content = ""
                logger.info("[WorkBuddy] tool emulation: model called %s",
                            [c["function"]["name"] for c in tool_calls])
        if not content and not tool_calls:
            content = "..."
        if not content and tool_calls:
            content = f"[调用工具: {', '.join(tc['function']['name'] for tc in tool_calls)}]"

        user_lines = [f"[{m.get('role', '?')}]: {m.get('content', '')}" for m in messages]
        log_llm_interaction(
            backend="workbuddy", call_type="agent_chat",
            model=self.model, system_prompt=system_prompt,
            user_prompt="\n".join(user_lines), response=content,
            latency_ms=latency,
            extra={"tool_calls": len(tool_calls) if tool_calls else 0,
                   "tools": ",".join(collect_tool_names(tools or [])),
                   "emulation": TOOL_EMULATION},
        )
        return content, tool_calls, ""

    def consolidate_memory(self, existing_memory: str,
                           new_messages: list[dict]) -> str:
        if not new_messages:
            return existing_memory

        msg_lines = []
        for m in new_messages[-200:]:
            sender = m.get("sender_name", "?")
            content = m.get("content", "")
            if content:
                msg_lines.append(f"{sender}: {content}")
        if not msg_lines:
            return existing_memory

        existing_display = existing_memory if existing_memory else "（暂无，这是第一次整理记忆）"
        system_prompt = MEMORY_CONSOLE_PROMPT.format(
            existing_memory=existing_display,
            new_messages="\n".join(msg_lines),
        )
        try:
            text = self._complete(
                system_prompt,
                [{"role": "user", "content": "请输出更新后的完整记忆日记。"}],
            ).strip()
            if len(text) > 2000:
                text = text[:2000]
            return text
        except Exception as exc:  # noqa: BLE001
            logger.warning("[WorkBuddy] memory consolidation failed: %s", exc)
            return existing_memory
