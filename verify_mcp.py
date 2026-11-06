"""Verify stdio or HTTP MCP against OpenCode using an owned temporary session."""
# Copyright (c) 2026 佐々木隆志 (Takashi Sasaki) <takashi316@gmail.com> (https://x.com/TakashiSasaki)

import argparse
import asyncio
import json
from pathlib import Path
import tempfile
import time
import uuid

from mcp import Client, StdioServerParameters

import server


async def verify(url: str | None, inference: bool, model: str | None = None) -> None:
    endpoint = url or StdioServerParameters(command=str(server.PROJECT / "run.sh"))
    results = {}
    async with Client(endpoint, mode="legacy", raise_exceptions=True) as client:
        async def call(name, arguments=None):
            result = await client.call_tool(name, arguments or {})
            if result.is_error:
                raise AssertionError(f"{name} failed: {result.content}")
            return result.structured_content

        catalog = await client.list_tools()
        definitions = [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in catalog.tools]
        info = await call("opencode_info", {"tool": "opencode_start_task", "include_instructions": True})
        assert info["tool_names"] == sorted(t["name"] for t in definitions)
        assert json.loads(info["contract"]["tool_json"]) == next(t for t in definitions if t["name"] == "opencode_start_task")
        assert info["transport"] == ("streamable-http" if url else "stdio")
        results["info"] = info
        for name in ("opencode_projects", "opencode_workspaces", "opencode_workspace", "opencode_sessions", "opencode_usage"):
            results[name] = await call(name)
            print(name, "OK", flush=True)
        invalid = await client.call_tool("opencode_get_task", {"session_id": "ses_bad/../credential"})
        assert invalid.is_error
        free = await call("opencode_models", {"free_only": True})
        assert all(m["free_by_catalog"] is True for m in free["models"])
        results["free_models"] = free
        default = next(m for m in free["models"] if m["model"] == info["default_model"])
        with tempfile.TemporaryDirectory(prefix="mcp-verification-", dir=info["default_directory"]) as temporary:
            first, second = Path(temporary)/"first", Path(temporary)/"second"
            first.mkdir()
            second.mkdir()
            session_id = "ses_mcp_verify_" + uuid.uuid4().hex
            await server.api("post", "/api/session", {"id": session_id, "title": "Owned MCP workspace verification",
                "location": {"directory": str(first)}, "model": {"providerID": default["providerID"], "id": default["id"]}})
            try:
                before = await call("opencode_get_task", {"session_id": session_id})
                assert before["status"] == "idle"
                assert before["session"]["location"]["directory"] == str(first)
                results["move"] = await call("opencode_move_task", {"session_id": session_id, "directory_path": str(second)})
                after = await call("opencode_get_task", {"session_id": session_id})
                assert after["session"]["location"]["directory"] == str(second)
                assert results["move"]["applied"]
                assert (await call("opencode_info"))["default_directory"] == info["default_directory"]
                resolved = await call("opencode_workspace", {"directory_path": str(second)})
                assert resolved["location"]["directory"] == str(second)
                variant_model = next((m for m in free["models"] if m["reasoning_efforts"]), None)
                if variant_model:
                    variant = variant_model["reasoning_efforts"][0]
                    results["model_selection"] = await call("opencode_set_model", {"session_id": session_id,
                        "model": variant_model["model"], "reasoning_effort": variant})
                    actual = await server.checked_session(session_id)
                    assert actual["model"]["variant"] == variant
                    invalid = await client.call_tool("opencode_set_model", {"session_id": session_id,
                        "model": variant_model["model"], "reasoning_effort": "unsupported-verification-value"})
                    assert invalid.is_error
                restored = await call("opencode_move_task", {"session_id": session_id, "directory_path": str(first)})
                assert restored["applied"]
                print("owned session workspace/model changes OK", flush=True)
            finally:
                # This fresh, uniquely named session never received a model prompt.
                await server.api("delete", f"/api/session/{session_id}")

        if inference:
            selected = model or info["default_model"]
            assert any(m["model"] == selected for m in free["models"]), "Inference verification requires a current zero-catalog-price model"
            request = {"prompt": "Connection test: do not use tools or change files. Reply with exactly OPENCODE_MCP_56.",
                       "request_id": "mcp-smoke-" + uuid.uuid4().hex, "directory_path": info["default_directory"],
                       "title": "MCP execution verification"}
            if model:
                request["model"] = model
            started = await call("opencode_start_task", request)
            session_id = started["session_id"]
            results["started"] = started
            repeated = await call("opencode_start_task", request)
            assert repeated["reused"] and repeated["session_id"] == session_id
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                task = await call("opencode_get_task", {"session_id": session_id})
                if task["status"] in {"completed", "failed", "interrupted", "waiting_for_approval", "waiting_for_input"}:
                    results["task"] = task
                    break
                await asyncio.sleep(1)
            else:
                await call("opencode_interrupt_task", {"session_id": session_id})
                raise AssertionError(f"Task {session_id} did not settle within 60 seconds; interrupt requested")
            if task["status"] == "failed":
                errors = [m.get("error", {}) for m in task["messages"] if m.get("error")]
                results["inference"] = "provider_failure"
                results["provider_errors"] = errors
            elif task["status"] == "completed":
                assert any("OPENCODE_MCP_56" in m.get("text", "") for m in task["messages"] if m.get("type") == "assistant")
                results["inference"] = "completed"
            else:
                results["inference"] = task["status"]
            print("inference", results["inference"], flush=True)
    (server.PROJECT / "verification.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    print("MCP transport and workspace verification passed", flush=True)
    if inference and results.get("inference") != "completed":
        raise SystemExit("Model inference did not complete successfully; see verification.json for the task status and provider errors")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="HTTP MCP endpoint; omit to launch the stdio server")
    parser.add_argument("--inference", action="store_true", help="Opt into one small prompt on the current zero-catalog-price default model")
    parser.add_argument("--model", help="Explicit zero-catalog-price model for --inference; never changes the server default")
    args = parser.parse_args()
    if args.model and not args.inference:
        parser.error("--model requires --inference")
    asyncio.run(verify(args.url, args.inference, args.model))
