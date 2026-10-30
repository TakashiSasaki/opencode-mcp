"""Verify real stdio MCP discovery and OpenCode API access; inference is opt-in."""

import argparse
import asyncio
import json
from pathlib import Path
import time

from mcp import Client, StdioServerParameters


async def main(inference: bool) -> None:
    project = Path(__file__).resolve().parent
    results = {}
    async with Client(StdioServerParameters(command=str(project / "run.sh")), mode="legacy", raise_exceptions=True) as client:
        tools = await client.list_tools()
        results["tools"] = [tool.name for tool in tools.tools]
        for name in ("opencode_info", "opencode_models", "opencode_sessions", "opencode_usage", "opencode_projects", "opencode_workspaces", "opencode_workspace"):
            result = await client.call_tool(name, {})
            results[name] = result.structured_content
            print(name, "OK")
        result = await client.call_tool("opencode_get_task", {"session_id": "ses_bad/../credential"})
        if not result.is_error:
            raise AssertionError("Malformed session ID must be rejected")
        results["malformed_id_rejected"] = True
        free = await client.call_tool("opencode_models", {"free_only": True})
        results["free_models"] = free.structured_content
        if not all(m["free_by_catalog"] for m in results["free_models"]["models"]):
            raise AssertionError("Free-only filter included a non-free model")
        # Exercise model and effort selection on a blank session, without inference charges.
        import server
        session_id = "ses_mcp_settings_verification"
        try:
            await server.checked_session(session_id)
        except server.APIError as exc:
            if exc.tag != "SessionNotFoundError":
                raise
            await server.api("post", "/api/session", {"id": session_id, "title": "MCP model/effort verification (no inference)",
                "location": {"directory": str(server.DEFAULT_DIRECTORY)},
                "model": {"providerID": "opencode", "id": "beam-free", "variant": "low"}})
        changed = await client.call_tool("opencode_set_model", {"session_id": session_id, "model": "opencode/beam-free", "reasoning_effort": "high"})
        actual = await server.checked_session(session_id)
        if actual["model"].get("variant") != "high":
            raise AssertionError("OpenCode did not retain the requested reasoning variant")
        results["model_effort_verified"] = changed.structured_content
        unsupported = await client.call_tool("opencode_set_model", {"session_id": session_id, "model": "opencode/big-pickle", "reasoning_effort": "high"})
        if not unsupported.is_error:
            raise AssertionError("Unsupported reasoning effort must be rejected")
        results["unsupported_effort_rejected"] = True
        # Change only our blank verification session's context, then restore it. No model prompt is sent.
        original_directory = actual["location"]["directory"]
        try:
            changed = await client.call_tool("opencode_move_task", {"session_id": session_id, "directory_path": str(Path.home())})
            for _ in range(10):
                actual = await server.checked_session(session_id)
                if actual["location"]["directory"] == str(Path.home()):
                    break
                await asyncio.sleep(0.2)
            else:
                raise AssertionError("Workspace move did not apply to idle verification session")
            inspected = await client.call_tool("opencode_workspace", {"directory_path": str(Path.home())})
            if inspected.structured_content["location"]["directory"] != str(Path.home()):
                raise AssertionError("Explicit workspace inspection resolved the wrong location")
            results["workspace_move_verified"] = changed.structured_content
        finally:
            await client.call_tool("opencode_move_task", {"session_id": session_id, "directory_path": original_directory})
        print("workspace selection and move", "OK")
        if inference:
            request = {"prompt": "接続確認です。ファイル操作やツール実行はせず、『OpenCode MCP 接続成功』とだけ答えてください。",
                       "request_id": "setup-smoke-2026-10-27-v1", "title": "MCP connection verification"}
            started = await client.call_tool("opencode_start_task", request)
            results["started"] = started.structured_content
            session_id = results["started"]["session_id"]
            repeated = await client.call_tool("opencode_start_task", request)
            if not repeated.structured_content.get("reused"):
                raise AssertionError("Task retry must reuse the existing message")
            results["retry_reused"] = True
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                result = await client.call_tool("opencode_get_task", {"session_id": session_id})
                status = result.structured_content
                if status["status"] in {"completed", "failed", "interrupted", "waiting_for_approval", "waiting_for_input"}:
                    results["task"] = status
                    break
                await asyncio.sleep(1)
            else:
                raise AssertionError("Task did not complete within 40 seconds")
            if results["task"]["status"] == "failed":
                errors = [m.get("error", {}) for m in results["task"]["messages"] if m.get("error")]
                if not any(e.get("type") == "provider.free-limit" for e in errors):
                    raise AssertionError(f"Unexpected task failure: {errors}")
                results["inference"] = "blocked_by_provider_free_limit"
            elif results["task"]["status"] != "completed":
                raise AssertionError(f"Unexpected task status: {results['task']['status']}")
            else:
                results["inference"] = "completed"
            print("inference", results["inference"], "recorded_cost_usd", results["task"]["session"]["cost"])
    (project / "verification.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    print("MCP verification passed;", len(results["tools"]), "tools")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--inference", action="store_true", help="Send one greeting to the configured default model after verifying zero catalog prices")
    asyncio.run(main(parser.parse_args().inference))
