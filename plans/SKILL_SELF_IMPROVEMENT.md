# Plan : Auto-amélioration des Skills RELAIS — Forgeron

**Objectif** : Un brick dédié `Forgeron` accumule les traces d'exécution de skills, analyse statistiquement les patterns d'erreur, et réécrit les fichiers skill quand il a suffisamment de données pour être sûr de l'amélioration. Validation empirique post-patch + rollback automatique si régression.

**Solution retenue** : S3 (Changelog séparé + Consolidation périodique) — évolution de B+D.

**Status** : IMPLÉMENTÉ — S3 en production (remplace A0/SkillAnnotator)

### S3 — Changelog séparé + Consolidation périodique

**Mécanisme en deux phases** :

- **Phase 1** (cheap, chaque trigger) : `ChangelogWriter` (LLM fast) extrait 1-3 observations
  et les écrit dans `CHANGELOG.md`. Le SKILL.md n'est jamais touché.
- **Phase 2** (cher, périodique) : quand `CHANGELOG.md` dépasse `consolidation_line_threshold`
  lignes (défaut 80), `SkillConsolidator` (LLM precise) relit les deux fichiers, réécrit
  SKILL.md en absorbant les learnings, produit un `CHANGELOG_DIGEST.md` (audit), et vide
  le changelog.

**Évolution du skill** : SKILL.md reste propre entre les consolidations. Le changelog est la
"mémoire de travail". Consolidation = "sommeil réparateur".

**Fichiers** :
- `forgeron/changelog_writer.py` — Phase 1
- `forgeron/skill_consolidator.py` — Phase 2
- `forgeron/main.py` — câblage (remplace `SkillAnnotator`)

**Configuration** (`forgeron.yaml`) :
- `annotation_profile` — profil LLM pour Phase 1 (recommandé : fast/haiku)
- `consolidation_profile` — profil LLM pour Phase 2 (recommandé : precise/sonnet)
- `consolidation_line_threshold` — seuil de lignes pour déclencher Phase 2 (défaut : 80)
- `consolidation_cooldown_seconds` — cooldown entre deux consolidations (défaut : 604800 = 7j)
- `annotation_cooldown_seconds` — cooldown entre deux observations Phase 1 (défaut : 300)
- `annotation_call_threshold` — nombre d'appels cumulés avant observation sans erreur (défaut : 10)
- `notify_user_on_consolidation` — notifier l'utilisateur après consolidation (défaut : true)

**Risque** : très faible — le SKILL.md est intouché en Phase 1. Une consolidation échouée
ne perd aucune information (le changelog est préservé).

---

## Vue d'ensemble

```
Atelier._handle_envelope()
    → execute() termine
    → publie relais:skill:trace
              ↓
         FORGERON (brick dédié)
         ┌──────────────────────────────────────────┐
         │  SkillTraceStore (SQLite)                 │
         │  Accumule traces par skill                │
         │       ↓                                   │
         │  Phase 1 — ChangelogWriter (LLM fast)     │
         │  Extrait 1-3 observations → CHANGELOG.md  │
         │  (SKILL.md intouché)                      │
         │       ↓  (si changelog > seuil lignes)    │
         │  Phase 2 — SkillConsolidator (LLM precise)│
         │  Réécrit SKILL.md en absorbant changelog  │
         │  Produit CHANGELOG_DIGEST.md (audit)      │
         │  Vide CHANGELOG.md                        │
         └──────────────────────────────────────────┘
              ↓
         relais:events:system (skill_created)
         relais:messages:outgoing_pending (notifications)
```

---

## Prérequis communs (Étape 0)

Ces changements sont nécessaires avant toute implémentation.

### 0.1 — Enrichir `AgentResult` avec métriques d'exécution

**Fichier** : `atelier/agent_executor.py`

```python
@dataclass(frozen=True)
class AgentResult:
    reply_text: str
    messages_raw: list[dict]
    tool_error_count: int = 0    # nombre de ToolMessage avec status="error"
    tool_call_count: int = 0     # nombre total d'appels d'outil
```

Calculé à la fin de `_stream()` en comptant les `token.type == "tool"` avec `token.status == "error"` vs total.

### 0.2 — Stamper `skills_used` dans `AtelierCtx`

**Fichier** : `common/contexts.py` — ajouter dans `AtelierCtx` TypedDict :

