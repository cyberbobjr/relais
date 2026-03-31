"""Text utilities shared across bricks."""


def strip_outer_quotes(text: str) -> str:
    """Strip symmetric single or double quotes wrapping the entire string.

    Handles the common messaging-client behaviour where users paste slash
    commands surrounded by quotes (e.g. ``"/clear"`` or ``'/dnd'``).
    Only outer quotes are removed when they are symmetric and wrap the full
    string (after stripping whitespace).

    Args:
        text: Raw string that may be surrounded by quotes.

    Returns:
        The unquoted string (whitespace-stripped), or the original stripped
        value if no symmetric quotes are present.
    """
    stripped = text.strip()
    if len(stripped) > 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}:
        return stripped[1:-1].strip()
    return stripped
