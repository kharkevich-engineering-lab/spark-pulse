//! The container half of the node's Docker surface.
//!
//! Every function here has a counterpart in `spark_pulse/tools/docker.py`, and
//! the contract test drives both against the same scenarios. Where the two
//! could plausibly differ, the reason they must not is written down next to
//! the code rather than left for someone to rediscover.
//!
//! One difference from the Python service is deliberate and is an improvement:
//! nothing here shells out. `DockerService.copy_to_container` runs `docker cp`,
//! which means the *Docker CLI* has to be installed on every node beside the
//! daemon. This agent speaks the Engine API over the socket for everything, so
//! a node needs a daemon and nothing else.

use std::collections::{BTreeMap, HashMap};

use bollard::models::{
    ContainerCreateBody, ContainerSummary, DeviceMapping, DeviceRequest, HostConfig,
    ResourcesUlimits, RestartPolicy, RestartPolicyNameEnum,
};
use bollard::query_parameters::{
    CreateContainerOptionsBuilder, InspectContainerOptions, ListContainersOptionsBuilder,
    LogsOptionsBuilder, RemoveContainerOptionsBuilder, StartContainerOptions,
    StopContainerOptionsBuilder,
};
use bollard::Docker;
use futures_util::StreamExt;

use super::labels;
use super::{DockerResult, OpError};
use crate::proto::{ContainerInfo, ContainerStatus, ExecOutcome, RunContainer};

/// Bytes in a gigabyte, as `DockerService._gb_to_bytes` computes it.
fn gb_to_bytes(gb: f64) -> i64 {
    (gb * 1024.0 * 1024.0 * 1024.0) as i64
}

/// Memory-swap defaults to the limit plus 10 GB, matching
/// `DockerService._calc_memory_swap`. A rank that swaps is a rank that has
/// already lost, but an unset swap limit means *unlimited*, which is worse.
fn memory_swap(memory_limit_gb: f64) -> i64 {
    gb_to_bytes(memory_limit_gb + 10.0)
}

/// Create and start a container carrying spark-pulse labels.
pub async fn run_container(docker: &Docker, req: RunContainer) -> DockerResult<ContainerInfo> {
    let mut metadata = req.metadata.clone().unwrap_or_default();
    let label_map = labels::prepare(&mut metadata, &req.name, &req.image);
    let config = create_config(&req, &label_map);

    let created = docker
        .create_container(
            Some(
                CreateContainerOptionsBuilder::default()
                    .name(&req.name)
                    .build(),
            ),
            config,
        )
        .await
        .map_err(|error| match error {
            bollard::errors::Error::DockerResponseServerError {
                status_code: 404, ..
            } => OpError::new("RuntimeError", format!("Image not found: {}", req.image)),
            other => OpError::new("RuntimeError", format!("Docker API error: {other}")),
        })?;

    docker
        .start_container(&req.name, None::<StartContainerOptions>)
        .await
        .map_err(|error| OpError::new("RuntimeError", format!("Docker API error: {error}")))?;

    Ok(ContainerInfo {
        id: created.id,
        name: req.name.clone(),
        status: "running".into(),
        image: req.image.clone(),
        metadata: Some(metadata),
        labels: label_map.into_iter().collect(),
    })
}

