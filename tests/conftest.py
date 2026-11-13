import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
# Never pop "transfer running, really quit?" dialogs during automated runs.
os.environ["RC_NO_CONFIRM"] = "1"
