//! Running one external command with a hard, self-enforcing timeout.
//!
//! The agent shells out for the few things Docker's SDK cannot answer —
//! `nvidia-smi`, `df`, `nmcli`, `ip`, `ping`, a host probe. Every one of them
//! can wedge: a hung GPU driver leaves `nvidia-smi` blocked forever, a
//! NetworkManager mid-transaction leaves `nmcli` waiting on a D-Bus reply that
//! never comes. A blocked child here is not merely a slow answer — these run
//! on the executor, and `/sse/metrics` asks every node for its stats every few
//! seconds, so one wedged binary pins a worker and the node reads "unreachable"
//! while it is fine.
//!
//! So every external command runs through here, and three rules hold:
//!
//! * **Its own process group.** The child is placed in a fresh group
//!   (`setpgid(0, 0)`) so a timeout can signal the *whole tree* — a shell and
//!   every process in its pipeline — not just the leader we happened to spawn.
//!   A pipeline whose leader is killed while its children keep the output pipe
//!   open would otherwise never reach EOF, and the draining threads would
//!   block forever.
//!
//! * **`TERM`, a short grace, then `KILL`.** A child that ignores `SIGTERM`
//!   does not get to hold a worker: after a brief grace the group is `SIGKILL`ed,
//!   which cannot be caught, so the reap and the pipe EOF are guaranteed to
//!   arrive promptly.
//!
//! * **Signal before we reap.** Only this function ever waits on the child, and
//!   it signals the group *before* reaping the leader. A pid that has already
//!   been reaped can be reused by the kernel for an unrelated process, and
//!   signalling a reused pid is how a timeout ends up killing something it
//!   never started.

use std::io::{self, Read};
use std::os::unix::process::CommandExt;
use std::process::{Command, ExitStatus, Stdio};
use std::thread;
use std::time::{Duration, Instant};

/// How often the wait loop polls for the child having exited.
const POLL_INTERVAL: Duration = Duration::from_millis(20);

/// How long a timed-out child is given to honour `SIGTERM` before `SIGKILL`.
const KILL_GRACE: Duration = Duration::from_millis(500);

/// What a bounded run produced.
///
/// `status` is `None` exactly when the command was killed at its timeout;
/// `Some` carries the real exit status. `stdout`/`stderr` are whatever was
/// drained before the child ended, so a chatty command that timed out still
/// yields whatever it managed to say.
pub struct Output {
    pub status: Option<ExitStatus>,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
}

impl Output {
    /// True when the command was killed because it overran its timeout.
    pub fn timed_out(&self) -> bool {
        self.status.is_none()
    }
}

/// Run `command` with a hard `timeout`, killing its whole process group on
/// expiry. Errors only if the child could not be spawned at all.
pub fn run(mut command: Command, timeout: Duration) -> io::Result<Output> {
    command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    // A fresh process group, set in the child before it execs, so a timeout can
    // signal the entire tree with `kill(-pgid, ...)`. Safety: `setpgid` touches
    // only the calling process's group membership and nothing of the parent's.
    unsafe {
        command.pre_exec(|| {
            if libc::setpgid(0, 0) == -1 {
                return Err(io::Error::last_os_error());
            }
            Ok(())
        });
    }

    let mut child = command.spawn()?;
    // The group id equals the child's pid, because `setpgid(0, 0)` made the
    // child a group leader. Signalling `-pid` reaches the whole group.
    let pid = child.id() as libc::pid_t;

    // Drain both pipes on their own threads: a full pipe must never wedge the
    // child while we wait, and reading to EOF is how we learn every process in
    // the group has released the write end.
    let mut out = child.stdout.take().expect("stdout was piped");
    let mut err = child.stderr.take().expect("stderr was piped");
    let out_thread = thread::spawn(move || {
        let mut buf = Vec::new();
        let _ = out.read_to_end(&mut buf);
        buf
    });
    let err_thread = thread::spawn(move || {
        let mut buf = Vec::new();
        let _ = err.read_to_end(&mut buf);
        buf
    });

    let status = wait_bounded(&mut child, pid, timeout);

    // The group is dead (either it exited or we KILLed it), so the write ends
    // are closed and these joins return promptly.
    let stdout = out_thread.join().unwrap_or_default();
    let stderr = err_thread.join().unwrap_or_default();

    Ok(Output {
        status,
        stdout,
        stderr,
    })
}

