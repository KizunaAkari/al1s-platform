import hashlib
import hmac


def derive_quant_credential_key(master_key: str) -> str:
    if len(master_key.encode()) < 32:
        raise ValueError("quant_credential_key_invalid")
    return hmac.new(master_key.encode(), b"al1s-quant-credential-v1", hashlib.sha256).hexdigest()
