//! Running one command against this machine's Docker daemon.
//!
//! Two rules govern everything here, and they are the reason the agent exists
//! rather than being a convenience:
//!
//! **Outcomes travel as payload, never as a status.** `execute` does not fail.
//! Every path returns a `CommandResult` whose outcome is a typed success or a
//! `CommandFailure`. A result that arrives means the node was reachable and
//! the outcome is definite; no result means unreachable and unknown. A
//! gracefully shutting-down gRPC server also returns UNAVAILABLE, so a status
//! code cannot carry that distinction and is never asked to.
//!
//! **Fencing happens at the resource.** A command carrying an epoch older than
//! the highest this agent has seen is refused *here* — by the process that
//! owns the Docker daemon — so a command issued by a control plane that has
//! since been replaced cannot act even if it is still in flight somewhere.
//! There is no leader election to be on the wrong side of.

pub mod containers;
pub mod copy;
pub mod images;
pub mod labels;

use std::collections::BTreeMap;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;

use crate::facts;
use crate::proto::{
    agent_message, command, command_result, AgentMessage, Command, CommandFailure, CommandResult,
    ContainerList, ContainerRef, ImageList, ImageRef, NodeFacts, Progress, PullProgress,
    StringList, StringValue,
};

/// The exception class name the control plane re-raises for a stale epoch.
///
/// These names are interop surface: `CommandFailure.type` carries the class
/// name so a caller can react to the *kind* of failure without matching on a
/// message, and `spark_pulse.agent.sync_service.CONTRACT_EXCEPTIONS` maps some
/// of them back to real Python exception types. A name invented here that
/// Python does not know becomes a generic `NodeOperationError`, which is
/// correct but less useful — so they are the Python names.
pub const STALE_EPOCH: &str = "StaleEpochError";

/// A failure with the *name of the exception* the control plane should see.
///
/// `CommandFailure.type` carries a class name so a caller can react to the
/// kind of failure without matching on a message, and
/// `spark_pulse.agent.sync_service.CONTRACT_EXCEPTIONS` maps some of them back
/// to real Python exception types — `PullCancelled` is caught by type in three
/// places, and a cancelled pull arriving as a generic error would be recorded
/// as a deployment failure instead of a teardown.
#[derive(Debug, Clone)]
pub struct OpError {
    pub kind: String,
    pub message: String,
}

impl OpError {
    pub fn new(kind: &str, message: impl Into<String>) -> Self {
        Self {
            kind: kind.to_string(),
            message: message.into(),
        }
    }

    /// A Docker error, named the way the Python service names it.
    ///
    /// `DockerService` lets the SDK's exceptions propagate and the agent
    /// records their class name, so anything the daemon refuses arrives as a
    /// `RuntimeError` there. Matching that keeps the two agents' failures
    /// indistinguishable to a caller, which is what lets one replace the other
    /// without every error handler needing to know which is running.
    pub fn from_docker(error: bollard::errors::Error) -> Self {
        Self::new("RuntimeError", error.to_string())
    }
}

pub type DockerResult<T> = std::result::Result<T, OpError>;

/// What one command is allowed to know about the session running it.
///
/// Two things, and both exist so a long operation stays answerable while it
/// runs: whether it has been asked to stop, and where to report progress.
pub struct CommandContext {
    pub cancel: Arc<AtomicBool>,
    pub progress: Option<ProgressSink>,
}

impl CommandContext {
    /// A context for an operation nobody is watching and nobody will cancel.
    pub fn detached() -> Self {
        Self {
            cancel: Arc::new(AtomicBool::new(false)),
            progress: None,
        }
    }
}

/// Where pull progress goes: onto the same stream the command arrived on.
pub struct ProgressSink {
    command_id: String,
    outbox: tokio::sync::mpsc::Sender<AgentMessage>,
}

impl ProgressSink {
    pub fn new(command_id: String, outbox: tokio::sync::mpsc::Sender<AgentMessage>) -> Self {
        Self { command_id, outbox }
    }

    /// Report progress, or drop it.
    ///
    /// `try_send`, deliberately: **a progress message is never an outcome**.
    /// If the outbox is full the pull must keep pulling rather than wait for
    /// room to describe itself, and a caller that misses an intermediate
    /// percentage has lost nothing — the result is what says the pull
    /// finished.
    fn send(&self, pull: PullProgress) {
        let message = AgentMessage {
            body: Some(agent_message::Body::Progress(Progress {
                command_id: self.command_id.clone(),
                pull: Some(pull),
            })),
        };
        if self.outbox.try_send(message).is_err() {
            tracing::trace!(command = %self.command_id, "dropped a progress event");
        }
    }
}

/// A `Cmd` as Docker wants it: an argv list, or a shell string split the way
/// `DockerService` hands one to the SDK.
///
/// The proto keeps the two forms apart because they mean different things — an
/// argv list is executed directly, a shell string goes through `sh -c` — and
/// collapsing them is how a command containing a space becomes two arguments.
pub fn decode_cmd(cmd: &crate::proto::Cmd) -> Option<Vec<String>> {
    use crate::proto::cmd::Form;
    match cmd.form.as_ref()? {
        Form::Argv(argv) => Some(argv.parts.clone()),
        Form::Shell(shell) => Some(vec!["sh".into(), "-c".into(), shell.clone()]),
    }
}

