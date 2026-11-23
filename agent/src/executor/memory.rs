//! Compact this node's free memory before a run starts.
//!
//! After days of use a node's free memory is mostly small fragments, and a
//! launch that wants large contiguous mappings can fail with gigabytes "free".
//! Dropping the page cache and asking the kernel to compact folds them back.
//! The two writes are `echo 3 > /proc/sys/vm/drop_caches` and
//! `echo 1 > /proc/sys/vm/compact_memory`, which need root; the agent does not
//! run as root, so each goes through `sudo -n /usr/bin/tee <fixed path>` with
//! the value on stdin. The sudoers drop-in the installer writes names exactly
//! those two argvs, so the rule cannot become "write any file as root".
//!
//! Nothing here fails the operation: the caller wants the numbers and a reason,
//! never an error, because a refused compaction must not stop a deploy.

use std::fs;
use std::process::Command;
use std::time::Duration;

use crate::proto::{BuddyInfo, CompactMemory, MemoryCompaction};

use super::bounded;
use super::stats::{buddy_blocks, page_size};

const DROP_CACHES: &str = "/proc/sys/vm/drop_caches";
const COMPACT_MEMORY: &str = "/proc/sys/vm/compact_memory";
const TEE: &str = "/usr/bin/tee";

/// Compaction walks every zone and can take seconds on a node holding 100 GiB.
const WRITE_TIMEOUT: Duration = Duration::from_secs(60);

/// What one privileged write came to.
#[derive(Debug, PartialEq, Eq)]
pub(crate) enum Write {
    Done,
    /// sudo refused: the node has no rule for this command.
    NoSudoRule,
    Failed(String),
}

/// Classify a failed `sudo -n tee` from what it printed.
pub(crate) fn classify_failure(stderr: &str) -> Write {
    let lowered = stderr.to_lowercase();
    if lowered.contains("a password is required")
        || lowered.contains("not allowed to execute")
        || lowered.contains("may not run sudo")
        || lowered.contains("no tty present")
    {
        return Write::NoSudoRule;
    }
    let text = stderr.trim();
    Write::Failed(if text.is_empty() {
        "the write failed".to_string()
    } else {
        text.to_string()
    })
}

fn read_buddy() -> Option<BuddyInfo> {
    let text = fs::read_to_string("/proc/buddyinfo").ok()?;
    Some(BuddyInfo {
        page_size_bytes: page_size(),
        free_blocks: buddy_blocks(&text),
    })
}

/// `echo <value> | sudo -n /usr/bin/tee <path> > /dev/null`, bounded. The shell
/// line is built from constants only, so nothing a caller sent reaches it.
fn privileged_write(value: &str, path: &str) -> Write {
    let line = format!("echo {value} | sudo -n {TEE} {path} > /dev/null");
    let mut command = Command::new("sh");
    command.arg("-c").arg(line);
    let output = match bounded::run(command, WRITE_TIMEOUT) {
        Ok(output) => output,
        Err(error) => return Write::Failed(format!("could not run sudo ({error})")),
    };
    if output.timed_out() {
        return Write::Failed(format!("timed out after {}s", WRITE_TIMEOUT.as_secs()));
    }
    if output.status.map(|s| s.success()).unwrap_or(false) {
        return Write::Done;
    }
    classify_failure(&String::from_utf8_lossy(&output.stderr))
}

fn describe(what: &str, outcome: &Write) -> Option<String> {
    match outcome {
        Write::Done => None,
        Write::NoSudoRule => Some(format!(
            "{what}: sudo rule missing; reinstall the agent to add it"
        )),
        Write::Failed(reason) => Some(format!("{what}: {reason}")),
    }
}

pub fn compact(request: &CompactMemory) -> MemoryCompaction {
    let before = read_buddy();
    // SAFETY: sync takes no arguments and has no preconditions.
    unsafe { libc::sync() };

    let mut notes: Vec<String> = Vec::new();
    let mut dropped = false;
    let mut refused = false;
    if request.drop_caches {
        let outcome = privileged_write("3", DROP_CACHES);
        dropped = outcome == Write::Done;
        refused = outcome == Write::NoSudoRule;
        notes.extend(describe("drop_caches", &outcome));
    }
    // A node that refused the first write will refuse the second.
    let compacted = if refused {
        false
    } else {
        let outcome = privileged_write("1", COMPACT_MEMORY);
        notes.extend(describe("compact_memory", &outcome));
        outcome == Write::Done
    };

    MemoryCompaction {
        before,
        after: read_buddy(),
        dropped_caches: dropped,
        compacted,
        detail: notes.join("; "),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_password_prompt_is_a_missing_rule() {
        assert_eq!(
            classify_failure("sudo: a password is required"),
            Write::NoSudoRule
        );
        assert_eq!(
            classify_failure("Sorry, user x is not allowed to execute '/usr/bin/tee' as root"),
            Write::NoSudoRule
        );
    }

    #[test]
    fn any_other_failure_keeps_its_own_words() {
        assert_eq!(
            classify_failure("tee: /proc/sys/vm/compact_memory: Read-only file system\n"),
            Write::Failed("tee: /proc/sys/vm/compact_memory: Read-only file system".into())
        );
        assert_eq!(
            classify_failure(""),
            Write::Failed("the write failed".into())
        );
    }

    #[test]
    fn a_missing_rule_names_the_remedy() {
        let text = describe("drop_caches", &Write::NoSudoRule).unwrap();
        assert!(text.contains("sudo rule missing"));
        assert!(describe("x", &Write::Done).is_none());
    }
}
