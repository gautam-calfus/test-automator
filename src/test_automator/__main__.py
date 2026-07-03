"""Allow `python -m test_automator`."""

import sys

from test_automator.cli import main

if __name__ == "__main__":
    sys.exit(main())
