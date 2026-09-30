"""Create the deterministic, signed Stage-G browser acceptance package offline."""

# pyright: basic

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


MANIFEST = b"""manifest_version: "1"
package_id: com.acme.browserdemo
package_name: acme-browser-demo
package_version: 1.0.0
publisher: NervOS local acceptance
display_name: Browser Runtime Demo
runtime:
  language: python
  python: ">=3.12,<4"
  entrypoint: acme_browser.agent:BrowserAgent
nervos:
  min_version: 0.1.0
  max_version: 0.1.0
configuration:
  schema: config.schema.json
"""

ENTRYPOINT = b"""from nervos_sdk import AgentResult

class BrowserAgent:
    async def run(self, context):
        return AgentResult(final_message="stage-g-browser-runtime-ok")
"""


def main() -> None:
    sys.path.insert(0, str(ROOT / "packages" / "nervos-core" / "tests" / "unit"))
    from nervos_core.application.package_builder import PackageBuildInputs, package_build_bytes
    from nervos_core.application.package_signing import Ed25519PackageSigner
    from package_fixtures import (  # pyright: ignore[reportMissingImports]
        TEST_SIGNING_SEED,
        VALID_CONFIG_SCHEMA,
        build_wheel_bytes,
    )

    destination = Path(sys.argv[1]).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    artifact = package_build_bytes(
        PackageBuildInputs(
            manifest_bytes=MANIFEST,
            config_schema_bytes=VALID_CONFIG_SCHEMA,
            agent_wheel_bytes=build_wheel_bytes(
                name="acme_browser",
                metadata_name="acme-browser",
                members={"acme_browser/agent.py": ENTRYPOINT},
            ),
        ),
        signer=Ed25519PackageSigner.from_private_bytes(TEST_SIGNING_SEED),
    )
    destination.write_bytes(artifact)


if __name__ == "__main__":
    main()
