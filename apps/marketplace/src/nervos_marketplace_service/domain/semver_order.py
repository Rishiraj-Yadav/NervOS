"""Index projection of G's parsed SemVer; no independent version grammar."""

from nervos_core.domain.packages import PackageVersion


def precedence_key(version: PackageVersion) -> bytes:
    # G's TOTAL version bound is 64: every numeric component fits this width.
    key = b"".join(
        str(n).zfill(64).encode("ascii") for n in (version.major, version.minor, version.patch)
    )
    if not version.prerelease:
        return key + b"\x01"
    key += b"\x00"
    for part in version.prerelease:
        key += (
            b"\x01" + str(int(part)).zfill(64).encode("ascii")
            if part.isdigit()
            else b"\x02" + part.encode("ascii")
        ) + b"\x00"
    return key
