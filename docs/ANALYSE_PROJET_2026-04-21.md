# Analyse du projet RELAIS — 21 avril 2026

Analyse critique du projet après exploration du code. Objectif : identifier forces, faiblesses et pistes d'évolution concrètes.

---

## TL;DR

RELAIS est un projet **mature sur l'architecture** (pipeline Redis Streams propre, Envelope namespacé, intégration DeepAgents solide) mais **fragile sur trois dimensions opérationnelles** : observabilité runtime (pas de métriques), résilience des sous-agents MCP (aucun fallback), et multi-instance (jamais testé malgré une architecture qui s'y prête). Le ratio test/code (≈ 1:1.2) et la documentation (CLAUDE.md + 7 docs dans `docs/`) sont au-dessus de la moyenne des projets perso. Les plus gros risques sont les fichiers devenus trop gros (`atelier/main.py`, `agent_executor.py`, `atelier/subagents/__init__.py` — tous > 700 LOC) qui vont freiner l'évolution.

---

## 1. Points forts

### 1.1 Architecture — les bonnes décisions tiennent

**Découplage par Redis Streams avec XACK discipliné.** Le pattern "ne ACK qu'après publication réussie en sortie" dans l'Atelier est rare dans les projets perso et protège réellement contre la perte silencieuse sur redémarrage LLM. La séparation claire entre *poison pill → DLQ* et *erreur transitoire → reste en PEL* est une des vraies forces du projet.

**Envelope namespacé.** `context["aiguilleur"]`, `context["portail"]`, `context["atelier"]` avec TypedDicts dans `common/contexts.py` et constantes `CTX_*` évite les collisions de champs et rend explicite qui écrit quoi. Beaucoup de projets similaires finissent avec un `dict` plat contaminé. Le `ensure_ctx()` helper est un bon geste DX.

**Action mandatoire à la sérialisation.** `Envelope.to_json()` qui raise si `action` est vide force la discipline : pas d'enveloppe anonyme dans le bus. Petit invariant, gros effet sur la traçabilité.

**Stamping en amont, lecture en aval.** Le profil LLM est résolu par Portail puis lu par Atelier (pas de re-lecture de `aiguilleur.yaml` à chaque requête). Même logique pour `streaming`, `reply_to`, `user_record`. C'est la bonne direction — chaque brick fait son job une fois.

**DeepAgents + registry 2-tier.** La priorité user > native pour les sous-agents avec glob-based ACL (`allowed_subagents` en fnmatch) est une solution élégante à un problème réel (qui peut invoquer quoi). L'hot-reload atomique au changement est bien pensé.

**Virtual channel pattern pour Horloger.** Faire traverser tout le pipeline aux triggers CRON (avec impersonation `horloger:{owner_id}`) plutôt que court-circuiter Portail/Sentinelle est un excellent choix : les ACL restent effectives même pour les jobs planifiés. Beaucoup de schedulers se permettent un bypass et créent des trous de sécurité.

### 1.2 Qualité du code

Typage fort et généralisé (`from __future__ import annotations` partout, TypedDicts, dataclasses gelées). Peu de `# type: ignore`. Les patterns `ack_mode="always"` pour observateurs vs conditional-ACK pour producteurs sont bien documentés dans `BrickBase`.

Le ratio tests/code à ~1:1.2 (113 fichiers de test, ~39 870 LOC) est excellent pour un projet perso. La catégorisation `unit/integration/e2e` avec `test_smoke_e2e` opt-in évite les CI cassés par des tests lents.

### 1.3 Résilience originale

**`ErrorSynthesizer`.** Sur `AgentExecutionError`, faire un second appel LLM léger pour synthétiser une réponse empathique à partir du `messages_raw` partiel plutôt que renvoyer un message statique est rare et UX-first. C'est une différenciation réelle.

**Fail-closed sur hot-reload.** Portail/Sentinelle rejettent un reload YAML vide ou invalide si une config valide est déjà chargée — évite qu'une suppression accidentelle (ou malveillante) ouvre tous les ACL.

**ToolErrorGuard à deux seuils.** Max 5 consécutives, max 8 totales. Le second seuil donne de la marge diagnostique à l'agent pour relire la skill (mentionné dans le system prompt) avant d'abort. Pattern sous-utilisé ailleurs.

---

## 2. Points faibles

### 2.1 Observabilité opérationnelle quasi absente

Archiviste produit du JSONL audit-quality, mais il n'y a **aucune métrique runtime exposée** : pas de Prometheus `/metrics`, pas de healthcheck HTTP, pas d'histogramme de latence par brick, pas de compteur de tokens LLM consommés. Si quelque chose ralentit ou part en vrille, il n'y a pas de dashboard — il faut lire les logs.