/// Executes commands against this machine's Docker daemon.
pub struct Executor {
    docker: Option<bollard::Docker>,
    epoch: AtomicU64,
}

impl Executor {
    /// Connect to the local Docker daemon, or record that we could not.
    ///
    /// A node whose daemon is down still starts, connects and *reports* that
    /// its daemon is down. Failing to construct here would mean the node never
    /// appears at all, and "unreachable" is a much worse description of a
    /// machine that is running fine with a stopped Docker.
    pub fn connect() -> Self {
        let docker = match bollard::Docker::connect_with_defaults() {
            Ok(docker) => Some(docker),
            Err(error) => {
                tracing::warn!(%error, "no Docker daemon; the agent will report it");
                None
            }
        };
        Self {
            docker,
            epoch: AtomicU64::new(0),
        }
    }

    /// The highest controller epoch seen.
    pub fn epoch(&self) -> u64 {
        self.epoch.load(Ordering::SeqCst)
    }

    /// Record a controller epoch, keeping the highest.
    pub fn note_epoch(&self, epoch: u64) {
        self.epoch.fetch_max(epoch, Ordering::SeqCst);
    }

    /// The daemon's version string, or empty when there is no daemon.
    ///
    /// Asked on every heartbeat, so it must never be the thing that first
    /// connects: a daemon that is down would then stop the heartbeat and the
    /// node would report as unreachable rather than as having no daemon.
    pub async fn docker_version(&self) -> String {
        let Some(docker) = &self.docker else {
            return String::new();
        };
        match docker.version().await {
            Ok(version) => version.version.unwrap_or_default(),
            Err(error) => {
                tracing::debug!(%error, "the Docker daemon did not answer version()");
                String::new()
            }
        }
    }

    /// Describe this machine, including whatever Docker says about itself.
    pub async fn collect_facts(&self) -> NodeFacts {
        facts::collect(self.docker_version().await)
    }

    /// Run one command and return its outcome as payload. Never fails.
    pub async fn execute(&self, command: Command, context: CommandContext) -> CommandResult {
        let id = command.command_id.clone();
        let Some(op) = command.op else {
            return failure(id, "ValueError", "command carries no op");
        };
        if command.epoch != 0 && command.epoch < self.epoch() {
            return failure(
                id,
                STALE_EPOCH,
                format!(
                    "command epoch {} is older than {}; a newer control plane \
                     has taken over",
                    command.epoch,
                    self.epoch()
                ),
            );
        }
        self.note_epoch(command.epoch);
        self.dispatch(id, op, context).await
    }

    /// The Docker daemon, or a failure naming its absence.
    ///
    /// A node with no daemon answers every container operation with the same
    /// definite failure rather than falling over: the control plane learns
    /// "this node cannot do that" instead of "this node is gone", and those
    /// are very different instructions.
    fn docker(&self) -> DockerResult<&bollard::Docker> {
        self.docker.as_ref().ok_or_else(|| {
            OpError::new(
                "RuntimeError",
                "Cannot connect to the Docker daemon on this node",
            )
        })
    }

