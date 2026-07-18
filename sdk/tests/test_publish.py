"""Tests for the remote-jobs publish CLI: image refs, digests, manifest, upsert."""

from __future__ import annotations

import textwrap

import pytest

from conftest import FakeResponse, FakeSession
from nautobot_remote_jobs_sdk.cli import build_parser, main
from nautobot_remote_jobs_sdk.cli.publish import (
    DIGEST_RE,
    PublishError,
    build_payload,
    load_manifest,
    parse_image_ref,
    resolve_digest,
    upsert_job_definition,
)

GOOD_DIGEST = "sha256:" + "a1" * 32

MANIFEST_YAML = textwrap.dedent(
    """
    name: rotate-local-admin
    description: Rotate local admin passwords on network devices
    image: registry.example.com/jobs/rotate-admin
    zone_policy: per_device
    capabilities: [ssh-access]
    secrets_groups: [tacacs-prod]
    timeout_seconds: 1800
    dryrun_supported: true
    requires_zone_local: true
    inputs:
      type: object
      required: [devices]
      properties:
        devices:
          type: array
          items: {type: string, format: uuid}
          x-remote-jobs-target: device
        commit:
          type: boolean
          default: false
    """
)


# -- digest regex ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("digest", "ok"),
    [
        (GOOD_DIGEST, True),
        ("sha256:" + "0" * 64, True),
        ("sha256:" + "0" * 63, False),
        ("sha256:" + "g" * 64, False),
        ("sha512:" + "0" * 64, False),
        ("0" * 64, False),
        ("sha256:" + "A" * 64, False),  # uppercase hex not allowed
    ],
)
def test_digest_regex(digest, ok):
    assert bool(DIGEST_RE.match(digest)) is ok


# -- image reference parsing -----------------------------------------------------


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        (
            "registry.example.com/jobs/rotate-admin:1.4.0",
            ("registry.example.com", "jobs/rotate-admin", "1.4.0", None),
        ),
        ("alpine", ("docker.io", "library/alpine", "latest", None)),
        ("networktocode/nautobot:2.4", ("docker.io", "networktocode/nautobot", "2.4", None)),
        ("localhost:5000/img:dev", ("localhost:5000", "img", "dev", None)),
        (
            f"registry.example.com/jobs/x:1.0@{GOOD_DIGEST}",
            ("registry.example.com", "jobs/x", "1.0", GOOD_DIGEST),
        ),
        ("ghcr.io/org/img", ("ghcr.io", "org/img", "latest", None)),
    ],
)
def test_parse_image_ref(image, expected):
    assert parse_image_ref(image) == expected


# -- digest resolution ------------------------------------------------------------


def test_resolve_digest_plain_200(fake_session):
    fake_session.add(
        "HEAD",
        "/v2/jobs/rotate-admin/manifests/1.4.0",
        FakeResponse(headers={"Docker-Content-Digest": GOOD_DIGEST}),
    )
    digest = resolve_digest("registry.example.com/jobs/rotate-admin:1.4.0", session=fake_session)
    assert digest == GOOD_DIGEST
    (_, url, _) = fake_session.calls[0]
    assert url == "https://registry.example.com/v2/jobs/rotate-admin/manifests/1.4.0"


def test_resolve_digest_bearer_flow_docker_hub(fake_session):
    """Anonymous Docker Hub: 401 challenge -> token fetch -> retry with Bearer."""
    challenge = (
        'Bearer realm="https://auth.docker.io/token",'
        'service="registry.docker.io"'
    )
    state = {"heads": 0}

    def head_handler(method, url, **kwargs):
        state["heads"] += 1
        if state["heads"] == 1:
            return FakeResponse(status_code=401, headers={"WWW-Authenticate": challenge})
        assert kwargs["headers"]["Authorization"] == "Bearer hub-token"
        return FakeResponse(headers={"Docker-Content-Digest": GOOD_DIGEST})

    fake_session.add("HEAD", "registry-1.docker.io/v2/library/alpine/manifests/3.19", head_handler)
    fake_session.add(
        "GET", "auth.docker.io/token", FakeResponse(json_data={"token": "hub-token"})
    )

    assert resolve_digest("alpine:3.19", session=fake_session) == GOOD_DIGEST
    token_call = fake_session.calls_for("GET", "auth.docker.io/token")[0]
    assert token_call[2]["params"]["scope"] == "repository:library/alpine:pull"
    assert token_call[2]["params"]["service"] == "registry.docker.io"


def test_resolve_digest_uses_existing_digest():
    assert resolve_digest(f"reg.example.com/img:1@{GOOD_DIGEST}", session=None) == GOOD_DIGEST


