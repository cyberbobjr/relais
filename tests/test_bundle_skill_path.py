"""Tests for bundle skill path propagation (skill_paths dict through Atelier → Forgeron).

Feature: pass skill_paths: dict[str, str] from Atelier trace through to Forgeron
so it can correct bundle-installed skills whose SKILL.md lives under
~/.relais/bundles/<name>/skills/<name>/ rather than the default skills_dir.
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _load_write_skill_module():
    """Load write_skill.py via importlib (directory name has hyphen, not importable normally)."""
    module_path = (
        Path(__file__).parent.parent
        / "atelier" / "subagents" / "skill-designer" / "tools" / "write_skill.py"
    )
    spec = importlib.util.spec_from_file_location("_write_skill_impl", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load write_skill from {module_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_ws_mod = _load_write_skill_module()


# ---------------------------------------------------------------------------
# 1. SkillTraceCtx — skill_paths field
# ---------------------------------------------------------------------------

class TestSkillTraceCtx:
    """SkillTraceCtx must expose the skill_paths key."""

    def test_skill_paths_key_exists_in_typedef(self) -> None:
        from common.contexts import SkillTraceCtx
        assert "skill_paths" in SkillTraceCtx.__annotations__, (
            "SkillTraceCtx must declare skill_paths: dict[str, str]"
        )

    def test_skill_trace_ctx_instantiation_with_skill_paths(self) -> None:
        from common.contexts import SkillTraceCtx
        ctx: SkillTraceCtx = {
            "skill_names": ["my-skill"],
            "tool_call_count": 3,
            "tool_error_count": 1,
            "messages_raw": [],
            "skill_paths": {"my-skill": "/home/user/.relais/bundles/my-bundle/skills/my-skill"},
        }
        assert ctx["skill_paths"]["my-skill"].endswith("my-skill")


# ---------------------------------------------------------------------------
# 2. SkillTrace model — skill_path column
# ---------------------------------------------------------------------------

class TestSkillTraceModel:
    """SkillTrace SQLModel must include skill_path nullable column."""

    def test_skill_path_field_exists(self) -> None:
        from forgeron.models import SkillTrace
        trace = SkillTrace(
            skill_name="my-skill",
            correlation_id="corr-abc",
            tool_call_count=5,
            tool_error_count=2,
        )
        assert hasattr(trace, "skill_path"), "SkillTrace must have skill_path attribute"
        assert trace.skill_path is None, "skill_path must default to None"

    def test_skill_path_can_be_set(self) -> None:
        from forgeron.models import SkillTrace
        bundle_path = "/home/.relais/bundles/my-bundle/skills/my-skill"
        trace = SkillTrace(
            skill_name="my-skill",
            correlation_id="corr-abc",
            tool_call_count=1,
            tool_error_count=0,
            skill_path=bundle_path,
        )
        assert trace.skill_path == bundle_path


# ---------------------------------------------------------------------------
# 3. SkillEditor.edit — skill_path override
# ---------------------------------------------------------------------------

class TestSkillEditorEditWithSkillPath:
    """SkillEditor.edit() must accept an explicit skill_path override."""

    @pytest.mark.asyncio
    async def test_edit_uses_explicit_skill_path_over_skills_dir(
        self, tmp_path: Path
    ) -> None:
        """When skill_path is provided, it bypasses skills_dir resolution."""
        from forgeron.skill_editor import SkillEditor, SkillEditResult
        from forgeron.config import ForgeonConfig

        skill_dir = tmp_path / "my-bundle" / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("# My Skill\nDoes stuff.", encoding="utf-8")

        different_dir = tmp_path / "default-skills"
        different_dir.mkdir()

        config = ForgeonConfig(edit_min_tool_errors=1, edit_cooldown_seconds=0)
        redis_mock = AsyncMock()
        redis_mock.ttl = AsyncMock(return_value=0)
        redis_mock.setex = AsyncMock()

        profile_mock = MagicMock()
        editor = SkillEditor(profile=profile_mock, skills_dir=different_dir)

        result = SkillEditResult(
            updated_skill="# My Skill\nImproved content.",
            changed=True,
            reason="improved",
        )
        messages_with_skill = [
            {"type": "human", "content": "use my-skill"},
            {
                "type": "ai",
                "content": "",
                "tool_calls": [
                    {"id": "tc-1", "name": "read_skill", "args": {"skill_name": "my-skill"}}
                ],
            },
            {"type": "tool", "tool_call_id": "tc-1", "content": "my-skill content"},
        ]
        with patch.object(editor, "_call_llm", new=AsyncMock(return_value=result)):
            edited = await editor.edit(
                skill_name="my-skill",
                messages_raw=messages_with_skill,
                config=config,
                redis_conn=redis_mock,
                skill_path=skill_dir,
            )

        assert edited is True, (
            "edit() must succeed when an explicit skill_path is provided "
            "even if the skill does not exist under skills_dir"
        )

    @pytest.mark.asyncio
    async def test_edit_without_skill_path_falls_back_to_skills_dir(
        self, tmp_path: Path
    ) -> None:
        """Without skill_path, the original skills_dir lookup is used."""
        from forgeron.skill_editor import SkillEditor, SkillEditResult
        from forgeron.config import ForgeonConfig

        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# My Skill\nOriginal.", encoding="utf-8")

        config = ForgeonConfig(edit_min_tool_errors=1, edit_cooldown_seconds=0)
        redis_mock = AsyncMock()
        redis_mock.ttl = AsyncMock(return_value=0)
        redis_mock.setex = AsyncMock()

        editor = SkillEditor(profile=MagicMock(), skills_dir=tmp_path)
        result = SkillEditResult(
            updated_skill="# My Skill\nUpdated.",
            changed=True,
            reason="improved",
        )
        messages_with_skill = [
            {"type": "human", "content": "use my-skill"},
            {
                "type": "ai",
                "content": "",
                "tool_calls": [
                    {"id": "tc-1", "name": "read_skill", "args": {"skill_name": "my-skill"}}
                ],
            },
            {"type": "tool", "tool_call_id": "tc-1", "content": "my-skill content"},
        ]
        with patch.object(editor, "_call_llm", new=AsyncMock(return_value=result)):
            edited = await editor.edit(
                skill_name="my-skill",
                messages_raw=messages_with_skill,
                config=config,
                redis_conn=redis_mock,
            )
        assert edited is True


# ---------------------------------------------------------------------------
# 4. WriteSkillTool — skill_path override
# ---------------------------------------------------------------------------

class TestWriteSkillToolWithSkillPath:
    """WriteSkillTool._run() must accept an explicit skill_path to write to."""

    def test_run_uses_explicit_skill_path(self, tmp_path: Path) -> None:
        tool = _ws_mod.WriteSkillTool()
        bundles_root = tmp_path / "bundles"
        # Real bundle layout: <bundles_root>/<bundle-name>/skills/<skill-name>
        bundle_skill_dir = bundles_root / "my-bundle" / "skills" / "my-skill"
        bundle_skill_dir.mkdir(parents=True)

        with patch.object(_ws_mod, "resolve_bundles_dir", return_value=bundles_root), \
             patch.object(_ws_mod, "resolve_skills_dir", return_value=tmp_path / "skills"):
            result = tool._run(
                skill_name="my-skill",
                content="# My Skill\nBundle content.",
                overwrite=True,
                skill_path=str(bundle_skill_dir),
            )

        assert "ERROR" not in result, f"Expected success but got: {result}"
        expected = bundle_skill_dir / "SKILL.md"
        assert expected.exists(), "SKILL.md should be written at the explicit skill_path"
        assert expected.read_text(encoding="utf-8") == "# My Skill\nBundle content."

    def test_run_without_skill_path_uses_skills_dir(self, tmp_path: Path) -> None:
        tool = _ws_mod.WriteSkillTool()
        with patch.object(_ws_mod, "resolve_skills_dir", return_value=tmp_path):
            result = tool._run(
                skill_name="my-skill",
                content="# Default path skill",
            )
        assert "ERROR" not in result
        assert (tmp_path / "my-skill" / "SKILL.md").exists()


# ---------------------------------------------------------------------------
# 5. Forgeron._process_trace — reads skill_paths from context
# ---------------------------------------------------------------------------

class TestForgeronProcessTraceSkillPaths:
    """_process_trace must extract skill_paths from the trace context and pass
    the per-skill path to SkillEditor.edit() as skill_path."""

    @pytest.mark.asyncio
    async def test_process_trace_passes_skill_path_to_skill_editor(
        self, tmp_path: Path
    ) -> None:
        """When skill_paths is present in the trace context, Forgeron must
        pass the matching Path to SkillEditor.edit()."""
        from forgeron.main import Forgeron
        from common.contexts import CTX_SKILL_TRACE
        from common.envelope import Envelope

        bundle_skill_dir = tmp_path / "bundle" / "skills" / "my-skill"
        bundle_skill_dir.mkdir(parents=True)
        (bundle_skill_dir / "SKILL.md").write_text("# My Skill", encoding="utf-8")

        forgeron = _make_forgeron(tmp_path)
        forgeron._config.edit_mode = True
        forgeron._config.edit_min_tool_errors = 1

        envelope = _make_trace_envelope(
            skill_names=["my-skill"],
            tool_call_count=3,
            tool_error_count=2,
            skill_paths={"my-skill": str(bundle_skill_dir)},
        )

        edit_calls: list[dict] = []

        async def mock_edit(**kwargs) -> bool:
            edit_calls.append(kwargs)
            return True

        redis_mock = AsyncMock()
        redis_mock.ttl = AsyncMock(return_value=0)
        redis_mock.setex = AsyncMock()

        with patch("forgeron.main.SkillEditor") as MockEditor:
            editor_instance = MagicMock()
            editor_instance.edit = AsyncMock(side_effect=mock_edit)
            MockEditor.return_value = editor_instance

            await forgeron._process_trace(envelope, redis_mock)

        assert edit_calls, "SkillEditor.edit() must be called"
        call_kwargs = edit_calls[0]
        assert "skill_path" in call_kwargs, (
            "_process_trace must pass skill_path kwarg to SkillEditor.edit()"
        )
        assert call_kwargs["skill_path"] == bundle_skill_dir

    @pytest.mark.asyncio
    async def test_process_trace_no_skill_paths_passes_none(self, tmp_path: Path) -> None:
        """When skill_paths is absent, skill_path=None is passed to SkillEditor.edit()."""
        from forgeron.main import Forgeron
        from common.contexts import CTX_SKILL_TRACE
        from common.envelope import Envelope

        forgeron = _make_forgeron(tmp_path)
        forgeron._config.edit_mode = True
        forgeron._config.edit_min_tool_errors = 1

        envelope = _make_trace_envelope(
            skill_names=["my-skill"],
            tool_call_count=2,
            tool_error_count=1,
            skill_paths=None,
        )

        edit_calls: list[dict] = []

        async def mock_edit(**kwargs) -> bool:
            edit_calls.append(kwargs)
            return False

        redis_mock = AsyncMock()
        redis_mock.ttl = AsyncMock(return_value=0)
        redis_mock.setex = AsyncMock()

        with patch("forgeron.main.SkillEditor") as MockEditor:
            editor_instance = MagicMock()
            editor_instance.edit = AsyncMock(side_effect=mock_edit)
            MockEditor.return_value = editor_instance

            await forgeron._process_trace(envelope, redis_mock)

        assert edit_calls
        call_kwargs = edit_calls[0]
        assert call_kwargs.get("skill_path") is None


# ---------------------------------------------------------------------------
# 6. Forgeron._trigger_skill_design — injects skill_path into CTX_FORGERON
# ---------------------------------------------------------------------------

class TestForgeronTriggerSkillDesignSkillPath:
    """_trigger_skill_design must inject skill_path into CTX_FORGERON on the
    task envelope when a skill_path is provided."""

    @pytest.mark.asyncio
    async def test_trigger_skill_design_injects_skill_path(self, tmp_path: Path) -> None:
        from forgeron.main import Forgeron
        from common.contexts import CTX_FORGERON
        from common.envelope import Envelope

        forgeron = _make_forgeron(tmp_path)
        forgeron._config.history_read_timeout_seconds = 1

        envelope = Envelope(
            content="",
            sender_id="user",
            channel="discord",
            session_id="sess-1",
            correlation_id="corr-1",
        )

        history = [[{"type": "human", "content": "hello"}]]
        redis_mock = AsyncMock()
        redis_mock.xadd = AsyncMock()
        redis_mock.brpop = AsyncMock(
            return_value=(b"relais:memory:response:corr-1", json.dumps(history).encode())
        )

        published_tasks: list = []

        async def mock_xadd(stream: str, data: dict) -> None:
            if stream.endswith("tasks"):
                payload = json.loads(data["payload"])
                published_tasks.append(payload)

        redis_mock.xadd = AsyncMock(side_effect=mock_xadd)

        bundle_skill_dir = str(tmp_path / "bundle" / "skills" / "my-skill")

        await forgeron._trigger_skill_design(
            envelope=envelope,
            channel="discord",
            sender_id="user",
            corrected_behavior="always use --flag",
            skill_name_hint="my-skill",
            redis_conn=redis_mock,
            skill_path=bundle_skill_dir,
        )

        assert published_tasks, "A task must be published to skill-designer"
        task_ctx = published_tasks[-1]["context"].get(CTX_FORGERON, {})
        assert task_ctx.get("skill_path") == bundle_skill_dir, (
            "_trigger_skill_design must inject skill_path into CTX_FORGERON"
        )

    @pytest.mark.asyncio
    async def test_trigger_skill_design_no_skill_path_omits_key(self, tmp_path: Path) -> None:
        from forgeron.main import Forgeron
        from common.contexts import CTX_FORGERON
        from common.envelope import Envelope

        forgeron = _make_forgeron(tmp_path)
        forgeron._config.history_read_timeout_seconds = 1

        envelope = Envelope(
            content="", sender_id="user", channel="discord",
            session_id="sess-2", correlation_id="corr-2",
        )

        history = [[{"type": "human", "content": "test"}]]
        redis_mock = AsyncMock()
        published_tasks: list = []

        async def mock_xadd(stream: str, data: dict) -> None:
            if "tasks" in stream:
                published_tasks.append(json.loads(data["payload"]))

        redis_mock.xadd = AsyncMock(side_effect=mock_xadd)
        redis_mock.brpop = AsyncMock(
            return_value=(b"key", json.dumps(history).encode())
        )

        await forgeron._trigger_skill_design(
            envelope=envelope,
            channel="discord",
            sender_id="user",
            corrected_behavior="fix",
            skill_name_hint=None,
            redis_conn=redis_mock,
        )

        assert published_tasks
        task_ctx = published_tasks[-1]["context"].get(CTX_FORGERON, {})
        assert "skill_path" not in task_ctx


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_forgeron(tmp_path: Path) -> "Forgeron":  # type: ignore[name-defined]
    """Instantiate Forgeron with mocked stores and config pointing to tmp_path."""
    from forgeron.main import Forgeron
    from forgeron.config import ForgeonConfig

    with patch("forgeron.main.load_forgeron_config", return_value=ForgeonConfig(skills_dir=tmp_path)), \
         patch("forgeron.main.load_profiles", return_value={}), \
         patch("forgeron.main.resolve_profile", return_value=MagicMock(model="test-model")), \
         patch("forgeron.main.resolve_storage_dir", return_value=tmp_path):
        forgeron = Forgeron.__new__(Forgeron)
        forgeron._config = ForgeonConfig(skills_dir=tmp_path)
        forgeron._llm_profile = MagicMock(model="test-model")
        forgeron._edit_profile = MagicMock(model="test-model")
        forgeron._skill_call_counts = {}
        forgeron._last_had_errors = {}
        forgeron._trace_store = MagicMock()
        forgeron._trace_store.add_trace = AsyncMock()
        forgeron._session_store = MagicMock()
    return forgeron


def _make_trace_envelope(
    skill_names: list[str],
    tool_call_count: int,
    tool_error_count: int,
    skill_paths: dict[str, str] | None,
) -> "Envelope":  # type: ignore[name-defined]
    from common.envelope import Envelope
    from common.contexts import CTX_SKILL_TRACE

    ctx: dict = {
        CTX_SKILL_TRACE: {
            "skill_names": skill_names,
            "tool_call_count": tool_call_count,
            "tool_error_count": tool_error_count,
            "messages_raw": [],
        }
    }
    if skill_paths is not None:
        ctx[CTX_SKILL_TRACE]["skill_paths"] = skill_paths

    return Envelope(
        content="",
        sender_id="atelier:test",
        channel="internal",
        session_id="sess-test",
        correlation_id="corr-test",
        context=ctx,
    )
