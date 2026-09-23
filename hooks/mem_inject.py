#!/usr/bin/env python3
# Stub: Phase 0. Phase 1 implements SessionStart injection.
import os, sys
if os.environ.get("CCMEM_DISABLED"):
    sys.exit(0)
try:
    sys.stdin.buffer.read()
except Exception:
    pass
