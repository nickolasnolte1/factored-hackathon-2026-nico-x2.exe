"""Id factory (CONTRACT.md section 3.1): `secrets` in dev and demo, a seeded generator in test and eval."""
import base64
import random
import secrets
import string

ALNUM = string.ascii_uppercase + string.digits


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class IdFactory:
    def __init__(self, seed=None):
        self._rng = random.Random(seed) if seed is not None else None

    @property
    def seeded(self):
        return self._rng is not None

    def _chars(self, alphabet, n):
        if self._rng is None:
            return "".join(secrets.choice(alphabet) for _ in range(n))
        return "".join(self._rng.choice(alphabet) for _ in range(n))

    def _bytes(self, n):
        return secrets.token_bytes(n) if self._rng is None else self._rng.randbytes(n)

    def code(self, prefix="", n=12):
        """prefix + n characters of [A-Z0-9] (CHL-, DSP-, HND-, draft ids)."""
        return prefix + self._chars(ALNUM, n)

    def tool_call_id(self):
        return "tc_" + self._bytes(8).hex()

    def sid(self):
        """22 base64url characters (128 random bits)."""
        return b64url(self._bytes(16))

    def otp(self, digits=6):
        return self._chars(string.digits, digits)
