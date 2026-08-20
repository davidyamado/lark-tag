from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_docker_image_bakes_s3_access_reader_skill():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert "COPY docs/s3-access-reader/s3-access-reader/ /opt/s3-access-reader-skill/" in dockerfile
    assert "!docs/s3-access-reader/s3-access-reader/**" in dockerignore


def test_entrypoint_installs_s3_access_reader_skill():
    entrypoint = (ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")

    assert 'S3_ACCESS_READER_SKILL_DIR="${CLAUDE_DIR}/skills/s3-access-reader"' in entrypoint
    assert "cp -r /opt/s3-access-reader-skill/." in entrypoint
    assert 'chown -R botuser:botuser "${S3_ACCESS_READER_SKILL_DIR}"' in entrypoint


def test_s3_access_reader_skill_uses_container_paths():
    skill = (ROOT / "docs/s3-access-reader/s3-access-reader/SKILL.md").read_text(encoding="utf-8")

    assert r".\.venv\Scripts\python.exe" not in skill
    assert 'python3 "$HOME/.claude/skills/s3-access-reader/scripts/s3_access_call.py"' in skill
    assert "USER_ACCESS_TOKEN_FILE" in skill


def test_s3_access_call_reads_tokens_from_file_env(monkeypatch, tmp_path):
    import importlib.util

    script_path = ROOT / "docs/s3-access-reader/s3-access-reader/scripts/s3_access_call.py"
    spec = importlib.util.spec_from_file_location("s3_access_call", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    token_file = tmp_path / "user-token.txt"
    token_file.write_text(" user-token-from-file \n", encoding="utf-8")
    monkeypatch.delenv("USER_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("USER_ACCESS_TOKEN_FILE", str(token_file))

    assert module._secret_first("USER_ACCESS_TOKEN") == "user-token-from-file"


def test_s3_access_call_rejects_missing_token_file(monkeypatch, tmp_path):
    import importlib.util

    script_path = ROOT / "docs/s3-access-reader/s3-access-reader/scripts/s3_access_call.py"
    spec = importlib.util.spec_from_file_location("s3_access_call_missing_file", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    monkeypatch.delenv("USER_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("USER_ACCESS_TOKEN_FILE", str(tmp_path / "missing.txt"))

    with pytest.raises(RuntimeError, match="USER_ACCESS_TOKEN_FILE"):
        module._secret_first("USER_ACCESS_TOKEN")
