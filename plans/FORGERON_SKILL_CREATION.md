# Plan : Forgeron — Création automatique de skills + notifications temps réel

**Objectif** : Permettre à Forgeron de détecter automatiquement les tâches récurrentes dans les archives de sessions (`relais:memory:request`) pour créer de nouveaux skills, et notifier l'utilisateur en temps réel à chaque création ou modification de skill via `relais:messages:outgoing_pending`.

**Approche** : Solution D — Forgeron consomme `relais:memory:request` via un consumer group dédié (`forgeron_archive_group`), indépendant de Souvenir. Un `IntentLabeler` (LLM Haiku) extrait un label d'intention par session. Quand N sessions partagent le même label, un `SkillCreator` (LLM precise) génère `SKILL.md`. Les notifications passent par `STREAM_OUTGOING_PENDING` → Sentinelle → adapter channel.

**Mode d'exécution** : git + GitHub CLI disponibles → branches par step, PR pour review.

**Status** : ✅ IMPLÉMENTÉ — tous les steps complétés, 975 tests passent.

---

## Invariants (vérifiés à chaque step)

- `pytest tests/ -x --timeout=30` passe (hors `test_smoke_e2e.py`)
- `ruff check forgeron/ common/` sans erreur
- Souvenir continue de consommer `relais:memory:request` sans interruption
- Le ACL Forgeron dans `redis.conf` est le seul point d'accès aux nouveaux streams

---

## Dépendances inter-steps

```
Step 1 (infra/ACL)
  ├── Step 2 (models + session_store)  ──┐
  ├── Step 3 (IntentLabeler)             ├── Step 6 (main.py orchestration)
  ├── Step 4 (SkillCreator)              │       └── Step 7 (tests)
  └── Step 5 (config extension)         ┘               └── Step 8 (docs)
```

Steps 2, 3, 4, 5 sont **parallélisables** après Step 1.

---

## Step 1 — Infrastructure : ACL Redis, constantes, contextes

**Branch** : `feat/forgeron-skill-creation-infra`
**Modèle** : default
**Dépendances** : aucune

### Contexte
Forgeron n'a accès qu'à `~relais:skill:*` et `~relais:events:system` (ligne 16 de `config/redis.conf`).
Pour consommer `relais:memory:request` et publier des notifications vers `relais:messages:outgoing_pending`, les ACL doivent être étendues. Deux nouvelles constantes d'action et deux champs de contexte sont nécessaires.

### Fichiers à modifier

**`config/redis.conf` — ligne 16** : étendre l'ACL Forgeron

```
# Avant
user forgeron    on >pass_forgeron ~relais:skill:* ~relais:events:system ~relais:logs +XREAD +XREADGROUP +XADD +XACK +XGROUP +CLIENT +SETNAME +RESET

# Après
user forgeron    on >pass_forgeron ~relais:skill:* ~relais:events:system ~relais:memory:request ~relais:messages:outgoing_pending ~relais:logs +XREAD +XREADGROUP +XADD +XACK +XGROUP +CLIENT +SETNAME +RESET
```

Note : `~relais:memory:request` est en lecture (XREADGROUP/XACK). `~relais:messages:outgoing_pending` est en écriture (XADD). Redis n'a pas de permission lecture/écriture séparée par stream — les deux sont couverts par les commandes listées.

**`common/envelope_actions.py`** — ajouter après `ACTION_SKILL_PATCH_ROLLED_BACK` :

```python
# Forgeron — création d'un nouveau skill à partir de sessions récurrentes
ACTION_SKILL_CREATED = "skill.created"
```

**`common/contexts.py`** — étendre `ForgeronCtx` :

```python
class ForgeronCtx(TypedDict, total=False):
    """Context stamped by Forgeron on skill lifecycle event envelopes."""
    skill_name: str       # Skill directory name (e.g. "mail-agent")
    patch_id: str         # UUID of the SkillPatch record
    pre_error_rate: float # Error rate that triggered the patch
    diff_preview: str     # First 500 chars of the unified diff
    # Champs ajoutés pour la création automatique
    skill_created: bool   # True when this event is for a new skill (vs patch)
    skill_path: str       # Absolute path to the created SKILL.md
    intent_label: str     # Intent label that triggered creation (e.g. "send-email")
    contributing_sessions: int  # Number of sessions that led to this creation
```

### Vérification
```bash
python -c "from common.envelope_actions import ACTION_SKILL_CREATED; print('OK')"
python -c "from common.contexts import ForgeronCtx; print('OK')"
grep "pass_forgeron" config/redis.conf | grep "outgoing_pending"
```

### Exit criteria
- Import `ACTION_SKILL_CREATED` sans erreur
- `ForgeronCtx` contient les 4 nouveaux champs
- `redis.conf` ligne forgeron contient `relais:memory:request` et `relais:messages:outgoing_pending`

---

## Step 2 — Data layer : modèles SQLite + SessionStore

**Branch** : `feat/forgeron-skill-creation-data`
**Modèle** : default
**Dépendances** : Step 1

### Contexte
Forgeron a besoin de deux nouvelles tables SQLite dans `forgeron.db` (même DB que `skill_traces` et `skill_patches`) :
- `SessionSummary` : une ligne par session analysée, avec son `intent_label`
- `SkillProposal` : une ligne par label d'intention avec compteur et statut de création

