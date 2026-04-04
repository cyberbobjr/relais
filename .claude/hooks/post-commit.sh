#!/bin/bash
# Hook post-commit : mise à jour docs + réindexation jCodemunch
set -e

REPO_DIR="/Users/benjaminmarchand/IdeaProjects/relais"
CLAUDE_BIN="/Users/benjaminmarchand/.local/bin/claude"
LOG="/tmp/relais_post_commit.log"

echo "[$(date)] post-commit hook démarré" >> "$LOG"

cd "$REPO_DIR"

# Lancer claude en non-interactif en arrière-plan
nohup "$CLAUDE_BIN" --dangerously-skip-permissions --model claude-haiku-4-5-20251001 -p \
"A git commit was just made in /Users/benjaminmarchand/IdeaProjects/relais. Do the following in order:

1. Run \`git diff HEAD~1 HEAD\` to see what changed in the last commit.
2. Update only the documentation files actually affected by those changes. Files to update as needed:
   - plans/RELAIS_ARCHITECTURE_COMPLETE_v12.md
   - README.md
   - docs/ARCHITECTURE.md
   - .claude/plan/relais-implementation.md
   Focus on: new or modified bricks/services, changed Redis stream names or schemas, updated configuration keys (channels.yaml, atelier/profiles.yaml, atelier/mcp_servers.yaml, atelier.yaml), modified pipeline flows, new dependencies in pyproject.toml, changed Envelope fields or ACL rules. Do NOT rewrite accurate documentation.
3. Re-index the project with jCodemunch: call mcp__jcodemunch__index_folder with path='/Users/benjaminmarchand/IdeaProjects/relais' and incremental=true.
4. For each bricks in the app, update if necessary all docstring headers in main.py to reflect change in the brick's behavior, inputs, outputs, workflow, redis stream, redis pubsub or configuration. Bricks to check are those that were modified by the commit, or that have their behavior impacted by the commit (e.g. if a brick's input schema changed, check all bricks consuming that input).
5. Update all todo plans located in \".claude/todo/\" dir, these plans focused on todo features or todo improvements that are still relevant after the commit. Update the todo plans files accordingly to changes made by the commits." \
>> "$LOG" 2>&1 &

echo "[$(date)] claude lancé en arrière-plan (PID $!)" >> "$LOG"
