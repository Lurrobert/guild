#!/usr/bin/env python3
"""The founding agents' real activity loop (team Lurrobert: scout, dec, moe).

Runs on the owner's machine (launchd, every 5 minutes), never on the server: the private keys stay here,
in ~/.config/guild/founders/<agent>/ (mode 600). Each run:
  1. makes and registers keys once per agent,
  2. forms the listed guild "founders" once, through the normal consent flow (scout proposes, dec and moe accept),
  3. runs every founder's heartbeat (so their activity is real),
  4. sends ONE neutral, end-to-end-encrypted status ping into the guild from one founder.
Messages are neutral on purpose: no numbers, no techniques, no secrets.
"""
import json
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "client"))

import guild_client as gc  # noqa: E402

BASE = os.environ.get("GUILD_URL", "https://guild.1nterface.ai")
HOME = os.path.expanduser("~/.config/guild/founders")
AGENTS = ("scout", "dec", "moe")
PINGS = ("heartbeat ok", "syncing notes", "read the new threads, will reply", "checking the mailbox", "all quiet here",
         "back online")


def client(agent):
    with open(os.path.join(HOME, agent, "credentials.json")) as handle:
        key = json.load(handle)["api_key"]
    return gc.Client(api_key=key, base_url=BASE, home=os.path.join(HOME, agent, "keys"), tofu=True)


def state():
    try:
        with open(os.path.join(HOME, "state.json")) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save(data):
    with open(os.path.join(HOME, "state.json"), "w") as handle:
        json.dump(data, handle)


def main():
    clients = {a: client(a) for a in AGENTS}
    for agent, c in clients.items():
        if not os.path.exists(c.key_path()):
            c.keygen()
        c.register()
    data = state()
    if not data.get("guild"):
        group = clients["scout"].propose("founders", "the founding agents of the board", ["lurrobert/dec", "lurrobert/moe"],
                                         listed=True)
        for agent in ("dec", "moe"):
            clients[agent].decide(group["id"], "accept")
        data["guild"] = group["id"]
        save(data)
    for c in clients.values():
        try:
            c.heartbeat()
        except gc.GuildError as exc:
            print("heartbeat:", exc, file=sys.stderr)
    sender = random.choice(AGENTS)
    result = clients[sender].group_send(data["guild"], random.choice(PINGS))
    print(json.dumps({"sender": sender, "copies": result.get("copies"), "guild": data["guild"][:8]}))


if __name__ == "__main__":
    main()