Le `SessionStore` implémente la même interface async que `SkillTraceStore` (SQLAlchemy async + SQLModel).

### Fichiers à créer/modifier

**`forgeron/models.py`** — ajouter à la suite des modèles existants :

```python
class SessionSummary(SQLModel, table=True):
    """Une session archivée analysée par Forgeron pour détecter des patterns."""

    __tablename__ = "session_summaries"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    session_id: str = Field(index=True)       # session_id de l'archive
    correlation_id: str = Field(index=True)   # correlation_id du tour
    channel: str                               # canal d'origine ("discord", "telegram", …)
    sender_id: str                             # sender_id d'origine
    intent_label: str | None = Field(default=None, index=True)  # label extrait par IntentLabeler (None = pas de pattern clair)
    user_content_preview: str = Field(default="")  # premiers 200 chars du message utilisateur
    created_at: float = Field(default_factory=time.time)


class SkillProposal(SQLModel, table=True):
    """Agrégat d'intentions récurrentes en attente ou réalisées de création de skill."""

    __tablename__ = "skill_proposals"

    id: str = Field(default_factory=lambda: str(uuid.uuid4()), primary_key=True)
    intent_label: str = Field(index=True, unique=True)  # clé de regroupement
    candidate_name: str                                   # nom proposé pour le skill (e.g. "send-email")
    session_count: int = Field(default=1)                # nb de sessions qui ont ce label
    representative_session_ids: str = Field(default="[]")  # JSON list[str] des N session_ids repr.
    draft_content: str | None = Field(default=None)      # SKILL.md généré (None = pas encore créé)
    # pending | created | skipped
    status: str = Field(default="pending")
    created_at: float = Field(default_factory=time.time)
    created_skill_name: str | None = Field(default=None)  # nom du skill finalement créé
```

**`forgeron/session_store.py`** — nouveau fichier :

```python
"""SessionStore — SQLite accumulator for per-session intent patterns."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from common.config_loader import resolve_storage_dir
from forgeron.config import ForgeonConfig
from forgeron.models import SessionSummary, SkillProposal

logger = logging.getLogger(__name__)


class SessionStore:
    """Persist and query session intent patterns for skill auto-creation.

    Shares the forgeron.db SQLite file with SkillTraceStore and SkillPatchStore
    (different tables, same engine path).
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or (resolve_storage_dir() / "forgeron.db")
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite+aiosqlite:///{self._db_path}"
        self._engine = create_async_engine(url, echo=False)
        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self._engine, class_=AsyncSession, expire_on_commit=False
        )

    async def _create_tables(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

    async def record_session(
        self,
        session_id: str,
        correlation_id: str,
        channel: str,
        sender_id: str,
        intent_label: str | None,
        user_content_preview: str,
    ) -> None:
        """Persist a session summary and update the SkillProposal aggregate.

        If intent_label is not None, creates or increments the corresponding
        SkillProposal row.

        Args:
            session_id: Session identifier from the archive envelope.
            correlation_id: Correlation ID of the archived turn.
            channel: Original channel (e.g. "discord").
            sender_id: Original sender_id.
            intent_label: Normalized intent extracted by IntentLabeler, or None.
            user_content_preview: First 200 chars of the user message.
        """
        summary = SessionSummary(
            session_id=session_id,
            correlation_id=correlation_id,
            channel=channel,
            sender_id=sender_id,
            intent_label=intent_label,
            user_content_preview=user_content_preview,
        )
        async with self._session_factory() as session:
            session.add(summary)
            await session.commit()

        if intent_label is not None:
            await self._upsert_proposal(intent_label, session_id)

    async def _upsert_proposal(self, intent_label: str, session_id: str) -> None:
        """Create or increment the SkillProposal for intent_label."""
        async with self._session_factory() as session:
            stmt = select(SkillProposal).where(col(SkillProposal.intent_label) == intent_label)
            result = await session.exec(stmt)
            proposal = result.first()

            if proposal is None:
                proposal = SkillProposal(
                    intent_label=intent_label,
                    candidate_name=intent_label.replace("_", "-"),
                    session_count=1,
                    representative_session_ids=json.dumps([session_id]),
                )
                session.add(proposal)
            else:
                proposal.session_count += 1
                existing = json.loads(proposal.representative_session_ids)
                if session_id not in existing:
                    existing.append(session_id)
                    proposal.representative_session_ids = json.dumps(existing[-10:])  # keep last 10
            await session.commit()

    async def should_create(
        self,
        intent_label: str,
        config: ForgeonConfig,
        redis_conn: object,
    ) -> bool:
        """Return True if a skill should be created for this intent_label.

        Conditions: session_count >= min_sessions_for_creation AND
        no existing skill with this label AND cooldown expired.

        Args:
            intent_label: The intent label to evaluate.
            config: Loaded ForgeonConfig.
            redis_conn: Active Redis connection for cooldown check.

        Returns:
            True if skill creation should be triggered.
        """
        async with self._session_factory() as session:
            stmt = select(SkillProposal).where(col(SkillProposal.intent_label) == intent_label)
            result = await session.exec(stmt)
            proposal = result.first()

        if proposal is None:
            return False
        if proposal.status != "pending":
            return False
        if proposal.session_count < config.min_sessions_for_creation:
            logger.debug(
                "intent '%s': not enough sessions (%d/%d)",
                intent_label, proposal.session_count, config.min_sessions_for_creation,
            )
            return False

        cooldown_key = f"relais:skill:creation_cooldown:{intent_label}"
        ttl = await redis_conn.ttl(cooldown_key)  # type: ignore[attr-defined]
        if ttl > 0:
            logger.debug("intent '%s': cooldown active (%ds remaining)", intent_label, ttl)
            return False

        return True

    async def get_proposal(self, intent_label: str) -> SkillProposal | None:
        """Fetch the SkillProposal for an intent_label."""
        async with self._session_factory() as session:
            stmt = select(SkillProposal).where(col(SkillProposal.intent_label) == intent_label)
            result = await session.exec(stmt)
            return result.first()

    async def get_representative_sessions(
        self, intent_label: str, limit: int = 5
    ) -> list[SessionSummary]:
        """Fetch the most recent sessions for an intent label."""
        async with self._session_factory() as session:
            stmt = (
                select(SessionSummary)
                .where(col(SessionSummary.intent_label) == intent_label)
                .order_by(col(SessionSummary.created_at).desc())
                .limit(limit)
            )
            result = await session.exec(stmt)
            return list(result.all())

    async def mark_created(self, intent_label: str, skill_name: str) -> None:
        """Mark a SkillProposal as created."""
        async with self._session_factory() as session:
            stmt = select(SkillProposal).where(col(SkillProposal.intent_label) == intent_label)
            result = await session.exec(stmt)
            proposal = result.first()
            if proposal:
                proposal.status = "created"
                proposal.created_skill_name = skill_name
                await session.commit()

    async def close(self) -> None:
        await self._engine.dispose()
```

