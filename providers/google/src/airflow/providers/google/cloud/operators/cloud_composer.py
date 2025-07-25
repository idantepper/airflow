#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import shlex
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from google.api_core.exceptions import AlreadyExists
from google.api_core.gapic_v1.method import DEFAULT, _MethodDefault
from google.cloud.orchestration.airflow.service_v1 import ImageVersion
from google.cloud.orchestration.airflow.service_v1.types import Environment, ExecuteAirflowCommandResponse

from airflow.configuration import conf
from airflow.exceptions import AirflowException
from airflow.providers.google.cloud.hooks.cloud_composer import CloudComposerHook
from airflow.providers.google.cloud.links.base import BaseGoogleLink
from airflow.providers.google.cloud.operators.cloud_base import GoogleCloudBaseOperator
from airflow.providers.google.cloud.triggers.cloud_composer import (
    CloudComposerAirflowCLICommandTrigger,
    CloudComposerExecutionTrigger,
)
from airflow.providers.google.common.consts import GOOGLE_DEFAULT_DEFERRABLE_METHOD_NAME

if TYPE_CHECKING:
    from google.api_core.retry import Retry
    from google.protobuf.field_mask_pb2 import FieldMask

    from airflow.utils.context import Context

CLOUD_COMPOSER_BASE_LINK = "https://console.cloud.google.com/composer/environments"
CLOUD_COMPOSER_DETAILS_LINK = (
    CLOUD_COMPOSER_BASE_LINK + "/detail/{region}/{environment_id}/monitoring?project={project_id}"
)
CLOUD_COMPOSER_ENVIRONMENTS_LINK = CLOUD_COMPOSER_BASE_LINK + "?project={project_id}"


class CloudComposerEnvironmentLink(BaseGoogleLink):
    """Helper class for constructing Cloud Composer Environment Link."""

    name = "Cloud Composer Environment"
    key = "composer_conf"
    format_str = CLOUD_COMPOSER_DETAILS_LINK


class CloudComposerEnvironmentsLink(BaseGoogleLink):
    """Helper class for constructing Cloud Composer Environment Link."""

    name = "Cloud Composer Environment List"
    key = "composer_conf"
    format_str = CLOUD_COMPOSER_ENVIRONMENTS_LINK


