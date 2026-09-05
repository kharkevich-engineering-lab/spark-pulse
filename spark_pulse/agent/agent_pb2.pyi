from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class EnrollRequest(_message.Message):
    __slots__ = ("token", "csr_pem", "requested_name", "facts")
    TOKEN_FIELD_NUMBER: _ClassVar[int]
    CSR_PEM_FIELD_NUMBER: _ClassVar[int]
    REQUESTED_NAME_FIELD_NUMBER: _ClassVar[int]
    FACTS_FIELD_NUMBER: _ClassVar[int]
    token: str
    csr_pem: bytes
    requested_name: str
    facts: NodeFacts
    def __init__(self, token: _Optional[str] = ..., csr_pem: _Optional[bytes] = ..., requested_name: _Optional[str] = ..., facts: _Optional[_Union[NodeFacts, _Mapping]] = ...) -> None: ...

class RenewRequest(_message.Message):
    __slots__ = ("csr_pem", "facts")
    CSR_PEM_FIELD_NUMBER: _ClassVar[int]
    FACTS_FIELD_NUMBER: _ClassVar[int]
    csr_pem: bytes
    facts: NodeFacts
    def __init__(self, csr_pem: _Optional[bytes] = ..., facts: _Optional[_Union[NodeFacts, _Mapping]] = ...) -> None: ...

class Identity(_message.Message):
    __slots__ = ("node_id", "certificate_pem", "trust_bundle_pem", "trust_bundle_spki", "not_before_unix", "not_after_unix", "cluster_id", "epoch", "spiffe_id")
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    CERTIFICATE_PEM_FIELD_NUMBER: _ClassVar[int]
    TRUST_BUNDLE_PEM_FIELD_NUMBER: _ClassVar[int]
    TRUST_BUNDLE_SPKI_FIELD_NUMBER: _ClassVar[int]
    NOT_BEFORE_UNIX_FIELD_NUMBER: _ClassVar[int]
    NOT_AFTER_UNIX_FIELD_NUMBER: _ClassVar[int]
    CLUSTER_ID_FIELD_NUMBER: _ClassVar[int]
    EPOCH_FIELD_NUMBER: _ClassVar[int]
    SPIFFE_ID_FIELD_NUMBER: _ClassVar[int]
    node_id: str
    certificate_pem: bytes
    trust_bundle_pem: bytes
    trust_bundle_spki: str
    not_before_unix: int
    not_after_unix: int
    cluster_id: str
    epoch: int
    spiffe_id: str
    def __init__(self, node_id: _Optional[str] = ..., certificate_pem: _Optional[bytes] = ..., trust_bundle_pem: _Optional[bytes] = ..., trust_bundle_spki: _Optional[str] = ..., not_before_unix: _Optional[int] = ..., not_after_unix: _Optional[int] = ..., cluster_id: _Optional[str] = ..., epoch: _Optional[int] = ..., spiffe_id: _Optional[str] = ...) -> None: ...

class AgentMessage(_message.Message):
    __slots__ = ("hello", "heartbeat", "result", "progress")
    HELLO_FIELD_NUMBER: _ClassVar[int]
    HEARTBEAT_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    PROGRESS_FIELD_NUMBER: _ClassVar[int]
    hello: Hello
    heartbeat: Heartbeat
    result: CommandResult
    progress: Progress
    def __init__(self, hello: _Optional[_Union[Hello, _Mapping]] = ..., heartbeat: _Optional[_Union[Heartbeat, _Mapping]] = ..., result: _Optional[_Union[CommandResult, _Mapping]] = ..., progress: _Optional[_Union[Progress, _Mapping]] = ...) -> None: ...

class ControlMessage(_message.Message):
    __slots__ = ("welcome", "command", "cancel")
    WELCOME_FIELD_NUMBER: _ClassVar[int]
    COMMAND_FIELD_NUMBER: _ClassVar[int]
    CANCEL_FIELD_NUMBER: _ClassVar[int]
    welcome: Welcome
    command: Command
    cancel: Cancel
    def __init__(self, welcome: _Optional[_Union[Welcome, _Mapping]] = ..., command: _Optional[_Union[Command, _Mapping]] = ..., cancel: _Optional[_Union[Cancel, _Mapping]] = ...) -> None: ...

