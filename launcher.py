"""Debug-aware brick launcher.

Usage:
    python launcher.py <brick/main.py>

Environment variables:
    DEBUGPY_ENABLED  Set to "1" to enable debugpy (default: "0")
    DEBUGPY_PORT     debugpy listen port (default: "5678")
    DEBUGPY_WAIT     Set to "1" to wait for debugger before starting (default: "0")

Example — attach VS Code to portail:
    DEBUGPY_ENABLED=1 DEBUGPY_PORT=5679 DEBUGPY_WAIT=1 uv run python launcher.py portail/main.py
"""
import os
import runpy
import sys


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: launcher.py <brick/main.py>", file=sys.stderr)
        sys.exit(1)

    if os.environ.get("DEBUGPY_ENABLED") == "1":
        import debugpy  # type: ignore[import]

        port = int(os.environ.get("DEBUGPY_PORT", "5678"))
        debugpy.listen(("0.0.0.0", port))
        print(
            f"[launcher] debugpy listening on port {port}",
            file=sys.stderr,
            flush=True,
        )
        if os.environ.get("DEBUGPY_WAIT") == "1":
            print(
                f"[launcher] Waiting for debugger to attach on port {port}...",
                file=sys.stderr,
                flush=True,
            )
            debugpy.wait_for_client()

    module_path = sys.argv[1]
    sys.argv = sys.argv[1:]
    runpy.run_path(module_path, run_name="__main__")


if __name__ == "__main__":
    main()