### Vérification
```bash
python -c "from forgeron.models import SessionSummary, SkillProposal; print('OK')"
python -c "from forgeron.session_store import SessionStore; print('OK')"
pytest tests/ -x --timeout=30 -q
```

### Exit criteria
- `SessionSummary` et `SkillProposal` importables
- `SessionStore` instantiable sans Redis (engine SQLite uniquement)
- Tests existants passent

---

## Step 3 — IntentLabeler : extraction d'intention via LLM Haiku

**Branch** : `feat/forgeron-intent-labeler`
**Modèle** : default
**Dépendances** : Step 1
**Parallèle avec** : Steps 2, 4, 5

### Contexte
`IntentLabeler` reçoit les messages bruts d'une session (`messages_raw` = liste de dicts LangChain sérialisés), extrait uniquement les messages HumanMessage, et appelle le LLM (profil `annotation_profile` = Haiku) pour obtenir un label normalisé en `snake_case` ou `None` si pas de pattern clair.

Le prompt est délibérément court et contraignant pour que Haiku réponde en un seul mot/label.

### Fichier à créer : `forgeron/intent_labeler.py`

```python
"""IntentLabeler — extrait un label d'intention d'une session via LLM Haiku."""

from __future__ import annotations

import json
import logging
import re

from common.profile_loader import ProfileConfig

logger = logging.getLogger(__name__)

# Labels réservés qui ne doivent pas déclencher de création de skill
_EXCLUDED_LABELS = frozenset({"none", "unknown", "general", "chat", "conversation", "question"})

# Regex pour valider qu'un label est bien en snake_case
_LABEL_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")


class IntentLabeler:
    """Extract a normalized intent label from a session's messages_raw.

    Uses a cheap LLM call (Haiku via annotation_profile) to classify the
    session's primary task type into a short snake_case label suitable for
    grouping into a skill.

    Args:
        profile: The annotation ProfileConfig (typically "fast" = Haiku).
    """

    _SYSTEM_PROMPT = (
        "You are a task classifier. Given a conversation, identify the single "
        "primary recurring task type it represents. Respond with ONLY a "
        "short snake_case label (e.g. send_email, summarize_pdf, search_web, "
        "create_calendar_event). If the conversation is generic chat or has "
        "no clear reusable task, respond with 'none'."
    )

    def __init__(self, profile: ProfileConfig) -> None:
        self._profile = profile

    def _extract_user_messages(self, messages_raw: list[dict]) -> list[str]:
        """Extract only HumanMessage content from a serialized message list.

        Args:
            messages_raw: Deserialized LangChain message list (list of dicts).

        Returns:
            List of user message content strings.
        """
        user_msgs: list[str] = []
        for msg in messages_raw:
            msg_type = msg.get("type", "")
            # LangChain serializes HumanMessage as type="human" or id=["langchain_core","messages","HumanMessage"]
            if msg_type == "human" or (
                isinstance(msg.get("id"), list) and "HumanMessage" in str(msg.get("id"))
            ):
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    user_msgs.append(content.strip()[:300])
        return user_msgs

    async def label(self, messages_raw: list[dict]) -> str | None:
        """Extract an intent label from a session's messages.

        Args:
            messages_raw: Full serialized LangChain message list for the turn.

        Returns:
            Normalized snake_case intent label, or None if no clear intent.
        """
        user_messages = self._extract_user_messages(messages_raw)
        if not user_messages:
            logger.debug("IntentLabeler: no user messages found in session")
            return None

        conversation_text = "\n".join(f"- {m}" for m in user_messages[:5])

        try:
            from common.profile_loader import build_chat_model  # noqa: PLC0415
            model = build_chat_model(self._profile)
            from langchain_core.messages import HumanMessage, SystemMessage  # noqa: PLC0415
            response = await model.ainvoke([
                SystemMessage(content=self._SYSTEM_PROMPT),
                HumanMessage(content=f"Conversation:\n{conversation_text}"),
            ])
            raw_label = response.content.strip().lower()
        except Exception as exc:  # noqa: BLE001
            logger.warning("IntentLabeler LLM call failed: %s", exc)
            return None

        # Validate format
        if not _LABEL_RE.match(raw_label):
            logger.debug("IntentLabeler: invalid label format '%s'", raw_label)
            return None
        if raw_label in _EXCLUDED_LABELS:
            logger.debug("IntentLabeler: excluded label '%s'", raw_label)
            return None

        logger.info("IntentLabeler: session → label='%s'", raw_label)
        return raw_label
```