class Hello(_message.Message):
    __slots__ = ("node_id", "agent_version", "facts", "known_epoch")
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    AGENT_VERSION_FIELD_NUMBER: _ClassVar[int]
    FACTS_FIELD_NUMBER: _ClassVar[int]
    KNOWN_EPOCH_FIELD_NUMBER: _ClassVar[int]
    node_id: str
    agent_version: str
    facts: NodeFacts
    known_epoch: int
    def __init__(self, node_id: _Optional[str] = ..., agent_version: _Optional[str] = ..., facts: _Optional[_Union[NodeFacts, _Mapping]] = ..., known_epoch: _Optional[int] = ...) -> None: ...

class Welcome(_message.Message):
    __slots__ = ("cluster_id", "epoch", "server_time_unix")
    CLUSTER_ID_FIELD_NUMBER: _ClassVar[int]
    EPOCH_FIELD_NUMBER: _ClassVar[int]
    SERVER_TIME_UNIX_FIELD_NUMBER: _ClassVar[int]
    cluster_id: str
    epoch: int
    server_time_unix: int
    def __init__(self, cluster_id: _Optional[str] = ..., epoch: _Optional[int] = ..., server_time_unix: _Optional[int] = ...) -> None: ...

class Heartbeat(_message.Message):
    __slots__ = ("seq", "sent_unix", "facts")
    SEQ_FIELD_NUMBER: _ClassVar[int]
    SENT_UNIX_FIELD_NUMBER: _ClassVar[int]
    FACTS_FIELD_NUMBER: _ClassVar[int]
    seq: int
    sent_unix: int
    facts: NodeFacts
    def __init__(self, seq: _Optional[int] = ..., sent_unix: _Optional[int] = ..., facts: _Optional[_Union[NodeFacts, _Mapping]] = ...) -> None: ...

class Cancel(_message.Message):
    __slots__ = ("command_id",)
    COMMAND_ID_FIELD_NUMBER: _ClassVar[int]
    command_id: str
    def __init__(self, command_id: _Optional[str] = ...) -> None: ...

class Progress(_message.Message):
    __slots__ = ("command_id", "pull")
    COMMAND_ID_FIELD_NUMBER: _ClassVar[int]
    PULL_FIELD_NUMBER: _ClassVar[int]
    command_id: str
    pull: PullProgress
    def __init__(self, command_id: _Optional[str] = ..., pull: _Optional[_Union[PullProgress, _Mapping]] = ...) -> None: ...

class PullProgress(_message.Message):
    __slots__ = ("ref", "status", "layers", "bytes_done", "bytes_total", "percent")
    REF_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    LAYERS_FIELD_NUMBER: _ClassVar[int]
    BYTES_DONE_FIELD_NUMBER: _ClassVar[int]
    BYTES_TOTAL_FIELD_NUMBER: _ClassVar[int]
    PERCENT_FIELD_NUMBER: _ClassVar[int]
    ref: str
    status: str
    layers: int
    bytes_done: int
    bytes_total: int
    percent: float
    def __init__(self, ref: _Optional[str] = ..., status: _Optional[str] = ..., layers: _Optional[int] = ..., bytes_done: _Optional[int] = ..., bytes_total: _Optional[int] = ..., percent: _Optional[float] = ...) -> None: ...

