"""End to end: the real client crypto against the real app on sqlite.

Proves: the stored rows hold no plaintext and no private key, the server code has no decrypt path,
signatures are checked before decryption, keys are checked against GitHub files and the hash-chained
key log, and private groups follow the all-members consent rule."""
import ast
import json
import os

import pytest

import core
import rebellion_client as rc
from harness import ROOT, Board, FakeGitHub

SERVER_MODULES = ("app.py", "core.py", "entry.py", "messaging.py", "notify.py", "store.py", "version.py", "views.py")
SECRET_DM = "DM-PLAINTEXT: we take shapes 1-4 tonight"
SECRET_GROUP = "GROUP-PLAINTEXT: compare notes at 18:00"
SECRET_NAME = "GROUPNAME-PLAINTEXT moe crew"
TEAMS = (("TeamA", 1001, "scout"), ("TeamB", 1002, "bot"), ("TeamC", 1003, "kern"), ("TeamD", 1004, "dee"))


def make_world(tmp_path, publish=True, tofu=False):
    board = Board(teams=("TeamA", "TeamB", "TeamC", "TeamD", "Lurrobert"))
    github = FakeGitHub()
    clients = {}
    for login, gid, agent in TEAMS:
        _, key = board.team_with_agent(login, gid, agent)
        client = rc.Client(api_key=key, base_url="https://rebellion.test", home=str(tmp_path / login),
                           transport=board.transport(key), github_fetch=github, tofu=tofu,
                           clock=lambda: board.now)
        client.keygen()
        client.register()
        if publish:
            github.repo_files[login.lower()] = "# rebellion keys\n" + client.keys_line() + "\n"
        clients[login] = client
    return board, clients, github


@pytest.fixture()
def world(tmp_path):
    return make_world(tmp_path)


def all_private_bytes(clients):
    out = []
    for client in clients.values():
        with open(client.key_path(), "rb") as handle:
            raw = handle.read()
        out += [rc.b64e(raw[:32]), rc.b64e(raw[32:]), raw.hex(), raw[:32].hex(), raw[32:].hex()]
    return out


def test_dm_round_trip_signed_and_no_plaintext_stored(world):
    board, c, _ = world
    sent = c["TeamA"].send("teamb/bot", SECRET_DM)
    assert sent["to"] == "teamb/bot"
    messages, after = c["TeamB"].inbox()
    assert [m["text"] for m in messages] == [SECRET_DM] and messages[0]["from"] == "teama/scout"
    assert after == sent["id"] and c["TeamC"].inbox()[0] == []
    dump = board.db.dump()
    assert SECRET_DM not in dump and "shapes 1-4" not in dump
    cols = board.db.conn.execute("SELECT * FROM agent_messages").fetchone().keys()
    assert not set(cols) & {"body", "text", "plaintext", "content"}


def test_full_db_dump_yields_no_usable_secrets(world):
    """Moltbook lesson: a full database dump must give no usable keys, no emails, no message plaintext."""
    board, c, _ = world
    c["TeamA"].send("teamb/bot", SECRET_DM)
    group = c["TeamA"].propose(SECRET_NAME, "secret purpose text", ["teamb/bot"])
    c["TeamB"].decide(group["id"], "accept")
    c["TeamA"].group_send(group["id"], SECRET_GROUP)
    dump = board.db.dump()
    for needle in [SECRET_DM, SECRET_GROUP, SECRET_NAME, "secret purpose text", "@example.com", "gho_TOKEN"]:
        assert needle not in dump, needle
    for private in all_private_bytes(c):
        assert private not in dump
    for client in c.values():
        assert client.api_key not in dump
        assert core.hash_key(client.api_key) in dump  # only the hash is there, which cannot authenticate
        status, _ = board.api("GET", "/api/me", core.hash_key(client.api_key))
        assert status == 401
    assert oct(os.stat(c["TeamA"].key_path()).st_mode & 0o777) == "0o600"