```python
class AtelierCtx(TypedDict, total=False):
    ...
    skills_used: list[str]   # noms des répertoires skill utilisés (ex: ["mail-agent"])
```

**Fichier** : `atelier/main.py` — dans `_handle_envelope()`, après résolution des skills :

```python
atelier_ctx = ensure_ctx(envelope, CTX_ATELIER)
atelier_ctx["skills_used"] = [Path(s).name for s in skills]
```

### 0.3 — Nouveau stream `relais:skill:trace`

**Fichier** : `common/streams.py` — ajouter :

```python
STREAM_SKILL_TRACE = "relais:skill:trace"
```

**Fichier** : `atelier/main.py` — publier après execute() :

```python
if agent_result.tool_call_count > 0 and atelier_ctx.get("skills_used"):
    trace_payload = {
        "skill_names": json.dumps(atelier_ctx["skills_used"]),
        "tool_error_count": str(agent_result.tool_error_count),
        "tool_call_count": str(agent_result.tool_call_count),
        "messages_raw": json.dumps(agent_result.messages_raw),
        "correlation_id": envelope.correlation_id,
        "timestamp": str(time.time()),
    }
    await redis_conn.xadd(STREAM_SKILL_TRACE, trace_payload)
```

### 0.4 — Config `forgeron.yaml`

**Fichier** : `config/forgeron.yaml`

```yaml
forgeron:
  min_traces_before_analysis: 5       # accumule N traces avant analyse
  min_error_rate: 0.3                  # déclenche si error_rate >= 30%
  min_improvement_interval_seconds: 3600  # cooldown par skill (1h)
  rollback_error_rate_threshold: 0.2  # rollback si error_rate monte de +20%
  rollback_window_traces: 3           # vérifie sur les 3 traces post-patch
  llm_profile: "precise"              # profile LLM pour l'analyse (résolu depuis profiles.yaml)
  annotation_profile: "fast"          # profile LLM pour les annotations immédiates
  annotation_mode: true               # active Solution D (annotations immédiates)
  patch_mode: true                    # active Solution B (réécriture complète)
  skills_dir: null                    # null = résolu depuis config cascade
```

> **Note (Solution C — implémentée)** : `llm_model` et `annotation_model` ont été remplacés par
> `llm_profile` et `annotation_profile`. Ces noms référencent des profils déclarés dans
> `atelier/profiles.yaml` (via la cascade config). `common/profile_loader.py` expose
> `load_profiles()`, `resolve_profile()` et `build_chat_model()` — partagés entre Atelier,
> Souvenir et Forgeron. `atelier/profile_loader.py` est un shim de compatibilité.

### 0.5 — Redis ACL pour Forgeron

**Fichier** : `config/redis.conf` — ajouter :

```
user forgeron on >{REDIS_PASS_FORGERON} ~relais:skill:* ~relais:events:* +XREAD +XREADGROUP +XADD +XACK +XGROUP +CLIENT
```

---

## Étape 1 — Brick Forgeron : infrastructure de base

**Branche** : `feat/forgeron-brick`

**Fichiers créés** :
```
forgeron/
├── __init__.py
├── main.py               # ForgeonBrick(BrickBase)
├── trace_store.py        # SkillTraceStore — SQLite, accumulateur
├── patch_store.py        # SkillPatchStore — SQLite, patches versionnés
└── models.py             # SkillTrace, SkillPatch (SQLModel)
```

### `forgeron/models.py`

```python
class SkillTrace(SQLModel, table=True):
    __tablename__ = "skill_traces"
    id: str = Field(primary_key=True, default_factory=lambda: str(uuid.uuid4()))
    skill_name: str = Field(index=True)
    correlation_id: str = Field(index=True)
    tool_error_count: int
    tool_call_count: int
    messages_raw: str = Field(default="[]")   # JSON blob
    created_at: float = Field(default_factory=time.time)
    patch_id: str | None = Field(default=None, index=True)  # patch actif au moment du tour

class SkillPatch(SQLModel, table=True):
    __tablename__ = "skill_patches"
    id: str = Field(primary_key=True, default_factory=lambda: str(uuid.uuid4()))
    skill_name: str = Field(index=True)
    original_content: str          # snapshot avant patch (rollback source)
    patched_content: str           # version améliorée
    diff: str                      # unified diff lisible
    rationale: str                 # explication LLM du patch
    trigger_correlation_id: str    # tour qui a déclenché l'analyse
    created_at: float = Field(default_factory=time.time)
    applied_at: float | None = None
    rolled_back_at: float | None = None
    pre_patch_error_rate: float    # error_rate sur les N traces d'analyse
    post_patch_error_rate: float | None = None  # mis à jour par SkillValidator
    status: str = Field(default="pending")  # pending | applied | rolled_back | validated
```