### Vérification
```bash
python -c "from forgeron.intent_labeler import IntentLabeler; print('OK')"
pytest tests/ -x --timeout=30 -q
```

### Exit criteria
- `IntentLabeler` importable
- `_extract_user_messages()` extrait correctement les messages HumanMessage
- Retourne `None` pour une conversation générique (testé en unit sans LLM réel)

---

## Step 4 — SkillCreator : génération de SKILL.md depuis des sessions exemples

**Branch** : `feat/forgeron-skill-creator`
**Modèle** : default
**Dépendances** : Step 1
**Parallèle avec** : Steps 2, 3, 5

### Contexte
`SkillCreator` reçoit un `intent_label` et N sessions représentatives (`SessionSummary` + leurs `messages_raw`), appelle le LLM (profil `llm_profile` = precise) et génère un `SKILL.md` complet.

Il crée physiquement le répertoire `skills_dir/{skill_name}/` et écrit `SKILL.md`. Il retourne un `SkillCreationResult` dataclass.

### Fichier à créer : `forgeron/skill_creator.py`

```python
"""SkillCreator — génère un nouveau SKILL.md depuis des sessions récurrentes."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from common.profile_loader import ProfileConfig

logger = logging.getLogger(__name__)


@dataclass
class SkillCreationResult:
    """Result of a successful skill creation."""
    skill_name: str           # e.g. "send-email"
    skill_path: Path          # absolute path to the written SKILL.md
    skill_content: str        # full generated SKILL.md content
    description: str          # one-line description for skill index


class SkillCreator:
    """Generate a new SKILL.md from representative sessions.

    Uses the 'precise' LLM profile to produce a high-quality SKILL.md that
    describes the task, lists required tools, and provides step-by-step
    instructions so future agent turns can execute without trial-and-error.

    Args:
        profile: The LLM ProfileConfig to use (typically "precise").
        skills_dir: Base directory where skill subdirectories are created.
    """

    _SYSTEM_PROMPT = """You are an expert at writing reusable AI agent skill documents.
A SKILL.md file describes a recurring task type and tells an AI agent exactly how to perform it efficiently.

Structure your SKILL.md as:
# {Skill Name}

## Description
One sentence describing what this skill does.

## When to use
Bullet list of triggers / user request patterns.

## Required tools
List of tools needed (e.g. bash, read_file, send_email).

## Step-by-step instructions
Numbered steps the agent should follow to complete the task without errors.

## Common mistakes to avoid
Bullet list of pitfalls observed in past executions.

## Example
A brief example of a successful execution.

Write complete, actionable instructions. Be specific. Avoid vague guidance."""

    def __init__(self, profile: ProfileConfig, skills_dir: Path) -> None:
        self._profile = profile
        self._skills_dir = skills_dir

    async def create(
        self,
        intent_label: str,
        session_examples: list[dict],
    ) -> SkillCreationResult | None:
        """Generate and write a SKILL.md for the given intent label.

        Args:
            intent_label: Normalized intent label (e.g. "send_email").
            session_examples: List of dicts with keys: user_content_preview, messages_raw_summary.

        Returns:
            SkillCreationResult on success, None if LLM fails or output is invalid.
        """
        skill_name = intent_label.replace("_", "-")

        examples_text = "\n\n".join(
            f"Session {i+1}:\n{ex.get('user_content_preview', '')}"
            for i, ex in enumerate(session_examples)
        )
        user_prompt = (
            f"Task type: {intent_label}\n\n"
            f"Here are {len(session_examples)} real examples of user requests for this task:\n\n"
            f"{examples_text}\n\n"
            f"Write a complete SKILL.md for skill named '{skill_name}'."
        )

        try:
            from common.profile_loader import build_chat_model  # noqa: PLC0415
            model = build_chat_model(self._profile)
            from langchain_core.messages import HumanMessage, SystemMessage  # noqa: PLC0415
            response = await model.ainvoke([
                SystemMessage(content=self._SYSTEM_PROMPT),
                HumanMessage(content=user_prompt),
            ])
            skill_content = response.content.strip()
        except Exception as exc:  # noqa: BLE001
            logger.error("SkillCreator LLM call failed for '%s': %s", intent_label, exc)
            return None

        if len(skill_content) < 100:
            logger.warning("SkillCreator: generated content too short for '%s'", intent_label)
            return None

        # Extract one-line description from the ## Description section
        description = self._extract_description(skill_content) or f"Auto-generated skill for: {intent_label}"

        # Write to disk
        skill_dir = self._skills_dir / skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_path = skill_dir / "SKILL.md"

        if skill_path.exists():
            logger.warning("SkillCreator: skill '%s' already exists, skipping.", skill_name)
            return None

        skill_path.write_text(skill_content, encoding="utf-8")
        logger.info("SkillCreator: created '%s' at %s", skill_name, skill_path)

        return SkillCreationResult(
            skill_name=skill_name,
            skill_path=skill_path,
            skill_content=skill_content,
            description=description,
        )

    @staticmethod
    def _extract_description(content: str) -> str | None:
        """Extract the first non-empty line after '## Description'."""
        in_section = False
        for line in content.splitlines():
            if line.strip().lower().startswith("## description"):
                in_section = True
                continue
            if in_section and line.strip():
                if line.startswith("#"):
                    break
                return line.strip()
        return None
```