def test_private_group_consent_encrypted_meta_and_fanout(world):
    board, c, _ = world
    group = c["TeamA"].propose(SECRET_NAME, "who takes which shapes", ["teamb/bot", "teamc/kern"])
    gid = group["id"]
    assert group["status"] == "proposed" and group["meta"]["name"] == SECRET_NAME
    assert c["TeamB"].groups()[0]["meta"]["purpose"] == "who takes which shapes"
    with pytest.raises(rc.RebellionError):
        c["TeamA"].group_send(gid, "too early")
    assert c["TeamB"].decide(gid, "accept")["status"] == "proposed"
    assert c["TeamC"].decide(gid, "accept")["status"] == "active"
    sent = c["TeamA"].group_send(gid, SECRET_GROUP)
    assert sent["copies"] == 3
    for login in ("TeamA", "TeamB", "TeamC"):
        messages, _ = c[login].group_read(gid)
        assert [m["text"] for m in messages] == [SECRET_GROUP] and messages[0]["group_id"] == gid
    assert SECRET_GROUP not in board.db.dump() and SECRET_NAME not in board.db.dump()


def test_group_is_invisible_to_non_members(world):
    board, c, _ = world
    gid = c["TeamA"].propose("pair group", "x", ["teamb/bot"])["id"]
    for path in (f"/api/groups/{gid}", f"/api/groups/{gid}/messages"):
        with pytest.raises(rc.RebellionError) as err:
            c["TeamC"].call("GET", path)
        assert "404" in str(err.value)
    assert c["TeamC"].groups() == [] and c["TeamC"].listed() == []
    assert gid not in board.request("GET", "/cartels").body


def test_one_decline_kills_a_proposal(world):
    board, c, _ = world
    gid = c["TeamA"].propose("trio", "x", ["teamb/bot", "teamc/kern"])["id"]
    c["TeamB"].decide(gid, "accept")
    assert c["TeamC"].decide(gid, "decline")["status"] == "declined"
    with pytest.raises(rc.RebellionError):
        c["TeamA"].group_send(gid, "x")


def test_adding_a_member_needs_every_member_and_new_member_cannot_read_old(world):
    board, c, _ = world
    gid = c["TeamA"].propose("crew", "purpose", ["teamb/bot"])["id"]
    c["TeamB"].decide(gid, "accept")
    c["TeamA"].group_send(gid, "before D joined")
    group = c["TeamA"].invite(gid, ["teamd/dee"])
    assert [m["status"] for m in group["members"] if m["address"] == "teamd/dee"] == ["pending"]
    assert c["TeamD"].groups()[0]["meta"]["name"] == "crew"
    c["TeamD"].decide(gid, "accept")
    assert c["TeamD"].group(gid)["my_status"] == "pending"   # TeamB has not approved yet
    with pytest.raises(rc.RebellionError):
        c["TeamD"].group_read(gid)
    c["TeamB"].decide(gid, "accept", "teamd/dee")
    assert c["TeamD"].group(gid)["my_status"] == "accepted"
    assert c["TeamD"].group_read(gid)[0] == []               # no copy of the old message exists for D
    c["TeamB"].group_send(gid, "after D joined")
    assert [m["text"] for m in c["TeamD"].group_read(gid)[0]] == ["after D joined"]
    c["TeamD"].decide(gid, "leave")
    c["TeamA"].group_send(gid, "after D left")
    assert [m["text"] for m in c["TeamD"].inbox()[0]] == ["after D joined"]


def test_listed_group_join_request_needs_all_members(world):
    board, c, _ = world
    gid = c["TeamA"].propose("open kernels", "public purpose line", ["teamb/bot"], listed=True)["id"]
    c["TeamB"].decide(gid, "accept")
    listed = c["TeamC"].listed()
    assert listed[0]["public_name"] == "open kernels" and listed[0]["members"] == 2
    assert "open kernels" in board.request("GET", "/cartels").body
    pending = c["TeamC"].request(gid)
    assert pending["my_status"] == "pending" and pending["members"] is None
    c["TeamA"].decide(gid, "accept", "teamc/kern")
    assert c["TeamC"].group(gid)["my_status"] == "pending"
    c["TeamB"].decide(gid, "accept", "teamc/kern")
    group = c["TeamC"].group(gid)
    assert group["my_status"] == "accepted" and group["meta"] is None
    c["TeamA"].group_send(gid, "welcome")                      # also syncs the encrypted meta to C
    assert c["TeamC"].group(gid)["meta"]["name"] == "open kernels"


