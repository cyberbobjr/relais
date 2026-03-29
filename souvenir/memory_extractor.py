"""Extraction de faits durables depuis un échange utilisateur/assistant."""

import json
import logging
import os

import httpx

from common.envelope import Envelope

logger = logging.getLogger(__name__)
# TODO : rendre les clefs de faits idempotentes (ex: "likes_python" au lieu de "aime_python") pour faciliter l'usage en aval, ou bien fournir les clefs existantes afin de ne pas créer de nouvelles clefs à chaque fois (ex: "aime_python" si l'utilisateur a déjà exprimé cette préférence dans un échange précédent). Cela permettrait d'avoir une mémoire plus structurée et facilement exploitable, au lieu d'avoir des faits redondants ou légèrement différents à chaque extraction.
EXTRACTION_PROMPT = (
    "Analyse cet échange et extrais les faits durables sur l'utilisateur "
    "(préférences, habitudes, informations personnelles pertinentes). "
    "Réponds UNIQUEMENT en JSON valide : "
    '[{"fact": "...", "category": "...", "confidence": 0.0-1.0}] '
    "ou [] si aucun fait pertinent. Ne mets rien d'autre dans ta réponse."
)

_CONFIDENCE_THRESHOLD = 0.7


class MemoryExtractor:
    """Extrait des faits durables sur l'utilisateur via un appel LLM.

    Conçu pour être utilisé en mode "fire-and-forget" : toute exception est
    capturée et journalisée — jamais propagée. La valeur de retour est toujours
    une liste (potentiellement vide).
    """

    def __init__(
        self,
        litellm_url: str = "http://localhost:4000",
        model: str = "gpt-3.5-turbo",
    ) -> None:
        """Initialise l'extracteur.

        Args:
            litellm_url: URL de base du proxy LiteLLM (sans trailing slash).
            model: Identifiant du modèle à utiliser pour l'extraction.
        """
        self._url = litellm_url.rstrip("/") + "/chat/completions"
        self._model = model

    async def extract(self, envelope: Envelope) -> list[dict]:
        """Extrait les faits durables depuis un échange.

        Fire-and-forget safe : retourne ``[]`` sur toute erreur (HTTP, JSON,
        connexion, etc.) sans propager d'exception.

        Args:
            envelope: L'enveloppe du message sortant. Le message utilisateur
                est lu depuis ``envelope.metadata["user_message"]`` et la
                réponse de l'assistant depuis ``envelope.content``.

        Returns:
            Liste de dicts ``{"fact", "category", "confidence"}`` filtrés
            par seuil de confiance (> 0.7). Retourne ``[]`` en cas d'erreur
            ou si ``user_message`` est vide.
        """
        user_message = envelope.metadata.get("user_message", "")
        assistant_reply = envelope.content

        if not user_message.strip():
            return []

        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": EXTRACTION_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"Utilisateur: {user_message}\n\nAssistant: {assistant_reply}"
                    ),
                },
            ],
            "max_tokens": 512,
            "temperature": 0.1,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    self._url,
                    json=payload,
                    headers={
                        "Authorization": (
                            f"Bearer {os.environ.get('LITELLM_MASTER_KEY', '')}"
                        )
                    },
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                facts = json.loads(content)
                if not isinstance(facts, list):
                    return []
                return [
                    f for f in facts if f.get("confidence", 0) > _CONFIDENCE_THRESHOLD
                ]
        except Exception as exc:
            logger.debug("Memory extraction failed (non-blocking): %s", exc)
            return []
