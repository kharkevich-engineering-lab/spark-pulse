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

use std::process::Command;
use std::time::Duration;

use super::bounded;
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

    let mut command = Command::new("/bin/sh");
    command.arg("-c").arg(&req.command);

    // The bounded runner puts the shell in its own process group and, on
    // timeout, kills the whole group (TERM then KILL) — so a pipeline's
    // children go with the shell rather than being orphaned.
    match bounded::run(command, Duration::from_secs(timeout)) {
        Ok(output) if output.timed_out() => HostProbeResult {
            exit_code: TIMEOUT_EXIT_CODE,
            stdout: String::new(),
            stderr: format!("timed out after {timeout}s"),
        },
        Ok(output) => HostProbeResult {
            exit_code: output.status.and_then(|s| s.code()).unwrap_or(-1),
            stdout: String::from_utf8_lossy(&output.stdout).into_owned(),
            stderr: String::from_utf8_lossy(&output.stderr).into_owned(),
        },
        Err(error) => HostProbeResult {
            exit_code: NO_SHELL_EXIT_CODE,
            stdout: String::new(),
            stderr: format!("could not start /bin/sh: {error}"),
        },
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
    fn a_timed_out_pipeline_with_a_grandchild_is_reaped_and_returns_promptly() {
        // The shell backgrounds a TERM-ignoring `sleep` that keeps the output
        // pipe open. Killing only the shell would leave the draining threads
        // blocked on that pipe forever; the whole-group kill makes this return.
        let started = std::time::Instant::now();
        let result = probe("trap '' TERM; sleep 30 & sleep 30", 1);
        assert_eq!(result.exit_code, TIMEOUT_EXIT_CODE);
        assert!(
            started.elapsed() < Duration::from_secs(5),
            "the group was not reaped promptly: {:?}",
            started.elapsed()
        );
    }

    #[test]
    fn a_zero_timeout_falls_back_to_the_default() {
        let result = probe("echo ok", 0);
        assert_eq!(result.exit_code, 0);
        assert_eq!(result.stdout.trim(), "ok");
    }
}