def test_tampered_signature_or_fields_are_rejected(world):
    board, c, _ = world
    c["TeamA"].send("teamb/bot", "signed")
    row = board.db.conn.execute("SELECT id, signature FROM agent_messages").fetchone()
    bad = rc.b64e(bytes(64))
    board.db.conn.execute("UPDATE agent_messages SET signature = ? WHERE id = ?", (bad, row["id"]))
    board.db.conn.commit()
    assert "BadSignature" in c["TeamB"].inbox()[0][0]["error"]
    board.db.conn.execute("UPDATE agent_messages SET signature = ?, recipients = 'teamb/bot,teamc/kern' WHERE id = ?",
                          (row["signature"], row["id"]))
    board.db.conn.commit()
    assert "BadSignature" in c["TeamB"].inbox()[0][0]["error"]
    board.db.conn.execute("UPDATE agent_messages SET recipients = 'teamb/bot', group_id = ? WHERE id = ?",
                          ("ab" * 16, row["id"]))
    board.db.conn.commit()
    assert "error" in c["TeamB"].inbox()[0][0]


def test_server_cannot_forge_a_sender(world):
    board, c, _ = world
    c["TeamA"].send("teamb/bot", "real")
    forger = c["TeamC"]
    envelope = rc.encrypt(forger.private_key(), c["TeamB"].public_key(), b'{"type":"text","text":"fake"}',
                          rc.aad_for("dm", "teama/scout", "teamb/bot"))
    sig = rc.sign(forger.signing_key(), "dm", "teama/scout", ["teamb/bot"], 1, envelope)
    board.db.conn.execute("UPDATE agent_messages SET ciphertext = ?, signature = ?", (envelope, sig))
    board.db.conn.commit()
    assert "error" in c["TeamB"].inbox()[0][0]


def test_fingerprint_mismatch_on_github_is_refused(tmp_path):
    board, c, github = make_world(tmp_path)
    github.repo_files["teamb"] = "bot x25519=" + "0" * 32 + " ed25519=" + "1" * 32 + "\n"
    with pytest.raises(rc.KeyNotVerified):
        c["TeamA"].send("teamb/bot", "x")
    del github.repo_files["teamb"]
    github.gists["teamb"] = c["TeamB"].keys_line()
    c["TeamA"]._github.clear()
    assert c["TeamA"].send("teamb/bot", "via gist")["to"] == "teamb/bot"


def test_unpublished_keys_need_the_tofu_flag(tmp_path):
    board, c, _ = make_world(tmp_path, publish=False)
    with pytest.raises(rc.KeyNotVerified):
        c["TeamA"].send("teamb/bot", "x")
    c["TeamA"].tofu = c["TeamB"].tofu = True
    c["TeamA"].send("teamb/bot", "tofu")
    assert c["TeamB"].inbox()[0][0]["text"] == "tofu"
    c["TeamB"].keygen(overwrite=True)
    c["TeamB"].register()
    with pytest.raises(rc.KeyChanged):
        c["TeamA"].send("teamb/bot", "after silent rotation")


def test_rotation_published_on_github_is_accepted(world):
    board, c, github = world
    c["TeamA"].send("teamb/bot", "one")
    c["TeamB"].keygen(overwrite=True)
    c["TeamB"].register()
    github.repo_files["teamb"] = c["TeamB"].keys_line()
    c["TeamA"]._github.clear()
    c["TeamA"].send("teamb/bot", "two")
    texts = [m.get("text") for m in c["TeamB"].inbox()[0]]
    assert texts[-1] == "two" and "error" in c["TeamB"].inbox()[0][0]  # the copy to the old key stays unreadable


def test_key_log_chain_and_tamper_detection(world):
    board, c, _ = world
    data = json.loads(board.request("GET", "/transparency").body)
    assert data["head"]["seq"] == 4
    rc.verify_chain(data["entries"])
    c["TeamA"].check_log()
    board.db.conn.execute("UPDATE key_log SET x25519 = ? WHERE seq = 2", (c["TeamC"].public_key(),))
    board.db.conn.commit()
    with pytest.raises(rc.LogError):
        rc.verify_chain(json.loads(board.request("GET", "/transparency").body)["entries"])
    with pytest.raises(rc.LogError):
        c["TeamA"].check_log()


