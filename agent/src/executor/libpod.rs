//! Creating an engine container on Podman, through its own API (#165, #167).
//!
//! Everything else this agent does to a container goes through bollard and
//! the Docker-compatible API, which Podman serves on the same socket and
//! honours for start, stop, exec, logs, inspect and list. *Create* is the one
//! call that cannot: the compat API accepts a GPU request and drops it without
//! a word (`DeviceRequests` comes back `null`), it cannot attach a CDI device
//! at all on Podman 4.9, and a rootless `User: uid:gid` without `keep-id`
//! maps the operator to a subuid that cannot write the bind-mounted cache. So
//! on a Podman node the create request is Podman's own spec, posted to
//! `/libpod/containers/create`, and the container it makes is then started
//! and driven through bollard like any other.
//!
//! What the spike measured, and what this therefore always does on Podman:
//!
//! * **The GPU is a CDI device** (`nvidia.com/gpu=all`), read from the node's
//!   own spec directories. No CDI spec means no GPU, and a GPU-less engine
//!   that starts and then fails is the worst outcome, so the create is
//!   refused with the command that fixes it.
//! * **Unprivileged, with `IPC_LOCK`.** Rootless and privileged with a CDI
//!   device fails at `runc create` on `/dev/dri/card1`.
//! * **`userns: keep-id`**, so the uid the engine runs as *is* the operator's
//!   on the host, and what it writes under the cache stays theirs — the
//!   guarantee `ENGINE_USER` makes on Docker.

use std::collections::BTreeMap;
use std::path::Path;

use http_body_util::{BodyExt, Full};
use hyper::body::Bytes;
use hyper::Request;
use hyper_util::rt::TokioIo;
use serde_json::{json, Map, Value};
use tokio::net::UnixStream;

use super::OpError;
use crate::proto::RunContainer;

/// The libpod API version the request names. Podman serves every version it
/// knows under its own prefix; 4.0.0 is the oldest this agent targets.
const API_PREFIX: &str = "/v4.0.0/libpod";

/// The kind CDI names an NVIDIA GPU by.
const NVIDIA_GPU_KIND: &str = "nvidia.com/gpu";

/// The CDI device that gives a container every GPU on the node, when the
/// node's spec names one. Pure.
///
/// `nvidia.com/gpu=all` is what `nvidia-ctk cdi generate` writes and what a
/// rank wants — the same "every GPU" the Docker path's `count: -1` asks for.
/// A spec written by hand without `all` still names its GPUs; the first is
/// used rather than refusing a node that has one.
pub fn gpu_device(cdi_devices: &[String]) -> Option<String> {
    let all = format!("{NVIDIA_GPU_KIND}=all");
    if cdi_devices.contains(&all) {
        return Some(all);
    }
    cdi_devices
        .iter()
        .find(|d| d.starts_with(&format!("{NVIDIA_GPU_KIND}=")))
        .cloned()
}

/// Why a Podman node cannot start an engine: it has no CDI spec naming a GPU.
pub fn no_gpu_device() -> OpError {
    OpError::new(
        "RuntimeError",
        "this node runs Podman and no CDI spec names a GPU, so an engine would \
         start without one. Generate the spec on the node: \
         `sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml`",
    )
}

/// A ulimit value as Podman's spec wants it: unsigned, with Docker's `-1`
/// (unlimited) as `RLIM_INFINITY`. Pure.
fn rlimit(value: i64) -> u64 {
    if value < 0 {
        u64::MAX
    } else {
        value as u64
    }
}

