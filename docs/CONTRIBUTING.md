# RELAIS — Guide de contribution

**Dernière mise à jour:** 2026-03-28
**Publique cible:** Contributeurs, mainteneurs

---

## Table des matières

1. [Setup développement](#setup-développement)
2. [Architecture des tests](#architecture-des-tests)
3. [Exécuter les tests](#exécuter-les-tests)
4. [Ajouter une nouvelle brique](#ajouter-une-nouvelle-brique)
5. [Ajouter un canal Aiguilleur](#ajouter-un-canal-aiguilleur)
6. [Conventions de code](#conventions-de-code)

---

## Setup développement

### Prérequis

- Python ≥ 3.11
- Redis ≥ 5.0
- supervisord (optionnel, pour dev)
- uv (gestionnaire de paquets recommandé)
- SQLite ≥ 3.35 (inclus dans Python stdlib)

### Installation locale

```bash
# 1. Cloner le repo
git clone <repo-url>
cd relais

# 2. Créer venv
python3.11 -m venv venv
source venv/bin/activate

# 3. Installer dépendances (avec dev)
uv pip install -e ".[dev]"
# ou
pip install -e ".[dev]"

# 4. Copier .env
cp .env.example .env
# Éditez .env avec vos valeurs de test

# 5. Appliquer les migrations Souvenir (SQLite)
alembic upgrade head

# 6. Démarrer un brick — ~/. relais/ sera auto-créé
# (aucune configuration manuelle requise)
PYTHONPATH=. uv run python portail/main.py
```

**Note:** La première exécution de tout brick initialise automatiquement `~/.relais/` avec tous les fichiers par défaut (config, prompts, SOUL, etc.). Cette initialisation est idempotente et thread-safe — safe d'appeler depuis plusieurs bricks concurrents.

### Lancer Redis en dev

**Option A : Docker**

```bash
docker run -d --name redis-relais \
  -p 6379:6379 \
  redis:7-alpine
```

**Option B : Homebrew (macOS)**

```bash
brew install redis
redis-server
```

**Option C : Depuis supervisord (recommandé)**

```bash
supervisord -c supervisord.conf
supervisorctl status  # Vérifier que Redis est up
```

### Lancer un brick pour tester

```bash
# Terminal 1 — Redis + LiteLLM
supervisord -c supervisord.conf

# Terminal 2 — un brick spécifique
PYTHONPATH=. uv run python portail/main.py
# Le brick crée automatiquement ~/.relais/ au premier lancement
```

**Première exécution:** Chaque brick appelle `initialize_user_dir()` au démarrage (avant async), ce qui:
- Crée la structure `~/.relais/` (config, logs, storage, prompts, soul, etc.)
- Copie les fichiers par défaut depuis le projet
- Jamais ne remplace les fichiers existants

L'initialisation est **idempotente** — safe de lancer plusieurs bricks en parallèle.

---

## Architecture des tests

### Couverture cible

80% minimum (règle projet).

### Types de tests

#### Unit tests (pytest)

Tester un module isolé, mock Redis:

```python
# tests/test_envelope.py
import pytest
from common.envelope import Envelope

def test_envelope_basic():
    env = Envelope(
        user_id="user_123",
        channel="discord",
        text="Hello",
    )
    assert env.user_id == "user_123"
    assert env.channel == "discord"

def test_envelope_json_roundtrip():
    env = Envelope(user_id="...", channel="discord", text="test")
    json_str = env.to_json()
    env2 = Envelope.from_json(json_str)
    assert env == env2
```

**Outils:**
- pytest
- pytest-asyncio (pour async)
- unittest.mock (mocks)

#### Integration tests (avec Redis réel)

Tester un brick complet avec Redis:

```python
# tests/integration/test_portail.py
import pytest
from portail.main import process_message
from common.redis_client import create_redis_conn
from common.envelope import Envelope

@pytest.mark.asyncio
async def test_portail_accepts_valid_message():
    redis = await create_redis_conn()

    env = Envelope(
        user_id="user_123",
        channel="discord",
        text="Hello",
    )

    result = await process_message(redis, env)
    assert result.status == "accepted"

    await redis.close()
```

**Prérequis:**
- Redis doit tourner localement
- Port 6379 par défaut
- Use `pytest.mark.asyncio` pour async

#### E2E tests (flow complet)

Tester le pipeline complet Discord → réponse:

```python
# tests/e2e/test_full_pipeline.py
import pytest
import asyncio
from aiguilleur.discord import DiscordRelay
from common.envelope import Envelope

@pytest.mark.asyncio
async def test_discord_message_flow():
    # Envoyer message Discord mock
    env = Envelope(
        user_id="discord_user_123",
        channel="discord",
        text="What is 2+2?",
    )

    # Attendre réponse (timeout 30s)
    response = await asyncio.wait_for(
        relay.send(env),
        timeout=30.0
    )

    assert response.text  # Vérifier qu'il y a une réponse
    assert "4" in response.text  # Vérifier contenu logique
```

---

## Exécuter les tests

### Lancer tous les tests

```bash
pytest tests/ -v

# Avec couverture
pytest tests/ --cov=common,portail,sentinelle,atelier,souvenir,aiguilleur,archiviste \
  --cov-report=term-missing

# Avec uv (recommandé)
PYTHONPATH=. uv run pytest tests/ --cov=common,portail,sentinelle,atelier,souvenir,aiguilleur,archiviste \
  --cov-report=term-missing
```

### Lancer un fichier de test

```bash
pytest tests/test_envelope.py -v
```

### Lancer un test spécifique

```bash
pytest tests/test_envelope.py::test_envelope_json_roundtrip -v
```

### Lancer avec log détaillé

```bash
pytest tests/ -vv -s --log-cli-level=DEBUG
```

### Tests d'intégration uniquement

```bash
pytest tests/integration/ -v -m integration
```

### Tests E2E uniquement

```bash
pytest tests/e2e/ -v -m e2e --timeout=60
```

---

## Ajouter une nouvelle brique

### Checklist création

- [ ] Créer répertoire `{brique}/`
- [ ] Créer `{brique}/main.py` (entry point)
- [ ] Hériter de type (Consumer, Transformer, Producer, Observer)
- [ ] Ajouter tests unit + intégration
- [ ] Documenter streams consommés/produits
- [ ] Mettre à jour supervisord.conf
- [ ] Mettre à jour README.md

### Structure de répertoire

```
brique/
├── main.py               # Entry point, boucle principale
├── processor.py          # Logique métier
├── models.py             # Pydantic models (si besoin)
└── __init__.py          # Exports publics
```

### Template minimal — Consumer

```python
# brique/main.py
import asyncio
import logging
from pathlib import Path
from common.init import initialize_user_dir
from common.redis_client import create_redis_conn
from common.stream_client import StreamConsumer
from common.shutdown import GracefulShutdown
from common.health import health

logger = logging.getLogger(__name__)

async def process_message(redis, envelope):
    """Logique métier."""
    # Traiter message
    logger.info(f"Processant: {envelope.user_id}")
    return envelope  # Ou modification

async def main():
    redis = await create_redis_conn("REDIS_PASS_BRIQUE")
    consumer = StreamConsumer(redis, "relais:input_stream", "brique")
    shutdown = GracefulShutdown()

    logger.info("Brique démarrée")

    try:
        async for message in consumer.consume(timeout=1000):
            try:
                envelope = Envelope.from_dict(message["data"])
                result = await process_message(redis, envelope)
                await consumer.ack(message["id"])
            except Exception as e:
                logger.error(f"Erreur processing: {e}", exc_info=True)
                # Rejeter (ne pas XACK) = redelivery automatique
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.error(f"Erreur fatal: {e}", exc_info=True)
        raise
    finally:
        await redis.close()

if __name__ == "__main__":
    initialize_user_dir(Path(__file__).parent.parent)  # Crée ~/.relais/ au premier lancement
    asyncio.run(main())
```

**Important:** Toute nouvelle brique DOIT appeler `initialize_user_dir()` dans son `__main__` block, **avant** `asyncio.run()`. Cela garantit que `~/.relais/` existe avec tous les fichiers par défaut, même au premier lancement.

### Template — Transformer (avec retry)

```python
# atelier/main.py (exemple)
import asyncio
from pathlib import Path
from common.init import initialize_user_dir
from atelier.executor import execute_with_resilience, ExhaustedRetriesError
from common.stream_client import StreamConsumer, StreamProducer

async def main():
    redis = await create_redis_conn("REDIS_PASS_ATELIER")
    consumer = StreamConsumer(redis, "relais:tasks", "atelier")
    producer = StreamProducer(redis)

    async for message in consumer.consume():
        success = False
        try:
            envelope = Envelope.from_dict(message["data"])
            result = await execute_with_resilience(http_client, envelope, context)
            await producer.publish("relais:messages:outgoing:discord", result)
            success = True
        except ExhaustedRetriesError as e:
            # DLQ — retries épuisés
            await producer.publish("relais:tasks:failed", {
                "payload": message["data"],
                "reason": str(e),
            })
            success = True  # ACK quand même (message en DLQ)
        except Exception as e:
            logger.error(f"Erreur: {e}")
            # Pas de success = pas XACK = redelivery automatique
        finally:
            if success:
                await consumer.ack(message["id"])

if __name__ == "__main__":
    initialize_user_dir(Path(__file__).parent.parent)  # Crée ~/.relais/ au premier lancement
    asyncio.run(main())
```

### Ajouter supervisord

```conf
# supervisord.conf — nouveau brick
[program:brique]
command=/Users/benjaminmarchand/.local/bin/uv run python brique/main.py
priority=10
autostart=true
autorestart=true
stdout_logfile=./.relais/logs/brique.log
redirect_stderr=true
environment=PYTHONUNBUFFERED="1",PYTHONPATH="/Users/benjaminmarchand/IdeaProjects/relais"

# Ajouter à groupe existant ou créer nouveau
[group:core]
programs=portail,sentinelle,atelier,souvenir,archiviste,brique
```

### Ajouter tests

```python
# tests/test_brique.py
import pytest
from brique.processor import process_message
from common.envelope import Envelope

@pytest.mark.asyncio
async def test_brique_accepts_message():
    env = Envelope(user_id="...", channel="discord", text="test")
    result = await process_message(env)
    assert result.processed

# tests/integration/test_brique_full.py
import pytest
from common.redis_client import create_redis_conn
from brique.main import main as brique_main

@pytest.mark.asyncio
async def test_brique_redis_flow():
    redis = await create_redis_conn()
    # Test avec Redis réel
    ...
    await redis.close()
```

### Mettre à jour docs

Ajouter dans README.md sous "Inventaire des briques":

```markdown
| **Brique** | type | Description | Taxonomie |
|---|---|---|---|
| Brique | consumer | Fait quelque chose | Consumer |
```

Ajouter dans docs/ARCHITECTURE.md:
- Stream consommé/produit
- Dépendances
- Order dans supervisord

---

## Ajouter un canal Aiguilleur

### Contrat AiguilleurBase

Tous les canaux héritent:

```python
from aiguilleur.base import AiguilleurBase
from common.envelope import Envelope

class AiguilleurCanal(AiguilleurBase):

    async def receive(self) -> Envelope:
        """Reçoit message du canal, retourne Envelope.

        Doit construire:
        - user_id: identifiant utilisateur unique
        - channel: nom du canal ("telegram", "slack", etc.)
        - text: contenu du message
        - media: liste MediaRef si applicable
        - metadata: dict {key: str} (optionnel)
        """
        ...

    async def send(self, envelope: Envelope) -> str:
        """Envoie message au canal.

        Args:
            envelope: Message formaté

        Returns:
            message_id si succès

        Raises:
            Exception si erreur réseau/API
        """
        ...

    def format_for_channel(self, text: str) -> str:
        """Convertit texte pour ce canal.

        Exemple:
        - Discord: supporte MD basique
        - Telegram: MarkdownV2
        - Slack: mrkdwn
        - REST: plaintext ou JSON
        """
        ...
```

### Template — Nouveau canal

```python
# aiguilleur/canal/main.py
import asyncio
import logging
import os
from pathlib import Path
from common.init import initialize_user_dir
from aiguilleur.base import AiguilleurBase
from common.envelope import Envelope, MediaRef
from common.stream_client import StreamConsumer
from common.redis_client import create_redis_conn
from common.markdown_converter import convert_md_to_canal

logger = logging.getLogger(__name__)

class AiguilleurCanal(AiguilleurBase):

    def __init__(self, redis, api_token):
        self.redis = redis
        self.api = APIClient(api_token)  # Client API du canal
        self.channel_name = "canal"

    async def receive(self) -> Envelope:
        """Poller ou webhook incoming."""
        message = await self.api.get_next_message()  # Bloquant ou webhook
        return Envelope(
            user_id=message["user_id"],
            channel=self.channel_name,
            text=message["text"],
            media=[MediaRef(url=m["url"], type="image") for m in message.get("media", [])],
            metadata={"message_id": message["id"]},
        )

    async def send(self, envelope: Envelope) -> str:
        """Envoyer réponse au canal."""
        formatted_text = self.format_for_channel(envelope.text)
        response = await self.api.send_message(
            user_id=envelope.user_id,
            text=formatted_text,
            media=envelope.media,
        )
        return response["message_id"]

    def format_for_channel(self, text: str) -> str:
        """Convertir MD → format canal."""
        return convert_md_to_canal(text)

async def main():
    redis = await create_redis_conn("REDIS_PASS_AIGUILLEUR")
    relay = AiguilleurCanal(redis, api_token=os.getenv("CANAL_API_TOKEN"))

    # Consumer: messages sortants
    consumer = StreamConsumer(
        redis,
        stream=f"relais:messages:outgoing:{relay.channel_name}",
        group="aiguilleur-canal"
    )

    logger.info(f"Aiguilleur {relay.channel_name} started")

    try:
        # Boucle 1: Incoming (receive)
        async def handle_incoming():
            while True:
                try:
                    envelope = await relay.receive()
                    # Publie vers relais:messages:incoming:canal
                    # Portail le reprendra
                    logger.info(f"Reçu message de {envelope.user_id}")
                    # (à implémenter selon si webhook ou polling)
                except Exception as e:
                    logger.error(f"Erreur incoming: {e}")
                    await asyncio.sleep(5)

        # Boucle 2: Outgoing (send)
        async def handle_outgoing():
            async for message in consumer.consume():
                try:
                    envelope = Envelope.from_dict(message["data"])
                    message_id = await relay.send(envelope)
                    logger.info(f"Envoyé à {envelope.user_id}: {message_id}")
                    await consumer.ack(message["id"])
                except Exception as e:
                    logger.error(f"Erreur outgoing: {e}")
                    # Ne pas ACK = redelivery automatique

        # Lancer les deux
        await asyncio.gather(
            handle_incoming(),
            handle_outgoing(),
        )
    finally:
        await redis.close()

if __name__ == "__main__":
    initialize_user_dir(Path(__file__).parent.parent.parent)  # Crée ~/.relais/ au premier lancement (parent.parent.parent car aiguilleur/canal/main.py)
    asyncio.run(main())
```

### Ajouter au supervisord

```conf
[program:aiguilleur-canal]
command=/Users/benjaminmarchand/.local/bin/uv run python aiguilleur/canal/main.py
priority=20
autostart=true
autorestart=true
stdout_logfile=./.relais/logs/aiguilleur-canal.log
redirect_stderr=true
environment=PYTHONUNBUFFERED="1",PYTHONPATH="/Users/benjaminmarchand/IdeaProjects/relais"

[group:relays]
programs=aiguilleur-discord,aiguilleur-canal
```

### Tester

```python
# tests/test_aiguilleur_canal.py
import pytest
from aiguilleur.canal.main import AiguilleurCanal
from common.envelope import Envelope

@pytest.mark.asyncio
async def test_aiguilleur_format():
    relay = AiguilleurCanal(redis=None, api_token="fake")
    result = relay.format_for_channel("**bold** text")
    # Vérifier format
    assert "bold" in result

@pytest.mark.asyncio
async def test_aiguilleur_send():
    relay = AiguilleurCanal(redis=..., api_token=...)
    env = Envelope(user_id="user_123", channel="canal", text="Hello")
    msg_id = await relay.send(env)
    assert msg_id  # Should return message ID
```

---

## Conventions de code

### Style

- **Python:** PEP 8 + Black (80 car max)
- **Imports:** Absolus, groupés (stdlib → libs → local)
- **Type hints:** Obligatoires sur public APIs
- **Docstrings:** Google-style pour fonctions publiques

### Async/await

```python
# BON
async def fetch_data():
    return await redis.get("key")

# MAUVAIS
def fetch_data():
    return redis.get("key")  # Pas d'await
```

### Error handling

```python
# BON — spécifique
try:
    await http_client.post(...)
except (httpx.ConnectError, httpx.TimeoutException):
    # Transient — retry
    ...
except httpx.HTTPStatusError as e:
    if e.response.status_code in (502, 503):
        # Retry aussi
        ...
    else:
        # Non-retriable
        raise

# MAUVAIS — générique
try:
    await http_client.post(...)
except Exception:  # Trop général!
    pass
```

### Logging

```python
import logging
logger = logging.getLogger(__name__)

# Levels:
logger.debug("Info détail")
logger.info("État nominal")
logger.warning("Problème mais continue")
logger.error("Erreur mais reste en vie")
logger.critical("Arrêt immédiat requis")

# Avec contexte
logger.error(f"Erreur pour user {user_id}", exc_info=True)
```

### Tests

```python
# Noms explicites
def test_portail_rejects_invalid_envelope():
    ...

def test_sentinelle_allows_admin_user():
    ...

# Arrange-Act-Assert
@pytest.mark.asyncio
async def test_atelier_handles_timeout():
    # Arrange
    mock_client = AsyncMock()
    mock_client.post.side_effect = asyncio.TimeoutError()

    # Act
    with pytest.raises(ExhaustedRetriesError):
        await execute_with_resilience(mock_client, envelope, context)

    # Assert
    assert mock_client.post.call_count == 3  # 3 retries
```

### Commits

Suivre conventional commits:

```
feat: add new brick {name}
fix: handle Redis connection timeout
docs: update architecture diagram
test: add integration tests for Atelier
refactor: extract common redis logic
```

### Pull Requests

1. Décrire le changement
2. Lier les issues
3. Ajouter tests (80%+ coverage)
4. Vérifier les logs existants

```markdown
## Description

Ajouter la brique Crieur pour notifications proactives.

## Changements

- Ajouter crieur/main.py (Transformer)
- Consomme relais:push:{urgency}
- Publie relais:notifications:{role}
- Tests intégration complets

Ferme #123
```

---

## Ressources

- [README.md](../README.md) — Vue d'ensemble
- [ARCHITECTURE.md](ARCHITECTURE.md) — Design technique
- [plans/RELAIS_ARCHITECTURE_COMPLETE_v12.md](../plans/RELAIS_ARCHITECTURE_COMPLETE_v12.md) — Spec détaillée
- [pytest docs](https://docs.pytest.org)
- [asyncio docs](https://docs.python.org/3/library/asyncio.html)
- [Redis Streams](https://redis.io/topics/streams-intro)

---

**Questions?** Ouvrir une issue ou discussion.
