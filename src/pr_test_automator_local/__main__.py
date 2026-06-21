"""Allow `python -m pr_test_automator_local`."""

import sys

from pr_test_automator_local.cli import main

if __name__ == "__main__":
    sys.exit(main())
