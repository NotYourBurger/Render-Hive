"""Test bootstrap: isolate all RenderHive user data into a temp folder
BEFORE coordinator.py is imported, so tests never touch the real
Documents/RenderHive directory or trigger the legacy-data migration."""
import os
import tempfile

os.environ.setdefault(
    "RENDERHIVE_DATA_DIR",
    os.path.join(tempfile.gettempdir(), "renderhive_test_data"),
)
