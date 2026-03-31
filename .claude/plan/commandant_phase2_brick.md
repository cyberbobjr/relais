# Phase 2 — Brick Le Commandant

**Fichiers à créer:**
- `commandant/__init__.py`
- `commandant/command_parser.py`
- `commandant/handlers.py`
- `commandant/main.py`

**Complexité:** MEDIUM

---

## `commandant/__init__.py`

Fichier vide. Nécessaire pour que Python reconnaisse le package.

```python
# commandant/__init__.py
```

---

## `commandant/command_parser.py`

### Responsabilité
Détecter si un message est une commande globale et la parser.

### Spec

```python
from dataclasses import dataclass, field

KNOWN_COMMANDS: frozenset[str] = frozenset({"clear", "dnd", "brb"})

@dataclass(frozen=True)
class CommandResult:
    """Résultat du parsing d'une commande globale.
    
    Attributes:
        command: Nom de la commande en minuscules (ex: "clear").
        args: Arguments supplémentaires (actuellement toujours vide).
    """
    command: str
    args: list[str] = field(default_factory=list)


def parse_command(text: str) -> CommandResult | None:
    """Parse un message texte pour détecter une commande globale.
    
    Une commande valide :
    - Commence par '/' après strip()
    - Le nom (après '/') appartient à KNOWN_COMMANDS (insensible à la casse)
    
    Args:
        text: Le contenu brut du message.
        
    Returns:
        CommandResult si commande connue détectée, None sinon.
    """
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    
    parts = stripped[1:].split()
    if not parts:
        return None
    
    command_name = parts[0].lower()
    if command_name not in KNOWN_COMMANDS:
        return None
    
    return CommandResult(command=command_name, args=parts[1:])
```

---

## `commandant/handlers.py`

### Responsabilité
Exécuter les actions Redis associées à chaque commande. Chaque handler :
1. Effectue l'action Redis
2. Publie un message de confirmation sur `relais:messages:outgoing:{channel}`

### Messages de confirmation (texte exact)

| Commande | Réponse |
|----------|---------|
| `/clear` | `"✓ Historique de conversation effacé."` |
| `/dnd` | `"✓ Mode DND activé — je ne répondrai plus jusqu'à /brb."` |
| `/brb` | `"✓ Je suis de retour ! Pipeline de réponse réactivé."` |

### Spec

```python
import json
import logging
from typing import Any

from common.envelope import Envelope

logger = logging.getLogger("commandant.handlers")


async def handle_clear(envelope: Envelope, redis_conn: Any) -> None:
    """Efface l'historique de session : Redis context + SQLite messages.
    
    Envoie action="clear" sur relais:memory:request pour que Souvenir
    effectue le nettoyage (context_store.clear + long_term_store.clear_session).
    
    Publie ensuite un message de confirmation sur le canal d'origine.
    
    Args:
        envelope: L'enveloppe du message /clear reçu.
        redis_conn: Connexion Redis async active.
    """
    # Demande le clear à Souvenir
    clear_request = {
        "action": "clear",
        "session_id": envelope.session_id,
        "correlation_id": envelope.correlation_id,
    }
    await redis_conn.xadd(
        "relais:memory:request",
        {"payload": json.dumps(clear_request)},
    )
    logger.info("Clear request sent for session=%s", envelope.session_id)
    
    # Confirmation vers le canal
    response = Envelope.from_parent(envelope, "✓ Historique de conversation effacé.")
    await redis_conn.xadd(
        f"relais:messages:outgoing:{envelope.channel}",
        {"payload": response.to_json()},
    )


async def handle_dnd(envelope: Envelope, redis_conn: Any) -> None:
    """Active le mode DND global en posant la clé relais:state:dnd.
    
    Pas de TTL : la clé persiste jusqu'à /brb ou suppression manuelle.
    
    Args:
        envelope: L'enveloppe du message /dnd reçu.
        redis_conn: Connexion Redis async active.
    """
    await redis_conn.set("relais:state:dnd", "1")
    logger.info("DND mode activated by sender=%s", envelope.sender_id)
    
    response = Envelope.from_parent(
        envelope,
        "✓ Mode DND activé — je ne répondrai plus jusqu'à /brb.",
    )
    await redis_conn.xadd(
        f"relais:messages:outgoing:{envelope.channel}",
        {"payload": response.to_json()},
    )


async def handle_brb(envelope: Envelope, redis_conn: Any) -> None:
    """Désactive le mode DND global en supprimant la clé relais:state:dnd.
    
    Args:
        envelope: L'enveloppe du message /brb reçu.
        redis_conn: Connexion Redis async active.
    """
    await redis_conn.delete("relais:state:dnd")
    logger.info("DND mode deactivated by sender=%s", envelope.sender_id)
    
    response = Envelope.from_parent(
        envelope,
        "✓ Je suis de retour ! Pipeline de réponse réactivé.",
    )
    await redis_conn.xadd(
        f"relais:messages:outgoing:{envelope.channel}",
        {"payload": response.to_json()},
    )


# Table de dispatch : command_name → handler coroutine
DISPATCH: dict[str, Any] = {
    "clear": handle_clear,
    "dnd": handle_dnd,
    "brb": handle_brb,
}
```

