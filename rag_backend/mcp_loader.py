import json
import logging
import os
from pathlib import Path
from contextlib import AsyncExitStack
from typing import Any

log = logging.getLogger(__name__)

_mcp_client = None
_mcp_stack: AsyncExitStack | None = None
_mcp_tools: list = []

#--- Default lebt im Code statt als escaped JSON in der .env (Composes Parser
#    liest Backslash-Quotes anders als python-dotenv).
_DEFAULT_MCP_SERVERS = (
    '[{"name":"fs","command":"python","args":["-m","rag_backend.mcp_servers.filesystem"]}]'
)


_ALLOWED_ENV_VARS = {"PATH", "HOME", "LANG", "PYTHONPATH", "NODE_ENV"}
_FORBIDDEN_ENV_SUBSTRINGS = {"SECRET", "TOKEN", "KEY", "PASSWORD"}


def _build_stdio_env(server_env: dict | None) -> dict:
    env = {}
    for k, v in os.environ.items():
        if k in _ALLOWED_ENV_VARS:
            if not any(bad in k.upper() for bad in _FORBIDDEN_ENV_SUBSTRINGS):
                env[k] = v
    if isinstance(server_env, dict):
        for k, v in server_env.items():
            if isinstance(k, str):
                env[k] = str(v)
    return env


def _is_allowed_stdio_command(command: str, args: list) -> bool:
    cmd_path = Path(command)
    resolved = cmd_path.resolve(strict=False)
    ws_root = Path("/host/argus_workspace").resolve(strict=False)
    norm_cmd = str(cmd_path).replace("\\", "/")
    if resolved == ws_root or ws_root in resolved.parents or "/host/argus_workspace" in norm_cmd:
        return False

    cmd_name = cmd_path.name.lower()
    if cmd_name in {"python", "python3", "python.exe"}:
        return len(args) >= 2 and args[0] == "-m" and str(args[1]).startswith("rag_backend.mcp_servers.")
    if not os.path.isabs(command):
        return False
    allowed_roots = [
        Path("/app").resolve(strict=False),
    ]
    return any(resolved == root or root in resolved.parents for root in allowed_roots)


def _is_allowed_mcp_url(url: str) -> bool:
    # url-Transport-MCP-Server default-deny: nur Hosts aus MCP_ALLOWED_URLS.
    # Leer -> kein remote MCP. Der stdio-fs-Default ist nicht betroffen.
    allow = [u.strip() for u in os.getenv("MCP_ALLOWED_URLS", "").split(",") if u.strip()]
    if not allow:
        return False
    from urllib.parse import urlparse
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    for a in allow:
        a_host = (urlparse(a).hostname or a).lower() if "://" in a else a.lower()
        if host == a_host:
            return True
    return False


def wrap_mcp_tool(tool):
    """Hängt Egress-Guard und Sensitive-Source-Markierung an ein geladenes MCP-Tool."""
    from langchain_core.tools import StructuredTool
    from rag_backend.egress_guard import (
        LOCAL_FS_MCP_READ_TOOLS,
        LOCAL_FS_MCP_TOOLS,
        check_egress_allowed,
        mark_sensitive_source,
    )

    t_name = getattr(tool, "name", "") or ""
    orig_coro = getattr(tool, "coroutine", None)
    orig_func = getattr(tool, "func", None)

    async def _wrapped_coro(*args, **kwargs):
        if t_name not in LOCAL_FS_MCP_TOOLS:
            blocked = await check_egress_allowed(t_name, {"args": args, "kwargs": kwargs})
            if blocked is not None:
                return blocked
        if orig_coro is not None:
            res = await orig_coro(*args, **kwargs)
        elif orig_func is not None:
            res = orig_func(*args, **kwargs)
        else:
            res = await tool.ainvoke(kwargs or (args[0] if args else {}))
        if t_name in LOCAL_FS_MCP_READ_TOOLS and res is not None:
            res_str = res if isinstance(res, str) else str(res)
            if res_str.strip() and res_str.strip() != "(empty)" and not res_str.startswith(("ERROR:", "Error:")):
                mark_sensitive_source(t_name, res_str)
        return res

    if isinstance(tool, StructuredTool):
        return StructuredTool(
            name=t_name,
            description=getattr(tool, "description", "") or "",
            args_schema=getattr(tool, "args_schema", None),
            coroutine=_wrapped_coro,
            response_format=getattr(tool, "response_format", "content"),
        )
    try:
        tool.coroutine = _wrapped_coro
    except Exception:
        pass
    return tool


async def startup_mcp() -> list:
    global _mcp_client, _mcp_stack, _mcp_tools
    raw = os.getenv("MCP_SERVERS", "").strip()
    if raw.lower() in {"none", "off", "[]"}:
        return []
    if not raw:
        raw = _DEFAULT_MCP_SERVERS
    try:
        servers_config: list[dict[str, Any]] = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning(f"MCP_SERVERS JSON-Fehler: {e}")
        return []
    if not servers_config:
        return []
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError:
        log.warning("langchain-mcp-adapters nicht installiert -- MCP-Tools uebersprungen.")
        return []
    servers: dict[str, dict] = {}
    for entry in servers_config:
        name = entry.get("name")
        if not name:
            continue
        if "url" in entry:
            if not _is_allowed_mcp_url(entry["url"]):
                log.warning("MCP url-Transport nicht erlaubt (MCP_ALLOWED_URLS): %s", entry["url"])
                continue
            servers[name] = {"url": entry["url"], "transport": entry.get("transport", "sse")}
        elif "command" in entry:
            cmd_raw = entry["command"]
            if isinstance(cmd_raw, list):
                if not cmd_raw:
                    continue
                command, args = cmd_raw[0], list(cmd_raw[1:])
            else:
                command = cmd_raw
                args = list(entry.get("args", []))
            if not _is_allowed_stdio_command(str(command), args):
                log.warning("MCP-Server command nicht erlaubt: %s", command)
                continue
            servers[name] = {
                "command": command,
                "args": args,
                "transport": "stdio",
                "env": _build_stdio_env(entry.get("env")),
            }
    if not servers:
        return []
    try:
        client = MultiServerMCPClient(servers)
        tools = await client.get_tools()
        _mcp_client = client
        _mcp_stack = None
        _mcp_tools = [wrap_mcp_tool(t) for t in tools]
        log.info(f"MCP gestartet: {len(_mcp_tools)} Tools von {list(servers.keys())}")
        return list(_mcp_tools)
    except Exception as e:
        log.warning(f"MCP-Client Start fehlgeschlagen: {e}")
        _mcp_client = None
        _mcp_stack = None
        return []


async def shutdown_mcp():
    global _mcp_client, _mcp_stack, _mcp_tools
    _mcp_client = None
    _mcp_stack = None
    _mcp_tools = []
