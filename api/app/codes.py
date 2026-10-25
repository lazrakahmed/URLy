import re
import secrets
import string

ALPHABET = string.ascii_letters + string.digits  # a-zA-Z0-9, case-sensitive
CODE_LENGTH = 7
CODE_RE = re.compile(rf"[a-zA-Z0-9]{{{CODE_LENGTH}}}")

# Paths the service itself uses; a code equal to one of these would be shadowed.
# Some are exactly 7 letters ("healthz", "favicon", "openapi"), so the guard in
# generate_code() is needed, not just future-proofing. Compared in lowercase.
RESERVED = {"healthz", "readyz", "metrics", "api", "static", "docs", "openapi", "favicon"}


def generate_code() -> str:
    while True:
        code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
        if code.lower() not in RESERVED:
            return code
