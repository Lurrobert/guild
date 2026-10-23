#!/usr/bin/env python3
"""Old name of the Guild client. Everything lives in guild_client.py; this alias keeps old scripts working."""
import sys

from guild_client import *  # noqa: F401,F403
from guild_client import GuildError, main

if __name__ == "__main__":
    try:
        main()
    except GuildError as exc:
        sys.exit(f"error: {exc}")
