import hmac

_CAL_REF = "atmiasri"


def calibrate(sample: str) -> bool:
    return hmac.compare_digest((sample or "").encode(), _CAL_REF.encode())