### Vérification
```bash
python -c "from forgeron.skill_creator import SkillCreator, SkillCreationResult; print('OK')"
pytest tests/ -x --timeout=30 -q
```

### Exit criteria
- `SkillCreator` et `SkillCreationResult` importables
- `_extract_description()` extrait correctement la description depuis un SKILL.md exemple
- Ne crée pas de fichier si `skill_path.exists()` (idempotence)

---

## Step 5 — Extension ForgeonConfig pour la création

**Branch** : `feat/forgeron-creation-config`
**Modèle** : default
**Dépendances** : Step 1
**Parallèle avec** : Steps 2, 3, 4

### Contexte
`ForgeonConfig` a besoin de nouveaux champs pour contrôler la création automatique de skills. Les ajouter dans `forgeron/config.py` et dans `config/forgeron.yaml.default`.

### Modifications

**`forgeron/config.py`** — ajouter dans `ForgeonConfig` dataclass :

```python
# --- Création automatique de skills (Solution D) ---
creation_mode: bool = True
"""Enable automatic skill creation from recurring session patterns."""

min_sessions_for_creation: int = 3
"""Minimum number of sessions with the same intent_label before creating a skill."""

creation_cooldown_seconds: int = 86400
"""Minimum interval between two creation attempts for the same intent label (seconds).
Redis TTL key: relais:skill:creation_cooldown:{intent_label}"""

max_sessions_for_labeling: int = 5
"""Maximum number of representative sessions to pass to SkillCreator."""

notify_user_on_patch: bool = True
"""Publish a notification to relais:messages:outgoing_pending when a patch is applied."""

notify_user_on_creation: bool = True
"""Publish a notification to relais:messages:outgoing_pending when a skill is created."""
```

Mettre à jour `load_forgeron_config()` pour lire ces nouveaux champs depuis le YAML.

**`config/forgeron.yaml.default`** — ajouter à la fin du fichier :

```yaml
  # --- Création automatique de skills (Solution D) ---

  # Enable automatic skill creation from recurring session patterns.
  # Forgeron consumes relais:memory:request (forgeron_archive_group) to detect
  # sessions with the same intent_label and create a new SKILL.md.
  creation_mode: true

  # Minimum number of sessions sharing the same intent_label before creating a skill.
  min_sessions_for_creation: 3

  # Minimum interval between two creation attempts for the same intent label (seconds).
  creation_cooldown_seconds: 86400

  # Number of representative sessions passed to SkillCreator LLM.
  max_sessions_for_labeling: 5

  # Notify the user when Forgeron applies a patch to an existing skill.
  notify_user_on_patch: true

  # Notify the user when Forgeron creates a new skill.
  notify_user_on_creation: true
```

### Vérification
```bash
python -c "from forgeron.config import ForgeonConfig; c = ForgeonConfig(); print(c.creation_mode, c.min_sessions_for_creation)"
python -c "from forgeron.config import load_forgeron_config; c = load_forgeron_config(); print('OK')"
pytest tests/ -x --timeout=30 -q
```

### Exit criteria
- `ForgeonConfig()` instanciable avec les 6 nouveaux champs et leurs valeurs par défaut
- `load_forgeron_config()` lit les nouveaux champs depuis YAML sans erreur
- Tests existants passent

---

## Step 6 — Forgeron main.py : orchestration archive + notifications

**Branch** : `feat/forgeron-skill-creation-main`
**Modèle** : default (Sonnet)
**Dépendances** : Steps 2, 3, 4, 5

### Contexte
C'est le step central. `forgeron/main.py` reçoit deux modifications majeures :

1. **Nouveau StreamSpec** sur `STREAM_MEMORY_REQUEST` avec groupe `forgeron_archive_group` — consomme les archives Atelier sans impacter Souvenir.

2. **`_notify_user()`** — helper qui publie vers `STREAM_OUTGOING_PENDING` avec `ACTION_MESSAGE_OUTGOING_PENDING`. Appelé après chaque patch appliqué, patch rollback, et création de skill.

3. **`_handle_archive()`** — handler du nouveau stream : extrait `messages_raw` et `original_env` depuis `CTX_SOUVENIR_REQUEST`, appelle `IntentLabeler`, enregistre dans `SessionStore`, déclenche `SkillCreator` si seuils atteints.

### Extraction du channel/sender_id depuis l'archive