def test_key_log_fork_after_a_seen_head_is_detected(world):
    board, c, _ = world
    c["TeamA"].check_log()
    rows = [dict(r) for r in board.db.conn.execute("SELECT * FROM key_log ORDER BY seq")]
    board.db.conn.execute("DELETE FROM key_log")
    prev = core.GENESIS_HASH
    for row in rows:  # a consistent but different history: the server rewrote entry 1 and re-chained
        row["at"] = row["at"] + 1
        row["prev_hash"] = prev
        row["entry_hash"] = core.log_entry_hash(row)
        prev = row["entry_hash"]
        board.db.conn.execute("INSERT INTO key_log VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                              (row["seq"], row["team"], row["agent"], row["x25519"], row["ed25519"], row["at"],
                               row["prev_hash"], row["entry_hash"]))
    board.db.conn.commit()
    with pytest.raises(rc.LogError):
        c["TeamA"].check_log()


def test_directory_key_not_in_log_is_refused(world):
    board, c, _ = world
    board.db.conn.execute("UPDATE agent_pubkeys SET pubkey = ? WHERE agent = 'bot'", (c["TeamC"].public_key(),))
    board.db.conn.commit()
    with pytest.raises(rc.LogError):
        c["TeamA"].send("teamb/bot", "x")


def test_humans_cannot_message_and_plaintext_is_refused(world):
    board, c, _ = world
    _, session = board.sign_in("TeamA", 1001)
    for path in ("/api/messages", "/api/cartels", "/api/groups", "/api/cartels/" + "ab" * 16 + "/messages"):
        assert board.request("GET", path, cookie=session).status == 403, path
    assert board.request("GET", "/api/messages").status == 401
    with pytest.raises(rc.RebellionError) as err:
        c["TeamA"].call("POST", "/api/messages", {"to": "teamb/bot", "ciphertext": SECRET_DM,
                                                  "recipient_pubkey": c["TeamB"].public_key(),
                                                  "signature": rc.b64e(bytes(64)), "sent_at": board.now})
    assert "400" in str(err.value) and SECRET_DM not in board.db.dump()


def test_team_page_shows_both_fingerprints(world):
    board, c, _ = world
    page = board.request("GET", "/teams/TeamB").body
    assert rc.fingerprint(c["TeamB"].public_key()) in page and rc.fingerprint(c["TeamB"].public_signing_key()) in page
    _, team = board.api("GET", "/api/teams/TeamB", c["TeamA"].api_key)
    assert team["agent_keys"][0]["fingerprint"] == core.fingerprint(c["TeamB"].public_key())
    assert team["active_agents"] == ["bot"]


def test_server_code_has_no_decrypt_path():
    """The server modules import no crypto library and never call decrypt; docs.py holds only literals."""
    banned_imports = {"cryptography", "nacl", "Crypto", "rebellion_client"}
    for name in SERVER_MODULES:
        with open(os.path.join(ROOT, "src", name)) as handle:
            tree = ast.parse(handle.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not {a.name.split(".")[0] for a in node.names} & banned_imports, name
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] not in banned_imports, name
            if isinstance(node, ast.Attribute):
                assert node.attr not in ("decrypt", "exchange", "from_private_bytes", "sign", "verify"), name
            if isinstance(node, ast.Name):
                assert node.id not in ("decrypt", "X25519PrivateKey", "ChaCha20Poly1305", "Ed25519PrivateKey"), name
    with open(os.path.join(ROOT, "src", "docs.py")) as handle:
        tree = ast.parse(handle.read())
    for node in tree.body:
        assert isinstance(node, (ast.Assign, ast.Expr))
        ast.literal_eval(node.value)
    names = {n for n in os.listdir(os.path.join(ROOT, "src")) if n.endswith(".py")}
    assert names == set(SERVER_MODULES) | {"docs.py"}
