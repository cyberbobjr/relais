# Phase 1 — Tests TDD (RED first)

**Fichiers à créer:**
- `tests/test_commandant.py`
- `tests/test_portail_dnd.py`
- `tests/test_souvenir_clear.py`

**Règle:** Écrire tous les tests AVANT tout code d'implémentation. Tous doivent être RED (ImportError ou AssertionError).

---

## `tests/test_commandant.py`

### Fixtures

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from common.envelope import Envelope

@pytest.fixture
def sample_envelope() -> Envelope:
    """Envelope typique d'un message /clear venant de Discord."""
    return Envelope(
        content="/clear",
        sender_id="discord:123456",
        channel="discord",
        session_id="session_abc",
        correlation_id="corr_001",
    )

@pytest.fixture
def mock_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock()
    redis.delete = AsyncMock()
    redis.xadd = AsyncMock()
    redis.xreadgroup = AsyncMock(return_value=[])
    redis.xack = AsyncMock()
    redis.xgroup_create = AsyncMock()
    return redis
```

---

### Tests `commandant/command_parser.py`

Module: `from commandant.command_parser import parse_command, CommandResult`

```python
@pytest.mark.unit
def test_parse_clear_command():
    """'/clear' retourne CommandResult(command='clear', args=[])."""
    result = parse_command("/clear")
    assert result is not None
    assert result.command == "clear"
    assert result.args == []

@pytest.mark.unit
def test_parse_dnd_command():
    result = parse_command("/dnd")
    assert result is not None
    assert result.command == "dnd"

@pytest.mark.unit
def test_parse_brb_command():
    result = parse_command("/brb")
    assert result is not None
    assert result.command == "brb"

@pytest.mark.unit
def test_parse_unknown_command_returns_none():
    """Commande inconnue → None (pas de réponse, pas d'erreur)."""
    result = parse_command("/foo")
    assert result is None

@pytest.mark.unit
def test_parse_plain_message_returns_none():
    """Message normal (pas de slash) → None."""
    result = parse_command("bonjour")
    assert result is None

@pytest.mark.unit
def test_parse_empty_string_returns_none():
    result = parse_command("")
    assert result is None

@pytest.mark.unit
def test_parse_slash_only_returns_none():
    """'/' seul sans nom de commande → None."""
    result = parse_command("/")
    assert result is None

@pytest.mark.unit
def test_parse_command_case_insensitive():
    """/CLEAR et /Clear doivent être reconnus."""
    assert parse_command("/CLEAR") is not None
    assert parse_command("/Clear") is not None

@pytest.mark.unit
def test_parse_command_strips_whitespace():
    """'  /clear  ' → reconnu (strip avant parsing)."""
    result = parse_command("  /clear  ")
    assert result is not None
    assert result.command == "clear"

@pytest.mark.unit
def test_command_result_is_dataclass():
    """`CommandResult` est un dataclass avec .command et .args."""
    result = parse_command("/clear")
    assert hasattr(result, "command")
    assert hasattr(result, "args")
```

---

### Tests `commandant/handlers.py`

Module: `from commandant.handlers import handle_clear, handle_dnd, handle_brb`

```python
@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_clear_publishes_to_memory_request(mock_redis, sample_envelope):
    """handle_clear envoie action='clear' sur relais:memory:request."""
    await handle_clear(sample_envelope, mock_redis)
    
    # Vérifie qu'un XADD a été fait sur relais:memory:request
    calls = [str(c) for c in mock_redis.xadd.call_args_list]
    assert any("relais:memory:request" in c for c in calls)

@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_clear_publishes_confirmation(mock_redis, sample_envelope):
    """handle_clear publie un message de confirmation sur relais:messages:outgoing:discord."""
    await handle_clear(sample_envelope, mock_redis)
    
    expected_stream = f"relais:messages:outgoing:{sample_envelope.channel}"
    calls = [str(c) for c in mock_redis.xadd.call_args_list]
    assert any(expected_stream in c for c in calls)

@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_dnd_sets_redis_key(mock_redis, sample_envelope):
    """handle_dnd fait SET relais:state:dnd 1 (sans TTL)."""
    await handle_dnd(sample_envelope, mock_redis)
    
    mock_redis.set.assert_called_once_with("relais:state:dnd", "1")

@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_dnd_publishes_confirmation(mock_redis, sample_envelope):
    """handle_dnd publie une confirmation sur le canal."""
    sample_envelope_dnd = Envelope(
        content="/dnd", sender_id="discord:123456",
        channel="discord", session_id="session_abc",
    )
    await handle_dnd(sample_envelope_dnd, mock_redis)
    
    expected_stream = "relais:messages:outgoing:discord"
    calls = [str(c) for c in mock_redis.xadd.call_args_list]
    assert any(expected_stream in c for c in calls)

@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_brb_deletes_redis_key(mock_redis, sample_envelope):
    """handle_brb fait DEL relais:state:dnd."""
    sample_envelope_brb = Envelope(
        content="/brb", sender_id="discord:123456",
        channel="discord", session_id="session_abc",
    )
    await handle_brb(sample_envelope_brb, mock_redis)
    
    mock_redis.delete.assert_called_once_with("relais:state:dnd")