```python
# L'archive envelope a channel="internal" et sender_id="atelier:{original_sender_id}"
# L'original channel/sender_id est dans CTX_SOUVENIR_REQUEST["envelope_json"]
# qui est response_env.to_json() = from_parent(original_envelope)
souvenir_ctx = envelope.context.get(CTX_SOUVENIR_REQUEST, {})
envelope_json = souvenir_ctx.get("envelope_json", "")
if not envelope_json:
    return  # archive sans envelope_json, skip
original_env = Envelope.from_json(envelope_json)
channel = original_env.channel          # e.g. "discord"
sender_id = original_env.sender_id      # e.g. "discord:123456"
```

### Modifications dans `forgeron/main.py`

**Imports à ajouter** :
```python
from common.contexts import CTX_SOUVENIR_REQUEST, CTX_FORGERON, ensure_ctx
from common.envelope_actions import (
    ACTION_SKILL_PATCH_APPLIED, ACTION_SKILL_PATCH_ROLLED_BACK,
    ACTION_SKILL_CREATED, ACTION_MESSAGE_OUTGOING_PENDING,
)
from common.streams import (
    STREAM_EVENTS_SYSTEM, STREAM_LOGS, STREAM_SKILL_TRACE,
    STREAM_MEMORY_REQUEST, STREAM_OUTGOING_PENDING,
)
from forgeron.session_store import SessionStore
```

**`__init__`** — ajouter `SessionStore` :
```python
self._session_store = SessionStore(db_path=db_path)
```

**`stream_specs()`** — ajouter le second StreamSpec :
```python
StreamSpec(
    stream=STREAM_MEMORY_REQUEST,
    group="forgeron_archive_group",
    consumer="forgeron_1",
    handler=self._handle_archive,
    ack_mode="always",  # advisory — perdre un message est acceptable
),
```

**Nouvelle méthode `_handle_archive()`** :
```python
async def _handle_archive(self, envelope: Envelope, redis_conn: Any) -> bool:
    """Consume an Atelier session archive and detect recurring intent patterns.

    Extracts messages_raw and the original channel/sender_id, runs IntentLabeler,
    records the session in SessionStore, and triggers SkillCreator if thresholds met.

    Returns:
        Always True — advisory consumer, XACK unconditionally.
    """
    if not self._config.creation_mode:
        return True
    try:
        await self._process_archive(envelope, redis_conn)
    except Exception as exc:
        logger.error("Error processing archive: %s", exc, exc_info=True)
    return True

async def _process_archive(self, envelope: Envelope, redis_conn: Any) -> None:
    souvenir_ctx = envelope.context.get(CTX_SOUVENIR_REQUEST, {})
    envelope_json = souvenir_ctx.get("envelope_json", "")
    messages_raw_str = souvenir_ctx.get("messages_raw", "[]")

    if not envelope_json:
        logger.debug("Archive has no envelope_json, skipping intent labeling.")
        return

    original_env = Envelope.from_json(envelope_json)
    channel = original_env.channel
    sender_id = original_env.sender_id

    # Deserialize messages_raw (can be str or list depending on serialization)
    import json  # noqa: PLC0415
    if isinstance(messages_raw_str, str):
        try:
            messages_raw: list[dict] = json.loads(messages_raw_str)
        except (json.JSONDecodeError, ValueError):
            messages_raw = []
    else:
        messages_raw = messages_raw_str or []

    # Extract user content preview (first human message, max 200 chars)
    user_preview = ""
    for msg in messages_raw:
        if msg.get("type") == "human":
            user_preview = str(msg.get("content", ""))[:200]
            break

    # Run intent labeling with the cheap annotation profile
    intent_label: str | None = None
    if self._annotation_profile is not None:
        try:
            from forgeron.intent_labeler import IntentLabeler  # noqa: PLC0415
            labeler = IntentLabeler(profile=self._annotation_profile)
            intent_label = await labeler.label(messages_raw)
        except ImportError:
            logger.debug("IntentLabeler not yet available, skipping.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("IntentLabeler failed: %s", exc)

    # Record in SQLite
    await self._session_store.record_session(
        session_id=envelope.session_id,
        correlation_id=envelope.correlation_id,
        channel=channel,
        sender_id=sender_id,
        intent_label=intent_label,
        user_content_preview=user_preview,
    )

    if intent_label is None:
        return

    # Check creation thresholds
    should = await self._session_store.should_create(
        intent_label, self._config, redis_conn
    )
    if should:
        await self._trigger_creation(
            intent_label, channel, sender_id,
            envelope.session_id, envelope.correlation_id,
            redis_conn,
        )

async def _trigger_creation(
    self,
    intent_label: str,
    channel: str,
    sender_id: str,
    session_id: str,
    correlation_id: str,
    redis_conn: Any,
) -> None:
    """Create a new skill from recurring session patterns."""
    # Set cooldown to prevent concurrent triggers
    cooldown_key = f"relais:skill:creation_cooldown:{intent_label}"
    await redis_conn.set(cooldown_key, "1", ex=self._config.creation_cooldown_seconds)

    logger.info("Triggering skill creation for intent '%s'", intent_label)

    try:
        from forgeron.skill_creator import SkillCreator  # noqa: PLC0415
    except ImportError as exc:
        logger.warning("SkillCreator not available: %s", exc)
        return

    if self._config.skills_dir is None:
        logger.warning("skills_dir is None — cannot create skill.")
        return
    if self._llm_profile is None:
        logger.warning("llm_profile not loaded — cannot create skill.")
        return

    representative = await self._session_store.get_representative_sessions(
        intent_label, limit=self._config.max_sessions_for_labeling
    )
    session_examples = [
        {"user_content_preview": s.user_content_preview}
        for s in representative
    ]

    creator = SkillCreator(profile=self._llm_profile, skills_dir=self._config.skills_dir)
    result = await creator.create(intent_label, session_examples)
    if result is None:
        logger.warning("SkillCreator returned None for intent '%s'", intent_label)
        return

    await self._session_store.mark_created(intent_label, result.skill_name)

    # Publish system event
    event_env = Envelope(
        content=f"skill.created:{result.skill_name}",
        sender_id=sender_id,
        channel=channel,
        session_id=session_id,
        correlation_id=correlation_id,
    )
    event_env.action = ACTION_SKILL_CREATED
    event_env.add_trace("forgeron", "skill_created")
    ensure_ctx(event_env, CTX_FORGERON).update({
        "skill_name": result.skill_name,
        "skill_created": True,
        "skill_path": str(result.skill_path),
        "intent_label": intent_label,
        "contributing_sessions": len(representative),
    })
    await redis_conn.xadd(STREAM_EVENTS_SYSTEM, {"payload": event_env.to_json()})
    logger.info("Skill '%s' created at %s", result.skill_name, result.skill_path)

    # Notify user
    if self._config.notify_user_on_creation:
        await self._notify_user(
            channel=channel,
            sender_id=sender_id,
            session_id=session_id,
            correlation_id=correlation_id,
            message=(
                f"[Forgeron] Nouveau skill créé automatiquement : `{result.skill_name}`\n"
                f"{result.description}\n"
                f"_(basé sur {len(representative)} sessions récurrentes)_"
            ),
            redis_conn=redis_conn,
        )
```

