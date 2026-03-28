from abc import ABC, abstractmethod

from common.envelope import Envelope


class AiguilleurBase(ABC):
    """Abstract base class for all RELAIS channel adapters (aiguilleurs).

    Each concrete aiguilleur handles one communication channel (Discord,
    Telegram, Slack, REST, etc.) and must implement the three core methods
    that form the channel contract.

    Attributes:
        channel_name: Unique identifier for the channel (e.g., "discord").
    """

    channel_name: str = ""

    @abstractmethod
    async def receive(self) -> Envelope:
        """Receive one incoming message from the channel.

        Blocks until a message is available, then parses it into a standard
        Envelope for downstream processing by Le Portail.

        Returns:
            A fully populated Envelope representing the incoming message.
        """
        ...

    @abstractmethod
    async def send(self, envelope: Envelope, text: str) -> None:
        """Send a reply back to the originating channel.

        Args:
            envelope: The original envelope used to identify the destination
                      (channel, user, thread, etc.).
            text: The pre-formatted reply text to deliver.
        """
        ...

    @abstractmethod
    def format_for_channel(self, text: str) -> str:
        """Convert generic Markdown text to channel-native formatting.

        Each channel has its own markup conventions (Discord uses Markdown,
        Slack uses mrkdwn, Telegram uses MarkdownV2, etc.). This method
        applies the appropriate conversion.

        Args:
            text: Raw text, possibly containing standard Markdown.

        Returns:
            Text formatted according to the channel's conventions.
        """
        ...

    async def start(self) -> None:
        """Launch the main processing loop for this aiguilleur.

        The default implementation is a no-op. Override to run a long-lived
        receive/dispatch loop (e.g., a Discord gateway connection).
        """
        ...

    async def stop(self) -> None:
        """Gracefully stop the aiguilleur and release resources.

        Called on SIGTERM or when the supervisor shuts down the process.
        Override to close connections, cancel tasks, flush buffers, etc.
        """
        ...
