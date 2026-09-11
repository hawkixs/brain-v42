"""Executable boundary: private explicit configuration and safe failure output."""

import json
import os
import subprocess
import sys

import pytest
from pydantic import ValidationError

from brain_v42.delivery_config import DeliverySettings

TOKEN = "observer-fixture-credential-never-print"
PG = "postgresql+asyncpg://observer:db-fixture-secret@127.0.0.1:1/unused"


def private_config(tmp_path, content):
    path = tmp_path / "observer.env"
    path.write_text(content)
    path.chmod(0o600)
    return path


def cli_environment():
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("BRAIN_", "GITHUB_", "GH_", "POSTGRES_"))
    }


def invoke(*args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "brain_v42.delivery_observer", *map(str, args)],
        env=cli_environment() if env is None else env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


def test_module_help_does_not_need_credentials_or_database(tmp_path):
    env = cli_environment()
    env.update(BRAIN_DELIVERY_OBSERVER_ENV_PATH=str(tmp_path / "missing"))
    result = invoke("--help", env=env)
    assert result.returncode == 0, result.stderr
    assert "--once" in result.stdout and "--project-key" in result.stdout
    assert "--env-file" in result.stdout
    assert not result.stderr


@pytest.mark.parametrize(
    "content",
    [
        f"BRAIN_DELIVERY_ENABLED=true\nBRAIN_DELIVERY_GITHUB_TOKEN={TOKEN}\n",
        f"BRAIN_DELIVERY_ENABLED=true\nBRAIN_DELIVERY_POSTGRES_URL={PG}\n",
        f"BRAIN_DELIVERY_ENABLED=false\nBRAIN_DELIVERY_POSTGRES_URL={PG}\nBRAIN_DELIVERY_GITHUB_TOKEN={TOKEN}\n",
        f"BRAIN_DELIVERY_ENABLED=true\nBRAIN_DELIVERY_POSTGRES_URL=sqlite:///{TOKEN}\nBRAIN_DELIVERY_GITHUB_TOKEN={TOKEN}\n",
        f"BRAIN_DELIVERY_ENABLED=true\nBRAIN_DELIVERY_POSTGRES_URL={PG}\nBRAIN_DELIVERY_GITHUB_TOKEN={TOKEN}\nBRAIN_DELIVERY_GITHUB_APP_ID=12\n",
        f"BRAIN_DELIVERY_ENABLED=true\nBRAIN_DELIVERY_POSTGRES_URL={PG}\nBRAIN_DELIVERY_GITHUB_TOKEN={TOKEN}\nBRAIN_DELIVERY_POLL_SECONDS={TOKEN}\n",
    ],
)
def test_invalid_explicit_configuration_refuses_safely(tmp_path, content):
    result = invoke("--once", "--env-file", private_config(tmp_path, content))
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload == {"exit_code": 2, "error_code": "observer_configuration_invalid"}
    assert not result.stderr
    assert TOKEN not in result.stdout and "db-fixture-secret" not in result.stdout


@pytest.mark.parametrize("mode", ["absent", "public", "symlink", "duplicate", "unknown"])
def test_untrusted_private_configuration_is_rejected(tmp_path, mode):
    path = private_config(
        tmp_path,
        f"BRAIN_DELIVERY_ENABLED=true\nBRAIN_DELIVERY_POSTGRES_URL={PG}\nBRAIN_DELIVERY_GITHUB_TOKEN={TOKEN}\n",
    )
    if mode == "absent":
        path.unlink()
    elif mode == "public":
        path.chmod(0o644)
    elif mode == "symlink":
        link = tmp_path / "link.env"
        link.symlink_to(path)
        path = link
    elif mode == "duplicate":
        path.write_text(path.read_text() + f"BRAIN_DELIVERY_GITHUB_TOKEN={TOKEN}\n")
    else:
        path.write_text(path.read_text() + f"UNEXPECTED={TOKEN}\n")
    result = invoke("--once", "--env-file", path)
    assert result.returncode == 2
    assert json.loads(result.stdout)["error_code"] == "observer_configuration_invalid"
    assert not result.stderr and TOKEN not in result.stdout


def test_settings_keep_database_url_explicit_and_redacted(monkeypatch):
    monkeypatch.delenv("BRAIN_DELIVERY_POSTGRES_URL", raising=False)
    assert DeliverySettings().postgres_url is None
    settings = DeliverySettings(postgres_url=PG)
    assert settings.postgres_url.get_secret_value() == PG
    assert "db-fixture-secret" not in repr(settings)
    assert "db-fixture-secret" not in settings.model_dump_json()


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///secret",
        "https://host/db",
        "postgresql+asyncpg:///",
        "postgresql+asyncpg://host/",
    ],
)
def test_settings_reject_non_explicit_async_postgres_urls(url):
    with pytest.raises(ValidationError):
        DeliverySettings(postgres_url=url)


@pytest.mark.parametrize("scheme", ["postgres", "postgresql"])
def test_explicit_postgres_url_normalizes_the_async_driver(scheme):
    settings = DeliverySettings(postgres_url=PG.replace("postgresql+asyncpg", scheme))
    assert settings.postgres_url.get_secret_value() == PG