/// Wait for the child, escalating `TERM` → `KILL` if it overruns `timeout`.
///
/// Returns `Some(status)` if it exited on its own, `None` if we had to kill it.
fn wait_bounded(
    child: &mut std::process::Child,
    pid: libc::pid_t,
    timeout: Duration,
) -> Option<ExitStatus> {
    if let Some(status) = poll_for(child, timeout) {
        return Some(status);
    }

    // Overran. Signal the whole group before anyone reaps the leader, so a
    // reused pid cannot be hit, then give it a short grace to exit cleanly. We
    // still report a timeout (`None`) whether it dies from our TERM or needs
    // KILL — having had to signal it *is* the timeout; only finishing on its
    // own before we signalled counts as a real exit.
    kill_group(pid, libc::SIGTERM);
    if poll_for(child, KILL_GRACE).is_none() {
        // Still there. SIGKILL cannot be caught, so this ends it for certain,
        // and reaping the leader here means we — the only waiter — free the pid.
        kill_group(pid, libc::SIGKILL);
        let _ = child.wait();
    }
    None
}

/// Poll `try_wait` until the child exits or `dur` elapses.
fn poll_for(child: &mut std::process::Child, dur: Duration) -> Option<ExitStatus> {
    let deadline = Instant::now() + dur;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return Some(status),
            // A wait error means the child is unwaitable (already reaped
            // elsewhere, which cannot happen here) — treat it as gone.
            Err(_) => return None,
            Ok(None) => {}
        }
        if Instant::now() >= deadline {
            return None;
        }
        thread::sleep(POLL_INTERVAL);
    }
}

/// Signal a whole process group. Safety: `kill` takes two integers and, with a
/// negative pid, addresses the group whose id is `pid` — the group we created.
fn kill_group(pid: libc::pid_t, signal: libc::c_int) {
    unsafe {
        libc::kill(-pid, signal);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sh(script: &str) -> Command {
        let mut cmd = Command::new("/bin/sh");
        cmd.arg("-c").arg(script);
        cmd
    }

    #[test]
    fn it_returns_output_and_a_zero_status() {
        let out = run(sh("echo hello"), Duration::from_secs(5)).unwrap();
        assert!(!out.timed_out());
        assert_eq!(out.status.unwrap().code(), Some(0));
        assert_eq!(String::from_utf8_lossy(&out.stdout).trim(), "hello");
    }

    #[test]
    fn it_carries_a_non_zero_exit_and_stderr() {
        let out = run(sh("echo boom >&2; exit 7"), Duration::from_secs(5)).unwrap();
        assert_eq!(out.status.unwrap().code(), Some(7));
        assert_eq!(String::from_utf8_lossy(&out.stderr).trim(), "boom");
    }

    #[test]
    fn a_command_that_overruns_is_killed_and_returns_promptly() {
        let started = Instant::now();
        let out = run(sh("sleep 30"), Duration::from_millis(200)).unwrap();
        assert!(out.timed_out());
        // TERM + at most the grace, nowhere near the 30s the command asked for.
        assert!(
            started.elapsed() < Duration::from_secs(5),
            "took {:?}",
            started.elapsed()
        );
    }

    #[test]
    fn a_grandchild_that_outlives_a_killed_leader_is_reaped_too() {
        // The shell backgrounds a `sleep` that keeps the output pipe open and
        // ignores TERM, then waits. If the timeout only killed the shell, the
        // draining threads would block on the pipe forever and this test would
        // hang; killing the whole group makes it return.
        let started = Instant::now();
        let out = run(
            sh("trap '' TERM; sleep 30 & sleep 30"),
            Duration::from_millis(200),
        )
        .unwrap();
        assert!(out.timed_out());
        assert!(
            started.elapsed() < Duration::from_secs(5),
            "the group was not killed promptly: {:?}",
            started.elapsed()
        );
    }

    #[test]
    fn a_missing_program_is_an_error_not_a_hang() {
        let err = run(
            Command::new("/no/such/spark-pulse-binary"),
            Duration::from_secs(5),
        );
        assert!(err.is_err());
    }
}
