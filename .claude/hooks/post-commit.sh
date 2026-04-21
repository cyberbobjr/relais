#!/bin/bash
# Hook post-commit : mise à jour docs + réindexation jCodemunch
set -e

REPO_DIR="/Users/benjaminmarchand/IdeaProjects/relais"
CLAUDE_BIN="/Users/benjaminmarchand/.local/bin/claude"
LOG="/tmp/relais_post_commit.log"

# Filtre : ne s'exécute que si la commande Bash contient "git commit"
INPUT=$(cat)
if ! echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if 'git commit' in d.get('tool_input',{}).get('command','') else 1)" 2>/dev/null; then
  exit 0
fi

echo "[$(date)] post-commit hook démarré" >> "$LOG"

cd "$REPO_DIR"

# Reconstruire le graph de dépendances code-review-graph
echo "[$(date)] Lancement de code-review-graph build..." >> "$LOG"
if command -v code-review-graph &>/dev/null; then
  code-review-graph build 2>&1 | tee -a "$LOG"
  echo "[$(date)] code-review-graph build terminé (exit $?)" >> "$LOG"
else
  echo "[$(date)] WARN: code-review-graph introuvable, graph non mis à jour" >> "$LOG"
fi

# Lancer claude en non-interactif en arrière-plan
nohup "$CLAUDE_BIN" --dangerously-skip-permissions --add-dir "/Users/benjaminmarchand/IdeaProjects/relais/.claude" -p \
"A git commit was just made in /Users/benjaminmarchand/IdeaProjects/relais. Do the following in order:

1. Run \`git diff HEAD~1 HEAD\` to see what changed in the last commit.
2. Update only the documentation files actually affected by those changes. Files to update as needed:
   - plans/RELAIS_ARCHITECTURE_COMPLETE_v12.md
   - README.md
   - docs/ARCHITECTURE.md
   - .claude/plan/relais-implementation.md
   Focus on: new or modified bricks/services, changed Redis stream names or schemas, updated configuration keys (aiguilleur.yaml, atelier/profiles.yaml, atelier/mcp_servers.yaml, atelier.yaml), modified pipeline flows, new dependencies in pyproject.toml, changed Envelope fields or ACL rules. Do NOT rewrite accurate documentation.
3. For each bricks in the app, update if necessary all docstring headers in main.py to reflect change in the brick's behavior, inputs, outputs, workflow, redis stream, redis pubsub or configuration. Bricks to check are those that were modified by the commit, or that have their behavior impacted by the commit (e.g. if a brick's input schema changed, check all bricks consuming that input).
4. Update all todo plans located in \".claude/todo/\" dir, these plans focused on todo features or todo improvements that are still relevant after the commit. Update the todo plans files accordingly to changes made by the commits." \
>> "$LOG" 2>&1 &

echo "[$(date)] claude lancé en arrière-plan (PID $!)" >> "$LOG"