/// Podman's create spec for one rank. Pure.
///
/// Field for field the decisions `containers::create_config` makes for
/// Docker — the same binds, ulimits, pids limit, memory and swap, restart
/// policy, network and IPC — except where the module docs say Podman needs
/// otherwise. `user` arrives already resolved to `uid:gid`.
pub fn create_spec(
    req: &RunContainer,
    label_map: &BTreeMap<String, String>,
    user: Option<String>,
    gpu: &str,
) -> Value {
    let mut spec = Map::new();
    spec.insert("name".into(), json!(req.name));
    spec.insert("image".into(), json!(req.image));
    spec.insert("labels".into(), json!(label_map));
    spec.insert("env".into(), json!(req.env_vars));
    if req.entrypoint_clear.unwrap_or(true) {
        // An empty entrypoint clears the image's, as on Docker: the native
        // runtime starts an idle container and execs the engine into it.
        spec.insert("entrypoint".into(), json!([]));
    }
    if let Some(cmd) = req.command.as_ref().and_then(super::decode_cmd) {
        spec.insert("command".into(), json!(cmd));
    }
    if let Some(user) = user {
        spec.insert("user".into(), json!(user));
    }
    spec.insert("userns".into(), json!({"nsmode": "keep-id"}));

    let network_host = match req.network_host {
        None => req.port_mappings.is_empty(),
        Some(host) => host,
    };
    if network_host {
        spec.insert("netns".into(), json!({"nsmode": "host"}));
    }
    let ipc_host = req.ipc_host.unwrap_or(false);
    if ipc_host {
        spec.insert("ipcns".into(), json!({"nsmode": "host"}));
    } else {
        // Podman refuses a size with host IPC, and it means nothing there.
        let gb = req.shm_size_gb.unwrap_or(64.0);
        spec.insert(
            "shm_size".into(),
            json!((gb * 1024.0 * 1024.0 * 1024.0) as i64),
        );
    }

    spec.insert("privileged".into(), json!(false));
    let mut cap_add: Vec<String> = req.cap_add.clone();
    if !cap_add.iter().any(|c| c == "IPC_LOCK") {
        cap_add.push("IPC_LOCK".into());
    }
    spec.insert("cap_add".into(), json!(cap_add));

    let mut devices: Vec<Value> = vec![json!({"path": gpu})];
    devices.extend(req.devices.iter().map(|path| json!({"path": path})));
    spec.insert("devices".into(), json!(devices));

    let mut mounts: Vec<Value> = req.cache_dirs.iter().map(|dir| bind(dir, dir)).collect();
    mounts.extend(
        req.mounts
            .iter()
            .map(|(host, container)| bind(host, container)),
    );
    if !mounts.is_empty() {
        spec.insert("mounts".into(), json!(mounts));
    }

    let nofile = req.nofile_limit.unwrap_or(1_048_576);
    let mut r_limits = vec![json!({"type": "nofile", "soft": nofile, "hard": nofile})];
    for (name, raw) in &req.ulimits {
        if name == "nofile" {
            continue;
        }
        let (soft, hard) = raw.split_once(':').unwrap_or((raw.as_str(), raw.as_str()));
        let Ok(soft) = soft.trim().parse::<i64>() else {
            continue;
        };
        let hard = hard.trim().parse::<i64>().unwrap_or(soft);
        r_limits.push(json!({
            // Podman prefixes `RLIMIT_` itself; the spec takes the bare name.
            "type": name.to_ascii_lowercase(),
            "soft": rlimit(soft),
            "hard": rlimit(hard),
        }));
    }
    spec.insert("r_limits".into(), json!(r_limits));

    let mut resources = Map::new();
    resources.insert(
        "pids".into(),
        json!({"limit": req.pids_limit.map(i64::from).unwrap_or(4096)}),
    );
    if let Some(gb) = req.memory_limit_gb.filter(|gb| *gb > 0.0) {
        let bytes = |gb: f64| (gb * 1024.0 * 1024.0 * 1024.0) as i64;
        resources.insert(
            "memory".into(),
            json!({"limit": bytes(gb), "swap": bytes(gb + 10.0)}),
        );
    }
    spec.insert("resource_limits".into(), Value::Object(resources));

    // Never restart: a rank is one member of a gang, as on Docker.
    spec.insert("restart_policy".into(), json!("no"));
    spec.insert("remove".into(), json!(req.auto_remove.unwrap_or(true)));
    Value::Object(spec)
}