### `forgeron/trace_store.py`

```python
class SkillTraceStore:
    def add_trace(self, trace: SkillTrace) -> None: ...
    def get_traces(self, skill_name: str, since_patch_id: str | None = None) -> list[SkillTrace]: ...
    def error_rate(self, skill_name: str, window: int = 10) -> float: ...
    def should_analyze(self, skill_name: str, config: ForgeonConfig) -> bool:
        """True si: N traces accumulées + error_rate >= seuil + cooldown expiré."""
```

### `forgeron/main.py` — structure

```python
class Forgeron(BrickBase):
    def stream_specs(self) -> list[StreamSpec]:
        return [StreamSpec(
            stream=STREAM_SKILL_TRACE,
            group="forgeron_group",
            consumer="forgeron_1",
            handler=self._handle_trace,
        )]

    async def _handle_trace(self, envelope: Envelope, redis_conn: Any) -> bool:
        # 1. Parse trace depuis CTX_SOUVENIR_REQUEST (réutilise le pattern)
        # 2. Persist dans SkillTraceStore
        # 3. Si should_analyze() → lancer SkillAnalyzer
        # 4. Si patch produit → SkillPatcher.apply()
        # 5. Publier relais:events:system
        return True
```

**Supervisord** (`supervisord.conf`) :
```ini
[program:forgeron]
command=uv run python forgeron/main.py
priority=10
stdout_logfile=%(ENV_RELAIS_HOME)s/logs/forgeron.log
```

**Invariants** :
- `pytest tests/ -v -x --timeout=30 -m "not e2e"` passe
- Forgeron démarre et consomme `relais:skill:trace` sans messages
- Les autres bricks sont inchangés

---

## Étape 2 — SkillAnalyzer : analyse LLM batch

**Branche** : `feat/forgeron-analyzer`

**Dépend de** : Étape 1

**Fichier créé** : `forgeron/analyzer.py`

```python
class SkillAnalyzer:
    """Analyse N traces d'exécution et produit un patch pour le skill."""

    async def analyze(
        self,
        skill_name: str,
        skill_content: str,        # contenu actuel du SKILL.md
        traces: list[SkillTrace],  # N dernières traces
    ) -> SkillPatchProposal:
```

**Prompt LLM** (structure) :

```
Tu es un expert en optimisation d'agents IA.

Voici le fichier skill actuel :
<skill>
{skill_content}
</skill>

Voici {N} traces d'exécution de ce skill. Chaque trace contient
la liste complète des messages (appels d'outils, erreurs, corrections).

<traces>
{traces_serialized}
</traces>

Statistiques :
- Taux d'erreur moyen : {error_rate:.0%}
- Nombre moyen de cycles : {avg_cycles}

Analyse :
1. Identifie les patterns d'erreur récurrents
2. Identifie les stratégies de récupération qui ont fonctionné
3. Propose une version améliorée du skill qui :
   - Documente les erreurs communes et comment les éviter
   - Prescrit les séquences d'appels qui marchent
   - Supprime les approches qui ont systématiquement échoué

Réponds en JSON strict :
{
  "rationale": "Explication des problèmes identifiés",
  "patterns": ["pattern 1", "pattern 2"],
  "patched_content": "Contenu complet du SKILL.md amélioré"
}
```

**SkillPatchProposal** :

```python
@dataclass
class SkillPatchProposal:
    rationale: str
    patterns: list[str]
    patched_content: str
    diff: str              # calculé localement via difflib.unified_diff
```

**Tests** :
- `tests/test_forgeron_analyzer.py` — mock LLM, vérifier structure JSON valide
- Tester avec traces synthétiques contenant des erreurs répétées

---

## Étape 3 — SkillPatcher : écriture atomique + validation

**Branche** : `feat/forgeron-patcher`

**Dépend de** : Étape 2

**Fichier créé** : `forgeron/patcher.py`

