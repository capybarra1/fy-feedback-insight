"""Local-only demo server. Does not load model credentials."""
from pathlib import Path
from api import create_app
app = create_app(Path(__file__).parent / 'data/demo.sqlite3', testing=True)
