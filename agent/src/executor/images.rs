//! The image half: presence, inspection, listing, pulling and removal.
//!
//! The pull is the interesting one. It is the only operation that takes
//! minutes, the only one that reports progress while it runs, and the only one
//! that can be cancelled — and each of those is here for a reason that has
//! been paid for once already.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use bollard::query_parameters::{
    CreateImageOptionsBuilder, ListImagesOptionsBuilder, RemoveImageOptionsBuilder,
};
use bollard::Docker;
use futures_util::StreamExt;

use super::containers::is_not_found;
use super::{DockerResult, OpError};
use crate::proto::{ImageInfoValue, PullOutcome, PullProgress};

/// Pull progress is aggregated across layers and reported at most this often.
pub const PULL_PROGRESS_INTERVAL: f64 = 1.0;

/// Split a reference into `(repository, tag_or_digest)`.
///
/// `repo@sha256:…` keeps the digest as the tag, which is what the create-image
/// endpoint wants; `repo:tag` splits on the last colon that is not part of a
/// registry `host:port`; a bare repository defaults to `latest`.
pub fn split_ref(reference: &str) -> (String, String) {
    let reference = reference.trim();
    if reference.is_empty() {
        return (String::new(), String::new());
    }
    if let Some((repo, digest)) = reference.split_once('@') {
        return (repo.to_string(), digest.to_string());
    }
    match reference.rsplit_once(':') {
        // A colon in the last path segment is a tag; one before a "/" is a port.
        Some((head, tail)) if !tail.contains('/') => (head.to_string(), tail.to_string()),
        _ => (reference.to_string(), "latest".to_string()),
    }
}

/// Whether the reference resolves to an image on this host.
///
/// Any failure is `false`, including a daemon that will not answer — matching
/// `DockerService.image_exists`. That is a deliberate asymmetry with
/// `get_container_status`: a caller uses this to decide whether a pull is
/// needed, and "we could not tell" and "it is not here" both mean *pull*.
pub async fn image_exists(docker: &Docker, reference: &str) -> bool {
    if reference.is_empty() {
        return false;
    }
    docker.inspect_image(reference).await.is_ok()
}

/// `{id, size_bytes, created, repo_tags, repo_digests}` or None.
pub async fn image_info(docker: &Docker, reference: &str) -> Option<ImageInfoValue> {
    let image = docker.inspect_image(reference).await.ok()?;
    Some(ImageInfoValue {
        id: image.id.unwrap_or_default(),
        size_bytes: image.size.unwrap_or(0).max(0) as u64,
        created: image.created,
        repo_tags: image.repo_tags.unwrap_or_default(),
        repo_digests: image.repo_digests.unwrap_or_default(),
    })
}

/// Every image on the node, shaped like `image_info`.
pub async fn list_images(docker: &Docker) -> DockerResult<Vec<ImageInfoValue>> {
    let options = ListImagesOptionsBuilder::default().all(false).build();
    let summaries = docker
        .list_images(Some(options))
        .await
        .map_err(OpError::from_docker)?;
    Ok(summaries
        .into_iter()
        .map(|image| ImageInfoValue {
            id: image.id,
            size_bytes: image.size.max(0) as u64,
            // The listing endpoint reports creation as a unix timestamp while
            // inspect reports RFC 3339. Left as the daemon gives it rather
            // than converted, because `DockerService` passes `attrs["Created"]`
            // through untouched too and a caller comparing the two shapes is
            // already comparing two endpoints.
            created: Some(image.created.to_string()),
            repo_tags: image.repo_tags,
            repo_digests: image.repo_digests,
        })
        .collect())
}

/// Remove an image. False when it was not there.
pub async fn remove_image(docker: &Docker, reference: &str, force: bool) -> DockerResult<bool> {
    let options = RemoveImageOptionsBuilder::default().force(force).build();
    match docker.remove_image(reference, Some(options), None).await {
        Ok(_) => Ok(true),
        Err(error) if is_not_found(&error) => Ok(false),
        Err(error) => Err(OpError::from_docker(error)),
    }
}

