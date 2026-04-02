"""Registre centralisé des commandes du Commandant.

Ce module est l'unique source de vérité pour les commandes globales hors-LLM.
Ajouter une commande = une seule modification ici :
  1. Écrire le handler async (fonction ci-dessous)
  2. Ajouter une entrée dans COMMAND_REGISTRY

KNOWN_COMMANDS et parse_command() se mettent à jour automatiquement.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from common.envelope import Envelope
from common.text_utils import strip_outer_quotes

logger = logging.getLogger("commandant.commands")


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CommandSpec:
    """Métadonnées et handler d'une commande globale.

    Attributes:
        name: Nom de la commande en minuscules (ex: "clear").
        description: Description courte affichée par /help.
        handler: Coroutine async(envelope, redis_conn) exécutée quand la
                 commande est détectée.
    """
    name: str
    description: str
    handler: Callable[..., Awaitable[None]]


@dataclass(frozen=True)
class CommandResult:
    """Résultat du parsing d'une commande globale.

    Attributes:
        command: Nom de la commande en minuscules (ex: "clear").
        args: Arguments supplémentaires (actuellement toujours vide).
    """
    command: str
    args: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def handle_clear(envelope: Envelope, redis_conn: Any) -> None:
    """Efface l'historique de session : Redis context + SQLite messages.

    Envoie action="clear" sur relais:memory:request pour que Souvenir
    effectue le nettoyage (context_store.clear + long_term_store.clear_session)
    et publie la confirmation de retour vers le canal.

    Args:
        envelope: L'enveloppe du message /clear reçu.
        redis_conn: Connexion Redis async active.
    """
    clear_request = {
        "action": "clear",
        "session_id": envelope.session_id,
        "correlation_id": envelope.correlation_id,
        "envelope_json": envelope.to_json(),
    }

    await redis_conn.xadd(
        "relais:memory:request",
        {"payload": json.dumps(clear_request)},
    )
    logger.info("Clear request sent for session=%s", envelope.session_id)


async def handle_help(envelope: Envelope, redis_conn: Any) -> None:
    """Retourne la liste de toutes les commandes disponibles avec leur description.

    La liste est construite dynamiquement depuis COMMAND_REGISTRY, garantissant
    qu'elle est toujours à jour sans modification du handler.

    Args:
        envelope: L'enveloppe du message /help reçu.
        redis_conn: Connexion Redis async active.
    """
    lines = ["Commandes disponibles :"]
    for spec in COMMAND_REGISTRY.values():
        lines.append(f"  /{spec.name} — {spec.description}")
    help_text = "\n".join(lines)

    response = Envelope.from_parent(envelope, help_text)
    await redis_conn.xadd(
        f"relais:messages:outgoing:{envelope.channel}",
        {"payload": response.to_json()},
    )


# ---------------------------------------------------------------------------
# Registre — source unique de vérité
# ---------------------------------------------------------------------------

COMMAND_REGISTRY: dict[str, CommandSpec] = {
    "clear": CommandSpec(
        name="clear",
        description="Efface l'historique de conversation (Redis + SQLite).",
        handler=handle_clear,
    ),
    "help": CommandSpec(
        name="help",
        description="Affiche la liste des commandes disponibles.",
        handler=handle_help,
    ),
}

KNOWN_COMMANDS: frozenset[str] = frozenset(COMMAND_REGISTRY)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_command(text: str) -> CommandResult | None:
    """Parse un message texte pour détecter une commande globale.

    Une commande valide :
    - Commence par '/' après strip()
    - Peut être entourée de quotes simples ou doubles symétriques
    - Le nom (après '/') appartient à KNOWN_COMMANDS (insensible à la casse)

    Args:
        text: Le contenu brut du message.

    Returns:
        CommandResult si commande connue détectée, None sinon.
    """
    stripped = strip_outer_quotes(text)
    if not stripped.startswith("/"):
        return None

    parts = stripped[1:].split()
    if not parts:
        return None

    command_name = parts[0].lower()
    if command_name not in KNOWN_COMMANDS:
        return None

    return CommandResult(command=command_name, args=parts[1:])