    /// Every operation the protocol carries, written out.
    ///
    /// Exhaustively, and deliberately not with a catch-all arm: adding an
    /// operation to `agent.proto` then fails to compile here rather than
    /// silently answering "unknown operation" on a node in the field.
    async fn dispatch(
        &self,
        id: String,
        op: command::Op,
        context: CommandContext,
    ) -> CommandResult {
        use command::Op;
        use command_result::Outcome;

        // Every arm resolves to one outcome. The `?`-free shape is on purpose:
        // an operation that fails must still produce a `CommandResult`, so the
        // error is converted here rather than propagated out of `execute`.
        macro_rules! ok {
            ($outcome:expr) => {
                CommandResult {
                    command_id: id,
                    outcome: Some($outcome),
                }
            };
        }
        macro_rules! attempt {
            ($result:expr) => {
                match $result {
                    Ok(value) => value,
                    Err(error) => return failure(id, &error.kind, error.message),
                }
            };
        }

        let docker = match self.docker() {
            Ok(docker) => docker,
            Err(error) => {
                // `get_facts` is the one thing a node with no daemon can still
                // answer, and it is how an operator finds out *why* the node
                // is useless. Answering it needs no daemon, so it is answered.
                if matches!(op, Op::GetFacts(_)) {
                    return ok!(Outcome::Facts(self.collect_facts().await));
                }
                return failure(id, &error.kind, error.message);
            }
        };

        match op {
            Op::GetFacts(_) => ok!(Outcome::Facts(self.collect_facts().await)),

            Op::RunContainer(req) => {
                let info = attempt!(containers::run_container(docker, req).await);
                ok!(Outcome::Container(ContainerRef {
                    found: true,
                    container: Some(info),
                }))
            }
            Op::EnsureDirectories(req) => {
                ok!(Outcome::Strings(StringList {
                    values: ensure_directories(&req.paths),
                }))
            }
            Op::StopContainer(req) => {
                let gone = containers::stop_container(docker, &req.name, req.timeout).await;
                ok!(Outcome::Boolean(crate::proto::BoolValue { value: gone }))
            }
            Op::GetContainerStatus(req) => {
                ok!(Outcome::Status(
                    containers::get_container_status(docker, &req.name).await
                ))
            }
            Op::ExecInContainer(req) => {
                let command = req
                    .command
                    .as_ref()
                    .and_then(decode_cmd)
                    .unwrap_or_default();
                let outcome = attempt!(
                    containers::exec_in_container(
                        docker,
                        &req.container,
                        command,
                        req.detach.unwrap_or(false),
                    )
                    .await
                );
                ok!(Outcome::Exec(outcome))
            }
            Op::CopyToContainer(req) => {
                let copied = attempt!(
                    copy::copy_file(
                        docker,
                        &req.container,
                        &req.remote_path,
                        &req.content,
                        req.mode,
                    )
                    .await
                );
                ok!(Outcome::Boolean(crate::proto::BoolValue { value: copied }))
            }
            Op::CopyDirToContainer(req) => {
                let copied = attempt!(
                    copy::copy_dir(docker, &req.container, &req.remote_path, &req.tar_gz).await
                );
                ok!(Outcome::Boolean(crate::proto::BoolValue { value: copied }))
            }
            Op::GetLogs(req) => {
                let text = attempt!(containers::get_logs(docker, &req.name, req.tail).await);
                ok!(Outcome::Text(StringValue { value: text }))
            }
            Op::ListManagedContainers(req) => {
                let wanted: BTreeMap<String, String> = req.labels.into_iter().collect();
                let found = attempt!(containers::list_managed(docker, &wanted).await);
                ok!(Outcome::Containers(ContainerList { containers: found }))
            }
            Op::GetContainerByDeployment(req) => {
                let found = attempt!(containers::by_deployment(docker, &req.deployment).await);
                ok!(Outcome::Container(match found {
                    Some(container) => ContainerRef {
                        found: true,
                        container: Some(container),
                    },
                    None => ContainerRef::default(),
                }))
            }
            Op::GetContainerByRecipe(req) => {
                let found = attempt!(containers::by_recipe(docker, &req.recipe).await);
                ok!(Outcome::Containers(ContainerList { containers: found }))
            }
            Op::ImageExists(req) => {
                let present = images::image_exists(docker, &req.r#ref).await;
                ok!(Outcome::Boolean(crate::proto::BoolValue { value: present }))
            }
            Op::ImageInfo(req) => {
                ok!(Outcome::Image(
                    match images::image_info(docker, &req.r#ref).await {
                        Some(image) => ImageRef {
                            found: true,
                            image: Some(image),
                        },
                        None => ImageRef::default(),
                    }
                ))
            }
            Op::ListImages(_) => {
                let found = attempt!(images::list_images(docker).await);
                ok!(Outcome::Images(ImageList { images: found }))
            }
            Op::PullImage(req) => {
                let sink = req.want_progress.then_some(context.progress).flatten();
                let outcome = attempt!(
                    images::pull_image(
                        docker,
                        &req.r#ref,
                        req.interval,
                        req.stall_timeout,
                        Arc::clone(&context.cancel),
                        sink.map(|sink| move |pull: PullProgress| sink.send(pull)),
                    )
                    .await
                );
                ok!(Outcome::Pull(outcome))
            }
            Op::RemoveImage(req) => {
                let removed = attempt!(
                    images::remove_image(docker, &req.r#ref, req.force.unwrap_or(false)).await
                );
                ok!(Outcome::Boolean(crate::proto::BoolValue { value: removed }))
            }
        }
    }
}

/// `mkdir -p` every path. Returns the ones that failed.
///
/// Bind-mount sources have to exist before the container does, or the daemon
/// invents them **owned by root** — and every path passed here is one of the
/// login user's caches, so a directory created too late is how
/// `~/.cache/huggingface` becomes unwritable and every later model copy fails.
///
/// A failure is *reported*, not raised: the caller treats it as a warning
/// because Docker will still start the container.
fn ensure_directories(paths: &[String]) -> Vec<String> {
    let mut failed = Vec::new();
    for raw in paths {
        let path = raw.trim();
        if path.is_empty() {
            continue;
        }
        if let Err(error) = std::fs::create_dir_all(path) {
            tracing::warn!(%path, %error, "could not create a bind-mount source");
            failed.push(path.to_string());
        }
    }
    failed
}

/// A definite failure, which is a *reachable* node saying no.
pub fn failure(id: String, kind: &str, message: impl Into<String>) -> CommandResult {
    CommandResult {
        command_id: id,
        outcome: Some(command_result::Outcome::Failure(CommandFailure {
            r#type: kind.to_string(),
            message: message.into(),
        })),
    }
}
