"""uvicorn twiliovoice.main:app --port 8000   (ONE worker: pending answers live in the process)."""
import logging
import os

from .app import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
app = create_app(fake_brain=os.environ.get("FAKE_BRAIN") == "1")
