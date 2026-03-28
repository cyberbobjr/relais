# Handoff Phase 2.4 — ACL réelle + Guardrails de contenu

## Fichiers créés

### `sentinelle/acl.py` — ACLManager

Charge `users.yaml` et vérifie les droits d'accès par `user_id`, canal et action.

**Classe principale : `ACLManager`**

| Méthode | Description |
|---------|-------------|
| `__init__(config_path)` | Résolution du chemin config + chargement initial |
| `is_allowed(user_id, channel, action)` | Retourne True/False selon droits |
| `get_user_role(user_id)` | Retourne "admin", "user" ou "unknown" |
| `reload()` | Recharge users.yaml depuis disque (hot-reload) |

**Chemins de recherche (ordre de priorité) :**
1. Paramètre explicite `config_path`
2. `~/.relais/users.yaml`
3. `config/users.yaml.default`

**Mode permissif :** si aucun fichier trouvé, toutes les requêtes sont autorisées avec log WARNING.
C'est le comportement existant de `sentinelle/main.py` (stub `is_authorized = True`).

**Format users.yaml attendu :**
```yaml
users:
  - id: "discord:123456789"
    name: "Benjamin"
    role: admin
    channels: ["discord", "telegram"]
  - id: "discord:987654321"
    name: "Alice"
    role: user
    channels: ["discord"]

roles:
  admin:
    actions: ["send", "admin", "config"]
  user:
    actions: ["send"]
```

---

### `sentinelle/guardrails.py` — ContentFilter

Filtres de contenu pre/post LLM, sans dépendances ML.

**Classes :**

- `GuardrailResult` — dataclass résultat : `allowed`, `reason`, `modified_text`
- `ContentFilter` — filtre principal

| Méthode | Checks appliqués |
|---------|-----------------|
| `check_input(text, user_id)` | Longueur (hard block) → patterns dangereux (hard block) |
| `check_output(text, user_id)` | Longueur (soft truncation) → patterns dangereux (hard block) |

**Valeurs par défaut :**
- `max_input_length` : 4000 chars
- `max_output_length` : 8000 chars
- Patterns input built-in : prompt injection, jailbreak, DAN

**Chemins de recherche config (ordre de priorité) :**
1. `~/.relais/guardrails.yaml`
2. `config/guardrails.yaml.default`

**Format guardrails.yaml optionnel :**
```yaml
max_input_length: 4000
max_output_length: 8000
input_patterns:
  - "(?i)ignore\\s+(all\\s+)?previous\\s+instructions"
  - "(?i)jailbreak"
output_patterns: []
```

Si le fichier est absent, les defaults built-in s'appliquent silencieusement (niveau DEBUG).

---

## Intégration dans `sentinelle/main.py`

Le stub actuel (ligne 75) :
```python
# ACL Check MVP: Allow all for now
is_authorized = True
```

Peut être remplacé par :
```python
from sentinelle.acl import ACLManager
from sentinelle.guardrails import ContentFilter

# Dans __init__ :
self.acl = ACLManager()
self.guardrails = ContentFilter()

# Dans _process_stream, après parsing de l'envelope :
is_authorized = self.acl.is_allowed(
    envelope.sender_id, envelope.channel, action="send"
)
if is_authorized:
    result = await self.guardrails.check_input(envelope.content, envelope.sender_id)
    if not result.allowed:
        is_authorized = False  # traité comme non autorisé
```

---

## Tests critiques à écrire

### Unit — `tests/sentinelle/test_acl.py`

| Test | Description |
|------|-------------|
| `test_permissive_when_no_file` | Pas de users.yaml → is_allowed retourne True, log WARNING |
| `test_known_user_allowed_channel` | User avec channel autorisé → True |
| `test_known_user_wrong_channel` | User sans channel cible → False |
| `test_known_user_wrong_action` | Role sans l'action demandée → False |
| `test_unknown_user_blocked` | user_id absent → False |
| `test_get_user_role_admin` | Admin → "admin" |
| `test_get_user_role_unknown` | user_id absent → "unknown" |
| `test_reload` | Modifier le fichier + reload → nouvelles valeurs |

### Unit — `tests/sentinelle/test_guardrails.py`

| Test | Description |
|------|-------------|
| `test_input_ok` | Texte normal → allowed=True |
| `test_input_too_long` | Texte > 4000 chars → allowed=False, reason contient "too long" |
| `test_input_prompt_injection` | "ignore previous instructions" → allowed=False |
| `test_input_jailbreak` | "jailbreak" → allowed=False |
| `test_output_ok` | Réponse normale → allowed=True, modified_text=None |
| `test_output_truncated` | Réponse > 8000 chars → allowed=True, modified_text tronqué |
| `test_output_dangerous_pattern` | Pattern dangereux → allowed=False |
| `test_invalid_pattern_skipped` | Pattern regex invalide dans config → ignoré, log WARNING |
| `test_custom_config` | Config YAML personnalisé → limites et patterns surchargés |

---

## Impact sur les autres briques

| Brique | Impact |
|--------|--------|
| **Sentinelle** | Intégration directe — remplace le stub `is_authorized = True` |
| **Vigile (futur)** | `ACLManager.reload()` peut être déclenché sur `relais:admin:reload` |
| **Portail** | Aucun — le filtrage reste dans la Sentinelle |
| **Atelier** | Guardrails output peuvent être appliqués côté Atelier post-LLM (optionnel) |

---

## Dépendances

`pyyaml >= 6.0` — déjà déclaré dans `pyproject.toml`. Aucune nouvelle dépendance requise.
