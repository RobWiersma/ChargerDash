"""ChargerDash desktop app: the charger dashboard in its own window.

Runs the same server as server.py (so phones on the network can still connect) and shows the
page in an Edge WebView2 window. Closing the window quits the app.

Run from source:  python app.py
Build the exe:    build.bat
"""
import json
import os
import urllib.request

import webview

import server

PORT = 8765
TITLE = "ChargerDash for SkyRC iMAX B6AC v2"


def running_dashboard(port):
    """True if a ChargerDash server is already answering on this port."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/info", timeout=1) as r:
            return "local" in json.load(r)
    except (OSError, ValueError):
        return False


def main():
    if running_dashboard(PORT):
        # Another copy is already watching the chargers; just open a window onto it.
        port = PORT
    else:
        try:
            port = server.start_server("0.0.0.0", PORT).server_address[1]
        except OSError:
            # Something else has the usual port; take any free one (phones then need the new address).
            port = server.start_server("0.0.0.0", 0).server_address[1]

    webview.create_window(TITLE, f"http://127.0.0.1:{port}/", width=1200, height=900, min_size=(400, 500))
    # Keep browser storage (the sound on/off choice) between runs.
    webview.start(private_mode=False, storage_path=os.path.join(server.DATA_DIR, "webview"))


if __name__ == "__main__":
    main()
