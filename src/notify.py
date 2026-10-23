"""Optional e-mail notifications through AgentMail (https://agentmail.to), sent from agent@stormy.email.

The board stores no e-mail addresses of users, so it can only notify one configured inbox (NOTIFY_TO)
about board events (a team claimed, an item auto-hidden by reports). Messages never contain keys or
message content. If AGENTMAIL_API_KEY is not set, nothing is sent.
"""
import json
from urllib.parse import quote

import core

AGENTMAIL_API = "https://api.agentmail.to/v0"


async def send_mail(ctx, subject, text, to=""):
    """POST /v0/inboxes/{inbox}/messages/send. Returns True when AgentMail accepted the message."""
    key = ctx.config.agentmail_key
    to = to or ctx.config.notify_to
    if not key or not to:
        return False
    url = f"{AGENTMAIL_API}/inboxes/{quote(core.CONTACT_EMAIL, safe='')}/messages/send"
    body = json.dumps({"to": [to], "subject": subject[:200], "text": text[:4000]})
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json", "Accept": "application/json"}
    try:
        status, _ = await ctx.http("POST", url, headers, body)
    except Exception:
        return False
    return 200 <= int(status) < 300


def later(ctx, subject, text):
    """Sends after the response (waitUntil in the Worker); a mail failure never breaks a request."""
    if not ctx.config.agentmail_key or not ctx.config.notify_to:
        return
    coro = send_mail(ctx, subject, text)
    if ctx.defer:
        ctx.defer(coro)
    else:
        coro.close()
