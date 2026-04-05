# Plan — Registre de sous-agents avec auto-discovery

> **SUPERSEDED** — Ce plan décrit l'implémentation initiale basée sur des modules Python
> (`atelier/agents/`).  Cette architecture a été remplacée par le format répertoire
> (`config/atelier/subagents/<name>/`).  Voir `plans/SUBAGENTS_YAML_MIGRATION.md` pour la
> conception actuelle.

> ~~Remplace le câblage hardcodé du sous-agent config-admin par un système
> de plugins sous `atelier/agents/`. Chaque sous-agent est un module Python
> exposant un protocole minimal. Un registre auto-découvre les modules au
> démarrage, filtre par `allowed_subagents` dans le user_record, et assemble
> dynamiquement le prompt de délégation.

**Statut** : Implémenté
**Branche** : main

---

## Architecture

```
atelier/agents/
├── __init__.py           # Re-exporte SubagentRegistry
├── _protocol.py          # Protocol SubagentModule (type-checking + doc)
├── _registry.py          # SubagentRegistry — discover, filter, assemble
└── config_admin.py       # Premier sous-agent : gestion de la configuration
```

### Protocole sous-agent (3 noms)

Chaque module sous `atelier/agents/` (sauf `_*`) expose :

| Nom | Type | Rôle |
|-----|------|------|
| `SPEC_NAME` | `str` | Identifiant unique (ex: `"config-admin"`) |
| `build_spec()` | `-> dict` | Retourne le spec deepagents `{"name", "description", "system_prompt"}` |
| `delegation_snippet()` | `-> str` | Snippet markdown pour le prompt de délégation |

Pas de `ALLOWED_ROLES` — le contrôle d'accès est au niveau utilisateur.

### SubagentRegistry

```python
@dataclass(frozen=True)
class SubagentRegistry:
    @classmethod
    def discover(cls) -> SubagentRegistry
        # pkgutil.iter_modules sur atelier.agents, skip _*, valide duck-typing

    def specs_for_user(self, user_record: dict) -> list[dict]
        # Filtre par allowed_subagents (fnmatch sur SPEC_NAME)

    def delegation_prompt_for_user(self, user_record: dict) -> str
        # Preamble + snippets des sous-agents autorisés, ou "" si aucun

    @property
    def all_names(self) -> frozenset[str]
```

### Contrôle d'accès

Le champ `allowed_subagents` est défini dans les rôles (`portail.yaml`) :

```yaml
roles:
  admin:
    allowed_subagents: ["*"]        # tous les sous-agents
  user:
    allowed_subagents: []            # aucun
  guest:
    allowed_subagents: []
```

Portail stampe `allowed_subagents` dans `user_record` via `UserRecord.to_dict()`.
La registry lit `user_record.get("allowed_subagents")` et filtre par fnmatch.

### Flux dans Atelier

1. `Atelier.__init__()` : `self._subagent_registry = SubagentRegistry.discover()`
2. `_handle_message()` :
   - `subagents = self._subagent_registry.specs_for_user(ur)`
   - `delegation_prompt = self._subagent_registry.delegation_prompt_for_user(ur)`
3. `AgentExecutor.__init__()` :
   - Reçoit `subagents=` et `delegation_prompt=`
   - `_enrich_system_prompt(soul_prompt, delegation_prompt=...)` injecte le texte

### Ajouter un nouveau sous-agent

1. Créer `atelier/agents/{name}.py` :
   ```python
   SPEC_NAME = "my-agent"
   def build_spec() -> dict: ...
   def delegation_snippet() -> str: ...
   ```
2. Ajouter `"my-agent"` (ou `"*"`) dans `allowed_subagents` du rôle concerné
3. Aucune modification de `agent_executor.py` ou `main.py`

---

## Fichiers modifiés/créés

| Fichier | Action |
|---------|--------|
| `atelier/agents/__init__.py` | Créé |
| `atelier/agents/_protocol.py` | Créé |
| `atelier/agents/_registry.py` | Créé |
| `atelier/agents/config_admin.py` | Créé (migré depuis config_admin_prompt.py) |
| `atelier/config_admin_prompt.py` | Supprimé |
| `atelier/agent_executor.py` | Modifié — `delegation_prompt` string au lieu de bool |
| `atelier/main.py` | Modifié — registry au lieu de hardcoded |
| `common/user_record.py` | Modifié — champ `allowed_subagents` |
| `portail/user_registry.py` | Modifié — stamping `allowed_subagents` |
| `.relais/config/portail.yaml` | Modifié — `allowed_subagents` par rôle |
| `config/portail.yaml.default` | Modifié — template avec `allowed_subagents` |
| `tests/test_config_admin_subagent.py` | Réécrit — 36 tests (registry + protocol + gating) |
| `tests/test_*.py` (5 fichiers) | Modifiés — ajout `allowed_subagents=[]` aux UserRecord |

## Décisions clés

- **Modules, pas classes** — interface la plus légère (3 noms à exposer)
- **Preamble dans _registry.py** — l'executor ne mentionne jamais de sous-agent
- **allowed_subagents au niveau user_record** — cohérent avec allowed_mcp_tools
- **Broken module = skip + warning** — un sous-agent cassé ne crash pas Atelier