def test_resolve_digest_missing_header(fake_session):
    fake_session.add("HEAD", "/v2/img/manifests/latest", FakeResponse(headers={}))
    with pytest.raises(PublishError, match="Docker-Content-Digest"):
        resolve_digest("reg.example.com/img", session=fake_session)


def test_resolve_digest_http_error(fake_session):
    fake_session.add("HEAD", "/v2/img/manifests/latest", FakeResponse(status_code=404))
    with pytest.raises(PublishError, match="HTTP 404"):
        resolve_digest("reg.example.com/img", session=fake_session)


# -- manifest parsing / payload ----------------------------------------------------


def test_load_manifest_and_build_payload(tmp_path):
    manifest_path = tmp_path / "remote-job.yaml"
    manifest_path.write_text(MANIFEST_YAML)
    manifest = load_manifest(str(manifest_path))
    payload = build_payload(
        manifest, "registry.example.com/jobs/rotate-admin:1.4.0", GOOD_DIGEST
    )

    assert payload["name"] == "rotate-local-admin"
    assert payload["image"] == "registry.example.com/jobs/rotate-admin:1.4.0"
    assert payload["image_digest"] == GOOD_DIGEST
    assert payload["zone_policy"] == "per_device"
    assert payload["capabilities"] == ["ssh-access"]
    assert payload["secrets_groups"] == [{"name": "tacacs-prod"}]
    assert payload["timeout_seconds"] == 1800
    assert payload["dryrun_supported"] is True
    assert payload["requires_zone_local"] is True
    assert payload["input_schema"]["required"] == ["devices"]
    prop = payload["input_schema"]["properties"]["devices"]
    assert prop["x-remote-jobs-target"] == "device"
    assert "inputs" not in payload


def test_load_manifest_requires_name(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("description: no name here\n")
    with pytest.raises(PublishError, match="name"):
        load_manifest(str(path))


def test_load_manifest_missing_file(tmp_path):
    with pytest.raises(PublishError, match="Cannot read"):
        load_manifest(str(tmp_path / "nope.yaml"))


def test_build_payload_rejects_bad_digest(tmp_path):
    with pytest.raises(PublishError, match="Invalid image digest"):
        build_payload({"name": "x"}, "img", "sha256:short")


# -- upsert -------------------------------------------------------------------------


def test_upsert_creates_when_absent(fake_session):
    fake_session.add(
        "GET", "/job-definitions/", FakeResponse(json_data={"results": []})
    )
    fake_session.add(
        "POST", "/job-definitions/", FakeResponse(status_code=201, json_data={"id": "new-id"})
    )
    action, record = upsert_job_definition(
        fake_session, "https://nautobot.example.com", {"name": "rotate-local-admin"}
    )
    assert action == "created"
    assert record["id"] == "new-id"


def test_upsert_patches_when_present(fake_session):
    fake_session.add(
        "GET", "/job-definitions/", FakeResponse(json_data={"results": [{"id": "old-id"}]})
    )
    fake_session.add(
        "PATCH", "/job-definitions/old-id/", FakeResponse(json_data={"id": "old-id"})
    )
    action, record = upsert_job_definition(
        fake_session, "https://nautobot.example.com", {"name": "rotate-local-admin"}
    )
    assert action == "updated"
    assert record["id"] == "old-id"
    assert fake_session.calls_for("PATCH", "/job-definitions/old-id/")


# -- CLI wiring ---------------------------------------------------------------------


def test_cli_requires_image():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["publish"])


def test_cli_publish_missing_url_is_error(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("NAUTOBOT_URL", raising=False)
    monkeypatch.delenv("NAUTOBOT_TOKEN", raising=False)
    manifest = tmp_path / "remote-job.yaml"
    manifest.write_text(MANIFEST_YAML)
    exit_code = main(
        ["publish", "--image", "reg.example.com/img:1", "--manifest", str(manifest)]
    )
    assert exit_code == 1
    assert "NAUTOBOT_URL" in capsys.readouterr().err


def test_cli_publish_rejects_malformed_digest_override(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NAUTOBOT_URL", "https://nautobot.example.com")
    monkeypatch.setenv("NAUTOBOT_TOKEN", "tok")
    manifest = tmp_path / "remote-job.yaml"
    manifest.write_text(MANIFEST_YAML)
    exit_code = main(
        [
            "publish",
            "--image",
            "reg.example.com/img:1",
            "--manifest",
            str(manifest),
            "--digest",
            "sha256:nothex",
        ]
    )
    assert exit_code == 1
    assert "sha256" in capsys.readouterr().err
