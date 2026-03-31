# Plan d'implémentation — Le Commandant

**Feature:** Commandes globales hors-LLM (`/clear`, `/dnd`, `/brb`)
**Spec fonctionnelle:** `plans/RELAIS_ARCHITECTURE_COMPLETE_v12.md` § 24
**Complexité globale:** MEDIUM

---

## Architecture

```
relais:messages:incoming
        │
        ├──► [commandant_group] ──► Commandant
        │         ACK ALL              │
        │                         detect /cmd?
        │                         ├─ NO  → ignore (ACK)
        │                         └─ YES → execute handler
        │                               ├─ /clear  → XADD relais:memory:request {action:clear}
        │                               │            XADD relais:messages:outgoing:{ch} (confirm)
        │                               ├─ /dnd    → SET relais:state:dnd 1
        │                               │            XADD relais:messages:outgoing:{ch} (confirm)
        │                               └─ /brb    → DEL relais:state:dnd
        │                                            XADD relais:messages:outgoing:{ch} (confirm)
        │
        └──► [portail_group] ──► Portail
                  ACK ALL            │
                              check relais:state:dnd?
                              ├─ SET → DROP (log only, no response)
                              └─ NOT SET → forward to relais:security (normal flow)
```

```
relais:memory:request
        │
        └──► Souvenir [souvenir_group]
                  action == "clear"?
                  └─ YES → context_store.clear(session_id)
                           long_term_store.clear_session(session_id)
```

---

## Phases d'implémentation

| Phase | Description | Fichiers | Complexité | État |
|-------|-------------|---------|-----------|------|
| 1 | Tests TDD (red first) | `tests/test_commandant.py`, `tests/test_portail_dnd.py`, `tests/test_souvenir_clear.py` | LOW | [x] |
| 2 | Brick Commandant | `commandant/__init__.py`, `commandant/command_parser.py`, `commandant/handlers.py`, `commandant/main.py` | MEDIUM | [x] |
| 3 | Portail — check DND | `portail/main.py` | LOW | [x] |
| 4 | Souvenir — action clear | `souvenir/main.py`, `souvenir/long_term_store.py` | LOW | [x] |
| 5 | Config & infrastructure | `supervisord.conf`, `config/redis.conf` | LOW | [x] |
| 6 | Documentation | `docs/REDIS_BUS_API.md`, `README.md` | LOW | [x] |

---

## Dépendances entre phases

```
Phase 1 (tests) → peut être écrite avant tout le reste
Phase 2 (brick) → indépendant de 3 et 4
Phase 3 (portail) → indépendant de 2 et 4
Phase 4 (souvenir) → indépendant de 2 et 3
Phase 5 (config) → dépend de 2, 3, 4 (doit connaître les noms de services)
```

Les phases 2, 3 et 4 peuvent être implémentées en parallèle après la phase 1.
Phase 6 (docs) dépend de 2, 3, 4, 5 — à faire en dernier une fois le code stabilisé.

---

## Registre des risques

| Risque | Probabilité | Impact | Mitigation |
|--------|------------|--------|-----------|
| Race condition: `/dnd` set pendant qu'un message est déjà dans `relais:security` | MEDIUM | LOW | Normal — la file est déjà engagée, seuls les nouveaux messages sont bloqués. Acceptable. |
| Double ACK: Commandant et Portail opèrent en parallèle sur des groupes différents — pas d'interférence | LOW | NONE | Architecture correcte par conception. |
| `/clear` efface une session active pendant qu'Atelier traite un message | LOW | LOW | Atelier reçoit quand même la réponse. Le contexte est effacé pour la prochaine session. Acceptable. |
| `relais:state:dnd` key oubliée après restart Redis | LOW | HIGH | Pas de TTL → la clé survit aux redémarrages (persistance RDB/AOF). C'est le comportement souhaité. |
| Commandant absent → commandes tombent dans le pipeline LLM | MEDIUM | MEDIUM | Supervisord avec `autorestart=true` + ordre de démarrage priority 10. |

---

## Critères de succès

- [x] `pytest tests/test_commandant.py` : 100% pass
- [x] `pytest tests/test_portail_dnd.py` : 100% pass
- [x] `pytest tests/test_souvenir_clear.py` : 100% pass
- [ ] `/clear` en Discord → réponse immédiate + Redis context vide + SQLite messages vides
- [ ] `/dnd` en Discord → réponse immédiate + messages suivants ignorés par Portail
- [ ] `/brb` en Discord → réponse immédiate + pipeline reprend normalement
- [x] Commande inconnue `/foo` → ignorée silencieusement (pas de réponse LLM, pas d'erreur)
- [ ] `supervisorctl status commandant` → RUNNING
- [x] `docs/REDIS_BUS_API.md` reflète Le Commandant (consumer groups, relais:state:dnd)
- [x] `README.md` diagrammes ASCII et Mermaid mis à jour

---

**WAITING FOR CONFIRMATION**: Proceed with this plan? (yes/no/modify)
