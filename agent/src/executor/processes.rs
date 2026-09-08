//! Signalling a process on this node.
//!
//! Deliberately narrow: send one signal to one pid and say what happened. The
//! decision about *which* processes are signalled this way belongs to the
//! control plane, which is the only side that knows what it started — a GPU
//! process inside a container it launched is ended by stopping that container,
//! an operation that already exists. This is for the rest: the stray process
//! holding VRAM that no deployment claims.
//!
//! It exists so that "kill it" does not depend on which machine the operator
//! happens to be looking at. The control node used to be the only one where
//! that button could work, because the only implementation was a `os.kill` in
//! the control plane's own process.

use crate::proto::{ProcessTermination, TerminateProcess};

pub fn terminate(request: &TerminateProcess) -> ProcessTermination {
    if request.pid == 0 {
        // Signalling pid 0 means "every process in my process group", which
        // on this node is the agent and everything it started. Never that.
        return ProcessTermination {
            terminated: false,
            detail: "pid 0 is not a process".to_string(),
        };
    }

    let signal = if request.force {
        libc::SIGKILL
    } else {
        libc::SIGTERM
    };
    // Safety: `kill` takes two integers and touches nothing of ours.
    let sent = unsafe { libc::kill(request.pid as libc::pid_t, signal) };
    if sent == 0 {
        return ProcessTermination {
            terminated: true,
            detail: String::new(),
        };
    }

    let error = std::io::Error::last_os_error();
    let detail = match error.raw_os_error() {
        // Already gone. Not a failure: the operator asked for it to be gone.
        Some(libc::ESRCH) => "no such process".to_string(),
        Some(libc::EPERM) => "not permitted to signal this process".to_string(),
        _ => error.to_string(),
    };
    ProcessTermination {
        terminated: false,
        detail,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_process_that_is_not_there_says_so_rather_than_failing() {
        // A pid this high is not in use; and if it somehow were, the test
        // would signal it, which is why `force` stays false.
        let result = terminate(&TerminateProcess {
            pid: 4_000_000_000,
            force: false,
        });

        assert!(!result.terminated);
        assert_eq!(result.detail, "no such process");
    }

    #[test]
    fn pid_zero_is_refused_because_it_means_the_whole_group() {
        let result = terminate(&TerminateProcess {
            pid: 0,
            force: false,
        });

        assert!(!result.terminated);
        assert!(result.detail.contains("not a process"));
    }

    #[test]
    fn a_real_process_is_signalled() {
        let mut child = std::process::Command::new("sleep")
            .arg("30")
            .spawn()
            .expect("sleep is on every machine this runs on");

        let result = terminate(&TerminateProcess {
            pid: child.id(),
            force: true,
        });

        assert!(result.terminated, "{}", result.detail);
        let status = child.wait().unwrap();
        assert!(!status.success(), "the child should have been killed");
    }
}
