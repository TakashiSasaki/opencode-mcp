# opencode-mcp development

Read `/data/AGENTS.md` when working on this host. The shared bare repository is `/data/opencode-mcp/.bare`; edit source in `/data/opencode-mcp/main` or an explicitly requested branch worktree.

Keep stdout reserved for MCP. Preserve directory and identifier validation, credential redaction, retry idempotency, and permission boundaries. Never commit deployment credentials, tunnel identifiers, local URLs, runtime entrypoints, virtual environments, or verification output.

Validate source changes with `uv run --locked python -m unittest -v test_server.py`. `verify_mcp.py` is a live integration check that creates/updates a verification session; inference is opt-in. Do not send model prompts solely to test packaging or relocation.
