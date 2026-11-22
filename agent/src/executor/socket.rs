//! Which daemon socket the agent talks to (#167).
//!
//! bollard's `connect_with_defaults()` is `DOCKER_HOST`, else
//! `/var/run/docker.sock`. That is right on a Spark and wrong on a rootless
//! Podman desktop: there, `podman-docker` makes `/var/run/docker.sock` a link
//! to *rootful* Podman's socket, which the operator cannot read, and the
//! agent reported no daemon at all while the operator's own one sat at
//! `$XDG_RUNTIME_DIR/podman/podman.sock`. So, with no `DOCKER_HOST`, the
//! agent asks each socket a Docker-style node could have in turn and keeps
//! the first that answers. An explicit `DOCKER_HOST` always wins and is never
//! second-guessed: an operator who wrote one meant it.

use std::path::{Path, PathBuf};
use std::time::Duration;

use bollard::{Docker, API_DEFAULT_VERSION};

/// Docker's own default, and where `podman-docker` links rootful Podman.
pub const DEFAULT_SOCKET: &str = "/var/run/docker.sock";

/// How long a candidate has to answer a ping. A socket-activated Podman
/// starts its service on the first connection, which takes a moment.
const PING_TIMEOUT: Duration = Duration::from_secs(5);

/// The bollard request timeout, in seconds, matching `connect_with_defaults`.
const REQUEST_TIMEOUT_SECS: u64 = 120;

/// The unix sockets to try, in order, when `DOCKER_HOST` is unset. Pure.
///
/// The rootless Podman socket lives under the user's runtime directory:
/// `$XDG_RUNTIME_DIR` when the service manager set it, else the path systemd
/// uses for it, `/run/user/<uid>`.
pub fn candidates(xdg_runtime_dir: Option<&str>, uid: u32) -> Vec<PathBuf> {
    let runtime_dir = xdg_runtime_dir
        .filter(|dir| !dir.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(format!("/run/user/{uid}")));
    vec![
        PathBuf::from(DEFAULT_SOCKET),
        runtime_dir.join("podman").join("podman.sock"),
    ]
}

/// The unix socket a `DOCKER_HOST` names, if it names one. Pure.
pub fn unix_path(docker_host: &str) -> Option<PathBuf> {
    docker_host
        .strip_prefix("unix://")
        .filter(|path| !path.is_empty())
        .map(PathBuf::from)
}

/// The connection this agent uses, and the unix socket under it when there
/// is one — which is what Podman's own API is reached on.
pub struct Daemon {
    pub docker: Option<Docker>,
    pub socket: Option<PathBuf>,
}

/// Connect to the daemon this node runs. Never fails.
pub async fn connect() -> Daemon {
    if let Ok(host) = std::env::var("DOCKER_HOST") {
        if !host.trim().is_empty() {
            return explicit(&host);
        }
    }
    // SAFETY: `geteuid` cannot fail and touches no memory.
    let uid = unsafe { libc::geteuid() };
    let xdg = std::env::var("XDG_RUNTIME_DIR").ok();
    let tried = candidates(xdg.as_deref(), uid);
    for path in &tried {
        if !path.exists() {
            continue;
        }
        let Some(docker) = unix(path) else { continue };
        if answers(&docker).await {
            tracing::info!(socket = %path.display(), "using the daemon on this socket");
            return Daemon {
                docker: Some(docker),
                socket: Some(path.clone()),
            };
        }
        tracing::debug!(socket = %path.display(), "no daemon answered on this socket");
    }
    // Nothing answered. Keep Docker's default so a daemon that starts later
    // is found, and the node reports "no daemon" until then.
    let fallback = PathBuf::from(DEFAULT_SOCKET);
    tracing::warn!(
        tried = ?tried,
        "no daemon answered on any socket; the agent will report it"
    );
    Daemon {
        docker: unix(&fallback),
        socket: Some(fallback),
    }
}

fn explicit(host: &str) -> Daemon {
    let docker = match Docker::connect_with_defaults() {
        Ok(docker) => Some(docker),
        Err(error) => {
            tracing::warn!(%error, %host, "DOCKER_HOST is unusable; the agent will report it");
            None
        }
    };
    tracing::info!(%host, "using the daemon DOCKER_HOST names");
    Daemon {
        docker,
        socket: unix_path(host),
    }
}

fn unix(path: &Path) -> Option<Docker> {
    Docker::connect_with_unix(
        &path.to_string_lossy(),
        REQUEST_TIMEOUT_SECS,
        API_DEFAULT_VERSION,
    )
    .ok()
}

async fn answers(docker: &Docker) -> bool {
    matches!(
        tokio::time::timeout(PING_TIMEOUT, docker.ping()).await,
        Ok(Ok(_))
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dockers_default_first_then_the_users_podman() {
        assert_eq!(
            candidates(Some("/run/user/1000"), 1000),
            vec![
                PathBuf::from("/var/run/docker.sock"),
                PathBuf::from("/run/user/1000/podman/podman.sock"),
            ]
        );
    }

    #[test]
    fn without_a_runtime_dir_the_systemd_one_is_assumed() {
        assert_eq!(
            candidates(None, 1001)[1],
            PathBuf::from("/run/user/1001/podman/podman.sock")
        );
        assert_eq!(
            candidates(Some(""), 1001)[1],
            PathBuf::from("/run/user/1001/podman/podman.sock")
        );
    }

    #[test]
    fn a_docker_host_names_a_unix_socket_or_none() {
        assert_eq!(
            unix_path("unix:///run/user/1000/podman/podman.sock"),
            Some(PathBuf::from("/run/user/1000/podman/podman.sock"))
        );
        assert_eq!(unix_path("tcp://10.0.0.1:2375"), None);
        assert_eq!(unix_path("unix://"), None);
    }
}