class Command(_message.Message):
    __slots__ = ("command_id", "epoch", "timeout_seconds", "run_container", "ensure_directories", "stop_container", "get_container_status", "exec_in_container", "copy_to_container", "copy_dir_to_container", "get_logs", "list_managed_containers", "get_container_by_deployment", "get_container_by_recipe", "image_exists", "image_info", "list_images", "pull_image", "remove_image", "get_facts")
    COMMAND_ID_FIELD_NUMBER: _ClassVar[int]
    EPOCH_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_SECONDS_FIELD_NUMBER: _ClassVar[int]
    RUN_CONTAINER_FIELD_NUMBER: _ClassVar[int]
    ENSURE_DIRECTORIES_FIELD_NUMBER: _ClassVar[int]
    STOP_CONTAINER_FIELD_NUMBER: _ClassVar[int]
    GET_CONTAINER_STATUS_FIELD_NUMBER: _ClassVar[int]
    EXEC_IN_CONTAINER_FIELD_NUMBER: _ClassVar[int]
    COPY_TO_CONTAINER_FIELD_NUMBER: _ClassVar[int]
    COPY_DIR_TO_CONTAINER_FIELD_NUMBER: _ClassVar[int]
    GET_LOGS_FIELD_NUMBER: _ClassVar[int]
    LIST_MANAGED_CONTAINERS_FIELD_NUMBER: _ClassVar[int]
    GET_CONTAINER_BY_DEPLOYMENT_FIELD_NUMBER: _ClassVar[int]
    GET_CONTAINER_BY_RECIPE_FIELD_NUMBER: _ClassVar[int]
    IMAGE_EXISTS_FIELD_NUMBER: _ClassVar[int]
    IMAGE_INFO_FIELD_NUMBER: _ClassVar[int]
    LIST_IMAGES_FIELD_NUMBER: _ClassVar[int]
    PULL_IMAGE_FIELD_NUMBER: _ClassVar[int]
    REMOVE_IMAGE_FIELD_NUMBER: _ClassVar[int]
    GET_FACTS_FIELD_NUMBER: _ClassVar[int]
    command_id: str
    epoch: int
    timeout_seconds: float
    run_container: RunContainer
    ensure_directories: EnsureDirectories
    stop_container: StopContainer
    get_container_status: GetContainerStatus
    exec_in_container: ExecInContainer
    copy_to_container: CopyToContainer
    copy_dir_to_container: CopyDirToContainer
    get_logs: GetLogs
    list_managed_containers: ListManagedContainers
    get_container_by_deployment: GetContainerByDeployment
    get_container_by_recipe: GetContainerByRecipe
    image_exists: ImageExists
    image_info: ImageInfo
    list_images: ListImages
    pull_image: PullImage
    remove_image: RemoveImage
    get_facts: GetFacts
    def __init__(self, command_id: _Optional[str] = ..., epoch: _Optional[int] = ..., timeout_seconds: _Optional[float] = ..., run_container: _Optional[_Union[RunContainer, _Mapping]] = ..., ensure_directories: _Optional[_Union[EnsureDirectories, _Mapping]] = ..., stop_container: _Optional[_Union[StopContainer, _Mapping]] = ..., get_container_status: _Optional[_Union[GetContainerStatus, _Mapping]] = ..., exec_in_container: _Optional[_Union[ExecInContainer, _Mapping]] = ..., copy_to_container: _Optional[_Union[CopyToContainer, _Mapping]] = ..., copy_dir_to_container: _Optional[_Union[CopyDirToContainer, _Mapping]] = ..., get_logs: _Optional[_Union[GetLogs, _Mapping]] = ..., list_managed_containers: _Optional[_Union[ListManagedContainers, _Mapping]] = ..., get_container_by_deployment: _Optional[_Union[GetContainerByDeployment, _Mapping]] = ..., get_container_by_recipe: _Optional[_Union[GetContainerByRecipe, _Mapping]] = ..., image_exists: _Optional[_Union[ImageExists, _Mapping]] = ..., image_info: _Optional[_Union[ImageInfo, _Mapping]] = ..., list_images: _Optional[_Union[ListImages, _Mapping]] = ..., pull_image: _Optional[_Union[PullImage, _Mapping]] = ..., remove_image: _Optional[_Union[RemoveImage, _Mapping]] = ..., get_facts: _Optional[_Union[GetFacts, _Mapping]] = ...) -> None: ...

class CommandResult(_message.Message):
    __slots__ = ("command_id", "failure", "container", "strings", "boolean", "status", "exec", "text", "containers", "image", "images", "pull", "facts")
    COMMAND_ID_FIELD_NUMBER: _ClassVar[int]
    FAILURE_FIELD_NUMBER: _ClassVar[int]
    CONTAINER_FIELD_NUMBER: _ClassVar[int]
    STRINGS_FIELD_NUMBER: _ClassVar[int]
    BOOLEAN_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    EXEC_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    CONTAINERS_FIELD_NUMBER: _ClassVar[int]
    IMAGE_FIELD_NUMBER: _ClassVar[int]
    IMAGES_FIELD_NUMBER: _ClassVar[int]
    PULL_FIELD_NUMBER: _ClassVar[int]
    FACTS_FIELD_NUMBER: _ClassVar[int]
    command_id: str
    failure: CommandFailure
    container: ContainerRef
    strings: StringList
    boolean: BoolValue
    status: ContainerStatus
    exec: ExecOutcome
    text: StringValue
    containers: ContainerList
    image: ImageRef
    images: ImageList
    pull: PullOutcome
    facts: NodeFacts
    def __init__(self, command_id: _Optional[str] = ..., failure: _Optional[_Union[CommandFailure, _Mapping]] = ..., container: _Optional[_Union[ContainerRef, _Mapping]] = ..., strings: _Optional[_Union[StringList, _Mapping]] = ..., boolean: _Optional[_Union[BoolValue, _Mapping]] = ..., status: _Optional[_Union[ContainerStatus, _Mapping]] = ..., exec: _Optional[_Union[ExecOutcome, _Mapping]] = ..., text: _Optional[_Union[StringValue, _Mapping]] = ..., containers: _Optional[_Union[ContainerList, _Mapping]] = ..., image: _Optional[_Union[ImageRef, _Mapping]] = ..., images: _Optional[_Union[ImageList, _Mapping]] = ..., pull: _Optional[_Union[PullOutcome, _Mapping]] = ..., facts: _Optional[_Union[NodeFacts, _Mapping]] = ...) -> None: ...