/// Python's `%g`: `30.0` prints as `30`, `1.5` as `1.5`.
///
/// Only ever used inside a message an operator reads, but "no pull progress
/// for 30s" and "for 30.0000s" are not equally readable and the Python agent
/// wrote the first.
fn terse(value: f64) -> String {
    if value.fract() == 0.0 {
        format!("{}", value as i64)
    } else {
        format!("{value}")
    }
}

/// Per-layer download counters, aggregated.
#[derive(Default)]
struct Layers(HashMap<String, (u64, u64)>);

impl Layers {
    fn aggregate(&self) -> (u64, u64, f64) {
        let done: u64 = self.0.values().map(|(current, _)| *current).sum();
        let total: u64 = self.0.values().map(|(_, total)| *total).sum();
        let percent = if total > 0 {
            (done as f64 / total as f64) * 100.0
        } else {
            0.0
        };
        (done, total, percent)
    }
}

/// Pull an image, reporting aggregated progress and honouring cancellation.
///
/// Three things this has to get right:
///
/// * **Progress is aggregated, not per-layer.** A caller wants one number, and
///   a 40 GB image has dozens of layers reporting independently.
/// * **A stall is not a hang.** A registry that accepts the connection and
///   then stops sending would otherwise hold this forever; the deadline turns
///   silence into a named `PullStalled`.
/// * **A cancelled pull raises `PullCancelled`.** `native_runtime` and
///   `images` catch that by type in three places to record a teardown as a
///   teardown; a generic error there is filed as a deployment failure.
pub async fn pull_image(
    docker: &Docker,
    reference: &str,
    interval: Option<f64>,
    stall_timeout: Option<f64>,
    cancel: Arc<AtomicBool>,
    mut progress: Option<impl FnMut(PullProgress)>,
) -> DockerResult<PullOutcome> {
    let (repository, tag) = split_ref(reference);
    let options = CreateImageOptionsBuilder::default()
        .from_image(&repository)
        .tag(&tag)
        .build();

    let mut layers = Layers::default();
    let mut last_status = String::new();
    let mut last_emit = Instant::now()
        .checked_sub(Duration::from_secs(3600))
        .unwrap_or_else(Instant::now);
    let throttle = Duration::from_secs_f64(interval.unwrap_or(PULL_PROGRESS_INTERVAL).max(0.0));
    let stall = stall_timeout
        .filter(|s| *s > 0.0)
        .map(Duration::from_secs_f64);

    let mut stream = docker.create_image(Some(options), None, None);
    loop {
        // Cancellation is checked on every chunk *and* while waiting for one,
        // so a pull that has gone quiet still stops promptly when a teardown
        // asks it to.
        if cancel.load(Ordering::SeqCst) {
            return Err(OpError::new(
                "PullCancelled",
                format!("pull of {reference} cancelled"),
            ));
        }
        let next = match stall {
            Some(deadline) => match tokio::time::timeout(deadline, stream.next()).await {
                Ok(next) => next,
                Err(_) => {
                    return Err(OpError::new(
                        "PullStalled",
                        format!(
                            "pull of {reference} stalled: no pull progress for {}s",
                            terse(deadline.as_secs_f64())
                        ),
                    ))
                }
            },
            None => stream.next().await,
        };
        let Some(chunk) = next else { break };
        let chunk = chunk.map_err(|error| {
            OpError::new(
                "RuntimeError",
                format!("pull of {reference} failed: {error}"),
            )
        })?;

        if let Some(message) = chunk.error_detail.and_then(|detail| detail.message) {
            return Err(OpError::new(
                "RuntimeError",
                format!("pull of {reference} failed: {message}"),
            ));
        }
        let status = chunk.status.unwrap_or_default();
        if !status.is_empty() {
            last_status = status.clone();
        }
        let layer_id = chunk.id.unwrap_or_default();
        let detail = chunk.progress_detail;
        if !layer_id.is_empty() {
            let declared = detail.as_ref().and_then(|d| d.total).unwrap_or(0).max(0) as u64;
            if declared > 0 {
                let entry = layers.0.entry(layer_id).or_insert((0, 0));
                entry.1 = declared;
                if status.starts_with("Download") {
                    entry.0 = detail.and_then(|d| d.current).unwrap_or(0).max(0) as u64;
                } else if status == "Pull complete" || status == "Already exists" {
                    entry.0 = entry.1;
                }
            } else if status == "Pull complete" || status == "Already exists" {
                let entry = layers.0.entry(layer_id).or_insert((0, 0));
                entry.0 = entry.1;
            }
        }
        if let Some(emit) = progress.as_mut() {
            if last_emit.elapsed() >= throttle {
                last_emit = Instant::now();
                let (done, total, percent) = layers.aggregate();
                emit(PullProgress {
                    r#ref: reference.to_string(),
                    status: last_status.clone(),
                    layers: layers.0.len() as u32,
                    bytes_done: done,
                    bytes_total: total,
                    percent,
                });
            }
        }
    }

    let (done, total, _) = layers.aggregate();
    if let Some(emit) = progress.as_mut() {
        // One final event, unthrottled, so a caller's last reading is 100 and
        // not whatever the throttle happened to let through.
        for entry in layers.0.values_mut() {
            entry.0 = entry.1;
        }
        emit(PullProgress {
            r#ref: reference.to_string(),
            status: "pull complete".into(),
            layers: layers.0.len() as u32,
            bytes_done: done,
            bytes_total: total,
            percent: 100.0,
        });
    }
    let info = image_info(docker, reference).await.unwrap_or_default();
    Ok(PullOutcome {
        r#ref: reference.to_string(),
        repository,
        tag,
        bytes_done: done,
        bytes_total: total,
        percent: 100.0,
        id: info.id,
        size_bytes: info.size_bytes,
    })
}

