"""Forgeron brick — autonomous skill improvement and auto-creation.

Functional role
---------------
Forgeron improves existing skills via statistical trace analysis AND
auto-creates new skills from recurring session patterns.

**Trace analysis (patch mode)**:
Consumes skill execution traces published by Atelier on ``relais:skill:trace``,
accumulates them per skill in SQLite, and triggers an LLM-based analysis when
a skill shows a persistently high tool error rate.  If the analysis produces
an improved SKILL.md, the patch is applied atomically and monitored for
regression.

**Auto-creation from session archives**:
Consumes Atelier session archives on ``relais:memory:request`` via a dedicated
consumer group (``forgeron_archive_group``), independent of Souvenir's group.
An ``IntentLabeler`` (Fast LLM) extracts a normalized intent label from each
session.  When N sessions share the same label, a ``SkillCreator`` (precise LLM)
generates a new ``SKILL.md`` automatically.

**User notifications**:
When Forgeron applies a patch or creates a new skill, it publishes a
human-readable notification to ``relais:messages:outgoing_pending``.
Sentinelle picks it up and routes it to the user via the channel adapter.

Technical overview
------------------
Key classes:

* ``Forgeron`` — ``BrickBase`` subclass; two consumer loops:
  ``relais:skill:trace`` and ``relais:memory:request``.
* ``SkillTraceStore`` — SQLite accumulator; tracks one row per agent turn
  that used skills.
* ``SkillPatchStore`` — SQLite store for versioned patches (pending / applied
  / validated / rolled_back).
* ``SessionStore`` — SQLite accumulator for per-session intent patterns;
  drives the auto-creation pipeline.
* ``SkillAnalyzer`` — LLM analysis → ``SkillPatchProposal`` (lazy import).
* ``SkillPatcher`` — atomic write with ``.pending`` → ``.bak`` snapshot (lazy).
* ``SkillValidator`` — post-patch regression monitor (lazy).
* ``IntentLabeler`` — Fast LLM, extracts snake_case intent label (lazy).
* ``SkillCreator`` — precise LLM, generates SKILL.md from examples (lazy).

Redis channels
--------------
Consumed:
  - relais:skill:trace         (consumer group: forgeron_group)
  - relais:memory:request      (consumer group: forgeron_archive_group)

Produced:
  - relais:events:system       — skill_patch_applied / skill_patch_rolled_back
                                  / skill_created
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
from pathlib import Path
from typing import Any

from common.brick_base import BrickBase, StreamSpec
from common.config_loader import resolve_storage_dir
from common.contexts import CTX_FORGERON, CTX_SKILL_TRACE, CTX_SOUVENIR_REQUEST, SkillTraceCtx, ensure_ctx
from common.envelope import Envelope
from common.envelope_actions import (
    ACTION_MEMORY_ARCHIVE,
    ACTION_MESSAGE_OUTGOING_PENDING,
    ACTION_SKILL_CREATED,
    ACTION_SKILL_PATCH_APPLIED,
    ACTION_SKILL_PATCH_ROLLED_BACK,
)
from common.profile_loader import ProfileConfig, load_profiles, resolve_profile
from common.streams import (
    STREAM_EVENTS_SYSTEM,
    STREAM_LOGS,
    STREAM_MEMORY_REQUEST,
    STREAM_OUTGOING_PENDING,
    STREAM_SKILL_TRACE,
)
from forgeron.config import ForgeonConfig, load_forgeron_config
from forgeron.models import SkillTrace
from forgeron.patch_store import SkillPatchStore
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
        db_path = resolve_storage_dir() / "forgeron.db"
        self._trace_store = SkillTraceStore(db_path=db_path)
        self._patch_store = SkillPatchStore(db_path=db_path)
        self._session_store = SessionStore(db_path=db_path)

    def _load(self) -> None:
        """Reload Forgeron configuration from YAML.

        Called by BrickBase on startup and on hot-reload signals.
        """
        self._config = load_forgeron_config()
        profiles = load_profiles()
        self._llm_profile = resolve_profile(profiles, self._config.llm_profile)
        self._annotation_profile = resolve_profile(profiles, self._config.annotation_profile)
        logger.info(
            "Forgeron config loaded: min_traces=%d min_error_rate=%.0f%% "
            "patch_mode=%s annotation_mode=%s llm_profile=%s annotation_profile=%s",
            self._config.min_traces_before_analysis,
            self._config.min_error_rate * 100,
            self._config.patch_mode,
            self._config.annotation_mode,
            self._config.llm_profile,
            self._config.annotation_profile,
        )

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
        persists it, and checks whether analysis should be triggered.

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
            # Retrieve the active patch ID (if any) to tag this trace correctly.
            active_patch = await self._patch_store.get_applied_patch(skill_name)
            patch_id = active_patch.id if active_patch else None

            trace = SkillTrace(
                skill_name=skill_name,
                correlation_id=correlation_id,
                tool_call_count=tool_call_count,
                tool_error_count=tool_error_count,
                messages_raw=messages_raw,
                patch_id=patch_id,
            )
            await self._trace_store.add_trace(trace)

            logger.info(
                "Stored trace for skill '%s' (calls=%d errors=%d patch=%s)",
                skill_name,
                tool_call_count,
                tool_error_count,
                patch_id or "none",
            )
            await redis_conn.xadd(STREAM_LOGS, {
                "level": "INFO",
                "brick": "forgeron",
                "correlation_id": envelope.correlation_id,
                "sender_id": envelope.sender_id,
                "message": (
                    f"Trace stored for skill '{skill_name}' "
                    f"(calls={tool_call_count} errors={tool_error_count} patch={patch_id or 'none'})"
                ),
            })

            # Post-patch validation: if there is an active patch, check for
            # regression (Step 3 — SkillValidator; loaded lazily when available).
            if active_patch is not None and self._config.patch_mode:
                await self._maybe_validate_patch(
                    envelope, skill_name, active_patch, redis_conn
                )

            # Trigger full LLM analysis if thresholds are met.
            if self._config.patch_mode:
                should = await self._trace_store.should_analyze(
                    skill_name, self._config, redis_conn
                )
                if should:
                    await self._trigger_analysis(
                        envelope, skill_name, correlation_id, redis_conn
                    )

    async def _trigger_analysis(
        self,
        envelope: Envelope,
        skill_name: str,
        trigger_corr_id: str,
        redis_conn: Any,
    ) -> None:
        """Trigger LLM analysis for a skill with a high error rate.

        Sets the cooldown key immediately so concurrent trace messages don't
        trigger a second analysis while the first is still running.

        Args:
            envelope: The trace envelope that triggered this analysis (used as
                parent for the outgoing event envelope).
            skill_name: The skill to analyse.
            trigger_corr_id: Correlation ID of the trace that triggered this.
            redis_conn: Active Redis connection.
        """
        # Set cooldown immediately to prevent double-trigger.
        cooldown_key = f"relais:skill:last_improved:{skill_name}"
        await redis_conn.set(
            cooldown_key, "1", ex=self._config.min_improvement_interval_seconds
        )

        logger.info(
            "Triggering LLM analysis for skill '%s' (trigger=%s)",
            skill_name,
            trigger_corr_id,
        )
        await redis_conn.xadd(STREAM_LOGS, {
            "level": "INFO",
            "brick": "forgeron",
            "correlation_id": trigger_corr_id,
            "sender_id": envelope.sender_id,
            "message": f"LLM analysis triggered for skill '{skill_name}'",
        })

        # Step 2: SkillAnalyzer — imported lazily so the brick starts even if
        # the analyzer module is not yet implemented.
        try:
            from forgeron.analyzer import SkillAnalyzer  # noqa: PLC0415
            from forgeron.patcher import SkillPatcher  # noqa: PLC0415
        except ImportError as exc:
            logger.warning(
                "SkillAnalyzer/SkillPatcher not yet available (%s) — "
                "analysis skipped.",
                exc,
            )
            return

        skill_path = self._resolve_skill_path(skill_name)
        if skill_path is None:
            logger.warning(
                "Could not resolve path for skill '%s', skipping analysis.",
                skill_name,
            )
            return

        try:
            skill_content = skill_path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "Could not read skill file '%s': %s", skill_path, exc
            )
            return

        traces = await self._trace_store.get_traces(
            skill_name, limit=self._config.min_traces_before_analysis
        )
        error_rate = await self._trace_store.error_rate(
            skill_name, window=self._config.min_traces_before_analysis
        )

        if self._llm_profile is None:
            raise RuntimeError("llm_profile not loaded — _load() must succeed before _trigger_analysis()")
        analyzer = SkillAnalyzer(profile=self._llm_profile)
        try:
            proposal = await analyzer.analyze(skill_name, skill_content, traces)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "LLM analysis failed for skill '%s': %s", skill_name, exc
            )
            return

        from forgeron.models import SkillPatch  # noqa: PLC0415

        patch = SkillPatch(
            skill_name=skill_name,
            original_content=skill_content,
            patched_content=proposal.patched_content,
            diff=proposal.diff,
            rationale=proposal.rationale,
            trigger_correlation_id=trigger_corr_id,
            pre_patch_error_rate=error_rate,
        )
        await self._patch_store.save(patch)

        patcher = SkillPatcher(skills_dir=self._config.skills_dir)
        try:
            patcher.apply(skill_path, patch)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Failed to apply patch %s for skill '%s': %s",
                patch.id,
                skill_name,
                exc,
            )
            return

        await self._patch_store.mark_applied(patch)

        # Notify Archiviste and any listeners via a proper Envelope.
        # Use a minimal envelope — do NOT inherit the full upstream context (portail,
        # atelier, sentinelle) which would leak user records and internal state.
        event_env = Envelope(
            content=f"skill.patch_applied:{skill_name}",
            sender_id=envelope.sender_id,
            channel=envelope.channel,
            session_id=envelope.session_id,
            correlation_id=envelope.correlation_id,
        )
        event_env.action = ACTION_SKILL_PATCH_APPLIED
        event_env.add_trace("forgeron", "patch_applied")
        ensure_ctx(event_env, CTX_FORGERON).update({
            "skill_name": skill_name,
            "patch_id": patch.id,
            "pre_error_rate": error_rate,
            "diff_preview": patch.diff[:500],
        })
        await redis_conn.xadd(STREAM_EVENTS_SYSTEM, {"payload": event_env.to_json()})
        logger.info(
            "Patch %s applied for skill '%s' (pre_error_rate=%.0f%%)",
            patch.id,
            skill_name,
            error_rate * 100,
        )
        await redis_conn.xadd(STREAM_LOGS, {
            "level": "INFO",
            "brick": "forgeron",
            "correlation_id": envelope.correlation_id,
            "sender_id": envelope.sender_id,
            "message": (
                f"Patch '{patch.id[:8]}' applied for skill '{skill_name}' "
                f"(pre_error_rate={error_rate:.0%})"
            ),
        })

        if self._config.notify_user_on_patch:
            await self._notify_user(
                channel=envelope.channel,
                sender_id=envelope.sender_id,
                session_id=envelope.session_id,
                correlation_id=envelope.correlation_id,
                message=(
                    f"[Forgeron] Skill `{skill_name}` automatically improved "
                    f"(error rate: {error_rate:.0%} → patch `{patch.id[:8]}`)"
                ),
                redis_conn=redis_conn,
            )

    async def _maybe_validate_patch(
        self,
        envelope: Envelope,
        skill_name: str,
        patch: Any,
        redis_conn: Any,
    ) -> None:
        """Check for post-patch regression and roll back if needed.

        Args:
            envelope: The trace envelope that triggered this check (used as
                parent for the outgoing event envelope if rollback occurs).
            skill_name: Skill being monitored.
            patch: The active ``SkillPatch`` record.
            redis_conn: Active Redis connection.
        """
        try:
            from forgeron.validator import SkillValidator  # noqa: PLC0415
        except ImportError:
            return

        skill_path = self._resolve_skill_path(skill_name)
        if skill_path is None:
            return

        validator = SkillValidator(
            trace_store=self._trace_store,
            patch_store=self._patch_store,
        )
        rolled_back = await validator.check_and_rollback_if_needed(
            skill_name=skill_name,
            skill_path=skill_path,
            patch=patch,
            config=self._config,
        )
        if rolled_back:
            event_env = Envelope(
                content=f"skill.patch_rolled_back:{skill_name}",
                sender_id=envelope.sender_id,
                channel=envelope.channel,
                session_id=envelope.session_id,
                correlation_id=envelope.correlation_id,
            )
            event_env.action = ACTION_SKILL_PATCH_ROLLED_BACK
            event_env.add_trace("forgeron", "patch_rolled_back")
            ensure_ctx(event_env, CTX_FORGERON).update({
                "skill_name": skill_name,
                "patch_id": patch.id,
            })
            await redis_conn.xadd(STREAM_EVENTS_SYSTEM, {"payload": event_env.to_json()})
            await redis_conn.xadd(STREAM_LOGS, {
                "level": "WARNING",
                "brick": "forgeron",
                "correlation_id": envelope.correlation_id,
                "sender_id": envelope.sender_id,
                "message": (
                    f"Patch '{patch.id[:8]}' rolled back for skill '{skill_name}' "
                    f"(regression detected)"
                ),
            })

            if self._config.notify_user_on_patch:
                await self._notify_user(
                    channel=envelope.channel,
                    sender_id=envelope.sender_id,
                    session_id=envelope.session_id,
                    correlation_id=envelope.correlation_id,
                    message=(
                        f"[Forgeron] Patch `{patch.id[:8]}` on skill `{skill_name}` "
                        f"rolled back (regression detected, reverted to previous version)."
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
            intent_label,
            min_sessions=self._config.min_sessions_for_creation,
            redis_conn=redis_conn,
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
        logger.info("Skill '%s' created at %s", result.skill_name, result.skill_path)
        await redis_conn.xadd(STREAM_LOGS, {
            "level": "INFO",
            "brick": "forgeron",
            "correlation_id": correlation_id,
            "sender_id": sender_id,
            "message": (
                f"Skill '{result.skill_name}' created for intent '{intent_label}' "
                f"({len(representative)} contributing sessions)"
            ),
        })

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
        await redis_conn.xadd(STREAM_LOGS, {
            "level": "INFO",
            "brick": "forgeron",
            "correlation_id": correlation_id,
            "sender_id": sender_id,
            "message": f"Notification sent to {channel}/{sender_id}: {message[:60]}",
        })
        logger.info(
            "Notification sent to %s/%s: %s", channel, sender_id, message[:60]
        )

    def _resolve_skill_path(self, skill_name: str) -> Path | None:
        """Resolve the SKILL.md path for a given skill directory name.

        Args:
            skill_name: Directory name of the skill (e.g. ``"mail-agent"``).

        Returns:
            ``Path`` to ``{skills_dir}/{skill_name}/SKILL.md`` if it exists,
            otherwise ``None``.
        """
        if self._config.skills_dir is None:
            logger.warning("skills_dir is None — cannot resolve skill path.")
            return None

        candidate = self._config.skills_dir / skill_name / "SKILL.md"
        if not candidate.exists():
            logger.debug("Skill file not found: %s", candidate)
            return None
        return candidate


if __name__ == "__main__":
    from common.init import initialize_user_dir

    initialize_user_dir()
    forgeron = Forgeron()
    try:
        asyncio.run(forgeron.start())
    except KeyboardInterrupt:
        pass