**Nouvelle méthode `_notify_user()`** :
```python
async def _notify_user(
    self,
    channel: str,
    sender_id: str,
    session_id: str,
    correlation_id: str,
    message: str,
    redis_conn: Any,
) -> None:
    """Publish a notification to the user via relais:messages:outgoing_pending.

    Sentinelle's outgoing loop picks it up, applies guardrails, and routes
    it to relais:messages:outgoing:{channel} for the channel adapter to deliver.

    Args:
        channel: Original channel (e.g. "discord").
        sender_id: Original sender_id.
        session_id: Session ID for tracking.
        correlation_id: Correlation ID for tracking.
        message: Notification text to send to the user.
        redis_conn: Active Redis connection.
    """
    notif_env = Envelope(
        content=message,
        sender_id=sender_id,
        channel=channel,
        session_id=session_id,
        correlation_id=correlation_id,
    )
    notif_env.action = ACTION_MESSAGE_OUTGOING_PENDING
    await redis_conn.xadd(STREAM_OUTGOING_PENDING, {"payload": notif_env.to_json()})
    logger.info("Notification sent to %s/%s: %s", channel, sender_id, message[:60])
```

**Modifier `_trigger_analysis()`** — ajouter après `await redis_conn.xadd(STREAM_EVENTS_SYSTEM, ...)` :
```python
# Notify user about patch application
if self._config.notify_user_on_patch:
    await self._notify_user(
        channel=envelope.channel,
        sender_id=envelope.sender_id,
        session_id=envelope.session_id,
        correlation_id=envelope.correlation_id,
        message=(
            f"[Forgeron] Skill `{skill_name}` amélioré automatiquement "
            f"(taux d'erreur : {error_rate:.0%} → patch `{patch.id[:8]}`)"
        ),
        redis_conn=redis_conn,
    )
```

**Modifier `_maybe_validate_patch()`** — ajouter après le `xadd(STREAM_EVENTS_SYSTEM, ...)` pour le rollback :
```python
if self._config.notify_user_on_patch:
    await self._notify_user(
        channel=envelope.channel,
        sender_id=envelope.sender_id,
        session_id=envelope.session_id,
        correlation_id=envelope.correlation_id,
        message=(
            f"[Forgeron] Patch `{patch.id[:8]}` sur skill `{skill_name}` "
            f"annulé (régression détectée, retour à la version précédente)."
        ),
        redis_conn=redis_conn,
    )
```

### Vérification
```bash
python -c "from forgeron.main import Forgeron; print('OK')"
python -m py_compile forgeron/main.py && echo "syntax OK"
pytest tests/ -x --timeout=30 -q
```

### Exit criteria
- `Forgeron` s'instancie sans erreur
- `stream_specs()` retourne 2 StreamSpecs (skill:trace + memory:request)
- `_notify_user()` produit une Envelope avec `ACTION_MESSAGE_OUTGOING_PENDING`
- Tests existants passent

---

## Step 7 — Tests

**Branch** : `feat/forgeron-skill-creation-tests`
**Modèle** : default
**Dépendances** : Step 6

### Fichier à créer : `tests/test_forgeron_creation.py`

Couvre :

1. **`test_intent_labeler_extract_human_messages`** — unit, pas de LLM : vérifie que `_extract_user_messages()` extrait correctement les messages `type="human"`.

2. **`test_session_store_record_and_count`** — unit SQLite in-memory : crée 3 sessions avec le même `intent_label`, vérifie que `SkillProposal.session_count == 3`.

3. **`test_session_store_should_create_false_below_threshold`** — `should_create()` retourne False si `session_count < min_sessions_for_creation`.

4. **`test_session_store_should_create_true_at_threshold`** — `should_create()` retourne True quand seuil atteint et pas de cooldown Redis (mock redis TTL = -2).

