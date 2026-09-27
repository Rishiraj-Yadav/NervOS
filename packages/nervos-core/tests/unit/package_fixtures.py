"""Deterministic fixtures for the Stage G2 package tests.

Everything here is synthetic and deterministic: the wheels are built in memory from fixed bytes, and
the signing key is a raw 32-byte seed declared in source.

The signing seed is deliberately **not** stored as key material in a recognized private-key format.
It is a bare integer-like byte string in a `.py` module -- never a `.pem`, never a `.key` file, and
never a `-----BEGIN ... PRIVATE KEY-----` block -- so the repository security scanner has nothing to
mistake for a production credential and no scanner exclusion is needed. It is a test constant, and
treating it as a credential would be a category error.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Mapping

from nervos_core.application.package_wheel import inspect_wheel

# A fixed, publicly-known test seed. Synthetic by construction and safe to commit: it is a test
# vector, not a credential.
TEST_SIGNING_SEED = bytes(range(32))
OTHER_TEST_SIGNING_SEED = bytes(range(32, 64))

# Fixed entry timestamp for fixture wheels, so the same inputs always produce the same bytes.
FIXED_WHEEL_DATE_TIME = (1980, 1, 1, 0, 0, 0)

VALID_MANIFEST = b"""manifest_version: "1"
package_id: com.acme.invoice
package_name: acme-invoice
package_version: 1.2.3
publisher: Acme Tools
display_name: Acme Invoice Agent
runtime:
  language: python
  python: ">=3.12,<4"
  entrypoint: acme_invoice.agent:InvoiceAgent
nervos:
  min_version: 0.1.0
  max_version: 0.1.0
configuration:
  schema: config.schema.json
"""

VALID_CONFIG_SCHEMA = b"""{
  "type": "object",
  "properties": {
    "greeting": {"type": "string", "default": "hello", "x-nervos-immutable": false}
  },
  "additionalProperties": false
}
"""


def _write_deterministic_zip(
    entries: Mapping[str, bytes],
    *,
    create_system: int = 0,
    compression: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    """Write a ZIP whose metadata is fixed, so a fixture is byte-identical on every run.

    `ZipFile.writestr` with a plain string name stamps the *current wall-clock time* into the entry.
    A fixture built that way changes from run to run, which would make any reproducibility test that
    builds twice compare two different inputs and quietly prove nothing.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=compression) as archive:
        archive.comment = b"wheel fixture"
        for path, payload in sorted(entries.items()):
            info = zipfile.ZipInfo(filename=path, date_time=FIXED_WHEEL_DATE_TIME)
            info.compress_type = compression
            info.create_system = create_system
            # Ordinary Unix file mode when Unix metadata is present; DOS-style metadata otherwise.
            info.external_attr = (0o100644 << 16) if create_system == 3 else 0x20
            archive.writestr(info, payload)
    return buffer.getvalue()


def build_wheel_bytes(
    *,
    name: str = "acme_invoice_agent",
    version: str = "1.2.3",
    metadata_name: str = "acme-invoice-agent",
    metadata_version: str = "1.2.3",
    tag: str = "py3-none-any",
    tags: tuple[str, ...] | None = None,
    requires_dist: tuple[str, ...] = (),
    requires_python: str | None = ">=3.12",
    root_is_purelib: str = "true",
    members: Mapping[str, bytes] | None = None,
    extra_dist_info: bool = False,
    create_system: int = 0,
    compression: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    """Build a minimal but structurally valid wheel in memory.

    Defaults are deliberately valid, so a test only states the one property it is exercising.
    """
    dist_info = f"{name}-{version}.dist-info"
    tag_values = tags if tags is not None else (tag,)
    wheel_metadata = (
        f"Wheel-Version: 1.0\nGenerator: nervos-test\nRoot-Is-Purelib: {root_is_purelib}\n"
        + "".join(f"Tag: {value}\n" for value in tag_values)
    )
    lines = [
        "Metadata-Version: 2.3",
        f"Name: {metadata_name}",
        f"Version: {metadata_version}",
    ]
    if requires_python is not None:
        lines.append(f"Requires-Python: {requires_python}")
    lines.extend(f"Requires-Dist: {item}" for item in requires_dist)
    metadata = "\n".join(lines) + "\n"

    entries: dict[str, bytes] = {
        f"{name}/__init__.py": b"VALUE = 1\n",
        f"{name}/agent.py": b"class InvoiceAgent:\n    pass\n",
        f"{dist_info}/METADATA": metadata.encode("utf-8"),
        f"{dist_info}/WHEEL": wheel_metadata.encode("utf-8"),
        f"{dist_info}/RECORD": b"",
    }
    if extra_dist_info:
        entries["other_pkg-1.0.0.dist-info/METADATA"] = b"Name: other-pkg\nVersion: 1.0.0\n"
        entries["other_pkg-1.0.0.dist-info/WHEEL"] = wheel_metadata.encode("utf-8")
    if members:
        entries.update(members)

    return _write_deterministic_zip(entries, create_system=create_system, compression=compression)


def wheel_filename(
    wheel_bytes: bytes, *, fallback: str = "acme_invoice_agent-1.2.3-py3-none-any.whl"
) -> str:
    """A filename whose distribution name/version agree with the wheel's own METADATA.

    Tests that need the *name* rather than the bytes use this so the lock and the wheelhouse agree
    by construction.
    """
    try:
        metadata = inspect_wheel(wheel_bytes, filename=fallback)
    except Exception:
        return fallback
    return f"{metadata.name.replace('-', '_')}-{metadata.version}-py3-none-any.whl"


def valid_wheel() -> bytes:
    return build_wheel_bytes()


def valid_dependency_wheel(name: str = "helper-lib", version: str = "2.0.0") -> bytes:
    return build_wheel_bytes(
        name=name.replace("-", "_"),
        version=version,
        metadata_name=name,
        metadata_version=version,
        requires_python=">=3.12",
    )