/// The create request, as a pure function of what was asked for.
///
/// Separated from the call so it can be tested without a daemon, which
/// matters more here than anywhere else in this file: every field is a
/// decision `DockerService` also makes, and a divergence in any of them — a
/// missing swap limit, a restart policy that is not "no", a lost `IPC_LOCK` —
/// produces a container that *starts* and then behaves differently under
/// load. That is the worst kind of difference to go looking for.
///
/// It also cannot be exercised end to end on a machine without a GPU: every
/// container this builds asks for one, as it must on a Spark. So the shaping
/// is tested here and the daemon interaction is tested separately.
pub fn create_config(
    req: &RunContainer,
    label_map: &BTreeMap<String, String>,
) -> ContainerCreateBody {
    let privileged = req.privileged.unwrap_or(true);
    let shm_size_gb = req.shm_size_gb.unwrap_or(64.0);
    let auto_remove = req.auto_remove.unwrap_or(true);

    // Cache directories mount at the same path inside and out, which is what
    // makes a path in a recipe mean the same thing on either side.
    let mut binds: Vec<String> = req
        .cache_dirs
        .iter()
        .map(|dir| format!("{dir}:{dir}:rw"))
        .collect();
    for (host, container) in &req.mounts {
        binds.push(format!("{host}:{container}:rw"));
    }

    // nofile first and never overridden: the engines open a file descriptor
    // per shard per connection, and the default 1024 is reached long before
    // anything else goes wrong.
    let mut ulimits = vec![ResourcesUlimits {
        name: Some("nofile".into()),
        soft: Some(req.nofile_limit.unwrap_or(1_048_576) as i64),
        hard: Some(req.nofile_limit.unwrap_or(1_048_576) as i64),
    }];
    for (name, raw) in &req.ulimits {
        if name == "nofile" {
            continue;
        }
        let (soft, hard) = raw.split_once(':').unwrap_or((raw.as_str(), raw.as_str()));
        let Ok(soft) = soft.trim().parse::<i64>() else {
            continue;
        };
        let hard = hard.trim().parse::<i64>().unwrap_or(soft);
        ulimits.push(ResourcesUlimits {
            name: Some(name.clone()),
            soft: Some(soft),
            hard: Some(hard),
        });
    }

    // `None` keeps the long-standing behaviour: host networking unless ports
    // are published, because published ports and host networking are mutually
    // exclusive and the rendezvous needs host networking.
    let network_mode = match req.network_host {
        None => req.port_mappings.is_empty().then(|| "host".to_string()),
        Some(true) => Some("host".to_string()),
        Some(false) => None,
    };

    // IPC_LOCK is what lets the engine pin memory for RDMA. A privileged
    // container already has it; an unprivileged one has to be given it, or
    // NCCL silently falls back off the fabric.
    let mut cap_add: Vec<String> = req.cap_add.clone();
    if !privileged && !cap_add.iter().any(|c| c == "IPC_LOCK") {
        cap_add.push("IPC_LOCK".into());
    }

    let host_config = HostConfig {
        binds: (!binds.is_empty()).then_some(binds),
        privileged: Some(privileged),
        pids_limit: req.pids_limit.map(|v| v as i64).or(Some(4096)),
        shm_size: Some(gb_to_bytes(shm_size_gb)),
        ulimits: Some(ulimits),
        auto_remove: Some(auto_remove),
        // Never restart. A rank is one member of a sharded gang: a rebooting
        // node must not resurrect it into a deployment that was torn down.
        restart_policy: Some(RestartPolicy {
            name: Some(RestartPolicyNameEnum::NO),
            maximum_retry_count: None,
        }),
        device_requests: Some(vec![DeviceRequest {
            count: Some(-1),
            capabilities: Some(vec![vec!["gpu".to_string()]]),
            ..Default::default()
        }]),
        memory: req.memory_limit_gb.filter(|gb| *gb > 0.0).map(gb_to_bytes),
        memory_swap: req.memory_limit_gb.filter(|gb| *gb > 0.0).map(memory_swap),
        cap_add: (!privileged && !cap_add.is_empty()).then_some(cap_add),
        network_mode,
        ipc_mode: req.ipc_host.unwrap_or(false).then(|| "host".to_string()),
        devices: (!req.devices.is_empty()).then(|| {
            req.devices
                .iter()
                .map(|path| DeviceMapping {
                    path_on_host: Some(path.clone()),
                    path_in_container: Some(path.clone()),
                    cgroup_permissions: Some("rwm".into()),
                })
                .collect()
        }),
        port_bindings: None,
        ..Default::default()
    };

    ContainerCreateBody {
        image: Some(req.image.clone()),
        env: Some(
            req.env_vars
                .iter()
                .map(|(k, v)| format!("{k}={v}"))
                .collect(),
        ),
        labels: Some(label_map.clone().into_iter().collect::<HashMap<_, _>>()),
        // An empty entrypoint clears the image's, which is what lets the
        // native runtime start an idle container and exec into it.
        entrypoint: req.entrypoint_clear.unwrap_or(true).then(Vec::new),
        cmd: req.command.as_ref().and_then(super::decode_cmd),
        host_config: Some(host_config),
        ..Default::default()
    }
}

