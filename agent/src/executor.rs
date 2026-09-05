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

use std::sync::atomic::{AtomicU64, Ordering};

use crate::facts;
use crate::proto::{command, command_result, Command, CommandFailure, CommandResult, NodeFacts};

/// The exception class name the control plane re-raises for a stale epoch.
///
/// These names are interop surface: `CommandFailure.type` carries the class
/// name so a caller can react to the *kind* of failure without matching on a
/// message, and `spark_pulse.agent.sync_service.CONTRACT_EXCEPTIONS` maps some
/// of them back to real Python exception types. A name invented here that
/// Python does not know becomes a generic `NodeOperationError`, which is
/// correct but less useful — so they are the Python names.
pub const STALE_EPOCH: &str = "StaleEpochError";

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
    pub async fn execute(&self, command: Command) -> CommandResult {
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
        self.dispatch(id, op).await
    }

    /// Every operation the protocol carries, written out.
    ///
    /// Exhaustively, and deliberately not with a catch-all arm: adding an
    /// operation to `agent.proto` then fails to compile here rather than
    /// silently answering "unknown operation" on a node in the field.
    async fn dispatch(&self, id: String, op: command::Op) -> CommandResult {
        use command::Op;
        match op {
            Op::GetFacts(_) => CommandResult {
                command_id: id,
                outcome: Some(command_result::Outcome::Facts(self.collect_facts().await)),
            },
            Op::RunContainer(_) => not_yet(id, "run_container"),
            Op::EnsureDirectories(_) => not_yet(id, "ensure_directories"),
            Op::StopContainer(_) => not_yet(id, "stop_container"),
            Op::GetContainerStatus(_) => not_yet(id, "get_container_status"),
            Op::ExecInContainer(_) => not_yet(id, "exec_in_container"),
            Op::CopyToContainer(_) => not_yet(id, "copy_to_container"),
            Op::CopyDirToContainer(_) => not_yet(id, "copy_dir_to_container"),
            Op::GetLogs(_) => not_yet(id, "get_logs"),
            Op::ListManagedContainers(_) => not_yet(id, "list_managed_containers"),
            Op::GetContainerByDeployment(_) => not_yet(id, "get_container_by_deployment"),
            Op::GetContainerByRecipe(_) => not_yet(id, "get_container_by_recipe"),
            Op::ImageExists(_) => not_yet(id, "image_exists"),
            Op::ImageInfo(_) => not_yet(id, "image_info"),
            Op::ListImages(_) => not_yet(id, "list_images"),
            Op::PullImage(_) => not_yet(id, "pull_image"),
            Op::RemoveImage(_) => not_yet(id, "remove_image"),
        }
    }
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

/// An operation this build does not implement yet.
///
/// A named failure rather than silence, and emphatically not a fabricated
/// success: a caller must be able to tell "this agent cannot do that" from
/// "that did not work", and from "we never heard back".
fn not_yet(id: String, name: &str) -> CommandResult {
    failure(
        id,
        "NotImplementedError",
        format!("this agent build does not implement {name} yet"),
    )
}