fn bind(host: &str, container: &str) -> Value {
    json!({
        "type": "bind",
        "source": host,
        "destination": container,
        "options": ["rbind", "rw"],
    })
}

/// Post the spec to `/libpod/containers/create` and return the new id.
pub async fn create(socket: &Path, spec: &Value) -> Result<String, OpError> {
    let (status, body) = post(socket, &format!("{API_PREFIX}/containers/create"), spec)
        .await
        .map_err(|error| {
            OpError::new(
                "RuntimeError",
                format!("Podman API error at {}: {error}", socket.display()),
            )
        })?;
    let parsed: Value = serde_json::from_slice(&body).unwrap_or(Value::Null);
    if status == 201 || status == 200 {
        return parsed
            .get("Id")
            .and_then(Value::as_str)
            .map(str::to_string)
            .ok_or_else(|| OpError::new("RuntimeError", "Podman created a container with no id"));
    }
    let message = parsed
        .get("message")
        .and_then(Value::as_str)
        .map(str::to_string)
        .unwrap_or_else(|| String::from_utf8_lossy(&body).trim().to_string());
    if status == 404 && message.contains("image") {
        return Err(OpError::new(
            "RuntimeError",
            format!("Image not found: {message}"),
        ));
    }
    Err(OpError::new(
        "RuntimeError",
        format!("Podman API error ({status}): {message}"),
    ))
}

/// What Podman says about itself without being asked for `info` (#211).
///
/// `Some(version)` when the daemon on `socket` is Podman, read from the
/// `Libpod-Api-Version` header its `/_ping` carries; `None` when it is not
/// Podman or did not answer. `/_ping` is the one cheap question: Podman 4.9's
/// `/version` and `/info` both build its full host report, which on Debian
/// and Ubuntu runs `dpkg -S` once per helper binary — four seconds of CPU on
/// a desktop — and the agent used to ask both on every heartbeat.
pub async fn ping_version(socket: &Path) -> Option<String> {
    let headers = ping(socket).await.ok()?;
    let version = headers
        .get("libpod-api-version")
        .and_then(|value| value.to_str().ok())?;
    Some(version.trim().to_string())
}

async fn ping(socket: &Path) -> anyhow::Result<hyper::HeaderMap> {
    let stream = UnixStream::connect(socket).await?;
    let (mut sender, connection) =
        hyper::client::conn::http1::handshake(TokioIo::new(stream)).await?;
    let driver = tokio::spawn(async move {
        let _ = connection.await;
    });
    let request = Request::get("/_ping")
        .header("host", "d")
        .body(Full::new(Bytes::new()))?;
    let response = sender.send_request(request).await?;
    let headers = response.headers().clone();
    let _ = response.into_body().collect().await;
    driver.abort();
    Ok(headers)
}