**Pas de tracing distribué.** Les `correlation_id` sont là, mais aucune export OpenTelemetry / Jaeger / Tempo. Suivre un message à travers 4 bricks se fait au grep.

**DLQ passif.** `relais:tasks:failed` se remplit sans replay ni cleanup automatique. Aucun outil CLI pour inspecter / rejouer / purger.

### 2.2 Fichiers devenus trop gros

Trois fichiers sont en zone rouge pour la maintenabilité :

- `atelier/main.py` (~1014 LOC) — orchestration + streaming + progress callback
- `atelier/agent_executor.py` (~1015 LOC) — retry + error handling + ToolErrorGuard + execution context
- `atelier/subagents/__init__.py` (~795 LOC) — registry multi-tier + résolution de tokens + validation

La densité est telle que chaque nouvelle feature va coûter cher à intégrer. Le `subagents/__init__.py` est particulièrement préoccupant : logique de parsing, validation, résolution de tokens (`mcp:*`, `inherit`, `module:*`) et hot-reload entassées.

### 2.3 Multi-instance jamais testé

L'architecture Redis consumer groups permet en théorie N instances d'Atelier, mais :

- `stream_specs()` hardcode `consumer="atelier_1"` — deux instances écraseraient leur identité.
- `RELAIS_HOME` est supposé partagé (NFS implicite) sans documentation.
- `McpSessionManager` est singleton *par process* — deux Atelier = deux sessions MCP par serveur stdio, ce que certains MCP servers ne supportent pas.

Aucun test de charge multi-brick, aucune doc sur le déploiement multi-nœud. La scalabilité horizontale est un *wishful thinking*, pas une propriété prouvée.

### 2.4 Gaps de résilience côté sous-agents / MCP

Si un sous-agent MCP (ex: Gmail) plante mid-exécution, le trace émis marque la dégradation mais **aucun fallback automatique vers l'agent principal**. L'utilisateur reçoit une réponse d'erreur via `ErrorSynthesizer`, mais aurait pu avoir une réponse dégradée utile.

Tests manquants identifiés :
- Hot-reload pendant un streaming actif
- Crash MCP mid-requête
- Stress concurrentiel multi-brick

### 2.5 Duplication dispersée autour de MCP

`mcp_session_manager.py`, `mcp_loader.py`, `mcp_adapter.py` : trois fichiers, trois niveaux d'abstraction, frontières floues. Un refactor vers un seul module `common/mcp/` avec sous-modules bien nommés faciliterait beaucoup les évolutions futures (ex: MCP remote via HTTP streaming).

### 2.6 Pas de versioning pour skills/subagents/bundles

`bundle.yaml` a un champ `version` mais rien ne vérifie la compatibilité au chargement. Deux versions d'un même bundle = conflit silencieux (celui qui trie alphabétiquement le dernier gagne, d'après CLAUDE.md). Pour un système extensible, c'est un trou qui va faire mal dès qu'il y aura plusieurs contributeurs.

---

## 3. Idées à intégrer

Rangées par coût / valeur, avec les plus rentables en premier.

### 3.1 Métriques Prometheus (effort: 2-3j, valeur: énorme)

Ajouter un `common/metrics.py` exposant un `/metrics` HTTP par brick (port dédié, ex: 9100+offset). Métriques minimales :

- `relais_messages_processed_total{brick, action, status}` — compteur
- `relais_message_duration_seconds{brick}` — histogramme
- `relais_pel_length{stream, group}` — gauge (messages pending)
- `relais_llm_tokens_total{profile, direction}` — compteur (input/output)
- `relais_tool_errors_total{tool, type}` — compteur

Avec une stack Prometheus + Grafana (docker-compose), tu auras un dashboard opérationnel en un jour. Le gain en compréhension du comportement en runtime est énorme.

### 3.2 Refactor de `atelier/subagents/__init__.py` (effort: 2j, valeur: forte)

Éclater en :
- `atelier/subagents/registry.py` — discovery + hot-reload
- `atelier/subagents/loader.py` — parse YAML + validation
- `atelier/subagents/resolver.py` — résolution tokens (`mcp:*`, `inherit`, `module:*`)
- `atelier/subagents/__init__.py` — façade publique mince

Ça réduit le risque de régression sur les futures features et facilite le test unitaire par couche.

### 3.3 DLQ replay CLI (effort: 1j, valeur: moyenne-forte)

`relais dlq list` / `relais dlq show <id>` / `relais dlq replay <id>` / `relais dlq purge --older-than 7d`. Utile en dev pour debugger, utile en prod pour ne pas laisser pourrir la DLQ.

### 3.4 OpenTelemetry tracing (effort: 3-4j, valeur: forte à terme)

Instrumenter `BrickBase._run_stream_loop` pour émettre un span par message avec `correlation_id` comme trace ID. Propager via Envelope (un champ `traceparent`). Avec un export OTLP vers Tempo ou Honeycomb, tu vois le flow complet d'un message en 1 clic. C'est le pas logique après les `correlation_id`.

