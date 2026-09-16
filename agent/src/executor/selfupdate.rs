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
use std::time::Duration;

use super::bounded;
use super::OpError;
use crate::proto::{BundleInstalled, InstallBundle};

const UNIT: &str = "spark-pulse-agent.service";

/// How long a `systemctl cat` probe (used to find the unit's scope) may run.
const PROBE_TIMEOUT: Duration = Duration::from_secs(5);

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

    // The bundle is only as trustworthy as the mTLS channel it arrived on, but
    // a misrouted or malicious archive should not be able to write outside
    // `target` regardless — so every member is checked before anything is
    // unpacked, and `--no-absolute-names` is a second, redundant guard at
    // extraction time against the one case (`tar -tzf` and `-xzf` disagreeing
    // on what counts as absolute) that check can't see.
    if let Err(e) = validate_tar_members(&staged) {
        let _ = std::fs::remove_file(&staged);
        return Err(e);
    }

    let extract = Command::new("tar")
        .args(["--no-absolute-names", "-xzf"])
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

    // The version this install replaces, read before we repoint — kept
    // through the prune so a manual rollback still has a binary to point at.
    let previous = std::fs::read_link(root.join("current"))
        .ok()
        .and_then(|p| p.file_name().map(|n| n.to_string_lossy().into_owned()));

    // Repoint `current` atomically: write a new symlink to a temp name and
    // rename it over the old one, so a crash never leaves `current` dangling.
    let link_tmp = root.join(".current.new");
    let _ = std::fs::remove_file(&link_tmp);
    std::os::unix::fs::symlink(&request.dir_name, &link_tmp)
        .map_err(|e| err(format!("could not stage the current symlink: {e}")))?;
    std::fs::rename(&link_tmp, root.join("current"))
        .map_err(|e| err(format!("could not repoint current: {e}")))?;

    // Old version directories otherwise accumulate forever, one per update.
    // Keep the one just installed and the one it replaced; delete the rest.
    prune_old_versions(&root, &request.dir_name, previous.as_deref());

    // Restart onto it — after this reply is sent, because restarting inline
    // ends this process before the CommandResult can flush. We can therefore
    // not observe the deferred restart's result; what we *can* do is refuse to
    // claim `restarting: true` unless a systemctl scope actually knows this
    // unit, and say so plainly when neither does.
    let restarting = match detect_restart_scope() {
        Some(scope) => {
            schedule_restart(scope);
            tracing::info!(
                unit = UNIT,
                scope = if scope.is_empty() { "system" } else { scope },
                "scheduled a self-restart onto the new bundle",
            );
            true
        }
        None => {
            tracing::warn!(
                unit = UNIT,
                path = %target.display(),
                "the new bundle is staged and 'current' repointed, but neither \
                 'systemctl --user' nor 'systemctl' knows this unit; it will \
                 take effect on the next restart, which must be triggered by \
                 hand — reporting restarting=false",
            );
            false
        }
    };

    Ok(BundleInstalled {
        version: request.version.clone(),
        path: target.to_string_lossy().into_owned(),
        restarting,
    })
}

/// Reject a tar member whose path would land outside the extraction root: an
/// absolute path, or any `..` component. `tar -xzf` alone would follow either
/// straight through `-C target`.
fn is_unsafe_member(name: &str) -> bool {
    let path = Path::new(name);
    path.is_absolute()
        || path
            .components()
            .any(|c| matches!(c, std::path::Component::ParentDir))
}

/// List the staged archive's members and refuse it if any is unsafe, before
/// `tar` is asked to extract a single byte.
fn validate_tar_members(archive: &Path) -> Result<(), OpError> {
    let listing = Command::new("tar")
        .arg("-tzf")
        .arg(archive)
        .output()
        .map_err(|e| err(format!("could not inspect the bundle: {e}")))?;
    if !listing.status.success() {
        return Err(err(format!(
            "could not list the bundle's contents: {}",
            String::from_utf8_lossy(&listing.stderr).trim()
        )));
    }
    let names = String::from_utf8_lossy(&listing.stdout);
    for name in names.lines() {
        if is_unsafe_member(name) {
            return Err(err(format!(
                "the bundle contains an unsafe member path: {name:?}"
            )));
        }
    }
    Ok(())
}

/// Which systemctl scope manages this agent's unit: `Some("--user")`,
/// `Some("")` (system), or `None` when neither knows it.
///
/// `systemctl cat` succeeds only for a unit the scope can see, so it is a cheap
/// way to learn where a restart should be sent — and whether one can be sent at
/// all — without waiting for the deferred restart to fail.
fn detect_restart_scope() -> Option<&'static str> {
    for scope in ["--user", ""] {
        let mut command = Command::new("systemctl");
        if !scope.is_empty() {
            command.arg(scope);
        }
        command.arg("cat").arg(UNIT);
        if let Ok(output) = bounded::run(command, PROBE_TIMEOUT) {
            if output.status.map(|s| s.success()).unwrap_or(false) {
                return Some(scope);
            }
        }
    }
    None
}