/// One HTTP/1.1 POST over the unix socket, the body read whole.
async fn post(socket: &Path, path: &str, body: &Value) -> anyhow::Result<(u16, Bytes)> {
    let stream = UnixStream::connect(socket).await?;
    let (mut sender, connection) =
        hyper::client::conn::http1::handshake(TokioIo::new(stream)).await?;
    let driver = tokio::spawn(async move {
        let _ = connection.await;
    });
    let request = Request::post(path)
        .header("host", "d")
        .header("content-type", "application/json")
        .body(Full::new(Bytes::from(serde_json::to_vec(body)?)))?;
    let response = sender.send_request(request).await?;
    let status = response.status().as_u16();
    let bytes = response.into_body().collect().await?.to_bytes();
    driver.abort();
    Ok((status, bytes))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::proto::RunContainer;

    fn request() -> RunContainer {
        RunContainer {
            image: "vllm/vllm-openai:v0.11.1".into(),
            name: "rank-0".into(),
            cache_dirs: vec!["/home/ark/.cache/huggingface".into()],
            ipc_host: Some(true),
            ulimits: [("memlock".to_string(), "-1".to_string())].into(),
            ..Default::default()
        }
    }

    fn spec() -> Value {
        create_spec(
            &request(),
            &BTreeMap::new(),
            Some("1000:1000".into()),
            "nvidia.com/gpu=all",
        )
    }

    #[test]
    fn the_gpu_is_every_gpu_when_the_spec_says_all() {
        let devices = vec![
            "nvidia.com/gpu=0".to_string(),
            "nvidia.com/gpu=all".to_string(),
        ];
        assert_eq!(gpu_device(&devices).as_deref(), Some("nvidia.com/gpu=all"));
        let only_one = vec!["nvidia.com/gpu=0".to_string()];
        assert_eq!(gpu_device(&only_one).as_deref(), Some("nvidia.com/gpu=0"));
        let none = vec!["vendor.com/fpga=0".to_string()];
        assert_eq!(gpu_device(&none), None);
        assert_eq!(gpu_device(&[]), None);
    }

    #[test]
    fn it_is_the_shape_the_spike_measured_working() {
        // #165: devices nvidia.com/gpu=all, unprivileged, keep-id, host net
        // and IPC, IPC_LOCK -> the operator's uid and the GPU.
        let spec = spec();
        assert_eq!(spec["devices"], json!([{"path": "nvidia.com/gpu=all"}]));
        assert_eq!(spec["privileged"], json!(false));
        assert_eq!(spec["userns"], json!({"nsmode": "keep-id"}));
        assert_eq!(spec["user"], json!("1000:1000"));
        assert_eq!(spec["netns"], json!({"nsmode": "host"}));
        assert_eq!(spec["ipcns"], json!({"nsmode": "host"}));
        assert_eq!(spec["cap_add"], json!(["IPC_LOCK"]));
    }

    #[test]
    fn no_shm_size_with_host_ipc() {
        // Podman answers 500 "cannot set shmsize when running in the host IPC
        // Namespace", which is how every rank failed at create (#165).
        assert!(spec().get("shm_size").is_none());
        let mut private = request();
        private.ipc_host = Some(false);
        let spec = create_spec(&private, &BTreeMap::new(), None, "nvidia.com/gpu=all");
        assert_eq!(spec["shm_size"], json!(64_i64 * 1024 * 1024 * 1024));
        assert!(spec.get("ipcns").is_none());
    }

    #[test]
    fn unlimited_is_rlim_infinity_not_minus_one() {
        let limits = spec()["r_limits"].as_array().unwrap().clone();
        assert_eq!(
            limits[0],
            json!({"type": "nofile", "soft": 1_048_576, "hard": 1_048_576})
        );
        assert_eq!(
            limits[1],
            json!({"type": "memlock", "soft": u64::MAX, "hard": u64::MAX})
        );
    }

    #[test]
    fn caches_bind_at_the_same_path_and_the_entrypoint_is_cleared() {
        let spec = spec();
        assert_eq!(
            spec["mounts"],
            json!([{
                "type": "bind",
                "source": "/home/ark/.cache/huggingface",
                "destination": "/home/ark/.cache/huggingface",
                "options": ["rbind", "rw"],
            }])
        );
        assert_eq!(spec["entrypoint"], json!([]));
        assert_eq!(spec["restart_policy"], json!("no"));
        assert_eq!(spec["remove"], json!(true));
        assert_eq!(spec["resource_limits"], json!({"pids": {"limit": 4096}}));
    }

    #[test]
    fn a_memory_limit_carries_its_swap_as_on_docker() {
        let mut limited = request();
        limited.memory_limit_gb = Some(2.0);
        let spec = create_spec(&limited, &BTreeMap::new(), None, "nvidia.com/gpu=all");
        let gib = 1024_i64 * 1024 * 1024;
        assert_eq!(
            spec["resource_limits"]["memory"],
            json!({"limit": 2 * gib, "swap": 12 * gib})
        );
    }
}