### 3.5 Fallback dégradé sur crash MCP (effort: 1-2j, valeur: UX)

Dans `AgentExecutor`, si `AgentExecutionError` vient d'un outil MCP spécifique (pas d'une erreur agent générale), re-tenter l'exécution **sans ces tools** avec un system prompt préfixé expliquant la dégradation. L'utilisateur obtient une réponse partiellement utile au lieu d'une erreur.

### 3.6 Versioning semver des bundles (effort: 1j, valeur: future-proofing)

Valider `bundle.yaml:version` en semver au chargement. Refuser deux bundles de même nom / versions différentes (ou forcer le choix via config). Logger clairement le conflit plutôt que résolution alphabétique silencieuse.

### 3.7 Workflows DAG via Horloger v2 (effort: 5j+, valeur: produit)

Horloger v1 fait *trigger → message*. Une v2 pourrait enchaîner étapes conditionnelles :

```yaml
name: daily_mail_triage
steps:
  - id: fetch
    subagent: mail-reader
    produce: unread_emails
  - id: triage
    subagent: mail-triager
    if: "len(unread_emails) > 0"
    consume: unread_emails
  - id: notify
    channel: telegram
    if: "triage.urgent_count > 0"
```

C'est un vrai différenciateur vs. OpenClaw — la plupart des systèmes LLM n'ont pas de DAG avec état entre étapes, juste du ReAct. Avec ton Envelope + Redis Streams déjà en place, tu as 80% de la plomberie.

### 3.8 Multi-tenant léger (effort: 3-4j, valeur: optionnelle selon usage)

Si tu veux partager RELAIS avec des proches ou l'utiliser pour plusieurs contextes (perso / recherche biblique / autre), introduire un `workspace_id` dans Envelope + préfixe des streams Redis (`relais:{workspace}:*`) + namespacing de `RELAIS_HOME` (`~/.relais/workspaces/{workspace}/`). Rétrocompatible en gardant `default` comme workspace implicite.

### 3.9 Test harness pour hot-reload sous charge (effort: 1-2j, valeur: prévention bug)

Un test `test_reload_under_load.py` qui : publie un flux de 100 messages/s, déclenche 20 reloads config dans l'intervalle, vérifie que rien n'est perdu et qu'aucune config invalide n'a été chargée. Tu détectes les races avant prod.

### 3.10 Skill analytics (effort: 2j, valeur: meta-amélioration)

Tracker quelles skills sont invoquées, avec quel succès, combien de tokens, combien de tool errors. Stocker dans la SQLite de Souvenir (table `skill_invocations`). C'est la base pour Forgeron : il ne peut améliorer les skills que s'il sait lesquelles échouent le plus.

---

## 4. Priorisation suggérée

**Court terme (quelques soirées)** — gains rapides, peu de risque :
1. Métriques Prometheus (3.1)
2. DLQ replay CLI (3.3)
3. Versioning semver bundles (3.6)

**Moyen terme (1-2 semaines)** — investissements qui paient sur 6 mois :
4. Refactor `subagents/__init__.py` (3.2)
5. Fallback dégradé MCP (3.5)
6. Skill analytics (3.10)

**Long terme (à faire quand ça bloque)** :
7. OpenTelemetry (3.4)
8. Workflows DAG v2 (3.7) — *le plus impactant côté produit*
9. Multi-tenant (3.8) — *seulement si tu ouvres le projet*

Tracing (3.4) pourrait remonter si tu commences à avoir des comportements émergents que les logs ne suffisent plus à expliquer.

---

## 5. Ce qui n'a pas besoin d'être touché

Parfois le meilleur conseil est "ne refactor pas ça". Les zones à laisser tranquilles :

- **`common/envelope.py`** — le design est bon, les tests sont là, changer la forme de l'Envelope a un coût systémique énorme pour un bénéfice marginal.
- **`common/brick_base.py`** — abstraction saine, StreamSpec bien pensé.
- **Le pattern XACK conditionnel dans Atelier** — subtil mais correct.
- **La cascade de config user > system > project** — standard, fonctionne.

---

## Conclusion

RELAIS est un projet de research-grade amateur **au-dessus de la moyenne** sur l'architecture et la discipline de code, mais qui n'a pas encore passé le cap de l'industrialisation opérationnelle. Les gains les plus rentables sont dans l'observabilité runtime (métriques) et dans la prévention de la dette qui s'accumule sur les gros fichiers Atelier. Côté produit, le vrai différenciateur à moyen terme est le **workflow DAG v2** via l'Horloger — ta plomberie le rend quasi-gratuit à construire, et c'est un besoin réel non couvert par la plupart des frameworks agentiques.
