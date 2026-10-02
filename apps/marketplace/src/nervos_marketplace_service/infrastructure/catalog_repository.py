"""Parameterized PostgreSQL reads; every connection closes before artifact I/O."""

from datetime import UTC, datetime

from nervos_core.domain.packages import PackageVersion
from sqlalchemy import text
from sqlalchemy.engine import Engine, RowMapping
from sqlalchemy.exc import SQLAlchemyError

from nervos_marketplace_service.application.ports import CatalogPosition
from nervos_marketplace_service.domain.catalog import (
    PackageDetail,
    PackageReleaseDetail,
    PackageSummary,
    PackageVersionSummary,
)
from nervos_marketplace_service.domain.errors import MarketplaceError
from nervos_marketplace_service.domain.semver_order import precedence_key

LATEST = """(SELECT exact_version FROM package_releases r
 WHERE r.project_id=p.id AND r.publication_state='published'
 AND r.distribution_state='available' AND NOT r.is_prerelease
 ORDER BY r.semver_key DESC, r.exact_version COLLATE \"C\" ASC LIMIT 1)"""
AVAILABLE = """EXISTS(SELECT 1 FROM package_releases r WHERE r.project_id=p.id
 AND r.publication_state='published' AND r.distribution_state='available')"""
HISTORY = """EXISTS(SELECT 1 FROM package_releases r WHERE r.project_id=p.id
 AND r.publication_state='published')"""


class PostgresCatalogRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def storage_version(self, archive_sha256: str) -> str | None:
        # to_jsonb supports the accepted mp0001 schema during explicit legacy
        # migration/reconciliation. Missing legacy fields are NULL; new finalized
        # artifacts receive non-null evidence under mp0003 constraints.
        rows = self._fetch(
            "SELECT to_jsonb(a)->>'storage_version_id' AS storage_version_id "
            "FROM artifacts a WHERE archive_sha256=:digest",
            {"digest": archive_sha256},
        )
        if not rows:
            raise MarketplaceError("artifact_unavailable", 503)
        value = rows[0]["storage_version_id"]
        return str(value) if value is not None else None

    def _fetch(self, sql: str, params: dict[str, object]) -> list[RowMapping]:
        try:
            with self.engine.connect() as connection, connection.begin():
                return list(connection.execute(text(sql), params).mappings())
        except SQLAlchemyError:
            raise MarketplaceError("service_unavailable", 503) from None

    def packages(
        self, query: str, limit: int, after: CatalogPosition | None
    ) -> list[tuple[PackageSummary, CatalogPosition]]:
        sql = f"""WITH candidates AS (
 SELECT p.package_id,l.display_name,l.summary,l.revision AS listing_revision,
 {LATEST} AS latest_stable_version,
 CASE WHEN p.package_id=:q AND :q<>'' THEN 1 ELSE 0 END AS exact,
 CASE WHEN :q='' THEN 0 ELSE
 floor(ts_rank_cd(l.search_vector, plainto_tsquery('simple', :q))*1000000)::bigint
 END AS rank
 FROM package_projects p JOIN package_listings l ON l.project_id=p.id
 WHERE {AVAILABLE} AND (:q='' OR p.package_id=:q OR
 l.search_vector @@ plainto_tsquery('simple', :q)))
 SELECT * FROM candidates WHERE :first OR exact < :exact OR
 (exact=:exact AND rank < :rank) OR
 (exact=:exact AND rank=:rank AND package_id > :last)
 ORDER BY exact DESC,rank DESC,package_id COLLATE \"C\" ASC LIMIT :limit"""
        position = after or CatalogPosition()
        rows = self._fetch(
            sql,
            {
                "q": query,
                "first": after is None,
                "exact": position.exact,
                "rank": position.rank,
                "last": position.package_id,
                "limit": limit,
            },
        )
        return [
            (
                PackageSummary.model_validate({k: row[k] for k in PackageSummary.model_fields}),
                CatalogPosition(int(row["exact"]), int(row["rank"]), str(row["package_id"])),
            )
            for row in rows
        ]

    def package(self, package_id: str) -> PackageDetail:
        rows = self._fetch(
            f"""SELECT p.package_id,l.display_name,l.summary,l.description,
 l.revision AS listing_revision,l.updated_at AS listing_updated_at,
 {LATEST} AS latest_stable_version,{AVAILABLE} AS has_available_release
 FROM package_projects p JOIN package_listings l ON l.project_id=p.id
 WHERE p.package_id=:id AND {HISTORY}""",
            {"id": package_id},
        )
        if not rows:
            raise MarketplaceError("not_found", 404)
        return PackageDetail.model_validate(dict(rows[0]))

    def versions(
        self,
        package_id: str,
        limit: int,
        after: CatalogPosition | None,
        include_unavailable: bool,
        include_prerelease: bool,
    ) -> list[tuple[PackageVersionSummary, CatalogPosition]]:
        self.package(package_id)
        key = precedence_key(PackageVersion(after.version)) if after else b""
        rows = self._fetch(
            """SELECT r.exact_version,r.published_at,r.distribution_state,
 r.status_revision FROM package_releases r JOIN package_projects p ON p.id=r.project_id
 WHERE p.package_id=:id AND r.publication_state='published'
 AND (:unavailable OR r.distribution_state='available')
 AND (:prerelease OR NOT r.is_prerelease)
 AND (:first OR r.semver_key < :key OR (r.semver_key=:key AND r.exact_version > :version))
 ORDER BY r.semver_key DESC,r.exact_version COLLATE "C" ASC LIMIT :limit""",
            {
                "id": package_id,
                "limit": limit,
                "unavailable": include_unavailable,
                "prerelease": include_prerelease,
                "first": after is None,
                "key": key,
                "version": after.version if after else "",
            },
        )
        return [
            (
                PackageVersionSummary.model_validate(dict(row)),
                CatalogPosition(package_id=package_id, version=str(row["exact_version"])),
            )
            for row in rows
        ]

    def release(self, package_id: str, version: str) -> PackageReleaseDetail:
        rows = self._fetch(
            """SELECT p.package_id,r.exact_version,r.archive_sha256,
 r.content_digest,r.signer_fingerprint,a.size_bytes,r.manifest_version,r.published_at,
 r.distribution_state,r.status_revision,r.status_updated_at,r.runtime_language,
 r.runtime_python,r.nervos_min_version,r.nervos_max_version
 FROM package_releases r JOIN package_projects p ON p.id=r.project_id
 JOIN artifacts a ON a.archive_sha256=r.archive_sha256
 WHERE p.package_id=:id AND r.exact_version=:version AND r.publication_state='published'""",
            {"id": package_id, "version": version},
        )
        if not rows:
            raise MarketplaceError("not_found", 404)
        value = dict(rows[0])
        value["compatibility"] = {
            key: value.pop(key)
            for key in (
                "runtime_language",
                "runtime_python",
                "nervos_min_version",
                "nervos_max_version",
            )
        }
        value["observed_at"] = datetime.now(UTC)
        return PackageReleaseDetail.model_validate(value)