@pytest.mark.asyncio
@pytest.mark.unit
async def test_handle_brb_publishes_confirmation(mock_redis, sample_envelope):
    """handle_brb publie une confirmation sur le canal."""
    sample_envelope_brb = Envelope(
        content="/brb", sender_id="discord:123456",
        channel="discord", session_id="session_abc",
    )
    await handle_brb(sample_envelope_brb, mock_redis)
    
    expected_stream = "relais:messages:outgoing:discord"
    calls = [str(c) for c in mock_redis.xadd.call_args_list]
    assert any(expected_stream in c for c in calls)
```

---

### Tests `commandant/main.py` (boucle consumer)

Module: `from commandant.main import Commandant`

```python
@pytest.mark.asyncio
@pytest.mark.unit
async def test_commandant_acks_non_command_messages(mock_redis):
    """Messages non-commandes → ACK sans traitement (pas de xadd)."""
    envelope = Envelope(
        content="bonjour", sender_id="discord:999",
        channel="discord", session_id="s1",
    )
    import json
    mock_redis.xreadgroup = AsyncMock(return_value=[
        (b"relais:messages:incoming", [(b"1-1", {b"payload": json.dumps({
            "content": "bonjour",
            "sender_id": "discord:999",
            "channel": "discord",
            "session_id": "s1",
            "correlation_id": "c1",
            "timestamp": 0.0,
            "metadata": {},
            "media_refs": [],
        }).encode()})])
    ])
    
    commandant = Commandant()
    # Une itération seulement
    from common.shutdown import GracefulShutdown
    shutdown = MagicMock()
    shutdown.is_stopping.side_effect = [False, True]  # un tour puis stop
    
    await commandant._process_stream(mock_redis, shutdown=shutdown)
    
    mock_redis.xack.assert_called_once()
    # Pas de xadd vers outgoing
    outgoing_calls = [c for c in mock_redis.xadd.call_args_list
                      if "outgoing" in str(c)]
    assert len(outgoing_calls) == 0

@pytest.mark.asyncio
@pytest.mark.unit
async def test_commandant_acks_command_messages(mock_redis):
    """Messages-commandes → ACK + xadd confirmation."""
    import json
    mock_redis.xreadgroup = AsyncMock(return_value=[
        (b"relais:messages:incoming", [(b"1-1", {b"payload": json.dumps({
            "content": "/clear",
            "sender_id": "discord:999",
            "channel": "discord",
            "session_id": "s1",
            "correlation_id": "c1",
            "timestamp": 0.0,
            "metadata": {},
            "media_refs": [],
        }).encode()})])
    ])
    
    commandant = Commandant()
    from common.shutdown import GracefulShutdown
    shutdown = MagicMock()
    shutdown.is_stopping.side_effect = [False, True]
    
    await commandant._process_stream(mock_redis, shutdown=shutdown)
    
    mock_redis.xack.assert_called_once()
    # Au moins un xadd vers memory:request ET outgoing
    all_xadd_streams = [str(c) for c in mock_redis.xadd.call_args_list]
    assert any("relais:memory:request" in s for s in all_xadd_streams)
    assert any("relais:messages:outgoing:discord" in s for s in all_xadd_streams)
```

---

## `tests/test_portail_dnd.py`

```python
import pytest
import json
from unittest.mock import AsyncMock, MagicMock
from common.envelope import Envelope
from portail.main import Portail


@pytest.fixture
def mock_redis_no_dnd() -> AsyncMock:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)  # DND inactif
    redis.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis.xreadgroup = AsyncMock(return_value=[])
    redis.xadd = AsyncMock()
    redis.xack = AsyncMock()
    redis.hset = AsyncMock()
    redis.expire = AsyncMock()
    return redis


@pytest.fixture
def mock_redis_dnd_active() -> AsyncMock:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=b"1")  # DND actif
    redis.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis.xreadgroup = AsyncMock(return_value=[])
    redis.xadd = AsyncMock()
    redis.xack = AsyncMock()
    redis.hset = AsyncMock()
    redis.expire = AsyncMock()
    return redis


def _make_message(content: str) -> list:
    """Helper: retourne un résultat xreadgroup avec un seul message."""
    payload = json.dumps({
        "content": content,
        "sender_id": "discord:123",
        "channel": "discord",
        "session_id": "s1",
        "correlation_id": "c1",
        "timestamp": 0.0,
        "metadata": {},
        "media_refs": [],
    })
    return [(b"relais:messages:incoming", [(b"1-1", {b"payload": payload.encode()})])]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_portail_forwards_when_dnd_inactive(mock_redis_no_dnd):
    """Sans DND actif, le message est forwardé vers relais:security."""
    mock_redis_no_dnd.xreadgroup = AsyncMock(return_value=_make_message("bonjour"))
    
    portail = Portail()
    shutdown = MagicMock()
    shutdown.is_stopping.side_effect = [False, True]
    
    await portail._process_stream(mock_redis_no_dnd, shutdown=shutdown)
    
    # Doit xadd vers relais:security
    security_calls = [c for c in mock_redis_no_dnd.xadd.call_args_list
                      if "relais:security" in str(c)]
    assert len(security_calls) == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_portail_drops_message_when_dnd_active(mock_redis_dnd_active):
    """Avec DND actif, le message est ACKé mais PAS forwardé vers relais:security."""
    mock_redis_dnd_active.xreadgroup = AsyncMock(return_value=_make_message("bonjour"))
    
    portail = Portail()
    shutdown = MagicMock()
    shutdown.is_stopping.side_effect = [False, True]
    
    await portail._process_stream(mock_redis_dnd_active, shutdown=shutdown)
    
    # ACK doit avoir eu lieu
    mock_redis_dnd_active.xack.assert_called_once()
    
    # Pas de forward vers relais:security
    security_calls = [c for c in mock_redis_dnd_active.xadd.call_args_list
                      if "relais:security" in str(c)]
    assert len(security_calls) == 0


