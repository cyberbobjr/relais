"""Forgeron brick — autonomous skill improvement and auto-creation.

Functional role
---------------
Forgeron improves existing skills via S3 (changelog + periodic consolidation)
AND auto-creates new skills from recurring session patterns.

**Changelog + consolidation (S3)**:
Consumes skill execution traces published by Atelier on ``relais:skill:trace``.
Phase 1: ``ChangelogWriter`` (fast LLM) extracts 1-3 observations per turn and
writes them to a per-skill ``CHANGELOG.md``.  The SKILL.md is never touched.
Phase 2: when the changelog exceeds a line threshold, ``SkillConsolidator``
(precise LLM) rewrites SKILL.md by absorbing the observations, produces an
audit trail in ``CHANGELOG_DIGEST.md``, and clears the changelog.

**Auto-creation from session archives**:
Consumes Atelier session archives on ``relais:memory:request`` via a dedicated
consumer group (``forgeron_archive_group``), independent of Souvenir's group.
An ``IntentLabeler`` (Fast LLM) extracts a normalized intent label from each
session.  When N sessions share the same label, a ``SkillCreator`` (precise LLM)
generates a new ``SKILL.md`` automatically.

**User notifications**:
When Forgeron creates or consolidates a skill, it publishes a human-readable
notification to ``relais:messages:outgoing_pending``.  Sentinelle picks it up
and routes it to the user via the channel adapter.

Technical overview
------------------
Key classes:

* ``Forgeron`` — ``BrickBase`` subclass; two consumer loops:
  ``relais:skill:trace`` and ``relais:memory:request``.
* ``SkillTraceStore`` — SQLite accumulator; tracks one row per agent turn
  that used skills.
* ``ChangelogWriter`` — Phase 1: appends observations to CHANGELOG.md (fast LLM).
* ``SkillConsolidator`` — Phase 2: rewrites SKILL.md from changelog (precise LLM).
* ``SessionStore`` — SQLite accumulator for per-session intent patterns;
  drives the auto-creation pipeline.
* ``IntentLabeler`` — Fast LLM, extracts snake_case intent label (lazy).
* ``SkillCreator`` — precise LLM, generates SKILL.md from examples (lazy).

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
from typing import Any

from common.brick_base import BrickBase, StreamSpec
from common.config_loader import resolve_storage_dir
from common.contexts import CTX_FORGERON, CTX_SKILL_TRACE, CTX_SOUVENIR_REQUEST, SkillTraceCtx, ensure_ctx
from common.envelope import Envelope
from common.envelope_actions import (
    ACTION_MEMORY_ARCHIVE,
    ACTION_MESSAGE_OUTGOING_PENDING,
    ACTION_SKILL_CREATED,
)
from common.profile_loader import ProfileConfig, load_profiles, resolve_profile
from common.streams import (
    STREAM_EVENTS_SYSTEM,
    STREAM_MEMORY_REQUEST,
    STREAM_OUTGOING_PENDING,
    STREAM_SKILL_TRACE,
)
from forgeron.changelog_writer import ChangelogWriter
from forgeron.config import ForgeonConfig, load_forgeron_config
from forgeron.models import SkillTrace
from forgeron.session_store import SessionStore
from forgeron.trace_store import SkillTraceStore

logger = logging.getLogger("forgeron")


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
        self._annotation_profile: ProfileConfig | None = None
        self._consolidation_profile: ProfileConfig | None = None
        db_path = resolve_storage_dir() / "forgeron.db"
        self._trace_store = SkillTraceStore(db_path=db_path)
        self._session_store = SessionStore(db_path=db_path)
        # In-memory call counter per skill — triggers annotation every N calls.
        self._skill_call_counts: dict[str, int] = {}

    def _load(self) -> None:
        """Reload Forgeron configuration from YAML.

        Called by BrickBase on startup and on hot-reload signals.
        """
        self._config = load_forgeron_config()
        profiles = load_profiles()
        self._llm_profile = resolve_profile(profiles, self._config.llm_profile)
        self._annotation_profile = resolve_profile(profiles, self._config.annotation_profile)
        self._consolidation_profile = resolve_profile(profiles, self._config.consolidation_profile)
        logger.info(
            "Forgeron config loaded: annotation_mode=%s llm_profile=%s "
            "annotation_profile=%s consolidation_profile=%s",
            self._config.annotation_mode,
            self._config.llm_profile,
            self._config.annotation_profile,
            self._config.consolidation_profile,
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

    async def _handle_trace(self, envelope: Envelope, redis_conn: Any) -> bool:
        """Process a single skill trace message from the stream.

        Reads the trace payload from ``envelope.context[CTX_SKILL_TRACE]``,
        persists it to SQLite, and triggers Phase 1 changelog writing and
        Phase 2 consolidation when thresholds are met.

        Args:
            envelope: Envelope with ``action=ACTION_SKILL_TRACE`` carrying
                the trace payload in ``context[CTX_SKILL_TRACE]``.
            redis_conn: Active Redis connection.

        Returns:
            Always ``True`` — traces are advisory, XACK unconditionally.
        """
        try:
            await self._process_trace(envelope, redis_conn)
        except Exception as exc:
            logger.error("Error processing skill trace: %s", exc, exc_info=True)
        return True

    async def _process_trace(self, envelope: Envelope, redis_conn: Any) -> None:
        """Parse, persist, and potentially trigger analysis for a trace.

        Args:
            envelope: Envelope carrying the trace in ``context[CTX_SKILL_TRACE]``.
            redis_conn: Active Redis connection.
        """
        trace_ctx: SkillTraceCtx = envelope.context.get(CTX_SKILL_TRACE, {}) # type: ignore

        skill_names: list[str] = trace_ctx.get("skill_names", [])
        tool_call_count: int = trace_ctx.get("tool_call_count", 0)
        tool_error_count: int = trace_ctx.get("tool_error_count", 0)
        messages_raw_list: list[dict] = trace_ctx.get("messages_raw", [])
        messages_raw: str = json.dumps(messages_raw_list)
        correlation_id: str = envelope.correlation_id

        if not skill_names:
            logger.debug("Trace has no skill_names, skipping.")
            return

        for skill_name in skill_names:
            trace = SkillTrace(
                skill_name=skill_name,
                correlation_id=correlation_id,
                tool_call_count=tool_call_count,
                tool_error_count=tool_error_count,
                messages_raw=messages_raw,
            )
            await self._trace_store.add_trace(trace)

            await self.log.info(
                f"Trace stored for skill '{skill_name}' "
                f"(calls={tool_call_count} errors={tool_error_count})",
                correlation_id=correlation_id,
                sender_id=envelope.sender_id,
            )

            # Write observations to CHANGELOG.md on errors or after N calls.
            # tool_error_count == -1 is the sentinel for turns aborted before completion
            # (DLQ-routed by Atelier); these should always trigger analysis.
            is_aborted = tool_error_count == -1
            call_count = self._skill_call_counts.get(skill_name, 0) + 1
            self._skill_call_counts[skill_name] = call_count
            threshold_reached = call_count >= self._config.annotation_call_threshold
            if threshold_reached:
                self._skill_call_counts[skill_name] = 0  # reset so it fires every N calls
            if (tool_error_count > 0 or is_aborted or threshold_reached) and self._config.annotation_mode:
                if self._annotation_profile is not None:
                    writer = ChangelogWriter(
                        profile=self._annotation_profile,
                        skills_dir=self._config.skills_dir,
                    )
                    wrote = await writer.observe(
                        skill_name=skill_name,
                        tool_error_count=tool_error_count,
                        messages_raw=messages_raw_list,
                        config=self._config,
                        redis_conn=redis_conn,
                        force=threshold_reached,
                    )

                    # Check whether the changelog warrants consolidation.
                    if wrote and self._consolidation_profile is not None:
                        cl_path = writer.changelog_path(skill_name)
                        if cl_path is not None and await writer.should_consolidate(
                            cl_path, self._config, redis_conn, skill_name
                        ):
                            await self._maybe_consolidate(
                                skill_name=skill_name,
                                envelope=envelope,
                                redis_conn=redis_conn,
                            )

    async def _maybe_consolidate(
        self,
        skill_name: str,
        envelope: Envelope,
        redis_conn: Any,
    ) -> None:
        """Run periodic consolidation and optionally notify the user.

        Args:
            skill_name: Skill directory name.
            envelope: Original trace envelope (for notification routing).
            redis_conn: Active Redis connection.
        """
        from forgeron.skill_consolidator import SkillConsolidator  # noqa: PLC0415

        consolidator = SkillConsolidator(
            profile=self._consolidation_profile,  # type: ignore[arg-type]
            skills_dir=self._config.skills_dir,  # type: ignore[arg-type]
        )
        consolidated = await consolidator.consolidate(
            skill_name, redis_conn, self._config
        )
        if consolidated:
            await self.log.info(
                f"Consolidation completed for skill '{skill_name}'",
                correlation_id=envelope.correlation_id,
                sender_id=envelope.sender_id,
            )
            if self._config.notify_user_on_consolidation:
                await self._notify_user(
                    channel=envelope.channel,
                    sender_id=envelope.sender_id,
                    session_id=envelope.session_id,
                    correlation_id=envelope.correlation_id,
                    message=(
                        f"[Forgeron] Skill `{skill_name}` consolidated — "
                        "SKILL.md has been rewritten with accumulated observations."
                    ),
                    redis_conn=redis_conn,
                )

    # ------------------------------------------------------------------ #
    # Archive consumer — auto-creation pipeline                          #
    # ------------------------------------------------------------------ #

    async def _handle_archive(self, envelope: Envelope, redis_conn: Any) -> bool:
        """Consume an Atelier session archive and detect recurring intent patterns.

        Extracts ``messages_raw`` and the original channel/sender_id from
        ``CTX_SOUVENIR_REQUEST``, runs ``IntentLabeler``, records the session
        in ``SessionStore``, and triggers ``SkillCreator`` when thresholds are met.

        Args:
            envelope: Archive envelope with ``action=ACTION_MEMORY_ARCHIVE``.
            redis_conn: Active Redis connection.

        Returns:
            Always ``True`` — advisory consumer, ACK unconditionally.
        """
        if not self._config.creation_mode:
            return True
        try:
            await self._process_archive(envelope, redis_conn)
        except Exception as exc:  # noqa: BLE001
            logger.error("Error processing archive: %s", exc, exc_info=True)
        return True

    async def _process_archive(self, envelope: Envelope, redis_conn: Any) -> None:
        """Parse an archive envelope and run the intent labeling pipeline.

        Args:
            envelope: Archive envelope from ``relais:memory:request``.
            redis_conn: Active Redis connection.
        """
        corr = envelope.correlation_id

        if envelope.action != ACTION_MEMORY_ARCHIVE:
            logger.debug(
                "Skipping non-archive action '%s' on memory:request.", envelope.action
            )
            return

        souvenir_ctx = envelope.context.get(CTX_SOUVENIR_REQUEST, {})
        envelope_json: str = souvenir_ctx.get("envelope_json", "")
        messages_raw_raw = souvenir_ctx.get("messages_raw", "[]")

        if not envelope_json:
            logger.debug("Archive has no envelope_json, skipping intent labeling.")
            return

        original_env = Envelope.from_json(envelope_json)
        channel = original_env.channel
        sender_id = original_env.sender_id

        await self.log.info(
            f"Archive received from {channel}/{sender_id}",
            correlation_id=corr,
            sender_id=sender_id,
        )

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

        # Run intent labeling with the cheap annotation profile (Haiku)
        intent_label: str | None = None
        if self._annotation_profile is not None:
            try:
                from forgeron.intent_labeler import IntentLabeler  # noqa: PLC0415

                labeler = IntentLabeler(profile=self._annotation_profile)
                intent_label = await labeler.label(messages_raw)
            except ImportError:
                await self.log.warning(
                    "IntentLabeler not available, skipping.",
                    correlation_id=corr,
                )
            except Exception as exc:  # noqa: BLE001
                await self.log.warning(
                    f"IntentLabeler failed: {exc}",
                    correlation_id=corr,
                    sender_id=sender_id,
                )

        # Record in SQLite
        await self._session_store.record_session(
            session_id=envelope.session_id,
            correlation_id=envelope.correlation_id,
            channel=channel,
            sender_id=sender_id,
            intent_label=intent_label,
            user_content_preview=user_preview,
        )

        await self.log.info(
            f"Session recorded (intent={intent_label or 'none'}, "
            f"preview='{user_preview[:50]}...')",
            correlation_id=corr,
            sender_id=sender_id,
        )

        if intent_label is None:
            return

        # Check creation thresholds
        should = await self._session_store.should_create(
            intent_label,
            min_sessions=self._config.min_sessions_for_creation,
            redis_conn=redis_conn,
        )
        if should:
            await self.log.info(
                f"Creation threshold reached for intent '{intent_label}' "
                f"(min={self._config.min_sessions_for_creation})",
                correlation_id=corr,
                sender_id=sender_id,
            )
            await self._trigger_creation(
                intent_label=intent_label,
                channel=channel,
                sender_id=sender_id,
                session_id=envelope.session_id,
                correlation_id=envelope.correlation_id,
                redis_conn=redis_conn,
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

        Sets the cooldown key immediately, then calls ``SkillCreator`` with
        representative session examples.  On success, records the result in
        ``SessionStore``, publishes a ``skill.created`` event, and notifies
        the user.

        Args:
            intent_label: Normalized intent label (e.g. ``"send_email"``).
            channel: Original channel (e.g. ``"discord"``).
            sender_id: Original sender_id.
            session_id: Session ID for tracking.
            correlation_id: Correlation ID for tracking.
            redis_conn: Active Redis connection.
        """
        # Set cooldown to prevent concurrent triggers for the same label
        cooldown_key = f"relais:skill:creation_cooldown:{intent_label}"
        await redis_conn.set(cooldown_key, "1", ex=self._config.creation_cooldown_seconds)

        await self.log.info(
            f"Triggering skill creation for intent '{intent_label}'",
            correlation_id=correlation_id,
            sender_id=sender_id,
        )

        try:
            from forgeron.skill_creator import SkillCreator  # noqa: PLC0415
        except ImportError as exc:
            await self.log.warning(
                f"SkillCreator not available: {exc}",
                correlation_id=correlation_id,
            )
            return

        if self._config.skills_dir is None:
            await self.log.warning(
                "skills_dir is None — cannot create skill.",
                correlation_id=correlation_id,
            )
            return
        if self._llm_profile is None:
            await self.log.warning(
                "llm_profile not loaded — cannot create skill.",
                correlation_id=correlation_id,
            )
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
            await self.log.warning(
                f"SkillCreator returned None for intent '{intent_label}'",
                correlation_id=correlation_id,
            )
            return

        await self._session_store.mark_created(intent_label, result.skill_name)

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
        await self.log.info(
            f"Skill '{result.skill_name}' created for intent '{intent_label}' "
            f"({len(representative)} contributing sessions)",
            correlation_id=correlation_id,
            sender_id=sender_id,
        )

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

        Sentinelle's outgoing loop picks it up, applies guardrails, and routes
        it to ``relais:messages:outgoing:{channel}`` for the channel adapter to
        deliver.

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
        await self.log.info(
            f"Notification sent to {channel}/{sender_id}: {message[:60]}",
            correlation_id=correlation_id,
            sender_id=sender_id,
        )

if __name__ == "__main__":
    from common.init import initialize_user_dir

    initialize_user_dir()
    forgeron = Forgeron()
    try:
        asyncio.run(forgeron.start())
    except KeyboardInterrupt:
        pass
