# SPDX-FileCopyrightText: (c) 2024 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0

"""
Definition of the pydantic models used for data production.
"""

from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field


class Test(BaseModel):
    """
    Table containing information about the execution of CI/CD tests, each one associated
    with a specific CI/CD job execution.

    Only some CI/CD jobs execute tests, which are executed sequentially.
    """

    test_start_ts: datetime = Field(description="Timestamp with timezone when the test execution started.")
    test_end_ts: datetime = Field(description="Timestamp with timezone when the test execution ended.")
    test_case_name: str = Field(description="Name of the pytest function.")
    filepath: str = Field(description="Test file path and name.")
    category: str = Field(description="Name of the test category.")
    group: Optional[str] = Field(None, description="Name of the test group.")
    owner: Optional[str] = Field(None, description="Developer of the test.")
    frontend: Optional[str] = Field(description="Frontend or framework used to run the test.")
    model_name: Optional[str] = Field(description="Name of the model from which this op appears.")
    op_name: Optional[str] = Field(description="Name of the operation. e.g. ttnn.conv2d.")
    framework_op_name: Optional[str] = Field(description="Name of the operation in the framework. e.g. torch.conv2d.")
    op_kind: Optional[str] = Field(description="Kind of operation. e.g. Eltwise.")
    error_message: Optional[str] = Field(None, description="Succinct error string, such as exception type.")
    success: bool = Field(description="Test execution success.")
    skipped: bool = Field(description="Some tests in a job can be skipped.")
    full_test_name: str = Field(description="Test name plus config.")
    config: Optional[dict] = Field(None, description="Test configuration key/value " "pairs.")
    tags: Optional[dict] = Field(None, description="Tags associated with the test, as key/value pairs.")


class Job(BaseModel):
    """
    Contains information about the execution of CI/CD jobs, each one associated with a
    specific CI/CD pipeline.

    Each job may execute multiple tests, which are executed sequentially on a unique
    host.
    """

    github_job_id: Optional[int] = Field(
        None,
        description="Identifier for the Github Actions CI job, for pipelines " "orchestrated and executed by Github.",
    )
    github_job_link: Optional[str] = Field(
        None,
        description="Link to the Github Actions CI job, for pipelines orchestrated and " "executed by Github.",
    )
    name: str = Field(description="Name of the job.")
    job_submission_ts: datetime = Field(description="Timestamp with timezone when the job was submitted.")
    job_start_ts: datetime = Field(description="Timestamp with timezone when the job execution started.")
    job_end_ts: datetime = Field(description="Timestamp with timezone when the job execution ended.")
    job_success: bool = Field(
        description="Job execution success, independently from the test success "
        "criteria. Failure mechanisms that are only descriptive of the "
        "job itself."
    )
    docker_image: Optional[str] = Field(None, description="Name of the Docker image used for the CI job.")
    is_build_job: bool = Field(description="Flag identifying if the job is a software build.")
    job_matrix_config: Optional[dict] = Field(
        None, description="This attribute is included for future feature enhancement."
    )
    host_name: Optional[str] = Field(description="Unique host name.")
    card_type: Optional[str] = Field(description="Card type and version.")
    os: Optional[str] = Field(description="Operating system of the host.")
    location: Optional[str] = Field(description="Where the host is located.")
    failure_signature: Optional[str] = Field(None, description="Failure signature.")
    failure_description: Optional[str] = Field(None, description="Failure description.")
    tests: List[Test] = []


class Pipeline(BaseModel):
    """
    Contains information about the execution of CI/CD pipelines, which consist of the
    sequential execution of one or more jobs.

    Each pipeline is associated with a specific code repository and a specific commit.;
    """

    github_pipeline_id: Optional[int] = Field(
        None,
        description="Identifier for the Github Actions CI pipeline, for pipelines "
        "orchestrated and executed by Github.",
    )
    github_pipeline_link: Optional[str] = Field(
        None,
        description="Link to the Github Actions CI pipeline, for pipelines " "orchestrated and executed by Github.",
    )
    pipeline_submission_ts: datetime = Field(
        description="Timestamp with timezone when the pipeline was submitted for " "execution.",
    )
    pipeline_start_ts: datetime = Field(description="Timestamp with timezone when the pipeline execution started.")
    pipeline_end_ts: datetime = Field(description="Timestamp with timezone when the pipeline execution ended.")
    name: str = Field(description="Name of the pipeline.")
    project: Optional[str] = Field(None, description="Name of the software project.")
    trigger: Optional[str] = Field(None, description="Type of trigger that initiated the pipeline.")
    vcs_platform: Optional[str] = Field(
        None,
        description="Version control software used for the code tested in the pipeline.",
    )
    repository_url: str = Field(description="URL of the code repository.")
    git_branch_name: Optional[str] = Field(description="Name of the Git branch tested by the pipeline.")
    git_commit_hash: str = Field(description="Git commit that triggered the execution of the pipeline.")
    git_author: str = Field(description="Author of the Git commit.")
    orchestrator: Optional[str] = Field(None, description="CI/CD pipeline orchestration platform.")
    jobs: List[Job] = []


class TensorDesc(BaseModel):
    """
    Contains descriptions of tensors used as inputs or outputs of the operation in a ML
    kernel operation test.
    """

    shape: List[int] = Field(description="Shape of the tensor.")
    data_type: str = Field(description="Data type of the tensor, e.g. Float32, " "BFloat16, etc.")
    buffer_type: str = Field(description="Memory space of the tensor, e.g. Dram, L1, " "System.")
    layout: str = Field(description="Layout of the tensor, e.g. Interleaved, " "SingleBank, HeightSharded.")
    grid_shape: List[int] = Field(
        description="The grid shape describes a 2D region of cores which are used to "
        "store the tensor in memory. E.g. You have a tensor with shape "
        "128x128, you might decide to put this on a 2x2 grid of cores, "
        "meaning each core has a 64x64 slice."
    )


class OpTest(BaseModel):
    """
    Contains information about ML kernel operation tests, such as test execution,
    results, configuration.
    """

    github_job_id: int = Field(
        description="Identifier for the Github Actions CI job, which ran the test.",
    )
    full_test_name: str = Field(description="Test name plus config.")
    test_start_ts: datetime = Field(description="Timestamp with timezone when the test execution started.")
    test_end_ts: datetime = Field(description="Timestamp with timezone when the test execution ended.")
    test_case_name: str = Field(description="Name of the pytest function.")
    filepath: str = Field(description="Test file path and name.")
    success: bool = Field(description="Test execution success.")
    skipped: bool = Field(description="Some tests in a job can be skipped.")
    error_message: Optional[str] = Field(None, description="Succinct error string, such as exception type.")
    config: Optional[dict] = Field(default=None, description="Test configuration, as key/value pairs.")
    frontend: str = Field(description="ML frontend or framework used to run the test.")
    model_name: str = Field(description="Name of the ML model in which this operation is used.")
    op_kind: str = Field(description="Kind of operation, e.g. Eltwise.")
    op_name: str = Field(description="Name of the operation, e.g. ttnn.conv2d")
    framework_op_name: str = Field(description="Name of the operation within the framework, e.g. torch.conv2d")
    inputs: List[TensorDesc] = Field(description="List of input tensors.")
    outputs: List[TensorDesc] = Field(description="List of output tensors.")
    op_params: Optional[dict] = Field(
        default=None,
        description="Parametrization criteria for the operation, based on its kind, "
        "as key/value pairs, e.g. stride, padding, etc.",
    )
