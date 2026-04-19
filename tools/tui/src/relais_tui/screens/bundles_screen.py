"""Bundles management screen for the RELAIS TUI.

Displays installed bundles in a DataTable and provides controls for
installing new bundles from ZIP files and uninstalling existing ones.
"""

from __future__ import annotations

import logging
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Button, DataTable, Input, Label, Static

from relais_tui.bundles import BundleInfo, install_bundle, list_bundles, uninstall_bundle

_log = logging.getLogger(__name__)

_CSS = """
BundlesScreen {
    layout: vertical;
    padding: 1 2;
    background: #1a1a2e;
}

#bundles-table {
    height: 1fr;
    border: tall #0f3460;
}

#zip-input {
    height: 3;
    background: #16213e;
    border: tall #0f3460;
    color: #f8f8f2;
    padding: 0 1;
    margin-top: 1;
}

#zip-input:focus {
    border: tall #50fa7b;
}

#install-btn {
    margin-top: 1;
    margin-right: 1;
    background: #0f3460;
    color: #8be9fd;
    border: tall #50fa7b;
}

#install-btn:hover {
    background: #50fa7b;
    color: #1a1a2e;
}

#uninstall-btn {
    margin-top: 1;
    background: #0f3460;
    color: #ff5555;
    border: tall #ff5555;
}

#uninstall-btn:hover {
    background: #ff5555;
    color: #f8f8f2;
}

#bundles-status {
    height: 1;
    margin-top: 1;
    color: #6272a4;
    background: #16213e;
    padding: 0 1;
}
"""


class BundlesScreen(Static):
    """Inline panel listing installed bundles with install/uninstall actions.

    Embedded as a ``TabPane`` content widget rather than a full ``Screen``
    so it coexists with the chat tab inside ``TabbedContent``.
    """

    CSS = _CSS

    BINDINGS = [
        Binding("r", "refresh_table", "Refresh", show=True),
    ]

    def compose(self) -> ComposeResult:
        """Build the widget tree.

        Returns:
            Generator yielding child widgets in display order.
        """
        yield DataTable(id="bundles-table", show_cursor=True)
        yield Input(
            placeholder="Path to bundle ZIP file…",
            id="zip-input",
        )
        yield Button("Install", id="install-btn", variant="primary")
        yield Button("Uninstall", id="uninstall-btn", variant="error")
        yield Label("Ready.", id="bundles-status")

    def on_mount(self) -> None:
        """Populate the table after the DOM is ready."""
        table = self.query_one("#bundles-table", DataTable)
        table.add_columns("Name", "Version", "Description")
        self._refresh_table()

    def _refresh_table(self) -> None:
        """Reload installed bundles and repopulate the DataTable."""
        table = self.query_one("#bundles-table", DataTable)
        table.clear()
        try:
            bundles = list_bundles()
            for bundle in bundles:
                table.add_row(bundle.name, bundle.version, bundle.description, key=bundle.name)
            count = len(bundles)
            self.query_one("#bundles-status", Label).update(
                f"{count} bundle{'s' if count != 1 else ''} installed."
            )
        except Exception as exc:
            _log.exception("Error listing bundles: %s", exc)
            self.query_one("#bundles-status", Label).update(f"Error listing bundles: {exc}")

    def action_refresh_table(self) -> None:
        """Refresh the bundle list (bound to 'r').

        Returns:
            None. Side effect: DataTable is repopulated from disk.
        """
        self._refresh_table()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle Install and Uninstall button clicks.

        Args:
            event: The button-pressed event carrying the button ID.
        """
        if event.button.id == "install-btn":
            self._do_install()
        elif event.button.id == "uninstall-btn":
            self._do_uninstall()

    def _do_install(self) -> None:
        """Install the bundle from the ZIP path entered in the input widget.

        Reads the ZIP path from ``#zip-input``, calls :func:`install_bundle`,
        refreshes the table on success, and updates ``#bundles-status`` with
        the outcome.

        Returns:
            None. Side effects: bundle installed on disk, table refreshed,
            status label updated.
        """
        status = self.query_one("#bundles-status", Label)
        zip_input = self.query_one("#zip-input", Input)
        zip_path_str = zip_input.value.strip()
        if not zip_path_str:
            status.update("Please enter a ZIP file path.")
            return
        zip_path = Path(zip_path_str).expanduser()
        try:
            info: BundleInfo = install_bundle(zip_path)
            status.update(f"Installed bundle '{info.name}' v{info.version}.")
            zip_input.clear()
            self._refresh_table()
        except FileNotFoundError as exc:
            status.update(f"File not found: {exc}")
        except ValueError as exc:
            status.update(f"Invalid bundle: {exc}")
        except Exception as exc:
            _log.exception("Unexpected error installing bundle: %s", exc)
            status.update(f"Error: {exc}")

    def _do_uninstall(self) -> None:
        """Uninstall the bundle currently selected in the DataTable.

        Reads the bundle name from the first cell of the cursor row, calls
        :func:`uninstall_bundle`, and refreshes the table.

        Returns:
            None. Side effects: bundle removed from disk, table refreshed,
            status label updated.
        """
        status = self.query_one("#bundles-status", Label)
        table = self.query_one("#bundles-table", DataTable)
        if table.row_count == 0:
            status.update("No bundles installed.")
            return
        try:
            cell_value = table.get_cell_at((table.cursor_row, 0))
        except Exception:
            status.update("No bundle selected.")
            return
        name = str(cell_value)
        try:
            uninstall_bundle(name)
            status.update(f"Uninstalled bundle '{name}'.")
            self._refresh_table()
        except FileNotFoundError as exc:
            status.update(f"Bundle not found: {exc}")
        except Exception as exc:
            _log.exception("Unexpected error uninstalling bundle: %s", exc)
            status.update(f"Error: {exc}")
