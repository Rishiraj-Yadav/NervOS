"""Synthetic signed fixtures built through G, never a production seed path."""

import io
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime

from nervos_core.application.package_builder import PackageBuildInputs, package_build_bytes
from nervos_core.application.package_signing import Ed25519PackageSigner
from nervos_core.application.package_verification import VerifiedPackage, verify_package
from nervos_marketplace_service.domain.catalog import (
    Compatibility,
    DistributionState,
    PackageReleaseDetail,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
SENTINEL = b"raise RuntimeError('MARKETPLACE_MUST_NEVER_EXECUTE_THIS')\n"


@dataclass(frozen=True)
class PackageFixture:
    raw: bytes
    manifest: bytes
    verified: VerifiedPackage


def signed_package(package_id: str = "com.acme.invoice", version: str = "1.2.3") -> PackageFixture:
    manifest = f"""manifest_version: "1"
package_id: {package_id}
package_name: acme-invoice
package_version: {version}
publisher: Synthetic Test
display_name: Test Invoice
runtime:
  language: python
  python: ">=3.12,<4"
  entrypoint: acme_invoice.agent:InvoiceAgent
nervos:
  min_version: 0.1.0
  max_version: 0.1.0
configuration:
  schema: config.schema.json
""".encode()
    buffer = io.BytesIO()
    files = {
        "acme_invoice/__init__.py": b"",
        "acme_invoice/agent.py": SENTINEL,
        "acme_invoice-1.2.3.dist-info/METADATA": (
            b"Metadata-Version: 2.3\nName: acme-invoice\nVersion: 1.2.3\nRequires-Python: >=3.12\n"
        ),
        "acme_invoice-1.2.3.dist-info/WHEEL": (
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        "acme_invoice-1.2.3.dist-info/RECORD": b"",
    }
    with zipfile.ZipFile(buffer, "w") as wheel:
        for name, content in sorted(files.items()):
            wheel.writestr(zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)), content)
    raw = package_build_bytes(
        PackageBuildInputs(
            manifest_bytes=manifest,
            config_schema_bytes=b'{"type":"object","properties":{},"additionalProperties":false}',
            agent_wheel_bytes=buffer.getvalue(),
        ),
        signer=Ed25519PackageSigner.from_private_bytes(bytes(range(32))),
    )
    return PackageFixture(raw, manifest, verify_package(archive_bytes=raw))


def descriptor(fixture: PackageFixture) -> PackageReleaseDetail:
    verified = fixture.verified
    return PackageReleaseDetail(
        package_id=verified.manifest.package_id,
        exact_version=verified.manifest.package_version,
        archive_sha256=verified.archive_digest,
        content_digest=verified.content_digest,
        signer_fingerprint=verified.signer_fingerprint,
        size_bytes=len(fixture.raw),
        manifest_version=1,
        published_at=NOW,
        distribution_state=DistributionState.AVAILABLE,
        status_revision=1,
        status_updated_at=NOW,
        observed_at=NOW,
        compatibility=Compatibility(
            runtime_language="python",
            runtime_python=">=3.12,<4",
            nervos_min_version="0.1.0",
            nervos_max_version="0.1.0",
        ),
    )