@pytest.mark.asyncio
@pytest.mark.unit
async def test_portail_checks_dnd_key_name(mock_redis_dnd_active):
    """Le check DND utilise exactement la clé 'relais:state:dnd'."""
    mock_redis_dnd_active.xreadgroup = AsyncMock(return_value=_make_message("test"))
    
    portail = Portail()
    shutdown = MagicMock()
    shutdown.is_stopping.side_effect = [False, True]
    
    await portail._process_stream(mock_redis_dnd_active, shutdown=shutdown)
    
    # Vérifie que redis.get a été appelé avec la bonne clé
    mock_redis_dnd_active.get.assert_called_with("relais:state:dnd")
```

---

## `tests/test_souvenir_clear.py`

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from souvenir.main import Souvenir
from souvenir.context_store import ContextStore
import json


@pytest.fixture
def mock_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.xgroup_create = AsyncMock(side_effect=Exception("BUSYGROUP"))
    redis.xreadgroup = AsyncMock(return_value=[])
    redis.xadd = AsyncMock()
    redis.xack = AsyncMock()
    redis.delete = AsyncMock()
    return redis


def _make_clear_request(session_id: str = "s1", correlation_id: str = "c1") -> list:
    payload = json.dumps({
        "action": "clear",
        "session_id": session_id,
        "correlation_id": correlation_id,
    })
    return [(b"relais:memory:request", [(b"1-1", {b"payload": payload.encode()})])]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_souvenir_clear_calls_context_store_clear(mock_redis):
    """Action 'clear' appelle context_store.clear(session_id)."""
    mock_redis.xreadgroup = AsyncMock(return_value=_make_clear_request("my_session"))
    
    souvenir = Souvenir()
    context_store = AsyncMock(spec=ContextStore)
    context_store.clear = AsyncMock()
    
    shutdown = MagicMock()
    shutdown.is_stopping.side_effect = [False, True]
    
    with patch.object(souvenir._long_term, "clear_session", new_callable=AsyncMock) as mock_lt_clear:
        await souvenir._process_request_stream(mock_redis, context_store, shutdown=shutdown)
    
    context_store.clear.assert_called_once_with("my_session")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_souvenir_clear_calls_long_term_clear_session(mock_redis):
    """Action 'clear' appelle long_term_store.clear_session(session_id)."""
    mock_redis.xreadgroup = AsyncMock(return_value=_make_clear_request("my_session"))
    
    souvenir = Souvenir()
    context_store = AsyncMock(spec=ContextStore)
    context_store.clear = AsyncMock()
    
    shutdown = MagicMock()
    shutdown.is_stopping.side_effect = [False, True]
    
    with patch.object(souvenir._long_term, "clear_session", new_callable=AsyncMock) as mock_lt_clear:
        await souvenir._process_request_stream(mock_redis, context_store, shutdown=shutdown)
        mock_lt_clear.assert_called_once_with("my_session")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_souvenir_clear_acks_message(mock_redis):
    """Action 'clear' ACK le message après traitement."""
    mock_redis.xreadgroup = AsyncMock(return_value=_make_clear_request())
    
    souvenir = Souvenir()
    context_store = AsyncMock(spec=ContextStore)
    context_store.clear = AsyncMock()
    
    shutdown = MagicMock()
    shutdown.is_stopping.side_effect = [False, True]
    
    with patch.object(souvenir._long_term, "clear_session", new_callable=AsyncMock):
        await souvenir._process_request_stream(mock_redis, context_store, shutdown=shutdown)
    
    mock_redis.xack.assert_called_once()
```

---

## Commandes pour lancer les tests

```bash
# RED phase (avant implémentation — doit échouer avec ImportError ou AttributeError)
pytest tests/test_commandant.py tests/test_portail_dnd.py tests/test_souvenir_clear.py -v

# GREEN phase (après implémentation — doit passer à 100%)
pytest tests/test_commandant.py tests/test_portail_dnd.py tests/test_souvenir_clear.py -v --tb=short

# Coverage
pytest tests/test_commandant.py tests/test_portail_dnd.py tests/test_souvenir_clear.py \
    --cov=commandant --cov=portail --cov=souvenir --cov-report=term-missing
```
