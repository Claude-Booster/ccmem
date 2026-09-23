from ccmem.redact import redact


def test_strips_anthropic_key():
    out = redact("use sk-ant-api03-AAAAbbbbCCCCddddEEEEffffGGGG for the call")
    assert "sk-ant-api03-" not in out


def test_strips_openai_key():
    out = redact("OPENAI_API_KEY=sk-proj-1234567890abcdefghijklmn")
    assert "sk-proj-" not in out


def test_strips_aws_key():
    out = redact("AKIAIOSFODNN7EXAMPLE / wJalrXUtnFEMI")
    assert "AKIAIOSFODNN7EXAMPLE" not in out


def test_strips_github_pat():
    out = redact("token ghp_16CharsOfNonsense0000000000000000")
    assert "ghp_" not in out


def test_strips_slack_token():
    out = redact("xoxb-REDACTED-TEST-FIXTURE")
    assert "xoxb-" not in out


def test_strips_private_key():
    out = redact("-----BEGIN RSA PRIVATE KEY-----\nMIIEow==\n-----END RSA PRIVATE KEY-----")
    assert "MIIEow" not in out


def test_strips_pg_url_password():
    out = redact("postgres://admin:hunter2@db.internal:5432/prod")
    assert "hunter2" not in out


def test_strips_private_tag():
    out = redact("keep this <private>my therapist's name is Dana</private> out")
    assert "Dana" not in out


def test_strips_multiline_private():
    out = redact("a\n<private>\nline one\nline two\n</private>\nb")
    assert "line two" not in out


def test_leaves_benign_text_intact():
    t = "we chose RLS BEFORE INSERT triggers over app-layer limits"
    assert redact(t) == t


def test_idempotent():
    text = "use sk-ant-api03-AAAAbbbbCCCC for the call"
    once = redact(text)
    assert redact(once) == once
