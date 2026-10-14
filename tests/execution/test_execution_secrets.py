import base64
import importlib
import importlib.util

import pytest
from pydantic import SecretStr

from agent_runtime.settings import RuntimeSettings


def _decryptor():
    spec = importlib.util.find_spec("agent_runtime.execution.secrets")
    assert spec is not None, "execution credential decryptor is missing"
    return importlib.import_module("agent_runtime.execution.secrets")


def test_runtime_decrypts_shared_go_execution_credential_envelope():
    decryptor = _decryptor()
    settings = RuntimeSettings(
        _env_file=None,
        execution_encryption_key=SecretStr(bytes(range(32)).hex()),
    )

    value = decryptor.decrypt_execution_credential(
        "AAECAwQFBgcICQoLN3C5bayBp2mgMvLow4wMDbA9ISyez8BLVpsdh2sZiw==",
        settings.execution_encryption_key,
    )

    assert value == "provider-secret"


def test_runtime_rejects_tampered_execution_credential_envelope():
    decryptor = _decryptor()
    settings = RuntimeSettings(_env_file=None, execution_encryption_key=SecretStr(bytes(range(32)).hex()))
    envelope = bytearray(base64.b64decode("AAECAwQFBgcICQoLN3C5bayBp2mgMvLow4wMDbA9ISyez8BLVpsdh2sZiw=="))
    envelope[-1] ^= 1

    with pytest.raises(ValueError):
        decryptor.decrypt_execution_credential(base64.b64encode(envelope).decode(), settings.execution_encryption_key)