```python
class SkillPatcher:
    """Applique un patch sur un skill file avec écriture atomique et rollback."""

    def apply(self, skill_path: Path, patch: SkillPatch) -> None:
        """
        1. Vérifie que skill_path est dans skills_dir (path traversal guard)
        2. Valide le contenu patché (UTF-8, taille < 50KB, structure Markdown)
        3. Écrit {skill}.md.pending
        4. Snapshot : {skill}.md → {skill}.md.bak
        5. Rename atomique : .pending → .md
        6. Met à jour SkillPatch.applied_at + status="applied"
        """

    def rollback(self, skill_path: Path, patch: SkillPatch) -> None:
        """
        Restaure {skill}.md depuis {skill}.md.bak
        Met à jour SkillPatch.rolled_back_at + status="rolled_back"
        """
```

**Fichier créé** : `forgeron/validator.py`

```python
class SkillValidator:
    """Post-patch : surveille les N traces suivantes et rollback si régression."""

    async def check_and_rollback_if_needed(
        self,
        skill_name: str,
        patch: SkillPatch,
        config: ForgeonConfig,
    ) -> bool:
        """
        - Récupère les traces post-patch (depuis patch.applied_at)
        - Si count < rollback_window_traces : pas encore assez de données
        - Si post_error_rate > pre_error_rate + rollback_threshold → rollback()
        - Met à jour SkillPatch.post_patch_error_rate
        - Retourne True si rollback déclenché
        """
```

**Appelé dans `Forgeron._handle_trace()`** : à chaque nouvelle trace, si un patch récent est en status="applied", appeler `validator.check_and_rollback_if_needed()`.

**Invariants** :
- `pytest tests/test_forgeron_patcher.py` — tester apply + rollback + validation sur tmp_path
- Vérifier que `.bak` existe après apply
- Vérifier que rollback restaure exactement le fichier original

---

## Étape 4 — Solution D : annotations immédiates (complémentaire)

**Branche** : `feat/skill-annotations`

**Dépend de** : Étape 0 uniquement (indépendant du Forgeron)

**Principe** : couche légère dans Atelier. Après un tour avec erreurs et skills, Atelier appende un bloc `LESSONS LEARNED` au skill file. Le LLM le lira au message suivant (AgentExecutor est re-instancié par message).

**Fichier créé** : `atelier/skill_annotator.py`

```python
class SkillAnnotator:
    async def maybe_annotate(
        self,
        skills: list[str],           # paths absolus résolus
        agent_result: AgentResult,
        config: SkillImprovementConfig,
        redis_conn: Any,             # pour vérifier cooldown
    ) -> None:
        """
        Si tool_error_count >= min_tool_errors ET cooldown Redis expiré :
        - Appelle LLM (profil fast) : 3-5 bullet points de lessons learned
        - Écrit atomiquement dans le SKILL.md de chaque skill utilisé
        - Set Redis key relais:skill:annotated:{skill_name} avec TTL = cooldown
        """
```

**Écriture atomique** (identique à SkillPatcher) :
```python
async def _append_annotation(skill_md: Path, annotation: str) -> None:
    current = skill_md.read_text(encoding="utf-8")
    updated = _consolidate_if_needed(current + "\n\n" + annotation)
    _validate_skill_content(updated)           # UTF-8, <50KB, structure
    pending = skill_md.with_suffix(".md.pending")
    backup  = skill_md.with_suffix(".md.bak")
    pending.write_text(updated, encoding="utf-8")
    if skill_md.exists():
        skill_md.replace(backup)
    pending.replace(skill_md)
```

**Consolidation** : si ≥ 5 sections `## LESSONS LEARNED` existent, une passe LLM les fusionne en une seule.

**Appelé dans** `atelier/main.py` — après `agent_executor.execute()`, avant l'ACK, si `annotation_mode: true` dans config.

---

## Étape 5 — Notification utilisateur

**Branche** : `feat/forgeron-notifications`

**Dépend de** : Étape 3

**Principe** : Forgeron publie dans `relais:events:system` après chaque patch appliqué ou rollback. L'Archiviste le logue. Optionnellement, Sentinelle peut router un message vers l'utilisateur.