---

## `commandant/main.py`

### Responsabilité
Consumer group sur `relais:messages:incoming`. Traite les commandes, ignore le reste.

### Consumer group
- Stream: `relais:messages:incoming`
- Group: `commandant_group`
- Consumer: `commandant_1`

### Comportement
- **Commande connue** → appelle le handler correspondant → ACK
- **Commande inconnue** → ignoré silencieusement → ACK
- **Message normal** → ignoré silencieusement → ACK
- **Erreur de parsing** → log ERROR → ACK quand même (éviter PEL poison)

### Spec

```python
import asyncio
import logging
import sys
from typing import Any

from commandant.command_parser import parse_command
from commandant.handlers import DISPATCH
from common.envelope import Envelope
from common.redis_client import RedisClient
from common.shutdown import GracefulShutdown

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)-18s | %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("commandant")


class Commandant:
    """Brique Le Commandant — interprète les commandes globales hors-LLM.
    
    Consomme relais:messages:incoming en parallèle avec Le Portail via
    son propre consumer group (commandant_group). Traite les commandes /
    connues et ACK tous les messages (commandes ou non).
    """
    
    def __init__(self) -> None:
        self.client: RedisClient = RedisClient("commandant")
        self.stream_in: str = "relais:messages:incoming"
        self.group_name: str = "commandant_group"
        self.consumer_name: str = "commandant_1"
    
    async def _process_stream(
        self,
        redis_conn: Any,
        shutdown: GracefulShutdown | None = None,
    ) -> None:
        """Boucle principale de consommation.
        
        Args:
            redis_conn: Connexion Redis async active.
            shutdown: Instance GracefulShutdown. Si None, une nouvelle est créée.
        """
        if shutdown is None:
            shutdown = GracefulShutdown()
        
        try:
            await redis_conn.xgroup_create(
                self.stream_in, self.group_name, mkstream=True
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                logger.warning("Consumer group error: %s", exc)
        
        logger.info("Commandant listening on %s ...", self.stream_in)
        
        while not shutdown.is_stopping():
            try:
                results = await redis_conn.xreadgroup(
                    self.group_name,
                    self.consumer_name,
                    {self.stream_in: ">"},
                    count=10,
                    block=2000,
                )
                
                if not results:
                    continue
                
                for _, messages in results:
                    for message_id, data in messages:
                        try:
                            payload = data.get("payload", "{}")
                            if isinstance(payload, bytes):
                                payload = payload.decode()
                            
                            envelope = Envelope.from_json(payload)
                            result = parse_command(envelope.content)
                            
                            if result is not None:
                                handler = DISPATCH.get(result.command)
                                if handler is not None:
                                    await handler(envelope, redis_conn)
                                    logger.info(
                                        "Executed command /%s for sender=%s",
                                        result.command,
                                        envelope.sender_id,
                                    )
                                # Commande inconnue mais parse_command ne renvoie
                                # None que pour les commandes hors KNOWN_COMMANDS,
                                # donc cette branche est un garde-fou redondant.
                        
                        except Exception as inner_exc:
                            logger.error(
                                "Failed to process message %s: %s",
                                message_id,
                                inner_exc,
                            )
                        finally:
                            # ACK TOUJOURS — même pour les non-commandes
                            await redis_conn.xack(
                                self.stream_in, self.group_name, message_id
                            )
            
            except Exception as exc:
                logger.error("Stream error: %s", exc)
                await asyncio.sleep(1)
    
    async def start(self) -> None:
        """Point d'entrée de la brique Commandant.
        
        Installe les handlers de signal SIGTERM/SIGINT et démarre la boucle.
        """
        shutdown = GracefulShutdown()
        shutdown.install_signal_handlers()
        redis_conn = await self.client.get_connection()
        await redis_conn.xadd("relais:logs", {
            "level": "INFO",
            "brick": "commandant",
            "message": "Commandant started",
        })
        try:
            await self._process_stream(redis_conn, shutdown=shutdown)
        except asyncio.CancelledError:
            logger.info("Commandant shutting down...")
        finally:
            await self.client.close()
            logger.info("Commandant stopped gracefully")


if __name__ == "__main__":
    from common.init import initialize_user_dir
    initialize_user_dir()
    commandant = Commandant()
    try:
        asyncio.run(commandant.start())
    except KeyboardInterrupt:
        pass
```

---

## Notes d'implémentation

- `RedisClient("commandant")` utilise le mot de passe `REDIS_PASS_COMMANDANT` (variable d'env) — voir Phase 5 pour la configuration.
- `Envelope.from_parent(envelope, content)` préserve `sender_id`, `channel`, `session_id`, `correlation_id` du parent et génère un nouveau `correlation_id`.
- La boucle `finally: xack` garantit qu'aucun message ne reste dans la PEL du commandant, même en cas d'erreur.
