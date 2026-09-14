//! Updating this node's own agent, over the stream — no SSH.
//!
//! The control plane ships the same bundle the SSH installer would (a
//! `.tar.gz` of `bin/spark-pulse-agent` and a manifest). The agent unpacks it
//! beside the one it is running, verifies the new binary answers `--version`,
//! atomically repoints `current`, replies, and then restarts its own unit
//! onto the new binary. SSH is left for the *first* bootstrap only.
//!
//! **The restart is deferred until after the reply.** Restarting the unit ends
//! this process, so the result would never reach the control plane if it were
//! restarted inline. Instead the agent stages everything, returns
//! `restarting: true`, and a detached helper restarts the unit a moment later;
//! the control plane sees a brief disconnect and a reconnect at the new
//! version, which is how it confirms the update took.
//!
//! **Only a unit-managed install can self-update.** The control node runs its
//! agent as a child of the control-plane process, not from a `current`
//! symlink, so it has nothing to restart — it updates when its package does.
//! That case is refused here by name rather than half-done.

use std::path::{Path, PathBuf};
use std::process::Command;

use super::OpError;
use crate::proto::{BundleInstalled, InstallBundle};

const UNIT: &str = "spark-pulse-agent.service";

fn err(message: impl Into<String>) -> OpError {
    OpError::new("NativeRuntimeError", message)
}

/// The install root: the directory holding the `current` symlink the unit runs.
///
/// The running exe is `<root>/<version>/bin/spark-pulse-agent`, reached through
/// `<root>/current`. Resolving the exe and walking up three parents is the
/// root; it is only an install if `<root>/current` is actually a symlink.
fn install_root() -> Result<PathBuf, OpError> {
    let exe = std::env::current_exe().map_err(|e| err(format!("cannot find own path: {e}")))?;
    let exe = exe
        .canonicalize()
        .map_err(|e| err(format!("cannot resolve own path: {e}")))?;
    let root = exe
        .parent()
        .and_then(Path::parent)
        .and_then(Path::parent)
        .ok_or_else(|| err("the agent is not laid out under an install root"))?
        .to_path_buf();
    let current = root.join("current");
    if !current
        .symlink_metadata()
        .map(|m| m.file_type().is_symlink())
        .unwrap_or(false)
    {
        return Err(err(
            "this agent is not a unit-managed install (no 'current' symlink); it \
             updates when its package does, not over the stream",
        ));
    }
    Ok(root)
}

pub fn install(request: &InstallBundle) -> Result<BundleInstalled, OpError> {
    if request.dir_name.is_empty()
        || request.dir_name.contains('/')
        || request.dir_name.starts_with('.')
    {
        return Err(err(format!(
            "unsafe bundle dir name {:?}",
            request.dir_name
        )));
    }
    let root = install_root()?;
    let target = root.join(&request.dir_name);
    std::fs::create_dir_all(&target)
        .map_err(|e| err(format!("could not create {}: {e}", target.display())))?;

    // Extract the tarball via the system `tar`, exactly as the SSH installer
    // does on the node, so the on-disk result is identical.
    let staged = root.join(format!(".{}.tar.gz", request.dir_name));
    std::fs::write(&staged, &request.tarball)
        .map_err(|e| err(format!("could not stage the bundle: {e}")))?;
    let extract = Command::new("tar")
        .args(["-xzf"])
        .arg(&staged)
        .arg("-C")
        .arg(&target)
        .output()
        .map_err(|e| err(format!("could not run tar: {e}")))?;
    let _ = std::fs::remove_file(&staged);
    if !extract.status.success() {
        return Err(err(format!(
            "unpacking the bundle failed: {}",
            String::from_utf8_lossy(&extract.stderr).trim()
        )));
    }

    // The new binary must actually run before we point `current` at it: a
    // bundle for the wrong architecture would otherwise brick the node on the
    // next restart, with no agent left to fix it.
    let binary = target.join("bin/spark-pulse-agent");
    let versioned = Command::new(&binary)
        .arg("--version")
        .output()
        .map_err(|e| err(format!("the new binary does not run: {e}")))?;
    if !versioned.status.success() {
        return Err(err(
            "the new binary did not answer --version; not switching to it",
        ));
    }

    // Repoint `current` atomically: write a new symlink to a temp name and
    // rename it over the old one, so a crash never leaves `current` dangling.
    let link_tmp = root.join(".current.new");
    let _ = std::fs::remove_file(&link_tmp);
    std::os::unix::fs::symlink(&request.dir_name, &link_tmp)
        .map_err(|e| err(format!("could not stage the current symlink: {e}")))?;
    std::fs::rename(&link_tmp, root.join("current"))
        .map_err(|e| err(format!("could not repoint current: {e}")))?;

    // Restart onto it — after this reply is sent. A detached child waits a
    // moment (so the CommandResult flushes) and restarts the unit; this
    // process is then replaced.
    schedule_restart();

    Ok(BundleInstalled {
        version: request.version.clone(),
        path: target.to_string_lossy().into_owned(),
        restarting: true,
    })
}

/// Restart this agent's unit, detached, after a short delay.
///
/// `--user` for the rootless install this runs under; the delay lets the reply
/// reach the control plane before the process is replaced. Detached with
/// `setsid` so it outlives this process being torn down.
fn schedule_restart() {
    let _ = Command::new("setsid")
        .args([
            "sh",
            "-c",
            &format!("sleep 1; systemctl --user restart {UNIT} || systemctl restart {UNIT}"),
        ])
        .spawn();
}

#[cfg(test)]
mod tests {
    use super::install;
    use crate::proto::InstallBundle;

    fn req(dir: &str) -> InstallBundle {
        InstallBundle {
            tarball: b"x".to_vec(),
            dir_name: dir.to_string(),
            version: "1.0.0".to_string(),
        }
    }

    #[test]
    fn an_unsafe_dir_name_is_refused_before_touching_anything() {
        for bad in ["", "..", "a/b", "../evil", ".hidden"] {
            let e = install(&req(bad)).unwrap_err();
            assert!(
                e.message.contains("unsafe bundle dir name"),
                "{bad:?}: {}",
                e.message
            );
        }
    }

    #[test]
    fn a_non_unit_install_refuses_rather_than_half_updating() {
        // The test binary is not laid out under a `current` symlink, so a
        // well-formed request is refused with the reason, not half-applied.
        let e = install(&req("1.26.0-abcdef")).unwrap_err();
        assert!(
            e.message.contains("not a unit-managed install"),
            "{}",
            e.message
        );
    }
}
