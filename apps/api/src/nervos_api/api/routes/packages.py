"""Package lifecycle routes for inspection, installation, query, and removal."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, Query, Response, UploadFile, status
from nervos_core.domain.package_installation import (
    PackageInstallAuthorization,
    PackageInstallStatus,
    validate_package_status,
)
from nervos_core.domain.package_query import PackageRemovalOutcome
from starlette.concurrency import run_in_threadpool

from nervos_api.api.dependencies import (
    CurrentUserDependency,
    OriginDependency,
    PackageApplicationServiceDependency,
    PackageQueryServiceDependency,
    utc_now,
)
from nervos_api.api.schemas import (
    PackageInspectionResponse,
    PackageRemovalPlanResponse,
    PackageRemovalResponse,
    PackageVersionDetailResponse,
    PackageVersionPageResponse,
    PackageVersionSummaryResponse,
)

router = APIRouter(prefix="/packages")


@router.post(
    "/inspect",
    response_model=PackageInspectionResponse,
    status_code=status.HTTP_200_OK,
)
async def inspect_package(
    file: Annotated[UploadFile, File()],
    user: CurrentUserDependency,
    query_service: PackageQueryServiceDependency,
) -> PackageInspectionResponse:
    """Inspect a package artifact and return verified metadata without DB mutation."""
    del user
    inspected = query_service.inspect_artifact(file.file)
    return PackageInspectionResponse(
        package_id=inspected.package_id,
        package_version=inspected.package_version,
        display_name=inspected.display_name,
        manifest_version=inspected.manifest_version,
        signer_fingerprint=inspected.signer_fingerprint,
        content_digest=inspected.content_digest,
        archive_digest=inspected.archive_digest,
        min_nervos_version=inspected.min_nervos_version,
        max_nervos_version=inspected.max_nervos_version,
        is_compatible=inspected.is_compatible,
        entrypoint_module=inspected.entrypoint_module,
        entrypoint_object=inspected.entrypoint_object,
        tools_required=list(inspected.tools_required),
        tools_optional=list(inspected.tools_optional),
        memory_declarations=list(inspected.memory_declarations),
        trigger_declarations=list(inspected.trigger_declarations),
        config_schema=inspected.config_schema,
        resource_limits=inspected.resource_limits,
    )


@router.post(
    "/install",
    response_model=PackageVersionSummaryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def install_package(
    file: Annotated[UploadFile, File()],
    package_id: Annotated[str, Form()],
    package_version: Annotated[str, Form()],
    content_digest: Annotated[str, Form()],
    signer_fingerprint: Annotated[str, Form()],
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: PackageApplicationServiceDependency,
    response: Response,
    archive_digest: Annotated[str | None, Form()] = None,
) -> PackageVersionSummaryResponse:
    """Install a verified .nervos package with explicit per-install operator authorization."""
    del origin
    authorization = PackageInstallAuthorization(
        package_id=package_id,
        package_version=package_version,
        content_digest=content_digest,
        signer_fingerprint=signer_fingerprint,
        archive_digest=archive_digest,
        approved_by_user_id=user.id,
        approved_at=utc_now(),
    )
    result = await run_in_threadpool(service.install, file.file, authorization)
    if result.package.status is not PackageInstallStatus.ACTIVE:
        response.status_code = status.HTTP_200_OK
    return PackageVersionSummaryResponse(
        package_id=result.package.package_id,
        package_version=result.package.package_version,
        display_name=result.package.package_id,
        status=result.package.status.value,
        signer_fingerprint=result.package.signer_fingerprint,
        content_digest=result.package.content_digest,
        archive_digest=result.package.archive_digest,
        bound_instances_count=0,
        installed_at=result.package.created_at,
        activated_at=result.package.updated_at
        if result.package.status is PackageInstallStatus.ACTIVE
        else None,
        failed_at=None,
        removed_at=None,
        last_error_code=None,
        last_error_message=None,
    )


@router.get("", response_model=PackageVersionPageResponse)
def list_packages(
    user: CurrentUserDependency,
    query_service: PackageQueryServiceDependency,
    status: Annotated[str | None, Query()] = None,
) -> PackageVersionPageResponse:
    """List all installed package releases with status and bound instance counts."""
    del user
    status_enum = validate_package_status(status) if status is not None else None
    summaries = query_service.list_packages(status=status_enum)
    return PackageVersionPageResponse(
        items=[
            PackageVersionSummaryResponse(
                package_id=s.package_id,
                package_version=s.package_version,
                display_name=s.display_name,
                status=s.status.value,
                signer_fingerprint=s.signer_fingerprint,
                content_digest=s.content_digest,
                archive_digest=s.archive_digest,
                bound_instances_count=s.bound_instances_count,
                installed_at=s.installed_at,
                activated_at=s.activated_at,
                failed_at=s.failed_at,
                removed_at=s.removed_at,
                last_error_code=s.last_error_code,
                last_error_message=s.last_error_message,
            )
            for s in summaries
        ]
    )


@router.get(
    "/{package_id}/versions/{package_version}",
    response_model=PackageVersionDetailResponse,
)
def get_package_version_detail(
    package_id: str,
    package_version: str,
    user: CurrentUserDependency,
    query_service: PackageQueryServiceDependency,
) -> PackageVersionDetailResponse:
    """Return detailed metadata, schema, and readiness for one exact installed package version."""
    del user
    detail = query_service.get_package_detail(package_id, package_version)
    return PackageVersionDetailResponse(
        package_id=detail.package_id,
        package_version=detail.package_version,
        display_name=detail.display_name,
        status=detail.status.value,
        signer_fingerprint=detail.signer_fingerprint,
        content_digest=detail.content_digest,
        archive_digest=detail.archive_digest,
        manifest_version=detail.manifest_version,
        min_nervos_version=detail.min_nervos_version,
        max_nervos_version=detail.max_nervos_version,
        is_compatible=detail.is_compatible,
        entrypoint_module=detail.entrypoint_module,
        entrypoint_object=detail.entrypoint_object,
        tools_required=list(detail.tools_required),
        tools_optional=list(detail.tools_optional),
        memory_declarations=list(detail.memory_declarations),
        trigger_declarations=list(detail.trigger_declarations),
        config_schema=detail.config_schema,
        resource_limits=detail.resource_limits,
        bound_instances_count=detail.bound_instances_count,
        environment_id=detail.environment_id,
        environment_status=detail.environment_status.value
        if detail.environment_status is not None
        else None,
        installed_at=detail.installed_at,
        activated_at=detail.activated_at,
        failed_at=detail.failed_at,
        removed_at=detail.removed_at,
        last_error_code=detail.last_error_code,
        last_error_message=detail.last_error_message,
    )


@router.get(
    "/{package_id}/versions/{package_version}/removal-plan",
    response_model=PackageRemovalPlanResponse,
)
def get_package_removal_plan(
    package_id: str,
    package_version: str,
    user: CurrentUserDependency,
    query_service: PackageQueryServiceDependency,
) -> PackageRemovalPlanResponse:
    """Return the removal plan evaluating bound instances and active run blockers."""
    del user
    plan = query_service.get_removal_plan(package_id, package_version)
    return PackageRemovalPlanResponse(
        package_id=plan.package_id,
        package_version=plan.package_version,
        status=plan.status.value,
        bound_instance_ids=list(plan.bound_instance_ids),
        bound_instances_count=plan.bound_instances_count,
        nonterminal_run_ids=list(plan.nonterminal_run_ids),
        nonterminal_runs_count=plan.nonterminal_runs_count,
        is_environment_shared=plan.is_environment_shared,
        can_remove_immediately=plan.can_remove_immediately,
        can_begin_removal=plan.can_begin_removal,
        blocking_reasons=list(plan.blocking_reasons),
    )


@router.delete(
    "/{package_id}/versions/{package_version}",
    response_model=PackageRemovalResponse,
)
def delete_package_version(
    package_id: str,
    package_version: str,
    origin: OriginDependency,
    user: CurrentUserDependency,
    service: PackageApplicationServiceDependency,
    response: Response,
) -> PackageRemovalResponse:
    """Uninstall a package release. Returns 202 on pending drain, 200 on immediate removal."""
    del origin, user
    outcome = service.remove_package(package_id, package_version)
    if outcome is PackageRemovalOutcome.PENDING_REMOVAL:
        response.status_code = status.HTTP_202_ACCEPTED
    else:
        response.status_code = status.HTTP_200_OK
    return PackageRemovalResponse(
        package_id=package_id,
        package_version=package_version,
        outcome=outcome.value,
    )
