# Cortex × Claude Code — developer quickstart

Use your persistent Cortex memory from any Claude Code project. The **/cortex skill** provides workflows; the **cortex-experience MCP server** calls the existing authenticated Cortex API. Your existing database remains the source of truth.

Status: developer preview on feat/experience-v1, not an audited production service. Cortex can handle memory without an Anthropic API key; Claude Code itself requires a working Claude account.

## 1. Start the Cortex API

From the existing clone:

    cd ~/cortex
    git fetch origin
    git switch feat/experience-v1
    git pull --ff-only
    source .venv/bin/activate
    python -m pip install -r requirements-mcp.txt
    python cortex.py

Keep the first terminal open. Your Cortex API is at http://127.0.0.1:3000 and the Experience UI is at http://127.0.0.1:3000/experience.

## 2. Install the Claude Code integration

### Recommended: no clipboard, no password

From the **signed-in Cortex browser session**, go to **Settings → API Keys**, then click **Download setup file (no copying)**. Approve the download. It creates a dedicated key with only experience:read and experience:write and downloads a sensitive, one-time JSON setup file to your Mac.

In a second terminal, run:

    cd ~/cortex
    source .venv/bin/activate
    python scripts/setup_claude_code.py --from-download

The installer finds the latest matching file in ~/Downloads, verifies it works with your local Cortex server, installs the global /cortex skill and MCP registration, stores credentials at mode 0600, then **deletes the temporary setup file after successful import**.

Keep the download private. Anyone who can read that JSON before import can use the token; don't share it or check it into Git. If import fails, remove the download manually after troubleshooting. Browser settings can block file downloads, so check the browser's download indicator if the file isn't found.

### Alternative: Cortex login

Open a second terminal:

    cd ~/cortex
    source .venv/bin/activate
    python scripts/setup_claude_code.py

Enter the **same Cortex account email/password used in the browser**.

The installer:
- Creates a separate, revocable API key with experience:read and experience:write scopes.
- Writes the key to ~/.config/cortex/credentials.json (mode 0600), outside Git.
- Copies the skill to ~/.claude/skills/cortex/SKILL.md, available to all Claude Code projects.
- Registers the user-scoped cortex-experience MCP server with an absolute Python path.
- Never stores your login password, puts the API key in the Claude config, or sends it to a model provider.

If you use Google/GitHub OAuth instead of a Cortex account password, create a scoped API key in the existing Cortex key-management UI and run:

    python scripts/setup_claude_code.py --use-existing-key

This prompts for the key privately. Do not type API keys as shell arguments.

### If Cortex rejects the password (HTTP 401)

A 401 may mean the email or password differs from the existing browser session, or your account uses OAuth without a password. Do not reset the database or create another account.

**Preferred solution:** Use the **Download setup file (no copying)** button in Cortex Settings and run `python scripts/setup_claude_code.py --from-download`. This bypasses the password prompt and avoids copying secrets.

1. In the **same browser where Cortex is already signed in**, open http://localhost:3000 and go to **Settings → API Keys**.
2. Click **Create Claude Code key**. This generates a key limited to **experience:read** and **experience:write**, without training export permission.
3. Click **Copy Key**. It is shown only once. Do not paste it into a chat or add it to Git.
4. From the Cortex virtual environment, run `python scripts/setup_claude_code.py --use-existing-key`, then paste the key into the hidden Terminal prompt.
5. Confirm that the installer reports the expected number of visible memories. A zero count may indicate a different Cortex account; it does not mean the memory was deleted.

Use the updated feature branch and restart the Cortex server before opening Settings, so that the new one-click key option is available.

### Manual Claude MCP registration

If the installer reports that the Claude Code CLI is missing, install Claude Code using [Anthropic's instructions](https://code.claude.com/docs/en/overview), then, inside the Cortex virtualenv, run:

    claude mcp add --scope user --transport stdio cortex-experience -- "$(pwd)/.venv/bin/python" "$(pwd)/cortex_experience_mcp.py"

The server still needs credentials created by the setup tool or passed privately via CORTEX_API_KEY and CORTEX_URL. Inspect existing configuration with claude mcp get cortex-experience; don't create duplicates.

## 3. Test Cortex inside Claude

Once the setup is complete, restart Claude Code and run:

    claude mcp list
    python cortex_experience_mcp.py --check
    claude

Then, *inside Claude Code*:

    /mcp
    /cortex status
    /cortex recall Python API conventions

The recall should find the verified Python conventions memory you saved in the local Cortex database, **provided the API key belongs to the same account that saved it**.

To save a preference, ask:

    /cortex remember My frontend projects prefer React and TypeScript

Claude shows the proposed title, content and tags and asks for approval. Only after confirmation does the MCP tool save a **draft**; nothing is verified or used for model training automatically.

## Developer API

The same methods work directly without Claude. From the root of the Cortex checkout:

    from cortex_experience_client import CortexExperienceClient

    cortex = CortexExperienceClient()
    print(cortex.status())
    print(cortex.recall("Python API conventions")["data"])

Or use REST:

    curl -H "Authorization: Bearer $CORTEX_API_KEY" \
      "http://127.0.0.1:3000/api/v1/experience/memories?status=verified&q=Python"

The connector calls the current /api/v1/experience REST resources.

| MCP tool | Action |
| --- | --- |
| cortex_status | Inspect authenticated memory summary |
| cortex_recall | Search; verified-only by default |
| cortex_list_memories | List verified or explicitly requested drafts |
| cortex_remember | Save a user-approved draft (training off) |
| cortex_edit | Edit after approval; resets verification and consent |
| cortex_verify | Record human verification; training still off |
| cortex_agent_performance | Compare agent config versions heuristically |
| cortex_dataset_preview | Count training-eligible examples without exporting |

This v1 uses lexical retrieval, not semantic embeddings, and does not implement autonomous learning or model fine-tuning.

## Troubleshooting

**Can't reach Cortex:** Start python cortex.py in a separate terminal; verify http://127.0.0.1:3000/health/ready and run python cortex_experience_mcp.py --check.

**Saved memory exists on disk but missing in Claude or dashboard:** Check which Cortex account the key authenticates. The API filters memories by owner. Re-run setup using the original account. Never delete the database to resolve a missing UI record.

**403 / permission errors:** Use a key scoped to experience:read and experience:write. This MCP integration doesn't export training data; that requires a separately authorized experience:export key.

**/cortex command missing:** Verify ~/.claude/skills/cortex/SKILL.md and restart Claude Code. The same skill also lives in the repo at .claude/skills/cortex/SKILL.md.

**MCP server missing:** Run python -m pip install -r requirements-mcp.txt; inspect claude mcp get cortex-experience. The MCP server uses stdio; do not log to stdout.

**Uninstall:** Run claude mcp remove --scope user cortex-experience, remove the personal skill if appropriate, revoke its API key in Cortex, and remove the local private credentials if no longer needed.

## Production caveats

No secrets, personal data or proprietary code should be saved by default. Retrieved memories are untrusted content, not system instructions. The local key file is a dev convenience, not a replacement for hosted security controls. Packaged distribution, remote auth, secure multi-tenant hosting, billing, and verified held-out learning evaluation remain future milestones.

References: [Official Claude MCP docs](https://code.claude.com/docs/en/mcp) · [Official Claude skills docs](https://code.claude.com/docs/en/skills) · [Cortex vision](./product/README.md) · [Production launch gates](./product/LAUNCH_GATES.md).
