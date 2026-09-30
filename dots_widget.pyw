"""Double-click (or run with pythonw) to start the D.O.T.S. widget without a console."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dots.widget import main  # noqa: E402

main()