/// Stop and remove a container. False when it was not there.
///
/// Stop **and remove**: a container that is merely stopped still owns its name
/// and its ports, so a redeploy of the same rank collides with the corpse of
/// the last one. `missing` is the only state that frees a rank's ports, and
/// this is what produces it.
pub async fn stop_container(docker: &Docker, name: &str, timeout: Option<i32>) -> bool {
    let options = StopContainerOptionsBuilder::default()
        .t(timeout.unwrap_or(30))
        .build();
    match docker.stop_container(name, Some(options)).await {
        Ok(()) => {}
        Err(error) if is_not_found(&error) => return false,
        Err(error) => {
            tracing::error!(%name, %error, "failed to stop container");
            return false;
        }
    }
    let remove = RemoveContainerOptionsBuilder::default().force(true).build();
    match docker.remove_container(name, Some(remove)).await {
        Ok(()) => true,
        Err(error) if is_not_found(&error) => false,
        Err(error) => {
            tracing::error!(%name, %error, "failed to remove container");
            false
        }
    }
}

/// Three kinds of answer, and the difference between them is load-bearing.
///
/// Docker's own vocabulary (`running`, `exited`, `created`, `paused`,
/// `restarting`, `dead`) when the daemon described the container; `missing`
/// when the daemon said there is no such container; and `unknown` when the
/// daemon did not answer at all.
///
/// `unknown` is emphatically not `missing`. Nothing about a silent daemon is
/// evidence that a container is gone, and the orphan sweep frees a rank's
/// GPU and ports on exactly that evidence.
pub async fn get_container_status(docker: &Docker, name: &str) -> ContainerStatus {
    match docker
        .inspect_container(name, None::<InspectContainerOptions>)
        .await
    {
        Ok(response) => {
            let state = response.state.clone();
            let status = state
                .as_ref()
                .and_then(|s| s.status.as_ref())
                .map(|s| s.to_string())
                .unwrap_or_default();
            ContainerStatus {
                running: status == "running",
                status,
                id: response.id,
                state_json: state
                    .as_ref()
                    .and_then(|s| serde_json::to_string(s).ok())
                    .unwrap_or_else(|| "{}".into()),
                error: None,
            }
        }
        Err(error) if is_not_found(&error) => ContainerStatus {
            status: "missing".into(),
            running: false,
            id: None,
            state_json: "{}".into(),
            error: Some(format!("Container '{name}' not found")),
        },
        Err(error) => ContainerStatus {
            status: "unknown".into(),
            running: false,
            id: None,
            state_json: "{}".into(),
            error: Some(error.to_string()),
        },
    }
}

/// Execute a command inside a running container.
///
/// A detached exec returns an empty successful result, because there is
/// nothing to wait for and inventing an exit code for work that has not
/// finished would be a lie the caller cannot detect.
pub async fn exec_in_container(
    docker: &Docker,
    container: &str,
    command: Vec<String>,
    detach: bool,
) -> DockerResult<ExecOutcome> {
    let config = bollard::models::ExecConfig {
        cmd: Some(command),
        attach_stdout: Some(!detach),
        attach_stderr: Some(!detach),
        ..Default::default()
    };
    let created = docker
        .create_exec(container, config)
        .await
        .map_err(OpError::from_docker)?;

    if detach {
        docker
            .start_exec(
                &created.id,
                Some(bollard::exec::StartExecOptions {
                    detach: true,
                    ..Default::default()
                }),
            )
            .await
            .map_err(OpError::from_docker)?;
        return Ok(ExecOutcome {
            returncode: 0,
            stdout: String::new(),
            stderr: String::new(),
        });
    }

    let started = docker
        .start_exec(&created.id, None)
        .await
        .map_err(OpError::from_docker)?;
    let (mut stdout, mut stderr) = (String::new(), String::new());
    if let bollard::exec::StartExecResults::Attached { mut output, .. } = started {
        while let Some(chunk) = output.next().await {
            match chunk.map_err(OpError::from_docker)? {
                bollard::container::LogOutput::StdOut { message } => {
                    stdout.push_str(&String::from_utf8_lossy(&message))
                }
                bollard::container::LogOutput::StdErr { message } => {
                    stderr.push_str(&String::from_utf8_lossy(&message))
                }
                other => stdout.push_str(&String::from_utf8_lossy(&other.into_bytes())),
            }
        }
    }
    // Inspecting *after* draining: the exit code is only final once the
    // process has finished writing, and reading it early reports 0 for a
    // command that is about to fail.
    let inspected = docker
        .inspect_exec(&created.id)
        .await
        .map_err(OpError::from_docker)?;
    Ok(ExecOutcome {
        returncode: inspected.exit_code.unwrap_or(0) as i32,
        stdout,
        stderr,
    })
}