class CloudComposerCreateEnvironmentOperator(GoogleCloudBaseOperator):
    """
    Create a new environment.

    :param project_id: Required. The ID of the Google Cloud project that the service belongs to.
    :param region: Required. The ID of the Google Cloud region that the service belongs to.
    :param environment_id: Required. The ID of the Google Cloud environment that the service belongs to.
    :param environment:  The environment to create.
    :param gcp_conn_id:
    :param impersonation_chain: Optional service account to impersonate using short-term
        credentials, or chained list of accounts required to get the access_token
        of the last account in the list, which will be impersonated in the request.
        If set as a string, the account must grant the originating account
        the Service Account Token Creator IAM role.
        If set as a sequence, the identities from the list must grant
        Service Account Token Creator IAM role to the directly preceding identity, with first
        account from the list granting this role to the originating account (templated).
    :param retry: Designation of what errors, if any, should be retried.
    :param timeout: The timeout for this request.
    :param metadata: Strings which should be sent along with the request as metadata.
    :param deferrable: Run operator in the deferrable mode
    :param pooling_period_seconds: Optional: Control the rate of the poll for the result of deferrable run.
        By default, the trigger will poll every 30 seconds.
    """

    template_fields = (
        "project_id",
        "region",
        "environment_id",
        "environment",
        "impersonation_chain",
    )

    operator_extra_links = (CloudComposerEnvironmentLink(),)

    def __init__(
        self,
        *,
        project_id: str,
        region: str,
        environment_id: str,
        environment: Environment | dict,
        gcp_conn_id: str = "google_cloud_default",
        impersonation_chain: str | Sequence[str] | None = None,
        retry: Retry | _MethodDefault = DEFAULT,
        timeout: float | None = None,
        metadata: Sequence[tuple[str, str]] = (),
        deferrable: bool = conf.getboolean("operators", "default_deferrable", fallback=False),
        pooling_period_seconds: int = 30,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.project_id = project_id
        self.region = region
        self.environment_id = environment_id
        self.environment = environment
        self.retry = retry
        self.timeout = timeout
        self.metadata = metadata
        self.gcp_conn_id = gcp_conn_id
        self.impersonation_chain = impersonation_chain
        self.deferrable = deferrable
        self.pooling_period_seconds = pooling_period_seconds

    @property
    def extra_links_params(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "region": self.region,
            "environment_id": self.environment_id,
        }

    def execute(self, context: Context):
        hook = CloudComposerHook(
            gcp_conn_id=self.gcp_conn_id,
            impersonation_chain=self.impersonation_chain,
        )

        name = hook.get_environment_name(self.project_id, self.region, self.environment_id)
        if isinstance(self.environment, Environment):
            self.environment.name = name
        else:
            self.environment["name"] = name

        CloudComposerEnvironmentLink.persist(context=context)
        try:
            result = hook.create_environment(
                project_id=self.project_id,
                region=self.region,
                environment=self.environment,
                retry=self.retry,
                timeout=self.timeout,
                metadata=self.metadata,
            )
            context["ti"].xcom_push(key="operation_id", value=result.operation.name)

            if not self.deferrable:
                environment = hook.wait_for_operation(timeout=self.timeout, operation=result)
                return Environment.to_dict(environment)
            self.defer(
                trigger=CloudComposerExecutionTrigger(
                    project_id=self.project_id,
                    region=self.region,
                    operation_name=result.operation.name,
                    gcp_conn_id=self.gcp_conn_id,
                    impersonation_chain=self.impersonation_chain,
                    pooling_period_seconds=self.pooling_period_seconds,
                ),
                method_name=GOOGLE_DEFAULT_DEFERRABLE_METHOD_NAME,
            )
        except AlreadyExists:
            environment = hook.get_environment(
                project_id=self.project_id,
                region=self.region,
                environment_id=self.environment_id,
                retry=self.retry,
                timeout=self.timeout,
                metadata=self.metadata,
            )
            return Environment.to_dict(environment)

    def execute_complete(self, context: Context, event: dict):
        if event["operation_done"]:
            hook = CloudComposerHook(
                gcp_conn_id=self.gcp_conn_id,
                impersonation_chain=self.impersonation_chain,
            )

            env = hook.get_environment(
                project_id=self.project_id,
                region=self.region,
                environment_id=self.environment_id,
                retry=self.retry,
                timeout=self.timeout,
                metadata=self.metadata,
            )
            return Environment.to_dict(env)
        raise AirflowException(f"Unexpected error in the operation: {event['operation_name']}")


class CloudComposerDeleteEnvironmentOperator(GoogleCloudBaseOperator):
    """
    Delete an environment.

    :param project_id: Required. The ID of the Google Cloud project that the service belongs to.
    :param region: Required. The ID of the Google Cloud region that the service belongs to.
    :param environment_id: Required. The ID of the Google Cloud environment that the service belongs to.
    :param retry: Designation of what errors, if any, should be retried.
    :param timeout: The timeout for this request.
    :param metadata: Strings which should be sent along with the request as metadata.
    :param gcp_conn_id:
    :param impersonation_chain: Optional service account to impersonate using short-term
        credentials, or chained list of accounts required to get the access_token
        of the last account in the list, which will be impersonated in the request.
        If set as a string, the account must grant the originating account
        the Service Account Token Creator IAM role.
        If set as a sequence, the identities from the list must grant
        Service Account Token Creator IAM role to the directly preceding identity, with first
        account from the list granting this role to the originating account (templated).
    :param deferrable: Run operator in the deferrable mode
    :param pooling_period_seconds: Optional: Control the rate of the poll for the result of deferrable run.
        By default, the trigger will poll every 30 seconds.
    """

    template_fields = (
        "project_id",
        "region",
        "environment_id",
        "impersonation_chain",
    )

    def __init__(
        self,
        *,
        project_id: str,
        region: str,
        environment_id: str,
        retry: Retry | _MethodDefault = DEFAULT,
        timeout: float | None = None,
        metadata: Sequence[tuple[str, str]] = (),
        gcp_conn_id: str = "google_cloud_default",
        impersonation_chain: str | Sequence[str] | None = None,
        deferrable: bool = conf.getboolean("operators", "default_deferrable", fallback=False),
        pooling_period_seconds: int = 30,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.project_id = project_id
        self.region = region
        self.environment_id = environment_id
        self.retry = retry
        self.timeout = timeout
        self.metadata = metadata
        self.gcp_conn_id = gcp_conn_id
        self.impersonation_chain = impersonation_chain
        self.deferrable = deferrable
        self.pooling_period_seconds = pooling_period_seconds

    def execute(self, context: Context):
        hook = CloudComposerHook(
            gcp_conn_id=self.gcp_conn_id,
            impersonation_chain=self.impersonation_chain,
        )
        result = hook.delete_environment(
            project_id=self.project_id,
            region=self.region,
            environment_id=self.environment_id,
            retry=self.retry,
            timeout=self.timeout,
            metadata=self.metadata,
        )
        if not self.deferrable:
            hook.wait_for_operation(timeout=self.timeout, operation=result)
        else:
            self.defer(
                trigger=CloudComposerExecutionTrigger(
                    project_id=self.project_id,
                    region=self.region,
                    operation_name=result.operation.name,
                    gcp_conn_id=self.gcp_conn_id,
                    impersonation_chain=self.impersonation_chain,
                    pooling_period_seconds=self.pooling_period_seconds,
                ),
                method_name=GOOGLE_DEFAULT_DEFERRABLE_METHOD_NAME,
            )

    def execute_complete(self, context: Context, event: dict):
        pass


class CloudComposerGetEnvironmentOperator(GoogleCloudBaseOperator):
    """
    Get an existing environment.

    :param project_id: Required. The ID of the Google Cloud project that the service belongs to.
    :param region: Required. The ID of the Google Cloud region that the service belongs to.
    :param environment_id: Required. The ID of the Google Cloud environment that the service belongs to.
    :param retry: Designation of what errors, if any, should be retried.
    :param timeout: The timeout for this request.
    :param metadata: Strings which should be sent along with the request as metadata.
    :param gcp_conn_id:
    :param impersonation_chain: Optional service account to impersonate using short-term
        credentials, or chained list of accounts required to get the access_token
        of the last account in the list, which will be impersonated in the request.
        If set as a string, the account must grant the originating account
        the Service Account Token Creator IAM role.
        If set as a sequence, the identities from the list must grant
        Service Account Token Creator IAM role to the directly preceding identity, with first
        account from the list granting this role to the originating account (templated).
    """

    template_fields = (
        "project_id",
        "region",
        "environment_id",
        "impersonation_chain",
    )

    operator_extra_links = (CloudComposerEnvironmentLink(),)

    def __init__(
        self,
        *,
        project_id: str,
        region: str,
        environment_id: str,
        retry: Retry | _MethodDefault = DEFAULT,
        timeout: float | None = None,
        metadata: Sequence[tuple[str, str]] = (),
        gcp_conn_id: str = "google_cloud_default",
        impersonation_chain: str | Sequence[str] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.project_id = project_id
        self.region = region
        self.environment_id = environment_id
        self.retry = retry
        self.timeout = timeout
        self.metadata = metadata
        self.gcp_conn_id = gcp_conn_id
        self.impersonation_chain = impersonation_chain

    @property
    def extra_links_params(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "region": self.region,
            "environment_id": self.environment_id,
        }

    def execute(self, context: Context):
        hook = CloudComposerHook(
            gcp_conn_id=self.gcp_conn_id,
            impersonation_chain=self.impersonation_chain,
        )

        result = hook.get_environment(
            project_id=self.project_id,
            region=self.region,
            environment_id=self.environment_id,
            retry=self.retry,
            timeout=self.timeout,
            metadata=self.metadata,
        )
        CloudComposerEnvironmentLink.persist(context=context)
        return Environment.to_dict(result)


class CloudComposerListEnvironmentsOperator(GoogleCloudBaseOperator):
    """
    List environments.

    :param project_id: Required. The ID of the Google Cloud project that the service belongs to.
    :param region: Required. The ID of the Google Cloud region that the service belongs to.
    :param page_size: The maximum number of environments to return.
    :param page_token: The next_page_token value returned from a previous List
        request, if any.
    :param retry: Designation of what errors, if any, should be retried.
    :param timeout: The timeout for this request.
    :param metadata: Strings which should be sent along with the request as metadata.
    :param gcp_conn_id:
    :param impersonation_chain: Optional service account to impersonate using short-term
        credentials, or chained list of accounts required to get the access_token
        of the last account in the list, which will be impersonated in the request.
        If set as a string, the account must grant the originating account
        the Service Account Token Creator IAM role.
        If set as a sequence, the identities from the list must grant
        Service Account Token Creator IAM role to the directly preceding identity, with first
        account from the list granting this role to the originating account (templated).
    """

    template_fields = (
        "project_id",
        "region",
        "impersonation_chain",
    )

    operator_extra_links = (CloudComposerEnvironmentsLink(),)

    def __init__(
        self,
        *,
        project_id: str,
        region: str,
        page_size: int | None = None,
        page_token: str | None = None,
        retry: Retry | _MethodDefault = DEFAULT,
        timeout: float | None = None,
        metadata: Sequence[tuple[str, str]] = (),
        gcp_conn_id: str = "google_cloud_default",
        impersonation_chain: str | Sequence[str] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.project_id = project_id
        self.region = region
        self.page_size = page_size
        self.page_token = page_token
        self.retry = retry
        self.timeout = timeout
        self.metadata = metadata
        self.gcp_conn_id = gcp_conn_id
        self.impersonation_chain = impersonation_chain

    @property
    def extra_links_params(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
        }

    def execute(self, context: Context):
        hook = CloudComposerHook(
            gcp_conn_id=self.gcp_conn_id,
            impersonation_chain=self.impersonation_chain,
        )
        result = hook.list_environments(
            project_id=self.project_id,
            region=self.region,
            page_size=self.page_size,
            page_token=self.page_token,
            retry=self.retry,
            timeout=self.timeout,
            metadata=self.metadata,
        )
        return [Environment.to_dict(env) for env in result]


class CloudComposerUpdateEnvironmentOperator(GoogleCloudBaseOperator):
    r"""
    Update an environment.

    :param project_id: Required. The ID of the Google Cloud project that the service belongs to.
    :param region: Required. The ID of the Google Cloud region that the service belongs to.
    :param environment_id: Required. The ID of the Google Cloud environment that the service belongs to.
    :param environment:  A patch environment. Fields specified by the ``updateMask`` will be copied from the
        patch environment into the environment under update.
    :param update_mask:  Required. A comma-separated list of paths, relative to ``Environment``, of fields to
        update. If a dict is provided, it must be of the same form as the protobuf message
        :class:`~google.protobuf.field_mask_pb2.FieldMask`
    :param retry: Designation of what errors, if any, should be retried.
    :param timeout: The timeout for this request.
    :param metadata: Strings which should be sent along with the request as metadata.
    :param gcp_conn_id:
    :param impersonation_chain: Optional service account to impersonate using short-term
        credentials, or chained list of accounts required to get the access_token
        of the last account in the list, which will be impersonated in the request.
        If set as a string, the account must grant the originating account
        the Service Account Token Creator IAM role.
        If set as a sequence, the identities from the list must grant
        Service Account Token Creator IAM role to the directly preceding identity, with first
        account from the list granting this role to the originating account (templated).
    :param deferrable: Run operator in the deferrable mode
    :param pooling_period_seconds: Optional: Control the rate of the poll for the result of deferrable run.
        By default, the trigger will poll every 30 seconds.
    """

    template_fields = (
        "project_id",
        "region",
        "environment_id",
        "impersonation_chain",
    )

    operator_extra_links = (CloudComposerEnvironmentLink(),)

    def __init__(
        self,
        *,
        project_id: str,
        region: str,
        environment_id: str,
        environment: dict | Environment,
        update_mask: dict | FieldMask,
        retry: Retry | _MethodDefault = DEFAULT,
        timeout: float | None = None,
        metadata: Sequence[tuple[str, str]] = (),
        gcp_conn_id: str = "google_cloud_default",
        impersonation_chain: str | Sequence[str] | None = None,
        deferrable: bool = conf.getboolean("operators", "default_deferrable", fallback=False),
        pooling_period_seconds: int = 30,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.project_id = project_id
        self.region = region
        self.environment_id = environment_id
        self.environment = environment
        self.update_mask = update_mask
        self.retry = retry
        self.timeout = timeout
        self.metadata = metadata
        self.gcp_conn_id = gcp_conn_id
        self.impersonation_chain = impersonation_chain
        self.deferrable = deferrable
        self.pooling_period_seconds = pooling_period_seconds

    @property
    def extra_links_params(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "region": self.region,
            "environment_id": self.environment_id,
        }

    def execute(self, context: Context):
        hook = CloudComposerHook(
            gcp_conn_id=self.gcp_conn_id,
            impersonation_chain=self.impersonation_chain,
        )

        result = hook.update_environment(
            project_id=self.project_id,
            region=self.region,
            environment_id=self.environment_id,
            environment=self.environment,
            update_mask=self.update_mask,
            retry=self.retry,
            timeout=self.timeout,
            metadata=self.metadata,
        )

        CloudComposerEnvironmentLink.persist(context=context)
        if not self.deferrable:
            environment = hook.wait_for_operation(timeout=self.timeout, operation=result)
            return Environment.to_dict(environment)
        self.defer(
            trigger=CloudComposerExecutionTrigger(
                project_id=self.project_id,
                region=self.region,
                operation_name=result.operation.name,
                gcp_conn_id=self.gcp_conn_id,
                impersonation_chain=self.impersonation_chain,
                pooling_period_seconds=self.pooling_period_seconds,
            ),
            method_name=GOOGLE_DEFAULT_DEFERRABLE_METHOD_NAME,
        )

    def execute_complete(self, context: Context, event: dict):
        if event["operation_done"]:
            hook = CloudComposerHook(
                gcp_conn_id=self.gcp_conn_id,
                impersonation_chain=self.impersonation_chain,
            )

            env = hook.get_environment(
                project_id=self.project_id,
                region=self.region,
                environment_id=self.environment_id,
                retry=self.retry,
                timeout=self.timeout,
                metadata=self.metadata,
            )
            return Environment.to_dict(env)
        raise AirflowException(f"Unexpected error in the operation: {event['operation_name']}")


class CloudComposerListImageVersionsOperator(GoogleCloudBaseOperator):
    """
    List ImageVersions for provided location.

    :param request:  The request object. List ImageVersions in a project and location.
    :param retry: Designation of what errors, if any, should be retried.
    :param timeout: The timeout for this request.
    :param metadata: Strings which should be sent along with the request as metadata.
    :param gcp_conn_id:
    :param impersonation_chain: Optional service account to impersonate using short-term
        credentials, or chained list of accounts required to get the access_token
        of the last account in the list, which will be impersonated in the request.
        If set as a string, the account must grant the originating account
        the Service Account Token Creator IAM role.
        If set as a sequence, the identities from the list must grant
        Service Account Token Creator IAM role to the directly preceding identity, with first
        account from the list granting this role to the originating account (templated).
    """

    template_fields = (
        "project_id",
        "region",
        "impersonation_chain",
    )

    def __init__(
        self,
        *,
        project_id: str,
        region: str,
        page_size: int | None = None,
        page_token: str | None = None,
        include_past_releases: bool = False,
        retry: Retry | _MethodDefault = DEFAULT,
        timeout: float | None = None,
        metadata: Sequence[tuple[str, str]] = (),
        gcp_conn_id: str = "google_cloud_default",
        impersonation_chain: str | Sequence[str] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.project_id = project_id
        self.region = region
        self.page_size = page_size
        self.page_token = page_token
        self.include_past_releases = include_past_releases
        self.retry = retry
        self.timeout = timeout
        self.metadata = metadata
        self.gcp_conn_id = gcp_conn_id
        self.impersonation_chain = impersonation_chain

    def execute(self, context: Context):
        hook = CloudComposerHook(
            gcp_conn_id=self.gcp_conn_id,
            impersonation_chain=self.impersonation_chain,
        )
        result = hook.list_image_versions(
            project_id=self.project_id,
            region=self.region,
            page_size=self.page_size,
            page_token=self.page_token,
            include_past_releases=self.include_past_releases,
            retry=self.retry,
            timeout=self.timeout,
            metadata=self.metadata,
        )
        return [ImageVersion.to_dict(image) for image in result]


class CloudComposerRunAirflowCLICommandOperator(GoogleCloudBaseOperator):
    """
    Run Airflow command for provided Composer environment.

    :param project_id: The ID of the Google Cloud project that the service belongs to.
    :param region: The ID of the Google Cloud region that the service belongs to.
    :param environment_id: The ID of the Google Cloud environment that the service belongs to.
    :param command: Airflow command.
    :param retry: Designation of what errors, if any, should be retried.
    :param timeout: The timeout for this request.
    :param metadata: Strings which should be sent along with the request as metadata.
    :param gcp_conn_id: The connection ID used to connect to Google Cloud Platform.
    :param impersonation_chain: Optional service account to impersonate using short-term
        credentials, or chained list of accounts required to get the access_token
        of the last account in the list, which will be impersonated in the request.
        If set as a string, the account must grant the originating account
        the Service Account Token Creator IAM role.
        If set as a sequence, the identities from the list must grant
        Service Account Token Creator IAM role to the directly preceding identity, with first
        account from the list granting this role to the originating account (templated).
    :param deferrable: Run operator in the deferrable mode
    :param poll_interval: Optional: Control the rate of the poll for the result of deferrable run.
        By default, the trigger will poll every 10 seconds.
    """

    template_fields = (
        "project_id",
        "region",
        "environment_id",
        "command",
        "impersonation_chain",
    )

    def __init__(
        self,
        *,
        project_id: str,
        region: str,
        environment_id: str,
        command: str,
        retry: Retry | _MethodDefault = DEFAULT,
        timeout: float | None = None,
        metadata: Sequence[tuple[str, str]] = (),
        gcp_conn_id: str = "google_cloud_default",
        impersonation_chain: str | Sequence[str] | None = None,
        deferrable: bool = conf.getboolean("operators", "default_deferrable", fallback=False),
        poll_interval: int = 10,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.project_id = project_id
        self.region = region
        self.environment_id = environment_id
        self.command = command
        self.retry = retry
        self.timeout = timeout
        self.metadata = metadata
        self.gcp_conn_id = gcp_conn_id
        self.impersonation_chain = impersonation_chain
        self.deferrable = deferrable
        self.poll_interval = poll_interval

    def execute(self, context: Context):
        hook = CloudComposerHook(
            gcp_conn_id=self.gcp_conn_id,
            impersonation_chain=self.impersonation_chain,
        )

        self.log.info("Executing the command: [ airflow %s ]...", self.command)

        cmd, subcommand, parameters = self._parse_cmd_to_args(self.command)
        execution_cmd_info = hook.execute_airflow_command(
            project_id=self.project_id,
            region=self.region,
            environment_id=self.environment_id,
            command=cmd,
            subcommand=subcommand,
            parameters=parameters,
            retry=self.retry,
            timeout=self.timeout,
            metadata=self.metadata,
        )
        execution_cmd_info_dict = ExecuteAirflowCommandResponse.to_dict(execution_cmd_info)

        self.log.info("Command has been started. execution_id=%s", execution_cmd_info_dict["execution_id"])

        if self.deferrable:
            self.defer(
                trigger=CloudComposerAirflowCLICommandTrigger(
                    project_id=self.project_id,
                    region=self.region,
                    environment_id=self.environment_id,
                    execution_cmd_info=execution_cmd_info_dict,
                    gcp_conn_id=self.gcp_conn_id,
                    impersonation_chain=self.impersonation_chain,
                    poll_interval=self.poll_interval,
                ),
                method_name=GOOGLE_DEFAULT_DEFERRABLE_METHOD_NAME,
            )
            return

        result = hook.wait_command_execution_result(
            project_id=self.project_id,
            region=self.region,
            environment_id=self.environment_id,
            execution_cmd_info=execution_cmd_info_dict,
            retry=self.retry,
            timeout=self.timeout,
            metadata=self.metadata,
            poll_interval=self.poll_interval,
        )
        result_str = self._merge_cmd_output_result(result)
        self.log.info("Command execution result:\n%s", result_str)
        return result

    def execute_complete(self, context: Context, event: dict) -> dict:
        if event and event["status"] == "error":
            raise AirflowException(event["message"])
        result: dict = event["result"]
        result_str = self._merge_cmd_output_result(result)
        self.log.info("Command execution result:\n%s", result_str)
        return result

    def _parse_cmd_to_args(self, cmd: str) -> tuple:
        """Parse user command to command, subcommand and parameters."""
        cmd_dict = shlex.split(cmd)
        if not cmd_dict:
            raise AirflowException("The provided command is empty.")

        command = cmd_dict[0] if len(cmd_dict) >= 1 else None
        subcommand = cmd_dict[1] if len(cmd_dict) >= 2 else None
        parameters = cmd_dict[2:] if len(cmd_dict) >= 3 else None

        return command, subcommand, parameters

    def _merge_cmd_output_result(self, result) -> str:
        """Merge output to one string."""
        result_str = "\n".join(line_dict["content"] for line_dict in result["output"])
        return result_str
