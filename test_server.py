"""Boundary checks independent of provider inference and user files."""

import asyncio
import json
import unittest
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock, patch

import httpx2

import server
from availability import message_observation, merge_observations


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
            result = await server.opencode_models(free_only=True, include_recent_results=False)
        self.assertEqual([m["id"] for m in result["models"]], ["free"])

    async def test_unavailable_usage_route_is_explicit(self):
        with patch.object(server, "api", AsyncMock(side_effect=server.APIError("HTTP 404", status_code=404))):
            result = await server.opencode_usage(include_recent_results=False)
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


class ModelEvidence(unittest.IsolatedAsyncioTestCase):
    def response(self, model="free", completed=2000, error=None, finish="stop"):
        return {"id": "msg_sample", "type": "assistant", "model": {"providerID": "provider", "id": model},
                "time": {"created": 1000, "completed": completed}, "finish": finish, "error": error,
                "content": [{"type": "reasoning", "text": "private reasoning"}],
                "native": {"secret": "private native data"}}

    def test_retry_time_uses_failure_time_not_poll_time_and_never_implies_balance(self):
        error = {"type": "provider.free-limit", "status": 429, "retryAfterMs": 10000,
                 "message": "private error text", "response": {"headers": {"Authorization": "private header"}}}
        message = self.response(error=error, finish="error")
        first = message_observation(message, "ses_test", 3000)
        later = message_observation(message, "ses_test", 9000)
        self.assertEqual(first["state"], "quota_exhausted")
        self.assertEqual(first["retry_at"], "1970-01-01T00:00:12.000Z")
        self.assertEqual(first["retry_at"], later["retry_at"])
        self.assertEqual(later["retry_after_seconds"], 3)
        self.assertIsNone(first["quota_remaining"])
        self.assertNotIn("private", json.dumps(first))
        expired = message_observation(message, "ses_test", 13000)
        self.assertEqual(expired["state"], "retry_due")
        self.assertEqual(expired["retry_after_seconds"], 0)
        self.assertIsNone(expired["quota_remaining"])

    def test_generic_rate_limit_and_absent_invalid_retry_values_remain_distinct(self):
        for delay in (None, -1, True, "1000", float("inf")):
            with self.subTest(delay=delay):
                result = message_observation(self.response(error={"type": "provider.rate-limit", "status": 429,
                                             "retryAfterMs": delay}, finish="error"), "ses_test", 3000)
                self.assertEqual(result["state"], "rate_limited")
                self.assertIsNone(result["retry_at"])

    def test_unfinished_interrupted_unknown_and_model_less_messages_are_not_successes(self):
        for changes in ({"finish": "unknown"}, {"finish": "error"}, {"finish": "content-filter"},
                        {"time": {"created": 1000}}, {"model": None}, {"type": "idle"}):
            with self.subTest(changes=changes):
                self.assertIsNone(message_observation({**self.response(), **changes}, "ses_test", 3000))

    def test_latest_response_wins_independent_of_session_or_message_order(self):
        failed = self.response(completed=2000, error={"type": "provider.free-limit", "status": 429}, finish="error")
        success = self.response(completed=4000)
        other = self.response(model="other", completed=2500)
        merged = {}
        merge_observations(merged, [success, other], "ses_new", 5000)
        merge_observations(merged, [failed], "ses_old", 5000)
        self.assertEqual(merged["provider/free"]["state"], "last_request_succeeded")
        self.assertEqual(merged["provider/free"]["session_id"], "ses_new")
        self.assertEqual(len(merged), 2)

    async def test_history_scope_boundaries_truncation_and_model_attribution(self):
        path = server.DEFAULT_DIRECTORY
        async def fake_api(method, route, body=None, **query):
            if route == "/api/session":
                self.assertEqual(query, {"directory": str(path), "limit": 3})
                return {"data": [
                    {"id": "ses_match", "location": {"directory": str(path)}, "model": {"providerID": "wrong", "id": "now-selected"}},
                    {"id": "ses_outside", "location": {"directory": "/etc"}},
                    {"id": "ses_invalid/../path", "location": {"directory": str(path)}}], "cursor": {"next": "more"}}
            self.assertEqual(route, "/api/session/ses_match/message")
            self.assertEqual(query, {"limit": 5, "order": "desc"})
            return {"data": [self.response()]}
        with patch.object(server, "api", fake_api):
            observations, scan = await server.recent_model_observations(path, 3)
        self.assertEqual(set(observations), {"provider/free"})
        self.assertEqual(scan["sessions_examined"], 1)
        self.assertTrue(scan["continuation_available"])
        self.assertTrue(scan["session_limit_reached"])
        self.assertTrue(scan["partial"])

    async def test_end_cursor_does_not_mean_the_session_limit_was_reached(self):
        with patch.object(server, "api", AsyncMock(return_value={"data": [], "cursor": {"next": "continuation"}})):
            observations, scan = await server.recent_model_observations(server.DEFAULT_DIRECTORY, 20)
        self.assertEqual(observations, {})
        self.assertTrue(scan["continuation_available"])
        self.assertFalse(scan["session_limit_reached"])
        self.assertFalse(scan["partial"])

    async def test_inaccessible_history_does_not_hide_catalog_or_claim_availability(self):
        models = [{"providerID": "provider", "id": "free", "enabled": True,
                   "cost": [{"input": 0, "output": 0, "cache": {"read": 0, "write": 0}}]}]
        with patch.object(server, "model_catalog", AsyncMock(return_value=models)), \
             patch.object(server, "api", AsyncMock(side_effect=server.APIError("private error"))):
            result = await server.opencode_models(free_only=True)
        self.assertEqual(result["count"], 1)
        self.assertIsNone(result["models"][0]["last_observation"])
        self.assertEqual(result["models"][0]["quota_available"], "unknown")
        self.assertEqual(result["observation_scan"]["issues"], ["session_inventory_unavailable"])
        self.assertNotIn("private", json.dumps(result))

    async def test_history_timeout_cancels_reads_and_preserves_completed_evidence(self):
        cancelled = asyncio.Event()
        path = server.DEFAULT_DIRECTORY
        async def fake_api(method, route, body=None, **query):
            if route == "/api/session":
                return {"data": [{"id": "ses_fast", "location": {"directory": str(path)}},
                                 {"id": "ses_slow", "location": {"directory": str(path)}}]}
            if route.endswith("ses_fast/message"):
                return {"data": [self.response()]}
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()
        with patch.object(server, "api", fake_api), patch.object(server, "HISTORY_TIMEOUT_SECONDS", 0.02):
            observations, scan = await server.recent_model_observations(path, 2)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(scan["sessions_examined"], 1)
        self.assertTrue(scan["partial"])
        self.assertIn("history_timeout", scan["issues"])
        self.assertIn("provider/free", observations)

    async def test_null_account_limits_are_not_reported_as_available(self):
        with patch.object(server, "api", AsyncMock(return_value={"data": None})), \
             patch.object(server, "recent_model_observations", AsyncMock(return_value=({}, None))) as history:
            result = await server.opencode_usage()
        self.assertTrue(result["supported"])
        self.assertFalse(result["limits_available"])
        self.assertEqual(result["free_promotional_model_quota"], "unknown")
        history.assert_awaited_once()

    async def test_catalog_only_mode_does_not_read_session_history(self):
        with patch.object(server, "model_catalog", AsyncMock(return_value=[])), \
             patch.object(server, "recent_model_observations", AsyncMock()) as history:
            result = await server.opencode_models(include_recent_results=False)
        history.assert_not_awaited()
        self.assertIsNone(result["observation_scan"])

    async def test_output_schemas_expose_evidence_and_missing_balance(self):
        definitions = {tool.name: tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                       for tool in await server.mcp.list_tools()}
        schema = definitions["opencode_models"]["outputSchema"]
        self.assertIn("observation_scan", schema["properties"])
        self.assertIn("last_observation", schema["$defs"]["ModelSummary"]["properties"])
        observation = schema["$defs"]["ModelObservation"]["properties"]
        self.assertEqual(observation["quota_remaining"]["type"], "null")
        self.assertIn("retry_at", observation)
        self.assertIn("limits_available", definitions["opencode_usage"]["outputSchema"]["properties"])


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