```python
# Publication via Envelope minimale (pattern architectural RELAIS — implémenté)
# NOTE: on n'utilise PAS Envelope.from_parent() pour éviter de transmettre le contexte
# upstream complet (portail user_record, atelier state, etc.) dans un événement système.
event_env = Envelope(
    content=f"skill.patch_applied:{skill_name}",
    sender_id=envelope.sender_id,
    channel=envelope.channel,
    session_id=envelope.session_id,
    correlation_id=envelope.correlation_id,
)
event_env.action = ACTION_SKILL_PATCH_APPLIED  # ou ACTION_SKILL_PATCH_ROLLED_BACK
event_env.add_trace("forgeron", "patch_applied")
ensure_ctx(event_env, CTX_FORGERON).update({
    "skill_name": skill_name,
    "patch_id": patch.id,
    "pre_error_rate": error_rate,
    "diff_preview": patch.diff[:500],
})
await redis_conn.xadd(STREAM_EVENTS_SYSTEM, {"payload": event_env.to_json()})
```

**Commande optionnelle** `/forgeron-status` : affiche les patches récents (appliqués, en attente, rollbacks).

---

## Flux complet bout en bout

```
[1] Utilisateur envoie un message
[2] Atelier._handle_envelope() → execute()
      AgentResult{tool_error_count=3, tool_call_count=8, messages_raw=[...]}
[3] Atelier publie relais:skill:trace
      {skill_names: ["mail-agent"], errors: 3, calls: 8, messages_raw: ...}
[4] Atelier (si annotation_mode) → SkillAnnotator.maybe_annotate()
      → Ajoute "## LESSONS LEARNED" dans ~/.relais/skills/mail-agent/SKILL.md
[5] Forgeron consomme relais:skill:trace
      → Persiste SkillTrace dans SQLite
[6] SkillTraceStore.should_analyze()
      → 5 traces accumulées, error_rate=60% ≥ 30% seuil, cooldown expiré
      → OUI
[7] SkillAnalyzer.analyze(skill_content, traces[0..4])
      → LLM analyse les 5 traces
      → Identifie : "npm install lancé depuis mauvais répertoire dans 4/5 traces"
      → Produit SKILL.md amélioré avec note explicite sur le répertoire
[8] SkillPatcher.apply()
      → Validation contenu
      → Atomic write avec .bak snapshot
      → SkillPatch.status = "applied"
[9] relais:events:system ← skill_patch_applied
[10] Prochains tours de mail-agent :
       Forgeron surveille les traces post-patch
       Après 3 traces : post_error_rate = 10% (était 60%)
       → SkillValidator : amélioration confirmée → status = "validated"

Si dégradation :
[10b] Post_error_rate = 80% (était 60%)
       → SkillPatcher.rollback() depuis .bak
       → relais:events:system ← skill_patch_rolled_back
```

---

## Rollback & sécurité

| Risque | Protection |
|--------|-----------|
| LLM génère un skill corrompu | Validation avant écriture (UTF-8, taille, structure) |
| Patch dégrade les performances | SkillValidator auto-rollback depuis .bak |
| Path traversal sur skills_dir | Guard : `candidate.is_relative_to(skills_dir)` |
| Sur-analyse d'un skill populaire | Cooldown Redis TTL par skill |
| Forgeron plante | Brick isolé, pipeline principal inchangé |
| Perte du .bak | Le SkillPatchStore a aussi `original_content` en DB |

---

## Tests requis par étape

```bash
# Étape 0
pytest tests/test_agent_executor.py -v --timeout=30    # AgentResult.tool_error_count
pytest tests/test_atelier.py -v --timeout=30           # skills_used stampé

# Étape 1
pytest tests/test_forgeron_store.py -v --timeout=30    # SkillTraceStore.should_analyze()

# Étape 2
pytest tests/test_forgeron_analyzer.py -v --timeout=30 # mock LLM, JSON valide

# Étape 3
pytest tests/test_forgeron_patcher.py -v --timeout=30  # apply/rollback atomique

# Étape 4
pytest tests/test_skill_annotator.py -v --timeout=30   # annotation atomique

# Régression globale
pytest tests/ -v -x --timeout=30 -m "not e2e"
ruff check forgeron/ atelier/ common/
```

---

## Questions résolues

| Question | Décision |
|----------|----------|
| LLM externe autorisé pour messages_raw ? | **OUI** (confirmé par l'utilisateur) |
| Approval manuelle ou automatique ? | **Automatique** avec rollback empirique |
| Scope : tous les skills ou opt-in ? | Tous les skills (opt-out possible via `annotation_mode: false` dans forgeron.yaml) |
| Fréquence d'analyse | Déclenchée par accumulation de N traces, pas par cron |
| Quel LLM pour l'analyse ? | `claude-haiku-4-5` pour annotations (D), `claude-sonnet-4-6` pour rewrites (B) |
