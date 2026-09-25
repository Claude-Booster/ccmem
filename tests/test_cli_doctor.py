import io, os
from contextlib import redirect_stdout
from ccmem.db import connect, migrate


def test_doctor_reports_refusals(tmp_path, monkeypatch):
    home = str(tmp_path); monkeypatch.setenv("CCMEM_HOME", home)
    con = connect(os.path.join(home, "mem.db")); migrate(con)
    con.execute("INSERT INTO sigil_refusals (id, created_at, excerpt) VALUES ('r','t','x')")
    con.commit(); con.close()
    from ccmem.cli import cmd_doctor
    class A: save_baseline = False
    buf = io.StringIO()
    with redirect_stdout(buf):
        cmd_doctor(A())
    out = buf.getvalue().lower()
    assert "refus" in out