/// The tail of a container's stdout and stderr, interleaved as Docker sends it.
pub async fn get_logs(docker: &Docker, name: &str, tail: Option<i32>) -> DockerResult<String> {
    let options = LogsOptionsBuilder::default()
        .stdout(true)
        .stderr(true)
        .tail(&tail.unwrap_or(200).to_string())
        .build();
    let mut stream = docker.logs(name, Some(options));
    let mut out = String::new();
    while let Some(chunk) = stream.next().await {
        match chunk {
            Ok(line) => out.push_str(&String::from_utf8_lossy(&line.into_bytes())),
            // A missing container is a *string*, not an error, because every
            // caller displays this and a 404 dump helps nobody.
            Err(error) if is_not_found(&error) => {
                return Ok(format!("Container '{name}' not found"))
            }
            Err(error) => return Err(OpError::from_docker(error)),
        }
    }
    Ok(out)
}

/// Every spark-pulse managed container, filtered by label.
pub async fn list_managed(
    docker: &Docker,
    wanted: &BTreeMap<String, String>,
) -> DockerResult<Vec<ContainerInfo>> {
    let mut filters = HashMap::new();
    filters.insert("label".to_string(), labels::label_filter(wanted));
    let options = ListContainersOptionsBuilder::default()
        .all(true)
        .filters(&filters)
        .build();
    let summaries = docker
        .list_containers(Some(options))
        .await
        .map_err(OpError::from_docker)?;
    // Filtered again here, because the daemon's label filter and ours have to
    // agree and only one of them is ours.
    Ok(summaries
        .into_iter()
        .map(summary_to_info)
        .filter(|info| {
            let owned: BTreeMap<String, String> = info.labels.clone().into_iter().collect();
            labels::labels_match(&owned, wanted)
        })
        .collect())
}

/// The container carrying one deployment label, or None.
pub async fn by_deployment(
    docker: &Docker,
    deployment: &str,
) -> DockerResult<Option<ContainerInfo>> {
    let wanted: BTreeMap<String, String> =
        [(labels::DEPLOYMENT.to_string(), deployment.to_string())]
            .into_iter()
            .collect();
    let found: Vec<ContainerInfo> = list_unfiltered(docker, &wanted).await?;
    Ok(found.into_iter().next())
}

/// Every container carrying one recipe label.
pub async fn by_recipe(docker: &Docker, recipe: &str) -> DockerResult<Vec<ContainerInfo>> {
    let wanted: BTreeMap<String, String> = [(labels::RECIPE.to_string(), recipe.to_string())]
        .into_iter()
        .collect();
    list_unfiltered(docker, &wanted).await
}

/// The daemon's filter only — no second pass.
///
/// `get_container_by_deployment` and `get_container_by_recipe` in the Python
/// service do not re-filter, and this must not either: re-filtering would make
/// them disagree with it for a label whose value is empty.
async fn list_unfiltered(
    docker: &Docker,
    wanted: &BTreeMap<String, String>,
) -> DockerResult<Vec<ContainerInfo>> {
    let mut filters = HashMap::new();
    filters.insert("label".to_string(), labels::label_filter(wanted));
    let options = ListContainersOptionsBuilder::default()
        .all(true)
        .filters(&filters)
        .build();
    Ok(docker
        .list_containers(Some(options))
        .await
        .map_err(OpError::from_docker)?
        .into_iter()
        .map(summary_to_info)
        .collect())
}

fn summary_to_info(summary: ContainerSummary) -> ContainerInfo {
    let label_map: BTreeMap<String, String> =
        summary.labels.unwrap_or_default().into_iter().collect();
    let metadata = labels::from_labels(&label_map);
    ContainerInfo {
        id: summary.id.unwrap_or_default(),
        // Docker returns names with a leading slash; every caller wants the
        // name it asked for.
        name: summary
            .names
            .unwrap_or_default()
            .first()
            .map(|n| n.trim_start_matches('/').to_string())
            .unwrap_or_default(),
        status: summary.state.map(|s| s.to_string()).unwrap_or_default(),
        image: summary.image.unwrap_or_default(),
        metadata: Some(metadata),
        labels: label_map.into_iter().collect(),
    }
}