class CommandFailure(_message.Message):
    __slots__ = ("type", "message")
    TYPE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    type: str
    message: str
    def __init__(self, type: _Optional[str] = ..., message: _Optional[str] = ...) -> None: ...

class RunContainer(_message.Message):
    __slots__ = ("image", "name", "env_vars", "metadata", "privileged", "memory_limit_gb", "shm_size_gb", "pids_limit", "nofile_limit", "cache_dirs", "port_mappings", "entrypoint_clear", "detach", "command", "mounts", "network_host", "ipc_host", "devices", "cap_add", "ulimits", "auto_remove")
    class EnvVarsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    class MountsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    class UlimitsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    IMAGE_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    ENV_VARS_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    PRIVILEGED_FIELD_NUMBER: _ClassVar[int]
    MEMORY_LIMIT_GB_FIELD_NUMBER: _ClassVar[int]
    SHM_SIZE_GB_FIELD_NUMBER: _ClassVar[int]
    PIDS_LIMIT_FIELD_NUMBER: _ClassVar[int]
    NOFILE_LIMIT_FIELD_NUMBER: _ClassVar[int]
    CACHE_DIRS_FIELD_NUMBER: _ClassVar[int]
    PORT_MAPPINGS_FIELD_NUMBER: _ClassVar[int]
    ENTRYPOINT_CLEAR_FIELD_NUMBER: _ClassVar[int]
    DETACH_FIELD_NUMBER: _ClassVar[int]
    COMMAND_FIELD_NUMBER: _ClassVar[int]
    MOUNTS_FIELD_NUMBER: _ClassVar[int]
    NETWORK_HOST_FIELD_NUMBER: _ClassVar[int]
    IPC_HOST_FIELD_NUMBER: _ClassVar[int]
    DEVICES_FIELD_NUMBER: _ClassVar[int]
    CAP_ADD_FIELD_NUMBER: _ClassVar[int]
    ULIMITS_FIELD_NUMBER: _ClassVar[int]
    AUTO_REMOVE_FIELD_NUMBER: _ClassVar[int]
    image: str
    name: str
    env_vars: _containers.ScalarMap[str, str]
    metadata: ContainerMetadata
    privileged: bool
    memory_limit_gb: float
    shm_size_gb: float
    pids_limit: int
    nofile_limit: int
    cache_dirs: _containers.RepeatedScalarFieldContainer[str]
    port_mappings: _containers.RepeatedScalarFieldContainer[str]
    entrypoint_clear: bool
    detach: bool
    command: Cmd
    mounts: _containers.ScalarMap[str, str]
    network_host: bool
    ipc_host: bool
    devices: _containers.RepeatedScalarFieldContainer[str]
    cap_add: _containers.RepeatedScalarFieldContainer[str]
    ulimits: _containers.ScalarMap[str, str]
    auto_remove: bool
    def __init__(self, image: _Optional[str] = ..., name: _Optional[str] = ..., env_vars: _Optional[_Mapping[str, str]] = ..., metadata: _Optional[_Union[ContainerMetadata, _Mapping]] = ..., privileged: _Optional[bool] = ..., memory_limit_gb: _Optional[float] = ..., shm_size_gb: _Optional[float] = ..., pids_limit: _Optional[int] = ..., nofile_limit: _Optional[int] = ..., cache_dirs: _Optional[_Iterable[str]] = ..., port_mappings: _Optional[_Iterable[str]] = ..., entrypoint_clear: _Optional[bool] = ..., detach: _Optional[bool] = ..., command: _Optional[_Union[Cmd, _Mapping]] = ..., mounts: _Optional[_Mapping[str, str]] = ..., network_host: _Optional[bool] = ..., ipc_host: _Optional[bool] = ..., devices: _Optional[_Iterable[str]] = ..., cap_add: _Optional[_Iterable[str]] = ..., ulimits: _Optional[_Mapping[str, str]] = ..., auto_remove: _Optional[bool] = ...) -> None: ...

