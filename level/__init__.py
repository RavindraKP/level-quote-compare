"""Level: draft an RFx, read vendor quotes in any format, compare them, analyse them.

This package never imports Streamlit, so everything here runs from plain Python and tests.
"""

from pathlib import Path

from dotenv import find_dotenv, load_dotenv

# Every entry point (app, scripts/seed.py, scripts/read_inbox.py, evals.run, tests) imports
# `level`, so .env is read here once. Values already set in the environment win.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")
load_dotenv(find_dotenv(usecwd=True))  # also a .env in or above the current folder