5. **`test_skill_creator_extract_description`** — unit : vérifie que `SkillCreator._extract_description()` extrait la première ligne non-vide après `## Description`.

6. **`test_skill_creator_skips_existing_skill`** — avec un skill existant sur le filesystem, `create()` retourne `None` sans écrire.

7. **`test_forgeron_handle_archive_no_envelope_json`** — `_process_archive()` skip silencieusement si `envelope_json` est absent.

8. **`test_forgeron_notify_user_publishes_to_outgoing_pending`** — mock Redis : `_notify_user()` appelle `xadd(STREAM_OUTGOING_PENDING, ...)` avec une Envelope dont `action == ACTION_MESSAGE_OUTGOING_PENDING`.

9. **`test_forgeron_stream_specs_has_two_consumers`** — `Forgeron().stream_specs()` retourne exactement 2 `StreamSpec`, dont un sur `STREAM_MEMORY_REQUEST` avec `group="forgeron_archive_group"`.

### Structure des fixtures

```python
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path

@pytest_asyncio.fixture
async def session_store(tmp_path):
    """SessionStore backed by an in-memory-style SQLite in tmp_path."""
    from forgeron.session_store import SessionStore
    store = SessionStore(db_path=tmp_path / "test_forgeron.db")
    await store._create_tables()
    yield store
    await store.close()

@pytest.fixture
def mock_redis():
    redis = AsyncMock()
    redis.ttl = AsyncMock(return_value=-2)  # no cooldown
    redis.set = AsyncMock()
    redis.xadd = AsyncMock()
    return redis
```

### Vérification
```bash
pytest tests/test_forgeron_creation.py -v --timeout=30
pytest tests/ -x --timeout=30 -q
```

### Exit criteria
- 9/9 tests passent
- Aucun test ne démarre de LLM réel (mocks uniquement)
- Couverture `forgeron/` >= 70%

---

## Step 8 — Documentation

**Branch** : `feat/forgeron-skill-creation-docs`
**Modèle** : default
**Dépendances** : Step 6

### Fichiers à mettre à jour

**`docs/REDIS_BUS_API.md`** :
- Ajouter section `relais:memory:request` avec la ligne : "Consommé par Forgeron (groupe `forgeron_archive_group`) en lecture seule, en plus de Souvenir."
- Ajouter action `ACTION_SKILL_CREATED` dans la table des actions Forgeron
- Ajouter `| relais:messages:outgoing_pending | Forgeron | Sentinelle |` pour les notifications

**`docs/ARCHITECTURE.md`** :
- Mettre à jour le flux Forgeron pour inclure :
  ```
  relais:memory:request → Forgeron (forgeron_archive_group)
    → IntentLabeler (Haiku)
    → SessionStore (SQLite)
    → SkillCreator (LLM precise) → skills_dir/{name}/SKILL.md
    → relais:messages:outgoing_pending (notification utilisateur)
  ```
- Ajouter `SessionSummary`, `SkillProposal`, `IntentLabeler`, `SkillCreator` dans la section Forgeron

**`plans/SKILL_SELF_IMPROVEMENT.md`** :
- Marquer Solution D comme implémentée
- Documenter l'architecture finale : double StreamSpec, SessionStore, cycle de vie intent_label

**`CLAUDE.md`** :
- Mettre à jour la section Forgeron pour mentionner `forgeron_archive_group` et la création de skills

**`common/envelope_actions.py`** — ajouter un commentaire explicatif pour `ACTION_SKILL_CREATED`.

### Vérification
```bash
grep -n "forgeron_archive_group" docs/ARCHITECTURE.md
grep -n "ACTION_SKILL_CREATED" docs/REDIS_BUS_API.md
grep -n "SkillCreator" CLAUDE.md
```

### Exit criteria
- Les 3 fichiers de doc mentionnent `forgeron_archive_group`
- `ACTION_SKILL_CREATED` documenté dans REDIS_BUS_API.md
- `plans/SKILL_SELF_IMPROVEMENT.md` marque Solution D comme implémentée

---

## Résumé

| Step | Titre | Parallèle avec | Modèle |
|------|-------|----------------|--------|
| 1 | Infrastructure ACL + constantes | — | default |
| 2 | Data layer (models + session_store) | 3, 4, 5 | default |
| 3 | IntentLabeler (LLM Haiku) | 2, 4, 5 | default |
| 4 | SkillCreator (LLM precise) | 2, 3, 5 | default |
| 5 | ForgeonConfig extension | 2, 3, 4 | default |
| 6 | Forgeron main.py orchestration | — | default |
| 7 | Tests | 8 | default |
| 8 | Documentation | 7 | default |

**Total** : 8 steps, 2 vagues parallèles (steps 2-5 en parallèle, steps 7-8 en parallèle).

---

## Rollback

- Steps 1-5 : chaque branch est mergeable/revertable indépendamment
- Step 6 : si régression, `git revert` du commit main.py — les StreamSpec / handlers sont ajoutifs
- Step 2 (SQLite) : les nouvelles tables (`session_summaries`, `skill_proposals`) n'impactent pas les tables existantes (`skill_traces`, `skill_patches`). Un `DROP TABLE` les supprime proprement.
- Redis ACL : revenir à la ligne originale dans `config/redis.conf` + restart Redis
