import json

from tests.test_chat_sessions import AUTHOR, new_session


def add_paths(client, sid, paths, text=""):
    return client.post(f"/api/sessions/{sid}/messages/paths", json={"paths": paths, "text": text}, headers=AUTHOR)


def test_adds_a_file_by_path_and_confirms_receipt(client, project_id, tmp_path):
    doc = tmp_path / "design.sql"
    doc.write_text("SELECT 1;", encoding="utf-8")
    sid = new_session(client, project_id)
    r = add_paths(client, sid, [str(doc)])
    assert r.status_code == 201
    user_msg, confirmation = r.json()["messages"]
    assert json.loads(user_msg["attachments_json"])[0]["filename"] == "design.sql"
    assert "Received 1 file" in confirmation["content"] and "readable" in confirmation["content"]
    assert len(client.get(f"/api/sessions/{sid}").json()["files"]) == 1


def test_quoted_paths_from_explorer_copy_as_path_work_one_per_line(client, project_id, tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("alpha", encoding="utf-8")
    b.write_text("bravo", encoding="utf-8")
    sid = new_session(client, project_id)
    r = add_paths(client, sid, [f'"{a}"\n"{b}"'])
    assert "Received 2 files" in r.json()["messages"][1]["content"]


def test_a_folder_adds_its_direct_files_only(client, project_id, tmp_path):
    folder = tmp_path / "docs"
    (folder / "nested").mkdir(parents=True)
    (folder / "one.txt").write_text("1", encoding="utf-8")
    (folder / "two.txt").write_text("2", encoding="utf-8")
    (folder / "nested" / "deep.txt").write_text("3", encoding="utf-8")
    sid = new_session(client, project_id)
    r = add_paths(client, sid, [str(folder)])
    names = sorted(a["filename"] for a in json.loads(r.json()["messages"][0]["attachments_json"]))
    assert names == ["one.txt", "two.txt"]


def test_missing_path_is_a_clear_error(client, project_id, tmp_path):
    sid = new_session(client, project_id)
    r = add_paths(client, sid, [str(tmp_path / "nope.docx")])
    assert r.status_code == 422 and "not found" in r.json()["detail"]


def test_good_and_bad_paths_mix_reports_the_skipped_one(client, project_id, tmp_path):
    good = tmp_path / "ok.txt"
    good.write_text("fine", encoding="utf-8")
    sid = new_session(client, project_id)
    r = add_paths(client, sid, [str(good), str(tmp_path / "missing.txt")])
    assert r.status_code == 201 and "Skipped: not found" in r.json()["messages"][0]["content"]


def test_credential_files_are_refused(client, project_id, tmp_path):
    secret = tmp_path / ".env"
    secret.write_text("ANTHROPIC_API_KEY=sk-ant-x", encoding="utf-8")
    key = tmp_path / "server.pem"
    key.write_text("-----BEGIN PRIVATE KEY-----", encoding="utf-8")
    sid = new_session(client, project_id)
    r = add_paths(client, sid, [str(secret), str(key)])
    assert r.status_code == 422 and "not allowed" in r.json()["detail"]
    assert client.get(f"/api/sessions/{sid}").json()["files"] == []


def test_empty_input_and_locked_step(client, project_id, tmp_path):
    sid = new_session(client, project_id)
    assert add_paths(client, sid, ["  \n "]).status_code == 422
    doc = tmp_path / "x.txt"
    doc.write_text("x", encoding="utf-8")
    client.post(f"/api/sessions/{sid}/messages", data={"text": "hi"}, headers=AUTHOR)
    assert add_paths(client, 9999, [str(doc)]).status_code == 404