/// Restart this agent's unit, detached, after a short delay.
///
/// `scope` is what `detect_restart_scope` found (`"--user"` or `""` for the
/// system manager); the delay lets the reply reach the control plane before
/// the process is replaced. Detached with `setsid` so it outlives this process
/// being torn down. The restart's own outcome is recorded by systemd in the
/// unit's journal — this process is gone before it completes.
fn schedule_restart(scope: &str) {
    let restart = if scope.is_empty() {
        format!("systemctl restart {UNIT}")
    } else {
        format!("systemctl {scope} restart {UNIT}")
    };
    let _ = Command::new("setsid")
        .args(["sh", "-c", &format!("sleep 1; {restart}")])
        .spawn();
}

/// Delete version directories this install has superseded.
///
/// Everything under the root that is a plain directory — not `current`, not the
/// dot-prefixed staging temps — and is neither the version just installed nor
/// the one it replaced is removed. A failure to prune is logged, never fatal:
/// a stale directory wastes disk, but refusing the update over it would be
/// worse.
fn prune_old_versions(root: &Path, keep_new: &str, keep_prev: Option<&str>) {
    let entries = match std::fs::read_dir(root) {
        Ok(entries) => entries,
        Err(error) => {
            tracing::warn!(%error, "could not read the install root to prune old versions");
            return;
        }
    };
    for entry in entries.flatten() {
        let raw = entry.file_name();
        let name = raw.to_string_lossy();
        if name == "current" || name.starts_with('.') {
            continue;
        }
        if name == keep_new || Some(name.as_ref()) == keep_prev {
            continue;
        }
        if !entry.file_type().map(|t| t.is_dir()).unwrap_or(false) {
            continue;
        }
        let path = entry.path();
        match std::fs::remove_dir_all(&path) {
            Ok(()) => tracing::info!(dir = %path.display(), "pruned a superseded agent version"),
            Err(error) => {
                tracing::warn!(%error, dir = %path.display(), "could not prune an old agent version")
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{install, prune_old_versions, validate_tar_members};
    use crate::proto::InstallBundle;

    /// Build a `.tar.gz` at `path` containing one member named `member_name`,
    /// writing the name straight into the header's raw bytes so a crafted
    /// `..`/absolute path — which the crate's own `set_path` refuses to
    /// produce, because it is meant for well-behaved archives — reaches the
    /// archive exactly as an adversarial one would.
    fn write_archive_with_member(path: &std::path::Path, member_name: &str) {
        let file = std::fs::File::create(path).unwrap();
        let enc = flate2::write::GzEncoder::new(file, flate2::Compression::default());
        let mut builder = tar::Builder::new(enc);
        let data = b"x";
        let mut header = tar::Header::new_gnu();
        let name_field = &mut header.as_gnu_mut().unwrap().name;
        let bytes = member_name.as_bytes();
        name_field[..bytes.len()].copy_from_slice(bytes);
        header.set_size(data.len() as u64);
        header.set_cksum();
        builder.append(&header, &data[..]).unwrap();
        builder.into_inner().unwrap().finish().unwrap();
    }

    #[test]
    fn pruning_keeps_the_new_and_previous_versions_and_deletes_the_rest() {
        let root = tempfile::tempdir().unwrap();
        let root = root.path();
        for version in ["1.0.0", "1.1.0", "1.2.0", "1.3.0"] {
            std::fs::create_dir(root.join(version)).unwrap();
        }
        // Things pruning must never touch: the symlink, a staging temp, a file.
        std::os::unix::fs::symlink("1.3.0", root.join("current")).unwrap();
        std::fs::create_dir(root.join(".1.3.0.tar.gz")).unwrap();
        std::fs::write(root.join("a-file"), b"x").unwrap();

        prune_old_versions(root, "1.3.0", Some("1.2.0"));

        assert!(root.join("1.3.0").is_dir(), "the new version stays");
        assert!(root.join("1.2.0").is_dir(), "the prior version stays");
        assert!(!root.join("1.1.0").exists(), "an older version is pruned");
        assert!(!root.join("1.0.0").exists(), "an older version is pruned");
        assert!(root.join("current").exists(), "the symlink is untouched");
        assert!(
            root.join(".1.3.0.tar.gz").exists(),
            "a staging temp is left"
        );
        assert!(root.join("a-file").exists(), "a non-version file is left");
    }

    #[test]
    fn pruning_with_no_previous_keeps_only_the_new_version() {
        let root = tempfile::tempdir().unwrap();
        let root = root.path();
        for version in ["1.0.0", "1.1.0"] {
            std::fs::create_dir(root.join(version)).unwrap();
        }
        prune_old_versions(root, "1.1.0", None);
        assert!(root.join("1.1.0").is_dir());
        assert!(!root.join("1.0.0").exists());
    }

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
    fn a_tar_member_with_a_parent_dir_component_is_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let archive = dir.path().join("evil.tar.gz");
        write_archive_with_member(&archive, "../evil");

        let e = validate_tar_members(&archive).unwrap_err();
        assert!(e.message.contains("unsafe member path"), "{}", e.message);
    }

    #[test]
    fn a_tar_member_with_an_absolute_path_is_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let archive = dir.path().join("evil.tar.gz");
        write_archive_with_member(&archive, "/etc/passwd");

        let e = validate_tar_members(&archive).unwrap_err();
        assert!(e.message.contains("unsafe member path"), "{}", e.message);
    }

    #[test]
    fn a_tar_with_only_safe_members_passes_validation() {
        let dir = tempfile::tempdir().unwrap();
        let archive = dir.path().join("fine.tar.gz");
        write_archive_with_member(&archive, "bin/spark-pulse-agent");

        validate_tar_members(&archive).unwrap();
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