/// Where a relayed pull has to listen: the reference's own registry host.
///
/// A relay exists to put the control node's registry on this node's
/// loopback, so the reference must name loopback — `127.0.0.1:<port>/…` —
/// and anything else is refused rather than bound: listening on an address
/// the caller did not mean is how a relay ends up open to the LAN.
pub fn relay_listen_address(reference: &str) -> Result<String, OpError> {
    let (repository, _) = split_ref(reference);
    let host = repository.split('/').next().unwrap_or_default();
    match host.split_once(':') {
        Some(("127.0.0.1", port)) if port.parse::<u16>().is_ok() && repository.contains('/') => {
            Ok(host.to_string())
        }
        _ => Err(OpError::new(
            "ValueError",
            format!("a relayed pull needs a 127.0.0.1:<port> reference, not {reference}"),
        )),
    }
}

/// A loopback listener forwarding every connection to the control node's
/// registry, for exactly as long as it is held.
///
/// Docker refuses a plain-HTTP registry anywhere but `127.0.0.0/8`, and the
/// daemon is a host process, so a listener here is one it will talk to. The
/// alternative — an `insecure-registries` entry — is root-owned daemon
/// configuration whose change restarts the daemon and every engine on it.
pub struct Relay {
    task: tokio::task::JoinHandle<()>,
}

impl Drop for Relay {
    fn drop(&mut self) {
        self.task.abort();
    }
}

