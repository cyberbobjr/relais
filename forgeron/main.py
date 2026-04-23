"""Forgeron brick — autonomous skill improvement and auto-creation.

Functional role
---------------
Forgeron has two independent pipelines:

1. **Skill improvement** (direct edit) — consumes execution traces from Atelier
   and directly rewrites SKILL.md files via a single LLM call scoped per skill.
2. **Skill auto-creation** — consumes session archives and detects recurring
   user intent patterns to generate new skills from scratch.

Skill improvement — trigger rules
----------------------------------
Atelier publishes a trace on ``relais:skill:trace`` after every agent turn
that used skills and made at least one tool call.  The trace carries
``skill_names``, ``tool_call_count``, ``tool_error_count``, and the full
``messages_raw`` conversation history.

For each skill in the trace, Forgeron evaluates **four** trigger conditions.
Analysis fires when **any one** is true (AND ``edit_mode`` is enabled):

1. **Tool errors** — ``tool_error_count >= edit_min_tool_errors``
   (default 1).  Captures turns where the agent struggled.
2. **Aborted turn** — ``tool_error_count == -1`` (sentinel value published
   by Atelier on the DLQ path when ``ToolErrorGuard`` aborts the loop).
   ``messages_raw`` contains the partial conversation captured from the
   LangGraph state so the LLM can diagnose the root cause.
3. **Success after failure** — the current turn has 0 errors, but the
   *previous* turn for the **same skill** had errors.  This is the
   "correction turn" where the agent found the right approach; its
   ``messages_raw`` contains the fix that should be captured.  The flag
   is tracked in-memory per skill (``_last_had_errors``) and resets after
   being consumed (no double-trigger on consecutive successes).
4. **Usage threshold** — cumulative call count for a skill reaches
   ``edit_call_threshold`` (default 5).  Captures usage patterns even when
   there are no errors.  Counter resets after each trigger.

A per-skill **cooldown** (Redis TTL key
``relais:skill:edit_cooldown:{skill_name}``, default 300 s) prevents edit
spam.  If the cooldown is active, the trigger is silently skipped regardless
of the condition that fired.

Skill improvement — single-step direct edit
-------------------------------------------
``SkillEditor`` receives the current SKILL.md + the conversation trace scoped
to the target skill (via ``scope_messages_to_skill``).  It calls the LLM once
with ``with_structured_output`` to produce a rewritten SKILL.md and a
``changed`` flag.  SKILL.md is only written when ``changed=True`` and the
content differs from the current file.

Every attempt — success or failure — is appended to ``edit_history.jsonl``
(next to ``SKILL.md``), recording: Unix timestamp, trigger reason, LLM reason,
``changed`` flag, and the originating ``correlation_id``.  The journal is
capped at ``MAX_HISTORY_ENTRIES`` (50) entries; older lines are pruned via an
atomic tmp-replace write.

Skill auto-creation — trigger rules
-------------------------------------
Forgeron consumes ``relais:memory:request`` via a dedicated consumer group
(``forgeron_archive_group``), independent of Souvenir.  Only envelopes with
``action=ACTION_MEMORY_ARCHIVE`` are processed; others are silently skipped.

For each archive:

1. ``IntentLabeler`` (fast LLM) extracts a normalized snake_case intent
   label (e.g. ``"send_email"``) from ``messages_raw``.  If no clear
   reusable pattern is detected, the label is ``None`` and processing
   stops.
2. The session is recorded in ``SessionStore`` (SQLite) with its label.
3. When ``min_sessions_for_creation`` sessions (default 3) share the same
   label **AND** the creation cooldown has expired
   (``creation_cooldown_seconds``, default 24 h, Redis TTL key
   ``relais:skill:creation_cooldown:{intent_label}``):
   - ``SkillCreator`` (precise LLM) generates a complete ``SKILL.md``
     based on representative session examples.
   - The skill directory is created under ``skills_dir``.
   - A ``skill.created`` event is published on ``relais:events:system``.
   - If ``notify_user_on_creation`` is enabled, a notification is sent
     to the user via ``relais:messages:outgoing_pending``.

Configuration reference (forgeron.yaml)
----------------------------------------
::

    forgeron:
      edit_mode: true                     # enable direct SKILL.md editing
      edit_profile: "precise"             # LLM profile for edits
      edit_min_tool_errors: 1             # min errors to trigger on error condition
      edit_cooldown_seconds: 300          # per-skill cooldown between edits
      edit_call_threshold: 5              # usage-based trigger every N calls
      creation_mode: true                 # enable auto-creation pipeline
      min_sessions_for_creation: 3        # sessions with same intent before creating
      creation_cooldown_seconds: 86400    # 24h between creation attempts per intent
      max_sessions_for_labeling: 5        # max examples passed to SkillCreator
      notify_user_on_creation: true
      llm_profile: "precise"             # LLM profile for SkillCreator
      skills_dir: null                    # resolved from config cascade if null

Technical overview
------------------
Key classes:

* ``Forgeron`` — ``BrickBase`` subclass; two consumer loops:
  ``relais:skill:trace`` and ``relais:memory:request``.
* ``SkillTraceStore`` — SQLite accumulator; tracks one row per agent turn
  that used skills.
* ``SkillEditor`` — rewrites SKILL.md directly from scoped conversation traces.
* ``SessionStore`` — SQLite accumulator for per-session intent patterns.
* ``IntentLabeler`` — fast LLM, extracts snake_case intent label.
* ``SkillCreator`` — precise LLM, generates SKILL.md from examples.

Redis channels
--------------
Consumed:
  - relais:skill:trace         (consumer group: forgeron_group)
  - relais:memory:request      (consumer group: forgeron_archive_group)

Produced:
  - relais:events:system       — skill_created
  - relais:messages:outgoing_pending — user notifications
  - relais:logs                — operational log entries

XACK contract
-------------
  - Both streams use ``ack_mode="always"`` — advisory consumers; losing a
    message is acceptable.  ACKs unconditionally to avoid PEL accumulation.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from common.brick_base import BrickBase, StreamSpec
from common.config_loader import resolve_config_path, resolve_storage_dir
from common.contexts import CTX_FORGERON, CTX_SKILL_TRACE, CTX_SOUVENIR_REQUEST, SkillTraceCtx, ensure_ctx
from common.envelope import Envelope
from common.envelope_actions import (
    ACTION_MEMORY_ARCHIVE,
    ACTION_MEMORY_HISTORY_READ,
    ACTION_MESSAGE_OUTGOING_PENDING,
    ACTION_MESSAGE_TASK,
    ACTION_SKILL_CREATED,
)
from common.profile_loader import ProfileConfig, load_profiles, resolve_profile
from common.streams import (
    STREAM_EVENTS_SYSTEM,
    STREAM_MEMORY_REQUEST,
    STREAM_OUTGOING_PENDING,
    STREAM_SKILL_TRACE,
    STREAM_TASKS,
)
from forgeron.config import ForgeonConfig, load_forgeron_config
from forgeron.models import SkillTrace
from forgeron.session_store import SessionStore
from forgeron.skill_editor import SkillEditor
from forgeron.trace_store import SkillTraceStore

logger = logging.getLogger("forgeron")


@dataclass
class _ConfigSnapshot:
    config: ForgeonConfig
    llm_profile: ProfileConfig | None
    edit_profile: ProfileConfig | None


class Forgeron(BrickBase):
    """Autonomous skill improvement brick.

    Consumes execution traces from Atelier, accumulates them per skill, and
    triggers statistical LLM analysis when a skill shows persistent errors.
    """

    def __init__(self) -> None:
        """Initialise Forgeron with config and SQLite stores."""
        super().__init__("forgeron")
        self._config: ForgeonConfig = ForgeonConfig()
        self._llm_profile: ProfileConfig | None = None
        self._edit_profile: ProfileConfig | None = None
        db_path = resolve_storage_dir() / "forgeron.db"
        self._trace_store = SkillTraceStore(db_path=db_path)
        self._session_store = SessionStore(db_path=db_path)
        self._skill_call_counts: dict[str, int] = {}
        # Enables "success after failure" detection: the turn where the agent
        # recovers carries the fix and is the most valuable one to capture.
        self._last_had_errors: dict[str, bool] = {}
        self._load()

    def _load(self) -> None:
        """Load Forgeron configuration from YAML at init time only.

        Hot-reload uses _build_config_candidate() and _apply_config() instead.
        """
        self._config = load_forgeron_config()
        profiles = load_profiles()
        self._llm_profile = resolve_profile(profiles, self._config.llm_profile)
        self._edit_profile = resolve_profile(profiles, self._config.edit_profile)
        logger.info(
            "Config loaded: edit_mode=%s creation_mode=%s "
            "llm=%s edit=%s skills_dir=%s",
            self._config.edit_mode,
            self._config.creation_mode,
            self._config.llm_profile,
            self._config.edit_profile,
            self._config.skills_dir,
        )

    def _config_watch_paths(self) -> list[Path]:
        """Return forgeron.yaml path for file-based hot-reload.

        Returns:
            List containing the resolved forgeron.yaml path, or empty list if
            the file is not found.
        """
        try:
            return [resolve_config_path("forgeron.yaml")]
        except FileNotFoundError:
            return []

    def _build_config_candidate(self) -> _ConfigSnapshot:
        """Build a fresh config snapshot from disk without mutating self.

        Returns:
            _ConfigSnapshot with config and resolved profiles.
        """
        config = load_forgeron_config()
        profiles = load_profiles()
        return _ConfigSnapshot(
            config=config,
            llm_profile=resolve_profile(profiles, config.llm_profile),
            edit_profile=resolve_profile(profiles, config.edit_profile),
        )

    def _apply_config(self, candidate: _ConfigSnapshot) -> None:
        """Atomically swap in the new config snapshot.

        Args:
            candidate: Snapshot returned by ``_build_config_candidate()``.
        """
        self._config = candidate.config
        self._llm_profile = candidate.llm_profile
        self._edit_profile = candidate.edit_profile
        logger.info(
            "Config reloaded: edit_mode=%s creation_mode=%s "
            "llm=%s edit=%s skills_dir=%s",
            self._config.edit_mode,
            self._config.creation_mode,
            self._config.llm_profile,
            self._config.edit_profile,
            self._config.skills_dir,
        )

    async def on_startup(self, redis: Any) -> None:
        """Create SQLite tables for trace and session stores on first run.

        Args:
            redis: Active Redis connection (unused here, required by hook).
        """
        await self._trace_store._create_tables()
        await self._session_store._create_tables()
        logger.info("Forgeron SQLite tables initialised.")

    def stream_specs(self) -> list[StreamSpec]:
        """Declare the streams this brick consumes.

        Returns:
            Two ``StreamSpec`` entries: ``relais:skill:trace`` for trace analysis
            and ``relais:memory:request`` for auto-creation from archives.
        """
        return [
            StreamSpec(
                stream=STREAM_SKILL_TRACE,
                group="forgeron_group",
                consumer="forgeron_1",
                handler=self._handle_trace,
                ack_mode="always",
            ),
            StreamSpec(
                stream=STREAM_MEMORY_REQUEST,
                group="forgeron_archive_group",
                consumer="forgeron_1",
                handler=self._handle_archive,
                ack_mode="always",
            ),
        ]

    # ------------------------------------------------------------------ #
    # Trace consumer — skill improvement pipeline                        #
    # ------------------------------------------------------------------ #

    async def _handle_trace(self, envelope: Envelope, redis_conn: Any) -> bool:
        """Process a single skill trace message from the stream.

        Emits file-level ``logger.info()`` at every decision point for full
        observability in the supervisord stdout log.

        Args:
            envelope: Envelope with ``action=ACTION_SKILL_TRACE``.
            redis_conn: Active Redis connection.

        Returns:
            Always ``True`` — traces are advisory, XACK unconditionally.
        """
        logger.info(
            "[TRACE] received — corr=%s sender=%s action=%s",
            envelope.correlation_id,
            envelope.sender_id,
            envelope.action,
        )
        try:
            await self._process_trace(envelope, redis_conn)
        except Exception as exc:
            logger.error(
                "[TRACE] ERROR processing trace corr=%s: %s",
                envelope.correlation_id,
                exc,
                exc_info=True,
            )
        return True

    async def _process_trace(self, envelope: Envelope, redis_conn: Any) -> None:
        """Parse, persist, and potentially trigger analysis for a trace.

        Args:
            envelope: Envelope carrying the trace in ``context[CTX_SKILL_TRACE]``.
            redis_conn: Active Redis connection.
        """
        trace_ctx: SkillTraceCtx = envelope.context.get(CTX_SKILL_TRACE, {})  # type: ignore

        skill_names: list[str] = trace_ctx.get("skill_names", [])
        tool_call_count: int = trace_ctx.get("tool_call_count", 0)
        tool_error_count: int = trace_ctx.get("tool_error_count", 0)
        messages_raw_list: list[dict] = trace_ctx.get("messages_raw", [])
        messages_raw: str = json.dumps(messages_raw_list)
        skill_paths: dict[str, str] = trace_ctx.get("skill_paths", {})
        correlation_id: str = envelope.correlation_id
        is_aborted = tool_error_count == -1

        logger.info(
            "[TRACE] parsed — corr=%s skills=%s calls=%d errors=%d "
            "aborted=%s messages_count=%d",
            correlation_id,
            skill_names,
            tool_call_count,
            tool_error_count,
            is_aborted,
            len(messages_raw_list),
        )

        if not skill_names:
            logger.info(
                "[TRACE] SKIP — no skill_names in trace corr=%s",
                correlation_id,
            )
            return

        for skill_name in skill_names:
            trace = SkillTrace(
                skill_name=skill_name,
                correlation_id=correlation_id,
                tool_call_count=tool_call_count,
                tool_error_count=tool_error_count,
                messages_raw=messages_raw,
                skill_path=skill_paths.get(skill_name),
            )
            await self._trace_store.add_trace(trace)
            logger.info(
                "[TRACE] stored — skill='%s' corr=%s calls=%d errors=%d messages=%d",
                skill_name,
                correlation_id,
                tool_call_count,
                tool_error_count,
                len(messages_raw_list),
            )

            # Decide whether to trigger skill edit.
            call_count = self._skill_call_counts.get(skill_name, 0) + 1
            self._skill_call_counts[skill_name] = call_count
            threshold_reached = call_count >= self._config.edit_call_threshold
            if threshold_reached:
                self._skill_call_counts[skill_name] = 0

            # "Success after failure" — the previous turn for this skill had
            # errors, but this one succeeded. This is the correction turn where
            # the agent found the right approach; its messages_raw contains the
            # fix that should be captured in the skill.
            prev_had_errors = self._last_had_errors.get(skill_name, False)
            success_after_failure = (
                prev_had_errors
                and tool_error_count == 0
                and not is_aborted
            )

            # Update the flag for the next turn.
            self._last_had_errors[skill_name] = (
                tool_error_count > 0 or is_aborted
            )

            should_edit = (
                (tool_error_count > 0
                 or is_aborted
                 or threshold_reached
                 or success_after_failure)
                and self._config.edit_mode
            )

            if not should_edit:
                logger.info(
                    "[TRACE] edit NOT triggered — skill='%s' corr=%s "
                    "errors=%d aborted=%s threshold=%s/%d saf=%s mode=%s",
                    skill_name,
                    correlation_id,
                    tool_error_count,
                    is_aborted,
                    call_count,
                    self._config.edit_call_threshold,
                    success_after_failure,
                    self._config.edit_mode,
                )
                continue

            if self._edit_profile is None:
                logger.warning(
                    "[TRACE] edit SKIPPED — edit_profile is None "
                    "for skill='%s' corr=%s",
                    skill_name,
                    correlation_id,
                )
                continue

            reason = (
                "aborted" if is_aborted
                else f"{tool_error_count} errors" if tool_error_count > 0
                else "success after failure" if success_after_failure
                else f"threshold ({call_count} calls)"
            )
            logger.info(
                "[TRACE] edit TRIGGERED — skill='%s' reason=%s corr=%s "
                "profile=%s skills_dir=%s",
                skill_name,
                reason,
                correlation_id,
                self._edit_profile.model,
                self._config.skills_dir,
            )

            editor = SkillEditor(
                profile=self._edit_profile,
                skills_dir=self._config.skills_dir,
            )
            raw_skill_path = skill_paths.get(skill_name)
            skill_dir_override = Path(raw_skill_path) if raw_skill_path else None
            edited = await editor.edit(
                skill_name=skill_name,
                messages_raw=messages_raw_list,
                config=self._config,
                redis_conn=redis_conn,
                trigger_reason=reason,
                force=threshold_reached,
                skill_path=skill_dir_override,
                correlation_id=correlation_id,
            )
            logger.info(
                "[TRACE] edit result — skill='%s' edited=%s corr=%s",
                skill_name,
                edited,
                correlation_id,
            )

    # ------------------------------------------------------------------ #
    # Archive consumer — auto-creation pipeline                          #
    # ------------------------------------------------------------------ #

    async def _handle_archive(self, envelope: Envelope, redis_conn: Any) -> bool:
        """Consume an Atelier session archive and detect recurring intent patterns.

        Args:
            envelope: Archive envelope with ``action=ACTION_MEMORY_ARCHIVE``.
            redis_conn: Active Redis connection.

        Returns:
            Always ``True`` — advisory consumer, ACK unconditionally.
        """
        logger.info(
            "[ARCHIVE] received — corr=%s action=%s sender=%s session=%s",
            envelope.correlation_id,
            envelope.action,
            envelope.sender_id,
            envelope.session_id,
        )
        if not self._config.creation_mode and not self._config.correction_mode:
            logger.info(
                "[ARCHIVE] SKIP — creation_mode=False and correction_mode=False corr=%s",
                envelope.correlation_id,
            )
            return True
        try:
            await self._process_archive(envelope, redis_conn)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[ARCHIVE] ERROR processing archive corr=%s: %s",
                envelope.correlation_id,
                exc,
                exc_info=True,
            )
        return True

    async def _process_archive(self, envelope: Envelope, redis_conn: Any) -> None:
        """Parse an archive envelope and run the intent labeling pipeline.

        Args:
            envelope: Archive envelope from ``relais:memory:request``.
            redis_conn: Active Redis connection.
        """
        corr = envelope.correlation_id

        if envelope.action != ACTION_MEMORY_ARCHIVE:
            logger.info(
                "[ARCHIVE] SKIP non-archive action='%s' corr=%s",
                envelope.action,
                corr,
            )
            return

        souvenir_ctx = envelope.context.get(CTX_SOUVENIR_REQUEST, {})
        envelope_json: str = souvenir_ctx.get("envelope_json", "")
        messages_raw_raw = souvenir_ctx.get("messages_raw", "[]")

        if not envelope_json:
            logger.info(
                "[ARCHIVE] SKIP — no envelope_json in souvenir_request corr=%s",
                corr,
            )
            return

        original_env = Envelope.from_json(envelope_json)
        channel = original_env.channel
        sender_id = original_env.sender_id

        # Deserialize messages_raw (can be str or list depending on serialization)
        if isinstance(messages_raw_raw, str):
            try:
                messages_raw: list[dict] = json.loads(messages_raw_raw)
            except (json.JSONDecodeError, ValueError):
                messages_raw = []
        else:
            messages_raw = list(messages_raw_raw) if messages_raw_raw else []

        # Extract user content preview (first human message, max 200 chars)
        user_preview = ""
        for msg in messages_raw:
            if msg.get("type") == "human":
                user_preview = str(msg.get("content", ""))[:200]
                break

        logger.info(
            "[ARCHIVE] parsed — corr=%s channel=%s sender=%s "
            "messages=%d preview='%s'",
            corr,
            channel,
            sender_id,
            len(messages_raw),
            user_preview[:80],
        )

        # Run intent labeling with the fast LLM profile
        from forgeron.intent_labeler import IntentLabelResult  # noqa: PLC0415

        label_result: IntentLabelResult | None = None
        intent_label: str | None = None
        if self._llm_profile is not None:
            logger.info(
                "[ARCHIVE] intent labeling — corr=%s model=%s",
                corr,
                self._llm_profile.model,
            )
            try:
                from forgeron.intent_labeler import IntentLabeler  # noqa: PLC0415

                labeler = IntentLabeler(profile=self._llm_profile)
                label_result = await labeler.label(messages_raw)
                intent_label = label_result.label
                logger.info(
                    "[ARCHIVE] intent result — corr=%s label='%s' is_correction=%s",
                    corr,
                    intent_label,
                    label_result.is_correction,
                )
            except ImportError:
                logger.warning(
                    "[ARCHIVE] IntentLabeler not importable — skipping corr=%s",
                    corr,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[ARCHIVE] IntentLabeler failed — corr=%s error=%s",
                    corr,
                    exc,
                )
        else:
            logger.info(
                "[ARCHIVE] SKIP intent labeling — llm_profile=None corr=%s",
                corr,
            )

        # Correction pipeline — independent of creation_mode
        if (
            label_result is not None
            and label_result.is_correction
            and self._config.correction_mode
        ):
            logger.info(
                "[ARCHIVE] correction detected — triggering skill-designer corr=%s "
                "behavior='%s' hint='%s'",
                corr,
                label_result.corrected_behavior,
                label_result.skill_name_hint,
            )
            await self._trigger_skill_design(
                envelope=envelope,
                channel=channel,
                sender_id=sender_id,
                corrected_behavior=label_result.corrected_behavior or "",
                skill_name_hint=label_result.skill_name_hint,
                redis_conn=redis_conn,
            )
            return

        if not self._config.creation_mode:
            logger.info(
                "[ARCHIVE] SKIP creation — creation_mode=False corr=%s",
                envelope.correlation_id,
            )
            return

        # Record in SQLite
        await self._session_store.record_session(
            session_id=envelope.session_id,
            correlation_id=envelope.correlation_id,
            channel=channel,
            sender_id=sender_id,
            intent_label=intent_label,
            user_content_preview=user_preview,
        )
        logger.info(
            "[ARCHIVE] session recorded — corr=%s intent=%s session=%s",
            corr,
            intent_label or "none",
            envelope.session_id,
        )

        if intent_label is None:
            logger.info(
                "[ARCHIVE] STOP — no intent label, skipping creation check corr=%s",
                corr,
            )
            return

        # Check creation thresholds
        should = await self._session_store.should_create(
            intent_label,
            min_sessions=self._config.min_sessions_for_creation,
            redis_conn=redis_conn,
        )
        logger.info(
            "[ARCHIVE] creation check — intent='%s' should_create=%s "
            "min_sessions=%d corr=%s",
            intent_label,
            should,
            self._config.min_sessions_for_creation,
            corr,
        )
        if should:
            await self._trigger_creation(
                intent_label=intent_label,
                channel=channel,
                sender_id=sender_id,
                session_id=envelope.session_id,
                correlation_id=envelope.correlation_id,
                redis_conn=redis_conn,
            )

    async def _trigger_skill_design(
        self,
        envelope: Envelope,
        channel: str,
        sender_id: str,
        corrected_behavior: str,
        skill_name_hint: str | None,
        redis_conn: Any,
        skill_path: str | None = None,
    ) -> None:
        """Orchestrate the correction pipeline: fetch history, notify user, then publish task.

        Publishes a history-read request to Souvenir, sends a user notification
        (before the BRPOP so it's never blocked), then waits for the history
        payload.  If history is returned, publishes a task envelope to Atelier
        with ``force_subagent = "skill-designer"`` so it redesigns the skill.

        The notification is always published (even on BRPOP timeout) because it
        fires before the blocking call.

        Args:
            envelope: Original archive envelope (provides session/correlation context).
            channel: Channel for routing the outgoing notification.
            sender_id: Sender identifier for the outgoing notification.
            corrected_behavior: Description of the desired corrected behavior.
            skill_name_hint: Optional hint for the skill name to redesign.
            redis_conn: Active Redis connection.
        """
        corr = envelope.correlation_id
        session_id = envelope.session_id
        response_key = f"relais:memory:response:{corr}"

        # 1. Publish history-read request to Souvenir
        history_req = Envelope.create_response_to(envelope, "")
        history_req.action = ACTION_MEMORY_HISTORY_READ
        ctx = ensure_ctx(history_req, "souvenir_request")
        ctx["action"] = "history_read"
        ctx["session_id"] = session_id
        ctx["correlation_id"] = corr
        ctx["response_key"] = response_key
        await redis_conn.xadd(STREAM_MEMORY_REQUEST, {"payload": history_req.to_json()})
        logger.info(
            "[CORRECTION] history-read published — corr=%s session=%s key=%s",
            corr,
            session_id,
            response_key,
        )

        # 2. Publish user notification BEFORE BRPOP (non-blocking)
        notif = Envelope.create_response_to(envelope, "")
        notif.action = ACTION_MESSAGE_OUTGOING_PENDING
        notif.content = (
            "I am working on improving my skills, "
            "a new skill is being created..."
        )
        await redis_conn.xadd(STREAM_OUTGOING_PENDING, {"payload": notif.to_json()})
        logger.info(
            "[CORRECTION] user notification published — corr=%s",
            corr,
        )

        # 3. Wait for history payload (BRPOP)
        result = await redis_conn.brpop(
            response_key,
            timeout=self._config.history_read_timeout_seconds,
        )
        if result is None:
            logger.warning(
                "[CORRECTION] BRPOP timeout — no history received — corr=%s key=%s",
                corr,
                response_key,
            )
            return

        # 4. Parse history and publish task for skill-designer
        try:
            _key, raw_payload = result
            history_turns: list[list[dict]] = json.loads(raw_payload)
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.warning(
                "[CORRECTION] invalid history payload — corr=%s error=%s",
                corr,
                exc,
            )
            return

        task_env = Envelope.create_response_to(envelope, "")
        task_env.action = ACTION_MESSAGE_TASK
        forgeron_ctx = ensure_ctx(task_env, CTX_FORGERON)
        forgeron_ctx["force_subagent"] = "skill-designer"
        forgeron_ctx["corrected_behavior"] = corrected_behavior
        if skill_name_hint:
            forgeron_ctx["skill_name_hint"] = skill_name_hint
        if skill_path:
            forgeron_ctx["skill_path"] = skill_path
        forgeron_ctx["history_turns"] = history_turns

        await redis_conn.xadd(STREAM_TASKS, {"payload": task_env.to_json()})
        logger.info(
            "[CORRECTION] task published to skill-designer — corr=%s turns=%d",
            corr,
            len(history_turns),
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
        """Create a new skill from recurring session patterns.

        Args:
            intent_label: Normalized intent label (e.g. ``"send_email"``).
            channel: Original channel (e.g. ``"discord"``).
            sender_id: Original sender_id.
            session_id: Session ID for tracking.
            correlation_id: Correlation ID for tracking.
            redis_conn: Active Redis connection.
        """
        logger.info(
            "[CREATION] starting — intent='%s' corr=%s sender=%s",
            intent_label,
            correlation_id,
            sender_id,
        )

        # Set cooldown to prevent concurrent triggers for the same label
        cooldown_key = f"relais:skill:creation_cooldown:{intent_label}"
        await redis_conn.set(cooldown_key, "1", ex=self._config.creation_cooldown_seconds)

        try:
            from forgeron.skill_creator import SkillCreator  # noqa: PLC0415
        except ImportError as exc:
            logger.warning(
                "[CREATION] SkillCreator not importable: %s corr=%s",
                exc,
                correlation_id,
            )
            return

        if self._config.skills_dir is None:
            logger.warning(
                "[CREATION] ABORT — skills_dir is None corr=%s",
                correlation_id,
            )
            return
        if self._llm_profile is None:
            logger.warning(
                "[CREATION] ABORT — llm_profile not loaded corr=%s",
                correlation_id,
            )
            return

        representative = await self._session_store.get_representative_sessions(
            intent_label, limit=self._config.max_sessions_for_labeling
        )
        session_examples = [
            {"user_content_preview": s.user_content_preview}
            for s in representative
        ]
        logger.info(
            "[CREATION] calling SkillCreator — intent='%s' examples=%d "
            "model=%s corr=%s",
            intent_label,
            len(session_examples),
            self._llm_profile.model,
            correlation_id,
        )

        creator = SkillCreator(profile=self._llm_profile, skills_dir=self._config.skills_dir)
        result = await creator.create(intent_label, session_examples)
        if result is None:
            logger.warning(
                "[CREATION] SkillCreator returned None — intent='%s' corr=%s",
                intent_label,
                correlation_id,
            )
            return

        await self._session_store.mark_created(intent_label, result.skill_name)
        logger.info(
            "[CREATION] SUCCESS — skill='%s' intent='%s' path=%s "
            "sessions=%d corr=%s",
            result.skill_name,
            intent_label,
            result.skill_path,
            len(representative),
            correlation_id,
        )

        # Publish skill.created event on relais:events:system
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

        if self._config.notify_user_on_creation:
            await self._notify_user(
                channel=channel,
                sender_id=sender_id,
                session_id=session_id,
                correlation_id=correlation_id,
                message=(
                    f"[Forgeron] New skill automatically created: `{result.skill_name}`\n"
                    f"{result.description}\n"
                    f"_(based on {len(representative)} recurring sessions)_"
                ),
                redis_conn=redis_conn,
            )

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

        Args:
            channel: Original channel (e.g. ``"discord"``).
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
        logger.info(
            "[NOTIFY] sent to %s/%s — '%s' corr=%s",
            channel,
            sender_id,
            message[:60],
            correlation_id,
        )

if __name__ == "__main__":
    from common.init import initialize_user_dir

    initialize_user_dir()
    forgeron = Forgeron()
    try:
        asyncio.run(forgeron.start())
    except KeyboardInterrupt:
        pass
