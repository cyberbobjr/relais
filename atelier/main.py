import asyncio
import os
import json
import logging
import sys
import time
import uuid
import httpx
from typing import Any

from common.redis_client import RedisClient
from common.envelope import Envelope
from atelier import executor
from atelier.executor import ExhaustedRetriesError

# Configure logging to standard output
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("atelier")


class Atelier:
    """La brique L'Atelier du système RELAIS, responsable des générations IA.

    This class consumes validated tasks, requests historical context from
    Le Souvenir brick, queries the LiteLLM proxy for text generation, and
    returns the crafted response to the originating Aiguilleur.
    """

    def __init__(self) -> None:
        """Initializes L'Atelier with Redis stream and group configurations."""
        self.client: RedisClient = RedisClient("atelier")
        self.stream_in: str = "relais:tasks"
        self.group_name: str = "atelier_group"
        self.consumer_name: str = "atelier_1"
        self.litellm_url: str = os.environ.get("LITELLM_BASE_URL", "http://localhost:4000/v1")
        self.litellm_key: str = os.environ.get("LITELLM_MASTER_KEY", "")
        self.litellm_model: str = os.environ.get("LITELLM_MODEL", "mistral-small-2603")

    async def _get_memory_context(
        self, redis_conn: Any, session_id: str, new_message: str
    ) -> list[dict[str, str]]:
        """Fetch the conversational history context from the Memory brick.

        Sends requests to append the user's latest message and fetch the full context.

        Args:
            redis_conn: Active Redis connection.
            session_id: Unique identifier for the user conversation.
            new_message: Raw text from the user.

        Returns:
            List of message dictionaries (role/content pairs).
        """
        correlation_id = str(uuid.uuid4())

        # 1. Ask memory to append the user's message
        append_req = {
            "action": "append",
            "session_id": session_id,
            "correlation_id": correlation_id,
            "role": "user",
            "message": new_message
        }
        await redis_conn.xadd("relais:memory:request", {"payload": json.dumps(append_req)})

        # 2. Ask memory for current full context
        get_req = {
            "action": "get",
            "session_id": session_id,
            "correlation_id": correlation_id
        }

        last_id = "$"
        await redis_conn.xadd("relais:memory:request", {"payload": json.dumps(get_req)})

        # 3. Wait for the response with matching correlation_id
        for _ in range(15):  # Roughly 7.5 seconds
            try:
                results = await redis_conn.xread(
                    {"relais:memory:response": last_id},
                    count=10,
                    block=500
                )
                if results:
                    for _, messages in results:
                        for msg_id, data in messages:
                            last_id = msg_id
                            res = json.loads(data.get("payload", "{}"))
                            if res.get("correlation_id") == correlation_id:
                                return res.get("history", [])
            except Exception as e:
                logger.error(f"Error reading memory response: {e}")
            await asyncio.sleep(0.1)

        logger.warning(f"Timeout waiting for memory context for {session_id}")
        return [{"role": "user", "content": new_message}]

    async def _append_assistant_memory(
        self, redis_conn: Any, session_id: str, reply: str
    ) -> None:
        """Append the assistant's response to the context.

        Args:
            redis_conn: Active Redis connection.
            session_id: Unique identifier for the user conversation.
            reply: Generated AI text.
        """
        append_req = {
            "action": "append",
            "session_id": session_id,
            "correlation_id": str(uuid.uuid4()),
            "role": "assistant",
            "message": reply
        }
        await redis_conn.xadd("relais:memory:request", {"payload": json.dumps(append_req)})

    async def _handle_message(
        self,
        redis_conn: Any,
        http_client: httpx.AsyncClient,
        message_id: str,
        payload: str,
    ) -> bool:
        """Process a single task message from the Redis stream.

        Fetches memory context, calls LiteLLM, saves the assistant reply,
        and publishes the response envelope. On ExhaustedRetriesError the
        message is routed to the DLQ and True is returned so the caller
        can ACK it out of the PEL.

        Args:
            redis_conn: Active Redis connection.
            http_client: Shared httpx async client.
            message_id: Redis stream message ID (used for DLQ logging).
            payload: Raw JSON string of the task envelope.

        Returns:
            True when the message should be ACKed (success or DLQ routing).
            False when a transient error occurred and the message should
            remain in the PEL for re-delivery.
        """
        envelope: Envelope | None = None
        try:
            envelope = Envelope.from_json(payload)
            logger.info(
                f"Processing task: {envelope.correlation_id} "
                f"for {envelope.sender_id}"
            )
            await redis_conn.xadd("relais:logs", {
                            "level": "INFO",
                            "brick": "atelier",
                            "message": (
                                f"Processing task: {envelope.correlation_id} for {envelope.sender_id}"
                            )
                        })
            # 1. Get Context Window
            logger.debug(
                f"[{envelope.correlation_id}] Step 1: fetching memory "
                f"context for session {envelope.sender_id}"
            )
            context = await self._get_memory_context(
                redis_conn, envelope.sender_id, envelope.content
            )
            logger.debug(
                f"[{envelope.correlation_id}] Step 1 done: "
                f"{len(context)} message(s) in context"
            )

            # 2. Call LiteLLM Proxy via resilient executor
            logger.debug(
                f"[{envelope.correlation_id}] Step 2: calling LiteLLM "
                f"at {self.litellm_url} with model {self.litellm_model}, "
                f"{len(context)} message(s)"
            )
            reply_text = await executor.execute_with_resilience(
                http_client=http_client,
                envelope=envelope,
                context=context,
                litellm_url=self.litellm_url,
                model=self.litellm_model,
                api_key=self.litellm_key,
            )
            logger.debug(
                f"[{envelope.correlation_id}] Step 2 done: "
                f"reply length={len(reply_text)}"
            )

            # 3. Save assistant reply to memory
            logger.debug(
                f"[{envelope.correlation_id}] Step 3: saving assistant "
                f"reply to memory"
            )
            await self._append_assistant_memory(
                redis_conn, envelope.sender_id, reply_text
            )

            # 4. Craft Response Envelope
            response_env = Envelope.create_response_to(envelope, reply_text)
            response_env.add_trace(
                "atelier", f"Generated via {self.litellm_model}"
            )

            # 5. Send back to the originating aiguilleur
            out_stream = f"relais:messages:outgoing:{envelope.channel}"
            logger.debug(
                f"[{envelope.correlation_id}] Step 5: publishing to "
                f"{out_stream}"
            )
            await redis_conn.xadd(
                out_stream, {"payload": response_env.to_json()}
            )

            await redis_conn.xadd("relais:logs", {
                "level": "INFO",
                "brick": "atelier",
                "message": (
                    f"Answered {envelope.correlation_id} via {out_stream}"
                )
            })
            return True

        except ExhaustedRetriesError as exc:
            # All retries exhausted — move to Dead Letter Queue
            logger.error(
                f"[{envelope.correlation_id if envelope else message_id}] "
                f"Exhausted retries, routing to DLQ: {exc}"
            )
            await redis_conn.xadd("relais:tasks:failed", {
                "payload": payload,
                "reason": str(exc),
                "failed_at": str(time.time()),
            })
            await redis_conn.xadd("relais:logs", {
                "level": "ERROR",
                "brick": "atelier",
                "message": (
                    f"Task routed to DLQ after exhausted retries: {exc}"
                ),
            })
            # ACK — message is preserved in DLQ, not lost
            return True

        except executor.RETRIABLE as exc:
            # Transient error before retries started (unlikely with executor)
            # Do NOT ACK — message stays in PEL for re-delivery
            logger.error(
                f"Transient error on task {message_id}, leaving in PEL: {exc}",
                exc_info=True,
            )
            return False

        except Exception as inner_e:
            # Unexpected non-retriable error — log and ACK to avoid
            # poisoning the PEL with a message that will always fail
            logger.error(
                f"Failed to process task {message_id}: {inner_e}",
                exc_info=True
            )
            await redis_conn.xadd("relais:logs", {
                "level": "ERROR",
                "brick": "atelier",
                "message": f"Task execution failed: {inner_e}"
            })
            return True

    async def _process_stream(self, redis_conn: Any) -> None:
        """Main loop: reads from the Redis stream and dispatches messages.

        Args:
            redis_conn: Active Redis connection.
        """
        try:
            await redis_conn.xgroup_create(self.stream_in, self.group_name, mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                logger.warning(f"Consumer group error: {e}")

        logger.info("Workshop listening for tasks...")

        async with httpx.AsyncClient() as http_client:
            while True:
                try:
                    results = await redis_conn.xreadgroup(
                        self.group_name,
                        self.consumer_name,
                        {self.stream_in: ">"},
                        count=5,
                        block=2000
                    )

                    if not results:
                        continue

                    for _, messages in results:
                        for message_id, data in messages:
                            payload = data.get("payload", "{}")
                            should_ack = await self._handle_message(
                                redis_conn, http_client, message_id, payload
                            )
                            if should_ack:
                                await redis_conn.xack(
                                    self.stream_in, self.group_name, message_id
                                )

                except Exception as e:
                    logger.error(f"Stream error: {e}")
                    await asyncio.sleep(1)

    async def start(self) -> None:
        """Starts L'Atelier service and its main loop."""
        redis_conn = await self.client.get_connection()
        await redis_conn.xadd("relais:logs", {
            "level": "INFO",
            "brick": "atelier",
            "message": "Atelier started"
        })
        try:
            await self._process_stream(redis_conn)
        except asyncio.CancelledError:
            logger.info("Atelier shutting down...")
        finally:
            await self.client.close()


if __name__ == "__main__":
    from pathlib import Path
    from common.init import initialize_user_dir
    initialize_user_dir(Path(__file__).parent.parent)
    atelier = Atelier()
    try:
        asyncio.run(atelier.start())
    except KeyboardInterrupt:
        pass