/// Open the relay a pull of `reference` through `upstream` needs.
///
/// A port that is already taken fails the pull with `RelayUnavailable`
/// rather than pulling from whatever holds it: that would be some other
/// registry, answering for content we did not seed.
pub async fn open_relay(reference: &str, upstream: &str) -> DockerResult<Relay> {
    let listen = relay_listen_address(reference)?;
    let upstream = upstream.trim().to_string();
    if upstream.is_empty() {
        return Err(OpError::new(
            "ValueError",
            "a relayed pull needs an upstream",
        ));
    }
    let listener = tokio::net::TcpListener::bind(&listen)
        .await
        .map_err(|error| {
            OpError::new(
                "RelayUnavailable",
                format!("could not listen on {listen} to relay to {upstream}: {error}"),
            )
        })?;
    let task = tokio::spawn(async move {
        loop {
            let Ok((mut inbound, _)) = listener.accept().await else {
                // Out of descriptors, say: back off rather than spin.
                tokio::time::sleep(Duration::from_millis(100)).await;
                continue;
            };
            let upstream = upstream.clone();
            tokio::spawn(async move {
                match tokio::net::TcpStream::connect(&upstream).await {
                    Ok(mut outbound) => {
                        let _ = tokio::io::copy_bidirectional(&mut inbound, &mut outbound).await;
                    }
                    Err(error) => {
                        tracing::warn!("relay could not reach {upstream}: {error}");
                    }
                }
            });
        }
    });
    Ok(Relay { task })
}

#[cfg(test)]
mod tests {
    use super::{open_relay, relay_listen_address, split_ref};

    #[test]
    fn a_reference_splits_the_way_python_splits_it() {
        assert_eq!(split_ref("repo"), ("repo".into(), "latest".into()));
        assert_eq!(split_ref("repo:1.2"), ("repo".into(), "1.2".into()));
        assert_eq!(
            split_ref("ghcr.io/org/img:0.1.0"),
            ("ghcr.io/org/img".into(), "0.1.0".into())
        );
        // A port is not a tag.
        assert_eq!(
            split_ref("localhost:5000/img"),
            ("localhost:5000/img".into(), "latest".into())
        );
        // A digest stays the tag, which is what /images/create wants.
        assert_eq!(
            split_ref("repo@sha256:abc"),
            ("repo".into(), "sha256:abc".into())
        );
        assert_eq!(split_ref(""), (String::new(), String::new()));
    }

    #[test]
    fn a_relay_listens_only_on_the_loopback_the_reference_names() {
        assert_eq!(
            relay_listen_address("127.0.0.1:5000/org/vllm@sha256:abc").unwrap(),
            "127.0.0.1:5000"
        );
        for refused in [
            "192.168.1.5:5000/org/vllm@sha256:abc",
            "ghcr.io/org/vllm:1",
            "127.0.0.1:notaport/org/vllm:1",
            "vllm",
        ] {
            assert_eq!(
                relay_listen_address(refused).unwrap_err().kind,
                "ValueError"
            );
        }
    }

    #[tokio::test]
    async fn a_relay_forwards_to_its_upstream_and_closes_when_dropped() {
        use tokio::io::{AsyncReadExt, AsyncWriteExt};

        let upstream = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let upstream_addr = upstream.local_addr().unwrap().to_string();
        tokio::spawn(async move {
            let (mut socket, _) = upstream.accept().await.unwrap();
            let mut buf = [0u8; 4];
            socket.read_exact(&mut buf).await.unwrap();
            socket.write_all(b"pong").await.unwrap();
        });
        // A free port to relay on.
        let port = {
            let probe = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
            probe.local_addr().unwrap().port()
        };
        let reference = format!("127.0.0.1:{port}/org/vllm@sha256:abc");
        let relay = open_relay(&reference, &upstream_addr).await.unwrap();

        let mut client = tokio::net::TcpStream::connect(("127.0.0.1", port))
            .await
            .unwrap();
        client.write_all(b"ping").await.unwrap();
        let mut reply = [0u8; 4];
        client.read_exact(&mut reply).await.unwrap();
        assert_eq!(&reply, b"pong");

        drop(relay);
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        assert!(tokio::net::TcpStream::connect(("127.0.0.1", port))
            .await
            .is_err());
    }

    #[tokio::test]
    async fn a_taken_port_is_refused_not_shared() {
        let holder = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = holder.local_addr().unwrap().port();
        let reference = format!("127.0.0.1:{port}/org/vllm@sha256:abc");
        let error = open_relay(&reference, "127.0.0.1:1").await.err().unwrap();
        assert_eq!(error.kind, "RelayUnavailable");
    }
}