/// Whether an error is the daemon saying "no such thing".
///
/// By status code, not by message: the difference between "not there" and "we
/// could not ask" is the whole point of `get_container_status`'s three
/// answers, and a substring search over an English message is how the previous
/// transport got it wrong three times.
pub fn is_not_found(error: &bollard::errors::Error) -> bool {
    matches!(
        error,
        bollard::errors::Error::DockerResponseServerError {
            status_code: 404,
            ..
        }
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::proto::{Cmd, RunContainer};

    fn request(name: &str) -> RunContainer {
        RunContainer {
            image: "img:1".into(),
            name: name.into(),
            ..Default::default()
        }
    }

    fn config_for(req: &RunContainer) -> (ContainerCreateBody, BTreeMap<String, String>) {
        let mut metadata = req.metadata.clone().unwrap_or_default();
        let label_map = labels::prepare(&mut metadata, &req.name, &req.image);
        (create_config(req, &label_map), label_map)
    }

    fn host(req: &RunContainer) -> HostConfig {
        config_for(req).0.host_config.unwrap()
    }

    #[test]
    fn a_rank_never_restarts() {
        // A rank is one member of a sharded gang. A rebooting node must not
        // resurrect it into a deployment that was torn down.
        let policy = host(&request("c")).restart_policy.unwrap();
        assert_eq!(policy.name, Some(RestartPolicyNameEnum::NO));
    }

    #[test]
    fn every_container_asks_for_the_gpu() {
        let requests = host(&request("c")).device_requests.unwrap();
        assert_eq!(requests.len(), 1);
        assert_eq!(requests[0].count, Some(-1));
        assert_eq!(
            requests[0].capabilities,
            Some(vec![vec!["gpu".to_string()]])
        );
    }

    #[test]
    fn swap_is_the_memory_limit_plus_ten_gigabytes() {
        // Unset swap means *unlimited*, which is worse than a rank that swaps.
        let mut req = request("c");
        req.memory_limit_gb = Some(100.0);
        let config = host(&req);
        assert_eq!(config.memory, Some(100 * 1024 * 1024 * 1024));
        assert_eq!(config.memory_swap, Some(110 * 1024 * 1024 * 1024));
    }

    #[test]
    fn no_memory_limit_means_no_swap_limit_either() {
        let config = host(&request("c"));
        assert_eq!(config.memory, None);
        assert_eq!(config.memory_swap, None);
    }

    #[test]
    fn the_defaults_are_the_ones_docker_service_publishes() {
        let config = host(&request("c"));
        assert_eq!(config.shm_size, Some(64 * 1024 * 1024 * 1024));
        assert_eq!(config.pids_limit, Some(4096));
        assert_eq!(config.privileged, Some(true));
        assert_eq!(config.auto_remove, Some(true));
        let ulimits = config.ulimits.unwrap();
        assert_eq!(ulimits[0].name.as_deref(), Some("nofile"));
        assert_eq!(ulimits[0].soft, Some(1_048_576));
        assert_eq!(ulimits[0].hard, Some(1_048_576));
    }

    #[test]
    fn host_networking_unless_ports_are_published() {
        // The rendezvous needs host networking; published ports and host
        // networking are mutually exclusive, so publishing implies bridge.
        assert_eq!(host(&request("c")).network_mode.as_deref(), Some("host"));

        let mut with_ports = request("c");
        with_ports.port_mappings = vec!["8000:8000".into()];
        assert_eq!(host(&with_ports).network_mode, None);

        // An explicit answer is honoured either way.
        let mut forced = request("c");
        forced.port_mappings = vec!["8000:8000".into()];
        forced.network_host = Some(true);
        assert_eq!(host(&forced).network_mode.as_deref(), Some("host"));

        let mut refused = request("c");
        refused.network_host = Some(false);
        assert_eq!(host(&refused).network_mode, None);
    }

    #[test]
    fn ipc_lock_is_added_only_when_it_has_to_be() {
        // A privileged container already has it. An unprivileged one needs it
        // or NCCL silently falls back off the fabric.
        assert_eq!(host(&request("c")).cap_add, None);

        let mut unprivileged = request("c");
        unprivileged.privileged = Some(false);
        let caps = host(&unprivileged).cap_add.unwrap();
        assert!(caps.contains(&"IPC_LOCK".to_string()));

        // And not twice, when the caller already asked for it.
        let mut asked = request("c");
        asked.privileged = Some(false);
        asked.cap_add = vec!["IPC_LOCK".into()];
        let caps = host(&asked).cap_add.unwrap();
        assert_eq!(caps.iter().filter(|c| *c == "IPC_LOCK").count(), 1);
    }

    #[test]
    fn caches_mount_at_the_same_path_inside_and_out() {
        // Which is what makes a path in a recipe mean the same thing on either
        // side of the container boundary.
        let mut req = request("c");
        req.cache_dirs = vec!["/home/spark/.cache/vllm".into()];
        req.mounts = [("/models".to_string(), "/mnt/models".to_string())]
            .into_iter()
            .collect();
        let binds = host(&req).binds.unwrap();
        assert!(binds.contains(&"/home/spark/.cache/vllm:/home/spark/.cache/vllm:rw".to_string()));
        assert!(binds.contains(&"/models:/mnt/models:rw".to_string()));
    }

    #[test]
    fn devices_are_exposed_read_write_mknod() {
        let mut req = request("c");
        req.devices = vec!["/dev/infiniband".into()];
        let devices = host(&req).devices.unwrap();
        assert_eq!(devices[0].path_on_host.as_deref(), Some("/dev/infiniband"));
        assert_eq!(
            devices[0].path_in_container.as_deref(),
            Some("/dev/infiniband")
        );
        assert_eq!(devices[0].cgroup_permissions.as_deref(), Some("rwm"));
    }

    #[test]
    fn clearing_the_entrypoint_is_what_lets_a_rank_be_exec_into() {
        // The native runtime starts an idle container and execs the serve
        // script into it, which only works if the image's entrypoint is gone.
        let config = config_for(&request("c")).0;
        assert_eq!(config.entrypoint, Some(vec![]));

        let mut keep = request("c");
        keep.entrypoint_clear = Some(false);
        assert_eq!(config_for(&keep).0.entrypoint, None);
    }

    #[test]
    fn a_shell_command_and_an_argv_are_not_the_same_thing() {
        let mut shell = request("c");
        shell.command = Some(Cmd {
            form: Some(crate::proto::cmd::Form::Shell("sleep infinity".into())),
        });
        assert_eq!(
            config_for(&shell).0.cmd,
            Some(vec!["sh".into(), "-c".into(), "sleep infinity".into()])
        );

        let mut argv = request("c");
        argv.command = Some(Cmd {
            form: Some(crate::proto::cmd::Form::Argv(crate::proto::Argv {
                parts: vec!["sleep".into(), "infinity".into()],
            })),
        });
        assert_eq!(
            config_for(&argv).0.cmd,
            Some(vec!["sleep".into(), "infinity".into()])
        );
    }

    #[test]
    fn the_environment_and_the_labels_both_reach_the_container() {
        let mut req = request("c");
        req.env_vars = [("RANK".to_string(), "0".to_string())]
            .into_iter()
            .collect();
        let (config, label_map) = config_for(&req);
        assert!(config.env.unwrap().contains(&"RANK=0".to_string()));
        let applied = config.labels.unwrap();
        assert_eq!(
            applied.get(labels::MANAGED).map(String::as_str),
            Some("true")
        );
        assert_eq!(applied.get(labels::NAME).map(String::as_str), Some("c"));
        assert_eq!(applied.len(), label_map.len());
    }

    #[test]
    fn an_extra_ulimit_is_added_and_nofile_cannot_be_overridden() {
        let mut req = request("c");
        req.ulimits = [
            ("nofile".to_string(), "10".to_string()),
            ("memlock".to_string(), "-1:-1".to_string()),
            ("stack".to_string(), "67108864".to_string()),
        ]
        .into_iter()
        .collect();
        let ulimits = host(&req).ulimits.unwrap();
        let nofile: Vec<_> = ulimits
            .iter()
            .filter(|u| u.name.as_deref() == Some("nofile"))
            .collect();
        assert_eq!(nofile.len(), 1, "nofile must not be duplicated");
        assert_eq!(nofile[0].soft, Some(1_048_576), "nofile is not overridable");
        let stack = ulimits
            .iter()
            .find(|u| u.name.as_deref() == Some("stack"))
            .unwrap();
        // One value means soft and hard are the same, as Python's partition does.
        assert_eq!(stack.soft, Some(67_108_864));
        assert_eq!(stack.hard, Some(67_108_864));
        let memlock = ulimits
            .iter()
            .find(|u| u.name.as_deref() == Some("memlock"))
            .unwrap();
        assert_eq!(memlock.soft, Some(-1));
        assert_eq!(memlock.hard, Some(-1));
    }
}
