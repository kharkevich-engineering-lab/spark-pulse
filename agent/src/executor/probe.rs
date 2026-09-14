//! Running one read-only host diagnostic for the control plane's pre-flight.
//!
//! The pre-flight used to log in over SSH to read a node's host facts — the
//! docker version, the GPU, free memory, listening ports, network interfaces,
//! free disk. Every one of those is a short shell command, and the agent is
//! already the authenticated channel to the node, so it runs them here instead
//! and SSH is left for bootstrap.
//!
//! Three rules keep this narrow:
//!
//! * **Never privileged.** The command runs as the agent's own user under
//!   `/bin/sh -c`, never `sudo`. The one privileged thing the agent may do is
//!   `nmcli` (see `fabric.rs`); a probe is not it.
//! * **Always bounded.** A hung command must not hold the executor open, so it
//!   runs with a hard timeout and is killed when it expires — exit 124, the
//!   same code coreutils `timeout` uses.
//! * **The result is the shell's, verbatim.** A non-zero exit is not an error
//!   of this operation; it is a fact the pre-flight reads (docker absent, `ss`
//!   missing, a path that is not there). The operation "failed" only if the
//!   shell itself could not be started, and even then it answers — a result
//!   arriving at all is what tells the control plane the node was reachable.

use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::Duration;

use crate::proto::{HostProbeResult, RunHostProbe};

/// Seconds a probe may run when the control plane names no timeout.
const DEFAULT_TIMEOUT_SECS: u64 = 20;

/// Exit code for a probe killed at its timeout, matching coreutils `timeout`.
const TIMEOUT_EXIT_CODE: i32 = 124;

/// Exit code when `/bin/sh` itself could not be started.
const NO_SHELL_EXIT_CODE: i32 = 127;

/// Run the probe command and report what the shell said.
pub fn run(req: &RunHostProbe) -> HostProbeResult {
    let timeout = if req.timeout_seconds == 0 {
        DEFAULT_TIMEOUT_SECS
    } else {
        u64::from(req.timeout_seconds)
    };

    let child = Command::new("/bin/sh")
        .arg("-c")
        .arg(&req.command)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn();
    let child = match child {
        Ok(child) => child,
        Err(error) => {
            return HostProbeResult {
                exit_code: NO_SHELL_EXIT_CODE,
                stdout: String::new(),
                stderr: format!("could not start /bin/sh: {error}"),
            };
        }
    };

    // The child's id, kept before `wait_with_output` consumes it, so a timeout
    // can terminate the process it left running.
    let pid = child.id();
    let (tx, rx) = mpsc::channel();
    // A dedicated thread drains both pipes and waits, so a chatty command
    // cannot deadlock on a full pipe while we time it.
    thread::spawn(move || {
        let _ = tx.send(child.wait_with_output());
    });

    match rx.recv_timeout(Duration::from_secs(timeout)) {
        Ok(Ok(output)) => HostProbeResult {
            exit_code: output.status.code().unwrap_or(-1),
            stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
            stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
        },
        Ok(Err(error)) => HostProbeResult {
            exit_code: -1,
            stdout: String::new(),
            stderr: format!("probe could not run: {error}"),
        },
        Err(_) => {
            // Timed out. Terminate the process it left behind; the draining
            // thread then completes on its own and is discarded.
            let _ = Command::new("kill")
                .arg("-TERM")
                .arg(pid.to_string())
                .output();
            HostProbeResult {
                exit_code: TIMEOUT_EXIT_CODE,
                stdout: String::new(),
                stderr: format!("timed out after {timeout}s"),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn probe(command: &str, timeout_seconds: u32) -> HostProbeResult {
        run(&RunHostProbe {
            command: command.to_string(),
            timeout_seconds,
        })
    }

    #[test]
    fn it_reports_stdout_and_a_zero_exit() {
        let result = probe("echo spark-pulse-preflight", 5);
        assert_eq!(result.exit_code, 0);
        assert_eq!(result.stdout.trim(), "spark-pulse-preflight");
        assert!(result.stderr.is_empty());
    }

    #[test]
    fn it_reports_a_non_zero_exit_as_a_fact_not_an_error() {
        let result = probe("echo oops >&2; exit 3", 5);
        assert_eq!(result.exit_code, 3);
        assert_eq!(result.stderr.trim(), "oops");
    }

    #[test]
    fn it_kills_a_command_that_overruns_its_timeout() {
        let result = probe("sleep 10", 1);
        assert_eq!(result.exit_code, TIMEOUT_EXIT_CODE);
        assert!(result.stderr.contains("timed out"));
    }

    #[test]
    fn a_zero_timeout_falls_back_to_the_default() {
        let result = probe("echo ok", 0);
        assert_eq!(result.exit_code, 0);
        assert_eq!(result.stdout.trim(), "ok");
    }
}