class EnsureDirectories(_message.Message):
    __slots__ = ("paths",)
    PATHS_FIELD_NUMBER: _ClassVar[int]
    paths: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, paths: _Optional[_Iterable[str]] = ...) -> None: ...

class StopContainer(_message.Message):
    __slots__ = ("name", "timeout")
    NAME_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_FIELD_NUMBER: _ClassVar[int]
    name: str
    timeout: int
    def __init__(self, name: _Optional[str] = ..., timeout: _Optional[int] = ...) -> None: ...

class GetContainerStatus(_message.Message):
    __slots__ = ("name",)
    NAME_FIELD_NUMBER: _ClassVar[int]
    name: str
    def __init__(self, name: _Optional[str] = ...) -> None: ...

class ExecInContainer(_message.Message):
    __slots__ = ("container", "command", "detach", "timeout")
    CONTAINER_FIELD_NUMBER: _ClassVar[int]
    COMMAND_FIELD_NUMBER: _ClassVar[int]
    DETACH_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_FIELD_NUMBER: _ClassVar[int]
    container: str
    command: Cmd
    detach: bool
    timeout: int
    def __init__(self, container: _Optional[str] = ..., command: _Optional[_Union[Cmd, _Mapping]] = ..., detach: _Optional[bool] = ..., timeout: _Optional[int] = ...) -> None: ...

class CopyToContainer(_message.Message):
    __slots__ = ("container", "remote_path", "content", "mode", "timeout", "source_name")
    CONTAINER_FIELD_NUMBER: _ClassVar[int]
    REMOTE_PATH_FIELD_NUMBER: _ClassVar[int]
    CONTENT_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_FIELD_NUMBER: _ClassVar[int]
    SOURCE_NAME_FIELD_NUMBER: _ClassVar[int]
    container: str
    remote_path: str
    content: bytes
    mode: int
    timeout: int
    source_name: str
    def __init__(self, container: _Optional[str] = ..., remote_path: _Optional[str] = ..., content: _Optional[bytes] = ..., mode: _Optional[int] = ..., timeout: _Optional[int] = ..., source_name: _Optional[str] = ...) -> None: ...

class CopyDirToContainer(_message.Message):
    __slots__ = ("container", "remote_path", "tar_gz", "timeout", "source_name")
    CONTAINER_FIELD_NUMBER: _ClassVar[int]
    REMOTE_PATH_FIELD_NUMBER: _ClassVar[int]
    TAR_GZ_FIELD_NUMBER: _ClassVar[int]
    TIMEOUT_FIELD_NUMBER: _ClassVar[int]
    SOURCE_NAME_FIELD_NUMBER: _ClassVar[int]
    container: str
    remote_path: str
    tar_gz: bytes
    timeout: int
    source_name: str
    def __init__(self, container: _Optional[str] = ..., remote_path: _Optional[str] = ..., tar_gz: _Optional[bytes] = ..., timeout: _Optional[int] = ..., source_name: _Optional[str] = ...) -> None: ...

class GetLogs(_message.Message):
    __slots__ = ("name", "tail")
    NAME_FIELD_NUMBER: _ClassVar[int]
    TAIL_FIELD_NUMBER: _ClassVar[int]
    name: str
    tail: int
    def __init__(self, name: _Optional[str] = ..., tail: _Optional[int] = ...) -> None: ...

class ListManagedContainers(_message.Message):
    __slots__ = ("labels",)
    class LabelsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    LABELS_FIELD_NUMBER: _ClassVar[int]
    labels: _containers.ScalarMap[str, str]
    def __init__(self, labels: _Optional[_Mapping[str, str]] = ...) -> None: ...

class GetContainerByDeployment(_message.Message):
    __slots__ = ("deployment",)
    DEPLOYMENT_FIELD_NUMBER: _ClassVar[int]
    deployment: str
    def __init__(self, deployment: _Optional[str] = ...) -> None: ...

class GetContainerByRecipe(_message.Message):
    __slots__ = ("recipe",)
    RECIPE_FIELD_NUMBER: _ClassVar[int]
    recipe: str
    def __init__(self, recipe: _Optional[str] = ...) -> None: ...

class ImageExists(_message.Message):
    __slots__ = ("ref",)
    REF_FIELD_NUMBER: _ClassVar[int]
    ref: str
    def __init__(self, ref: _Optional[str] = ...) -> None: ...

