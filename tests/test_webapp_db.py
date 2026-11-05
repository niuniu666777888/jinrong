import db


def test_new_id_has_prefix():
    value = db.new_id("s")
    assert value.startswith("s_")
    assert len(value) == 14


def test_init_and_session_roundtrip(tmp_path):
    db.configure(str(tmp_path / "app.db"))
    db.init_db()

    created = db.create_session()
    session_id = created["session_id"]

    loaded = db.get_session(session_id)
    assert loaded is not None
    assert loaded["id"] == session_id
    assert loaded["title"] == "新会话"
