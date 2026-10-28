from sqlalchemy import create_engine
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from al1s.adapters.postgres.quant_credentials import QuantCredentials, derive_quant_credential_key
from al1s.adapters.postgres.quant_models import QuantProviderCredentialRow
from al1s.secrets.security import FernetSecretCipher


def test_quant_credentials_are_encrypted_isolated_and_removable():
    engine = create_engine("sqlite://")
    QuantProviderCredentialRow.__table__.create(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    master = "master-key-" * 5
    key = derive_quant_credential_key(master)
    vault = QuantCredentials(sessions, FernetSecretCipher(key))
    vault.save("tushare", "private-token")
    with sessions() as session:
        row = session.get(QuantProviderCredentialRow, "tushare")
        assert "private-token" not in row.ciphertext
        assert row.key_id != FernetSecretCipher(master).key_id
    assert vault.get("tushare") == "private-token" and vault.get("gugudata") is None
    assert vault.configured() == {"tushare"}
    vault.save("tushare", None)
    assert vault.get("tushare") is None


def test_credentials_snapshot_loads_all_sources_in_one_query():
    engine = create_engine("sqlite://")
    QuantProviderCredentialRow.__table__.create(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    vault = QuantCredentials(sessions, FernetSecretCipher(derive_quant_credential_key("x" * 40)))
    vault.save("tushare", "token-one")
    vault.save("gugudata", "token-two")
    queries = []
    event.listen(engine, "before_cursor_execute", lambda *_args: queries.append(1))
    assert vault.snapshot() == {"tushare": "token-one", "gugudata": "token-two"}
    assert len(queries) == 1