def test_loader_uses_deliberate_postgres_fallback_and_file_registry(tmp_path, monkeypatch):
    from brain_v42.delivery_observer.config import load_observer_settings

    monkeypatch.setenv("POSTGRES_URL", PG)
    path = private_config(
        tmp_path,
        f"BRAIN_DELIVERY_ENABLED=true\nBRAIN_DELIVERY_GITHUB_TOKEN={TOKEN}\n"
        'BRAIN_DELIVERY_REPOSITORY_REGISTRY={"executor":{"123":"owner/repo"}}\n',
    )
    settings = load_observer_settings(path)
    assert settings.postgres_url.get_secret_value() == PG
    assert settings.repositories_for("executor") == {123: "owner/repo"}
    assert settings.observer_env_path == path


def test_loader_pins_actual_private_path_and_file_database_before_ambient_values(
    tmp_path, monkeypatch
):
    from brain_v42.delivery_observer.config import load_observer_settings

    monkeypatch.setenv("BRAIN_DELIVERY_POSTGRES_URL", "postgresql+asyncpg://other/other")
    path = private_config(
        tmp_path,
        f"BRAIN_DELIVERY_ENABLED=true\nBRAIN_DELIVERY_GITHUB_TOKEN={TOKEN}\nBRAIN_DELIVERY_POSTGRES_URL={PG}\n"
        "BRAIN_DELIVERY_OBSERVER_ENV_PATH=/do/not/read/other.env\n",
    )
    settings = load_observer_settings(path)
    assert settings.postgres_url.get_secret_value() == PG
    assert settings.observer_env_path == path


def test_lazy_database_graph_exports_keep_the_existing_public_functions():
    import brain_v42.db as db
    import brain_v42.db.neo4j as graph

    for name in ("close_neo4j_driver", "create_neo4j_driver", "neo4j_healthcheck"):
        assert getattr(db, name) is getattr(graph, name)
    with pytest.raises(AttributeError):
        _ = db.unknown_graph_function


@pytest.fixture(scope="module")
def app_key_pem():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def app_config(tmp_path, pem, *, extra=""):
    key = tmp_path / "app.pem"
    key.write_text(pem)
    key.chmod(0o600)
    path = private_config(
        tmp_path,
        f"BRAIN_DELIVERY_ENABLED=true\nBRAIN_DELIVERY_POSTGRES_URL={PG}\n"
        "BRAIN_DELIVERY_GITHUB_APP_ID=4907416\nBRAIN_DELIVERY_GITHUB_INSTALLATION_ID=160825374\n"
        f"BRAIN_DELIVERY_GITHUB_PRIVATE_KEY_PATH={key}\n{extra}",
    )
    return path, key


def test_loader_accepts_a_complete_app_triplet_from_the_private_file(tmp_path, app_key_pem):
    from brain_v42.delivery_observer.config import load_observer_settings

    path, key = app_config(tmp_path, app_key_pem)
    settings = load_observer_settings(path)
    assert settings.github_app_id == 4907416
    assert settings.github_installation_id == 160825374
    assert settings.github_private_key_path == key
    assert settings.github_token.get_secret_value() == ""
    assert settings.observer_env_path == path


def test_loader_names_the_incomplete_app_triplet(tmp_path):
    from brain_v42.delivery_observer.config import load_observer_settings

    path = private_config(
        tmp_path,
        f"BRAIN_DELIVERY_ENABLED=true\nBRAIN_DELIVERY_POSTGRES_URL={PG}\n"
        "BRAIN_DELIVERY_GITHUB_APP_ID=12\n",
    )
    with pytest.raises(ValueError, match="observer application credentials are incomplete"):
        load_observer_settings(path)


@pytest.mark.parametrize("mode", ["public", "symlink"])
def test_loader_refuses_an_unsafe_app_key_file(tmp_path, app_key_pem, mode):
    from brain_v42.delivery_observer.config import load_observer_settings
    from brain_v42.models.delivery import DeliveryError

    path, key = app_config(tmp_path, app_key_pem)
    if mode == "public":
        key.chmod(0o644)
    else:
        target = tmp_path / "real.pem"
        key.rename(target)
        key.symlink_to(target)
    with pytest.raises(DeliveryError, match="provider_forbidden"):
        load_observer_settings(path)


def test_loader_refuses_a_pat_coexisting_with_app_credentials(tmp_path, app_key_pem):
    from brain_v42.delivery_observer.config import load_observer_settings

    path, _ = app_config(tmp_path, app_key_pem, extra=f"BRAIN_DELIVERY_GITHUB_TOKEN={TOKEN}\n")
    with pytest.raises(ValueError, match="observer credentials are ambiguous"):
        load_observer_settings(path)


def test_cli_refuses_a_pat_coexisting_with_complete_app_credentials(tmp_path, app_key_pem):
    path, _ = app_config(tmp_path, app_key_pem, extra=f"BRAIN_DELIVERY_GITHUB_TOKEN={TOKEN}\n")
    result = invoke("--once", "--env-file", path)
    assert result.returncode == 2
    assert json.loads(result.stdout) == {
        "exit_code": 2,
        "error_code": "observer_configuration_invalid",
    }
    assert not result.stderr and TOKEN not in result.stdout
