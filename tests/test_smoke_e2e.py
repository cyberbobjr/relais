"""Smoke test E2E: Discord → Portail → Sentinelle → Atelier → Souvenir → SQLite."""
import pytest
import pytest_asyncio
import fakeredis.aioredis as fake_redis_lib
from itertools import chain, repeat
from unittest.mock import AsyncMock, MagicMock, patch

from common.envelope import Envelope
from common.shutdown import GracefulShutdown
from portail.main import Portail
from sentinelle.main import Sentinelle
from atelier.main import Atelier
from souvenir.main import Souvenir
from souvenir.long_term_store import LongTermStore
from souvenir.context_store import ContextStore
from souvenir.models import ArchivedMessage

pytestmark = pytest.mark.integration


def _one_shot() -> MagicMock:
    """Return a GracefulShutdown mock that allows exactly one loop iteration.

    Returns False on first call to is_stopping() so the brick enters the loop body,
    then True on all subsequent calls so the brick exits after one xreadgroup batch.

    Returns:
        A MagicMock configured with is_stopping side_effect for one-shot execution.
    """
    m = MagicMock(spec=GracefulShutdown)
    m.is_stopping.side_effect = chain([False], repeat(True))
    return m


@pytest_asyncio.fixture
async def redis_conn():
    """Provide an in-memory FakeRedis connection for the smoke test.

    Yields:
        A FakeRedis async connection with decode_responses=True.
    """
    r = fake_redis_lib.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.mark.asyncio
async def test_discord_message_full_pipeline(redis_conn, tmp_path):
    """Verify the full pipeline: Discord → Portail → Sentinelle → Atelier → Souvenir → SQLite.

    Sends a simulated Discord message and asserts that:
    - Portail forwards it to relais:security
    - Sentinelle forwards it to relais:tasks
    - Atelier (with mocked SDK) publishes a response to relais:messages:outgoing:discord
    - Souvenir archives the response in SQLite

    Args:
        redis_conn: FakeRedis async connection (from fixture).
        tmp_path: pytest-provided temporary directory for SQLite.
    """
    # ── ARRANGE ──────────────────────────────────────────────────────────────
    # Pre-create all consumer groups with id='0' so that messages published by
    # upstream bricks are still delivered when the downstream brick starts.
    # Default id='$' would miss messages that already exist in the stream at
    # group-creation time.
    for stream, group in [
        ("relais:messages:incoming", "portail_group"),
        ("relais:security", "sentinelle_group"),
        ("relais:tasks", "atelier_group"),
        ("relais:messages:outgoing:discord", "souvenir_outgoing_group"),
    ]:
        await redis_conn.xgroup_create(stream, group, id="0", mkstream=True)

    initial = Envelope(
        content="Bonjour RELAIS !",
        sender_id="discord:111222333",
        channel="discord",
        session_id="sess-smoke-e2e-001",
        correlation_id="corr-smoke-e2e-001",
    )
    await redis_conn.xadd("relais:messages:incoming", {"payload": initial.to_json()})

    # ── STEP 1: Portail ───────────────────────────────────────────────────────
    portail = Portail()
    await portail._process_stream(redis_conn, shutdown=_one_shot())

    security_msgs = await redis_conn.xrange("relais:security", "-", "+")
    assert len(security_msgs) == 1, "Portail doit publier 1 message dans relais:security"

    # ── STEP 2: Sentinelle ────────────────────────────────────────────────────
    sentinelle = Sentinelle()
    await sentinelle._process_stream(redis_conn, shutdown=_one_shot())

    tasks_msgs = await redis_conn.xrange("relais:tasks", "-", "+")
    assert len(tasks_msgs) == 1, "Sentinelle doit publier 1 message dans relais:tasks"

    # ── STEP 3: Atelier (mocked SDK) ──────────────────────────────────────────
    mock_profile = MagicMock()
    mock_profile.model = "test-model"
    mock_profile.max_turns = 20
    mock_profile.name = "default"
    mock_profile.allowed_tools = None
    mock_profile.guardrails = ()

    with (
        patch("atelier.main.load_profiles", return_value={"default": mock_profile}),
        patch("atelier.main.load_for_sdk", return_value={}),
        patch("atelier.main.resolve_profile", return_value=mock_profile),
        patch("atelier.main.assemble_system_prompt", return_value="[SOUL MOCK]"),
        patch("atelier.main.SDKExecutor") as MockSDK,
        patch(
            "atelier.main.Atelier._fetch_context",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        mock_executor = AsyncMock()
        mock_executor.execute.return_value = "Je suis RELAIS, comment puis-je t'aider ?"
        MockSDK.return_value = mock_executor

        atelier = Atelier()
        await atelier._process_stream(redis_conn, shutdown=_one_shot())

    outgoing_msgs = await redis_conn.xrange("relais:messages:outgoing:discord", "-", "+")
    assert len(outgoing_msgs) == 1, "Atelier doit publier 1 réponse dans relais:messages:outgoing:discord"

    response_env = Envelope.from_json(outgoing_msgs[0][1]["payload"])
    assert response_env.content == "Je suis RELAIS, comment puis-je t'aider ?"
    assert response_env.channel == "discord"
    assert response_env.session_id == initial.session_id

    # ── STEP 4: Souvenir → SQLite ─────────────────────────────────────────────
    db_path = tmp_path / "smoke_memory.db"

    souvenir = Souvenir()
    souvenir._long_term = LongTermStore(db_path=db_path)
    await souvenir._long_term._create_tables()

    context_store = ContextStore(redis_conn)

    with patch.object(
        souvenir._extractor, "extract", new_callable=AsyncMock, return_value=[]
    ):
        await souvenir._process_outgoing_streams(
            redis_conn, context_store, shutdown=_one_shot()
        )

    # ── ASSERT: archivage SQLite ───────────────────────────────────────────────
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from sqlmodel.ext.asyncio.session import AsyncSession
    from sqlmodel import select

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as session:
        archived = (await session.exec(select(ArchivedMessage))).all()

    await engine.dispose()

    assert len(archived) >= 1, "Souvenir doit archiver au moins 1 message dans SQLite"
    assert any(
        m.session_id == "sess-smoke-e2e-001" for m in archived
    ), "L'archive SQLite doit contenir le session_id du message initial"