class ImageInfo(_message.Message):
    __slots__ = ("ref",)
    REF_FIELD_NUMBER: _ClassVar[int]
    ref: str
    def __init__(self, ref: _Optional[str] = ...) -> None: ...

class ListImages(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class PullImage(_message.Message):
    __slots__ = ("ref", "want_progress", "interval", "stall_timeout")
    REF_FIELD_NUMBER: _ClassVar[int]
    WANT_PROGRESS_FIELD_NUMBER: _ClassVar[int]
    INTERVAL_FIELD_NUMBER: _ClassVar[int]
    STALL_TIMEOUT_FIELD_NUMBER: _ClassVar[int]
    ref: str
    want_progress: bool
    interval: float
    stall_timeout: float
    def __init__(self, ref: _Optional[str] = ..., want_progress: _Optional[bool] = ..., interval: _Optional[float] = ..., stall_timeout: _Optional[float] = ...) -> None: ...

class RemoveImage(_message.Message):
    __slots__ = ("ref", "force")
    REF_FIELD_NUMBER: _ClassVar[int]
    FORCE_FIELD_NUMBER: _ClassVar[int]
    ref: str
    force: bool
    def __init__(self, ref: _Optional[str] = ..., force: _Optional[bool] = ...) -> None: ...

class GetFacts(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class Cmd(_message.Message):
    __slots__ = ("shell", "argv")
    SHELL_FIELD_NUMBER: _ClassVar[int]
    ARGV_FIELD_NUMBER: _ClassVar[int]
    shell: str
    argv: Argv
    def __init__(self, shell: _Optional[str] = ..., argv: _Optional[_Union[Argv, _Mapping]] = ...) -> None: ...

class Argv(_message.Message):
    __slots__ = ("parts",)
    PARTS_FIELD_NUMBER: _ClassVar[int]
    parts: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, parts: _Optional[_Iterable[str]] = ...) -> None: ...

class BoolValue(_message.Message):
    __slots__ = ("value",)
    VALUE_FIELD_NUMBER: _ClassVar[int]
    value: bool
    def __init__(self, value: _Optional[bool] = ...) -> None: ...

class StringValue(_message.Message):
    __slots__ = ("value",)
    VALUE_FIELD_NUMBER: _ClassVar[int]
    value: str
    def __init__(self, value: _Optional[str] = ...) -> None: ...

class StringList(_message.Message):
    __slots__ = ("values",)
    VALUES_FIELD_NUMBER: _ClassVar[int]
    values: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, values: _Optional[_Iterable[str]] = ...) -> None: ...

class ContainerMetadata(_message.Message):
    __slots__ = ("deployment", "recipe", "image", "mode", "created_at", "memory_limit_gb", "shm_size_gb", "privileged", "generation", "rank", "world_size", "cluster", "role", "node_rank", "head_ip", "ray_enabled")
    DEPLOYMENT_FIELD_NUMBER: _ClassVar[int]
    RECIPE_FIELD_NUMBER: _ClassVar[int]
    IMAGE_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    MEMORY_LIMIT_GB_FIELD_NUMBER: _ClassVar[int]
    SHM_SIZE_GB_FIELD_NUMBER: _ClassVar[int]
    PRIVILEGED_FIELD_NUMBER: _ClassVar[int]
    GENERATION_FIELD_NUMBER: _ClassVar[int]
    RANK_FIELD_NUMBER: _ClassVar[int]
    WORLD_SIZE_FIELD_NUMBER: _ClassVar[int]
    CLUSTER_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    NODE_RANK_FIELD_NUMBER: _ClassVar[int]
    HEAD_IP_FIELD_NUMBER: _ClassVar[int]
    RAY_ENABLED_FIELD_NUMBER: _ClassVar[int]
    deployment: str
    recipe: str
    image: str
    mode: str
    created_at: str
    memory_limit_gb: float
    shm_size_gb: float
    privileged: bool
    generation: int
    rank: int
    world_size: int
    cluster: str
    role: str
    node_rank: int
    head_ip: str
    ray_enabled: bool
    def __init__(self, deployment: _Optional[str] = ..., recipe: _Optional[str] = ..., image: _Optional[str] = ..., mode: _Optional[str] = ..., created_at: _Optional[str] = ..., memory_limit_gb: _Optional[float] = ..., shm_size_gb: _Optional[float] = ..., privileged: _Optional[bool] = ..., generation: _Optional[int] = ..., rank: _Optional[int] = ..., world_size: _Optional[int] = ..., cluster: _Optional[str] = ..., role: _Optional[str] = ..., node_rank: _Optional[int] = ..., head_ip: _Optional[str] = ..., ray_enabled: _Optional[bool] = ...) -> None: ...

