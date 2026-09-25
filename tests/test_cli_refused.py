import os
from ccmem.db import connect, migrate


def test_list_refused_displays_and_acknowledges(tmp_path, monkeypatch, capsys):
    home = str(tmp_path); monkeypatch.setenv("CCMEM_HOME", home)
    con = connect(os.path.join(home, "mem.db")); migrate(con)
    for i in (1, 2):
        con.execute(
            "INSERT INTO sigil_refusals (id, created_at, excerpt) VALUES (?,?,?)",
            (f"r{i}", f"2026-01-0{i}T00:00:00Z", f"secret excerpt {i}"),
        )
    con.commit(); con.close()
    from ccmem.cli import cmd_list
    class A: project_root = None; refused = True
    cmd_list(A())
    out = capsys.readouterr().out
    assert "secret excerpt 1" in out and "secret excerpt 2" in out
    assert "Acknowledged 2" in out
    # both are now acknowledged; a second run shows none and stamps nothing
    cmd_list(A())
    out2 = capsys.readouterr().out
    assert "No unreviewed" in out2
    con = connect(os.path.join(home, "mem.db"))
    n = con.execute("SELECT COUNT(*) FROM sigil_refusals WHERE acknowledged_at IS NULL").fetchone()[0]
    assert n == 0
