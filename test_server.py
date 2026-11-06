"""Boundary checks independent of provider inference and user files."""

import json
import unittest
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock, patch

import httpx2

import server


class Boundaries(unittest.TestCase):
    def test_relative_directory_uses_mcp_default_not_process_cwd(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            child = root / "workspace"
            child.mkdir()
            with patch.object(server, "DEFAULT_DIRECTORY", root), patch.object(server, "ROOTS", (root,)):
                self.assertEqual(server.directory("workspace"), child)

    def test_directory_scope_resolves_symlinks_and_rejects_parent(self):
        with patch.object(server, "ROOTS", (server.DEFAULT_DIRECTORY,)):
            self.assertEqual(server.directory(str(server.DEFAULT_DIRECTORY)), server.DEFAULT_DIRECTORY)
            with self.assertRaises(ValueError):
                server.directory(str(server.DEFAULT_DIRECTORY / ".."))
            with self.assertRaises(ValueError):
                server.directory("/proc/self/root/etc")

    def test_ids_cannot_inject_paths_or_queries(self):
        for value in ("ses_a/../credential", "ses_a?password=1", "../ses_a", "ses_a#x"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                server.identifier(value, "ses_")

    def test_catalog_redacts_configuration_and_requires_known_zero_prices(self):
        model = {"id": "free", "cost": [{"input": 0, "output": 0, "cache": {"read": 0, "write": 0}}],
                 "headers": {"Authorization": "example-secret"}, "settings": {"apiKey": "example-secret"}}
        self.assertTrue(server.zero_token_price(model))
        self.assertNotIn("headers", server.model_summary(model))
        self.assertNotIn("settings", server.model_summary(model))
        for prices in (None, [], [{"input": 0}], [{"input": 1, "output": 0}], [{"input": 0, "output": 0}],
                       [{"input": 0, "output": 0, "cache": {"read": 0}}], [{"input": 0, "output": 0, "cache": {"write": 1}}]):
            with self.subTest(prices=prices):
                self.assertFalse(server.zero_token_price({"cost": prices}))

    def test_price_tier_metadata_is_not_a_charge_and_unknown_is_not_free(self):
        zero_tiers = {"cost": [{"input": 0, "output": 0, "cache": {"read": 0, "write": 0}, "tier": {"type": "context", "size": 100000}}]}
        self.assertEqual(server.price_status(zero_tiers), "zero_catalog_price")
        self.assertEqual(server.price_status({"cost": [{"input": 0, "output": 2}]}), "paid")
        self.assertEqual(server.price_status({"cost": []}), "unknown")
        self.assertIsNone(server.model_summary({"cost": []})["free_by_catalog"])

    def test_reasoning_efforts_exposed_without_variant_secrets(self):
        model = {"id": "beam-free", "providerID": "opencode", "variants": [
            {"id": "high", "headers": {"Authorization": "example-secret"}, "settings": {"reasoningEffort": "high"}}
        ], "cost": [{"input": 0, "output": 0, "cache": {"read": 0, "write": 0}}]}
        result = server.model_summary(model)
        self.assertEqual(result["reasoning_efforts"], ["high"])
        self.assertTrue(result["free_by_catalog"])
        self.assertNotIn("example-secret", str(result))

    def test_status_does_not_confuse_waiting_or_idle_with_completion(self):
        previous_success = {"outcome": "succeeded"}
        cases = [
            ({}, False, [], [], [], "idle"),
            (previous_success, True, [], [], [], "running"),
            (previous_success, False, [{}], [], [], "queued"),
            (previous_success, True, [], [{}], [], "waiting_for_approval"),
            (previous_success, True, [], [], [{}], "waiting_for_input"),
            (previous_success, False, [], [], [], "completed"),
            ({"outcome": "failed"}, False, [], [], [], "failed"),
        ]
        for *args, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(server.execution_status(*args), expected)

    def test_projected_output_omits_reasoning_and_native_payload(self):
        message = {"type": "assistant", "native": {"secret": "value"}, "content": [
            {"type": "reasoning", "text": "private reasoning"},
            {"type": "text", "text": "finished"},
            {"type": "tool", "name": "bash", "state": {"status": "completed", "input": {"command": "secret"}}},
        ]}
        result = server.summarize_message(message, 4)
        self.assertEqual(result["text"], "fini")
        self.assertTrue(result["text_truncated"])
        self.assertEqual(result["tools"][0]["state"], "completed")
        self.assertNotIn("private reasoning", str(result))
        self.assertNotIn("secret", str(result))


class WorkspaceBoundaries(unittest.IsolatedAsyncioTestCase):
    async def test_workspace_inventory_distinguishes_loaded_saved_missing_and_global(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            loaded = root / "loaded"
            saved = root / "saved"
            loaded.mkdir()
            saved.mkdir()
            missing = root / "removed"
            projects = [{"id": "project1", "canonical": str(root), "sandboxes": []},
                        {"id": "global", "canonical": "/", "sandboxes": []}]

            async def fake_api(method, path, body=None, **query):
                self.assertEqual(method, "get")
                if path == "/api/project":
                    return projects
                if path == "/api/debug/location":
                    return [{"directory": str(loaded)}]
                if path == "/api/worktree":
                    return [] if query["projectID"] == "global" else [
                        {"directory": str(saved)}, {"directory": str(missing)}, {"directory": str(root)}]
                self.fail(f"Inventory unexpectedly queried {path}")

            with patch.object(server, "ROOTS", (root,)), patch.object(server, "DEFAULT_DIRECTORY", root), patch.object(server, "api", fake_api):
                result = await server.opencode_workspaces()
                paths = {item["directory"]: item for item in result["workspaces"]}
                self.assertEqual(len(paths), 4)
                self.assertNotIn("/", paths)
                self.assertTrue(paths[str(loaded)]["loaded"])
                self.assertEqual(paths[str(loaded)]["project_ids"], ["project1"])
                self.assertFalse(paths[str(saved)]["loaded"])
                self.assertTrue(paths[str(saved)]["selectable"])
                self.assertFalse(paths[str(missing)]["exists"])
                self.assertFalse(paths[str(missing)]["selectable"])
                self.assertEqual(set(paths[str(root)]["sources"]), {"project_root", "saved_worktree"})
                filtered = await server.opencode_workspaces(loaded_only=True)
                self.assertEqual([item["directory"] for item in filtered["workspaces"]], [str(loaded)])
                existing = await server.opencode_workspaces(existing_only=True)
                self.assertEqual(existing["count"], 3)

    async def test_unknown_project_is_rejected(self):
        async def fake_api(method, path, body=None, **query):
            return []
        with patch.object(server, "api", fake_api), self.assertRaises(ValueError):
            await server.opencode_workspaces(project_id="not-known")

    async def test_scoped_workspaces_excludes_loaded_directories_from_other_project(self):
        root = server.DEFAULT_DIRECTORY
        projects = [{"id": "parent", "canonical": str(root.parent)}, {"id": "child", "canonical": str(root)}]
        async def fake_api(method, path, body=None, **query):
            if path == "/api/project": return projects
            if path == "/api/debug/location": return [{"directory": str(root / "opencode-mcp")}]
            if path == "/api/worktree": return []
            self.fail(path)
        with patch.object(server, "api", fake_api):
            result = await server.opencode_workspaces(project_id="parent")
        self.assertEqual([item["directory"] for item in result["workspaces"]], [str(root.parent)])

    async def test_workspace_resolution_sends_selected_directory_as_deep_object_query(self):
        api = AsyncMock(return_value={"directory": str(server.DEFAULT_DIRECTORY)})
        with patch.object(server, "api", api):
            await server.opencode_workspace(str(server.DEFAULT_DIRECTORY))
        api.assert_awaited_once_with("get", "/api/location", **{"location[directory]": str(server.DEFAULT_DIRECTORY)})

    async def test_project_summary_does_not_return_setup_commands(self):
        project = {"id": "p", "canonical": str(server.DEFAULT_DIRECTORY), "commands": {"start": "private-command"}}
        self.assertNotIn("private-command", str(server.project_summary(project)))

    async def test_move_reports_queued_destination_without_claiming_applied(self):
        current = {"location": {"directory": str(server.DEFAULT_DIRECTORY)}}
        target = server.PROJECT
        api = AsyncMock(return_value=None)
        with patch.object(server, "checked_session", AsyncMock(return_value=current)), patch.object(server, "api", api):
            result = await server.opencode_move_task("ses_test", str(target))
        api.assert_awaited_once_with("post", "/api/session/ses_test/move", {"directory": str(target), "delivery": "queue"})
        self.assertFalse(result["applied"])
        self.assertEqual(result["requested_directory"], str(target))

    async def test_invalid_move_target_is_rejected_before_mutation(self):
        current = {"location": {"directory": str(server.DEFAULT_DIRECTORY)}}
        api = AsyncMock()
        with patch.object(server, "ROOTS", (server.DEFAULT_DIRECTORY,)), patch.object(server, "checked_session", AsyncMock(return_value=current)), patch.object(server, "api", api):
            with self.assertRaises(ValueError):
                await server.opencode_move_task("ses_test", "/etc")
        api.assert_not_awaited()

    async def test_task_creation_forwards_the_explicit_directory(self):
        target = server.PROJECT
        store = {}
        async def fake_api(method, path, body=None, **query):
            if method == "get":
                if "session" not in store:
                    raise server.APIError("missing", "SessionNotFoundError", 404)
                return {"data": store["session"]}
            self.assertEqual((method, path), ("post", "/api/session"))
            store["session"] = body
            return {"data": body}
        selected_model = AsyncMock(return_value={"providerID": "opencode", "id": "beam-free", "variant": "high"})
        submit = AsyncMock(return_value={"session_id": "ses_test"})
        with patch.object(server, "api", fake_api), patch.object(server, "select_model", selected_model), patch.object(server, "submit_message", submit):
            await server.opencode_start_task("explain", "location-boundary-test", str(target), model="opencode/beam-free", reasoning_effort="high")
        self.assertEqual(store["session"]["location"], {"directory": str(target)})
        self.assertEqual(store["session"]["model"]["variant"], "high")
        selected_model.assert_awaited_once_with(target, "opencode/beam-free", "high", require_zero=False)


class RetryBoundaries(unittest.IsolatedAsyncioTestCase):
    async def test_task_retry_reports_current_workspace_after_move(self):
        requested = server.DEFAULT_DIRECTORY
        moved = server.PROJECT
        prompt, key, title = "continue", "workspace-retry", "workspace test"
        payload = server.request_hash([prompt, str(requested), title, server.DEFAULT_MODEL])
        prior = {"metadata": {"opencode_mcp": {"payload_hash": payload}}, "location": {"directory": str(moved)}}
        submit = AsyncMock(return_value={"reused": True})
        with patch.object(server, "api", AsyncMock(return_value={"data": prior})), patch.object(server, "submit_message", submit):
            result = await server.opencode_start_task(prompt, key, str(requested), title)
        self.assertEqual(result["directory"], str(moved))
        self.assertEqual(result["requested_directory"], str(requested))

    async def test_effort_maps_to_variant_and_unsupported_effort_fails(self):
        catalog = AsyncMock(return_value=[{"providerID": "opencode", "id": "beam-free", "enabled": True,
                                           "variants": [{"id": "high"}], "cost": [{"input": 0, "output": 0, "cache": {"read": 0, "write": 0}}]}])
        with patch.object(server, "model_catalog", catalog):
            ref = await server.select_model(server.DEFAULT_DIRECTORY, "opencode/beam-free", "high", require_zero=True)
            self.assertEqual(ref, {"providerID": "opencode", "id": "beam-free", "variant": "high"})
            with self.assertRaises(ValueError):
                await server.select_model(server.DEFAULT_DIRECTORY, "opencode/beam-free", "invented")

    async def test_free_filter_does_not_include_paid_or_unknown_models(self):
        catalog = AsyncMock(return_value=[
            {"id": "free", "enabled": True, "cost": [{"input": 0, "output": 0, "cache": {"read": 0, "write": 0}}]},
            {"id": "paid", "enabled": True, "cost": [{"input": 0, "output": 1}]},
            {"id": "unknown", "enabled": True, "cost": []},
            {"id": "disabled", "enabled": False, "cost": [{"input": 0, "output": 0, "cache": {"read": 0, "write": 0}}]},
        ])
        with patch.object(server, "model_catalog", catalog):
            result = await server.opencode_models(free_only=True)
        self.assertEqual([m["id"] for m in result["models"]], ["free"])

    async def test_unavailable_usage_route_is_explicit(self):
        with patch.object(server, "api", AsyncMock(side_effect=server.APIError("HTTP 404", status_code=404))):
            result = await server.opencode_usage()
        self.assertFalse(result["supported"])
        self.assertIsNone(result["limits"])

    async def test_retry_of_enqueued_message_does_not_post_again(self):
        session_id = "ses_test"
        message_id = "msg_mcp_" + server.request_hash([session_id, "request-1"])[:32]
        mock = AsyncMock(return_value={"data": [{"id": message_id, "payload": {"text": "hello"}}]})
        with patch.object(server, "api", mock):
            result = await server.submit_message(session_id, "hello", "request-1")
        self.assertTrue(result["reused"])
        self.assertEqual(mock.await_count, 1)

    async def test_reusing_request_id_with_changed_text_fails(self):
        message_id = "msg_mcp_" + server.request_hash(["ses_test", "request-1"])[:32]
        mock = AsyncMock(return_value={"data": [{"id": message_id, "payload": {"text": "original"}}]})
        with patch.object(server, "api", mock), self.assertRaises(ValueError):
            await server.submit_message("ses_test", "different", "request-1")
        self.assertEqual(mock.await_count, 1)


class TransportBoundaries(unittest.IsolatedAsyncioTestCase):
    async def test_native_contract_matches_catalog_and_omits_unrequested_bodies(self):
        with patch.object(server, "api", AsyncMock(return_value={"version": "test-backend"})) as api:
            info = await server.opencode_info()
            self.assertIsNone(info.contract)
            self.assertEqual(info.transport, "stdio")
            self.assertIsNone(info.http_path)
            self.assertFalse(info.read_only_enforced)
            self.assertTrue(info.hostname)
            definitions = [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in await server.mcp.list_tools()]
            for definition in definitions:
                for name, field in definition["inputSchema"].get("properties", {}).items():
                    with self.subTest(tool=definition["name"], parameter=name):
                        self.assertTrue(field.get("description"))
            self.assertEqual(info.tool_names, sorted(item["name"] for item in definitions))
            self.assertEqual(info.tool_count, len(definitions))
            requested = await server.opencode_info("opencode_start_task", True)
            self.assertEqual(json.loads(requested.contract.tool_json), next(item for item in definitions if item["name"] == "opencode_start_task"))
            self.assertEqual(requested.contract.instructions, server.SERVER_INSTRUCTIONS)
            self.assertEqual(info.catalog_sha256, server.request_hash(sorted(definitions, key=lambda item: item["name"])))
            api.reset_mock()
            with self.assertRaises(server.InputError):
                await server.opencode_info("../../credentials")
            api.assert_not_awaited()

    async def test_http_discovery_and_proxy_host_origin_validation(self):
        app = server.http_app(["mcp.example.test"], ["https://mcp.example.test"])
        headers = {"Accept": "application/json, text/event-stream"}
        initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
        with patch.object(server, "TRANSPORT", "streamable-http"), patch.object(server, "api", AsyncMock(return_value={"version": "test-backend"})):
            async with app.router.lifespan_context(app), httpx2.AsyncClient(transport=httpx2.ASGITransport(app), base_url="http://127.0.0.1:8791", headers=headers) as client:
                health = await client.get("/healthz")
                self.assertEqual(health.status_code, 200)
                self.assertFalse(health.json()["backend_checked"])
                init = await client.post("/mcp", json=initialize)
                self.assertEqual(init.status_code, 200, init.text)
                self.assertEqual(init.json()["result"]["serverInfo"]["version"], server.VERSION)
                catalog = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
                names = sorted(item["name"] for item in catalog.json()["result"]["tools"])
                call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "opencode_info", "arguments": {}}}
                response = await client.post("/mcp", json=call)
                result = response.json()["result"]
                self.assertFalse(result.get("isError", False))
                info = result["structuredContent"]
                self.assertEqual(info["tool_names"], names)
                self.assertEqual(info["transport"], "streamable-http")
                self.assertEqual(info["http_path"], "/mcp")
                proxy = {"Host": "mcp.example.test", "Origin": "https://mcp.example.test"}
                self.assertEqual((await client.post("/mcp", json=call, headers=proxy)).status_code, 200)
                self.assertEqual((await client.post("/mcp", json=call, headers={"Host": "untrusted.test"})).status_code, 421)
                self.assertEqual((await client.post("/mcp", json=call, headers={"Origin": "https://untrusted.test"})).status_code, 403)
                self.assertNotIn("mcp-session-id", init.headers)


if __name__ == "__main__":
    unittest.main()