class ContainerInfo(_message.Message):
    __slots__ = ("id", "name", "status", "image", "metadata", "labels")
    class LabelsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    ID_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    IMAGE_FIELD_NUMBER: _ClassVar[int]
    METADATA_FIELD_NUMBER: _ClassVar[int]
    LABELS_FIELD_NUMBER: _ClassVar[int]
    id: str
    name: str
    status: str
    image: str
    metadata: ContainerMetadata
    labels: _containers.ScalarMap[str, str]
    def __init__(self, id: _Optional[str] = ..., name: _Optional[str] = ..., status: _Optional[str] = ..., image: _Optional[str] = ..., metadata: _Optional[_Union[ContainerMetadata, _Mapping]] = ..., labels: _Optional[_Mapping[str, str]] = ...) -> None: ...

class ContainerRef(_message.Message):
    __slots__ = ("found", "container")
    FOUND_FIELD_NUMBER: _ClassVar[int]
    CONTAINER_FIELD_NUMBER: _ClassVar[int]
    found: bool
    container: ContainerInfo
    def __init__(self, found: _Optional[bool] = ..., container: _Optional[_Union[ContainerInfo, _Mapping]] = ...) -> None: ...

class ContainerList(_message.Message):
    __slots__ = ("containers",)
    CONTAINERS_FIELD_NUMBER: _ClassVar[int]
    containers: _containers.RepeatedCompositeFieldContainer[ContainerInfo]
    def __init__(self, containers: _Optional[_Iterable[_Union[ContainerInfo, _Mapping]]] = ...) -> None: ...

class ContainerStatus(_message.Message):
    __slots__ = ("status", "running", "id", "state_json", "error")
    STATUS_FIELD_NUMBER: _ClassVar[int]
    RUNNING_FIELD_NUMBER: _ClassVar[int]
    ID_FIELD_NUMBER: _ClassVar[int]
    STATE_JSON_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    status: str
    running: bool
    id: str
    state_json: str
    error: str
    def __init__(self, status: _Optional[str] = ..., running: _Optional[bool] = ..., id: _Optional[str] = ..., state_json: _Optional[str] = ..., error: _Optional[str] = ...) -> None: ...

class ExecOutcome(_message.Message):
    __slots__ = ("returncode", "stdout", "stderr")
    RETURNCODE_FIELD_NUMBER: _ClassVar[int]
    STDOUT_FIELD_NUMBER: _ClassVar[int]
    STDERR_FIELD_NUMBER: _ClassVar[int]
    returncode: int
    stdout: str
    stderr: str
    def __init__(self, returncode: _Optional[int] = ..., stdout: _Optional[str] = ..., stderr: _Optional[str] = ...) -> None: ...

class ImageInfoValue(_message.Message):
    __slots__ = ("id", "size_bytes", "created", "repo_tags", "repo_digests")
    ID_FIELD_NUMBER: _ClassVar[int]
    SIZE_BYTES_FIELD_NUMBER: _ClassVar[int]
    CREATED_FIELD_NUMBER: _ClassVar[int]
    REPO_TAGS_FIELD_NUMBER: _ClassVar[int]
    REPO_DIGESTS_FIELD_NUMBER: _ClassVar[int]
    id: str
    size_bytes: int
    created: str
    repo_tags: _containers.RepeatedScalarFieldContainer[str]
    repo_digests: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, id: _Optional[str] = ..., size_bytes: _Optional[int] = ..., created: _Optional[str] = ..., repo_tags: _Optional[_Iterable[str]] = ..., repo_digests: _Optional[_Iterable[str]] = ...) -> None: ...

class ImageRef(_message.Message):
    __slots__ = ("found", "image")
    FOUND_FIELD_NUMBER: _ClassVar[int]
    IMAGE_FIELD_NUMBER: _ClassVar[int]
    found: bool
    image: ImageInfoValue
    def __init__(self, found: _Optional[bool] = ..., image: _Optional[_Union[ImageInfoValue, _Mapping]] = ...) -> None: ...

