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

import enum
import gzip
import io
from collections import namedtuple
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal, cast

from airflow.exceptions import AirflowException
from airflow.providers.amazon.aws.hooks.s3 import S3Hook
from airflow.providers.amazon.version_compat import BaseHook, BaseOperator

if TYPE_CHECKING:
    import pandas as pd

    from airflow.providers.common.sql.hooks.sql import DbApiHook
    from airflow.utils.context import Context


class FILE_FORMAT(enum.Enum):
    """Possible file formats."""

    CSV = enum.auto()
    JSON = enum.auto()
    PARQUET = enum.auto()


FileOptions = namedtuple("FileOptions", ["mode", "suffix", "function"])

FILE_OPTIONS_MAP = {
    FILE_FORMAT.CSV: FileOptions("r+", ".csv", "to_csv"),
    FILE_FORMAT.JSON: FileOptions("r+", ".json", "to_json"),
    FILE_FORMAT.PARQUET: FileOptions("rb+", ".parquet", "to_parquet"),
}


class SqlToS3Operator(BaseOperator):
    """
    Saves data from a specific SQL query into a file in S3.

    .. seealso::
        For more information on how to use this operator, take a look at the guide:
        :ref:`howto/operator:SqlToS3Operator`

    :param query: the sql query to be executed. If you want to execute a file, place the absolute path of it,
        ending with .sql extension. (templated)
    :param s3_bucket: bucket where the data will be stored. (templated)
    :param s3_key: desired key for the file. It includes the name of the file. (templated)
    :param replace: whether or not to replace the file in S3 if it previously existed
    :param sql_conn_id: reference to a specific database.
    :param sql_hook_params: Extra config params to be passed to the underlying hook.
        Should match the desired hook constructor params.
    :param parameters: (optional) the parameters to render the SQL query with.
    :param aws_conn_id: reference to a specific S3 connection
    :param verify: Whether or not to verify SSL certificates for S3 connection.
        By default SSL certificates are verified.
        You can provide the following values:

        - ``False``: do not validate SSL certificates. SSL will still be used
                (unless use_ssl is False), but SSL certificates will not be verified.
        - ``path/to/cert/bundle.pem``: A filename of the CA cert bundle to uses.
                You can specify this argument if you want to use a different
                CA cert bundle than the one used by botocore.
    :param file_format: the destination file format, only string 'csv', 'json' or 'parquet' is accepted.
    :param max_rows_per_file: (optional) argument to set destination file number of rows limit, if source data
        is larger than that, it will be dispatched into multiple files.
        Will be ignored if ``groupby_kwargs`` argument is specified.
    :param pd_kwargs: arguments to include in DataFrame ``.to_parquet()``, ``.to_json()`` or ``.to_csv()``.
    :param groupby_kwargs: argument to include in DataFrame ``groupby()``.
    """

    template_fields: Sequence[str] = (
        "s3_bucket",
        "s3_key",
        "query",
        "sql_conn_id",
    )
    template_ext: Sequence[str] = (".sql",)
    template_fields_renderers = {
        "query": "sql",
        "pd_kwargs": "json",
    }

    def __init__(
        self,
        *,
        query: str,
        s3_bucket: str,
        s3_key: str,
        sql_conn_id: str,
        sql_hook_params: dict | None = None,
        parameters: None | Mapping[str, Any] | list | tuple = None,
        replace: bool = False,
        aws_conn_id: str | None = "aws_default",
        verify: bool | str | None = None,
        file_format: Literal["csv", "json", "parquet"] = "csv",
        max_rows_per_file: int = 0,
        pd_kwargs: dict | None = None,
        groupby_kwargs: dict | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.query = query
        self.s3_bucket = s3_bucket
        self.s3_key = s3_key
        self.sql_conn_id = sql_conn_id
        self.aws_conn_id = aws_conn_id
        self.verify = verify
        self.replace = replace
        self.pd_kwargs = pd_kwargs or {}
        self.parameters = parameters
        self.max_rows_per_file = max_rows_per_file
        self.groupby_kwargs = groupby_kwargs or {}
        self.sql_hook_params = sql_hook_params

        if "path_or_buf" in self.pd_kwargs:
            raise AirflowException("The argument path_or_buf is not allowed, please remove it")

        if self.max_rows_per_file and self.groupby_kwargs:
            raise AirflowException(
                "SqlToS3Operator arguments max_rows_per_file and groupby_kwargs "
                "can not be both specified. Please choose one."
            )

        try:
            self.file_format = FILE_FORMAT[file_format.upper()]
        except KeyError:
            raise AirflowException(f"The argument file_format doesn't support {file_format} value.")

    @staticmethod
    def _fix_dtypes(df: pd.DataFrame, file_format: FILE_FORMAT) -> None:
        """
        Mutate DataFrame to set dtypes for float columns containing NaN values.

        Set dtype of object to str to allow for downstream transformations.
        """
        try:
            import numpy as np
            import pandas as pd
        except ImportError as e:
            from airflow.exceptions import AirflowOptionalProviderFeatureException

            raise AirflowOptionalProviderFeatureException(e)

        for col in df:
            if df[col].dtype.name == "object" and file_format == FILE_FORMAT.PARQUET:
                # if the type wasn't identified or converted, change it to a string so if can still be
                # processed.
                df[col] = df[col].astype(str)

            if "float" in df[col].dtype.name and df[col].hasnans:
                # inspect values to determine if dtype of non-null values is int or float
                notna_series: Any = df[col].dropna().values
                if np.equal(notna_series, notna_series.astype(int)).all():
                    # set to dtype that retains integers and supports NaNs
                    # The type ignore can be removed here if https://github.com/numpy/numpy/pull/23690
                    # is merged and released as currently NumPy does not consider None as valid for x/y.
                    df[col] = np.where(df[col].isnull(), None, df[col])  # type: ignore[call-overload]
                    df[col] = df[col].astype(pd.Int64Dtype())
                elif np.isclose(notna_series, notna_series.astype(int)).all():
                    # set to float dtype that retains floats and supports NaNs
                    # The type ignore can be removed here if https://github.com/numpy/numpy/pull/23690
                    # is merged and released
                    df[col] = np.where(df[col].isnull(), None, df[col])  # type: ignore[call-overload]
                    df[col] = df[col].astype(pd.Float64Dtype())

    def execute(self, context: Context) -> None:
        sql_hook = self._get_hook()
        s3_conn = S3Hook(aws_conn_id=self.aws_conn_id, verify=self.verify)
        data_df = sql_hook.get_df(sql=self.query, parameters=self.parameters, df_type="pandas")
        self.log.info("Data from SQL obtained")
        self._fix_dtypes(data_df, self.file_format)
        file_options = FILE_OPTIONS_MAP[self.file_format]

        for group_name, df in self._partition_dataframe(df=data_df):
            buf = io.BytesIO()
            self.log.info("Writing data to in-memory buffer")
            object_key = f"{self.s3_key}_{group_name}" if group_name else self.s3_key

            if self.pd_kwargs.get("compression") == "gzip":
                pd_kwargs = {k: v for k, v in self.pd_kwargs.items() if k != "compression"}
                with gzip.GzipFile(fileobj=buf, mode="wb", filename=object_key) as gz:
                    getattr(df, file_options.function)(gz, **pd_kwargs)
            else:
                if self.file_format == FILE_FORMAT.PARQUET:
                    getattr(df, file_options.function)(buf, **self.pd_kwargs)
                else:
                    text_buf = io.TextIOWrapper(buf, encoding="utf-8", write_through=True)
                    getattr(df, file_options.function)(text_buf, **self.pd_kwargs)
                    text_buf.flush()
            buf.seek(0)

            self.log.info("Uploading data to S3")
            s3_conn.load_file_obj(
                file_obj=buf, key=object_key, bucket_name=self.s3_bucket, replace=self.replace
            )

    def _partition_dataframe(self, df: pd.DataFrame) -> Iterable[tuple[str, pd.DataFrame]]:
        """Partition dataframe using pandas groupby() method."""
        try:
            import secrets
            import string

            import numpy as np
        except ImportError:
            pass
        # if max_rows_per_file argument is specified, a temporary column with a random unusual name will be
        # added to the dataframe. This column is used to dispatch the dataframe into smaller ones using groupby()
        random_column_name = ""
        if self.max_rows_per_file and not self.groupby_kwargs:
            random_column_name = "".join(secrets.choice(string.ascii_letters) for _ in range(20))
            df[random_column_name] = np.arange(len(df)) // self.max_rows_per_file
            self.groupby_kwargs = {"by": random_column_name}
        if not self.groupby_kwargs:
            yield "", df
            return
        for group_label in (grouped_df := df.groupby(**self.groupby_kwargs)).groups:
            yield (
                cast("str", group_label),
                grouped_df.get_group(group_label)
                .drop(random_column_name, axis=1, errors="ignore")
                .reset_index(drop=True),
            )

    def _get_hook(self) -> DbApiHook:
        self.log.debug("Get connection for %s", self.sql_conn_id)
        conn = BaseHook.get_connection(self.sql_conn_id)
        hook = conn.get_hook(hook_params=self.sql_hook_params)
        if not callable(getattr(hook, "get_df", None)):
            raise AirflowException("This hook is not supported. The hook class must have get_df method.")
        return hook