class ImageList(_message.Message):
    __slots__ = ("images",)
    IMAGES_FIELD_NUMBER: _ClassVar[int]
    images: _containers.RepeatedCompositeFieldContainer[ImageInfoValue]
    def __init__(self, images: _Optional[_Iterable[_Union[ImageInfoValue, _Mapping]]] = ...) -> None: ...

class PullOutcome(_message.Message):
    __slots__ = ("ref", "repository", "tag", "bytes_done", "bytes_total", "percent", "id", "size_bytes")
    REF_FIELD_NUMBER: _ClassVar[int]
    REPOSITORY_FIELD_NUMBER: _ClassVar[int]
    TAG_FIELD_NUMBER: _ClassVar[int]
    BYTES_DONE_FIELD_NUMBER: _ClassVar[int]
    BYTES_TOTAL_FIELD_NUMBER: _ClassVar[int]
    PERCENT_FIELD_NUMBER: _ClassVar[int]
    ID_FIELD_NUMBER: _ClassVar[int]
    SIZE_BYTES_FIELD_NUMBER: _ClassVar[int]
    ref: str
    repository: str
    tag: str
    bytes_done: int
    bytes_total: int
    percent: float
    id: str
    size_bytes: int
    def __init__(self, ref: _Optional[str] = ..., repository: _Optional[str] = ..., tag: _Optional[str] = ..., bytes_done: _Optional[int] = ..., bytes_total: _Optional[int] = ..., percent: _Optional[float] = ..., id: _Optional[str] = ..., size_bytes: _Optional[int] = ...) -> None: ...

class NodeFacts(_message.Message):
    __slots__ = ("hostname", "boot_id", "machine_id", "os_release", "kernel", "agent_version", "docker_version", "cpu_count", "memory_bytes", "gpu_count", "interfaces", "infiniband_interfaces", "hardware_fingerprint")
    HOSTNAME_FIELD_NUMBER: _ClassVar[int]
    BOOT_ID_FIELD_NUMBER: _ClassVar[int]
    MACHINE_ID_FIELD_NUMBER: _ClassVar[int]
    OS_RELEASE_FIELD_NUMBER: _ClassVar[int]
    KERNEL_FIELD_NUMBER: _ClassVar[int]
    AGENT_VERSION_FIELD_NUMBER: _ClassVar[int]
    DOCKER_VERSION_FIELD_NUMBER: _ClassVar[int]
    CPU_COUNT_FIELD_NUMBER: _ClassVar[int]
    MEMORY_BYTES_FIELD_NUMBER: _ClassVar[int]
    GPU_COUNT_FIELD_NUMBER: _ClassVar[int]
    INTERFACES_FIELD_NUMBER: _ClassVar[int]
    INFINIBAND_INTERFACES_FIELD_NUMBER: _ClassVar[int]
    HARDWARE_FINGERPRINT_FIELD_NUMBER: _ClassVar[int]
    hostname: str
    boot_id: str
    machine_id: str
    os_release: str
    kernel: str
    agent_version: str
    docker_version: str
    cpu_count: int
    memory_bytes: int
    gpu_count: int
    interfaces: _containers.RepeatedCompositeFieldContainer[NetworkInterface]
    infiniband_interfaces: _containers.RepeatedScalarFieldContainer[str]
    hardware_fingerprint: str
    def __init__(self, hostname: _Optional[str] = ..., boot_id: _Optional[str] = ..., machine_id: _Optional[str] = ..., os_release: _Optional[str] = ..., kernel: _Optional[str] = ..., agent_version: _Optional[str] = ..., docker_version: _Optional[str] = ..., cpu_count: _Optional[int] = ..., memory_bytes: _Optional[int] = ..., gpu_count: _Optional[int] = ..., interfaces: _Optional[_Iterable[_Union[NetworkInterface, _Mapping]]] = ..., infiniband_interfaces: _Optional[_Iterable[str]] = ..., hardware_fingerprint: _Optional[str] = ...) -> None: ...

class NetworkInterface(_message.Message):
    __slots__ = ("name", "ip", "mac", "speed_mbps")
    NAME_FIELD_NUMBER: _ClassVar[int]
    IP_FIELD_NUMBER: _ClassVar[int]
    MAC_FIELD_NUMBER: _ClassVar[int]
    SPEED_MBPS_FIELD_NUMBER: _ClassVar[int]
    name: str
    ip: str
    mac: str
    speed_mbps: int
    def __init__(self, name: _Optional[str] = ..., ip: _Optional[str] = ..., mac: _Optional[str] = ..., speed_mbps: _Optional[int] = ...) -> None: ...
