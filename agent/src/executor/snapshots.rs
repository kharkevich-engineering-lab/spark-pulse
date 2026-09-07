//! Model snapshots on this node's disk.
//!
//! Deliberately only two operations, and neither of them decides anything:
//! list what is there, and delete it. Whether a listing *means* the model is
//! verified, partial or absent is the control plane's question — it holds the
//! manifest, it knows the revision that was asked for, and `hub_cache` already
//! answers it from exactly this input.
//!
//! That split is the whole point. The previous arrangement shipped
//! `hub_cache.py` to each node over SSH and ran it there, which is a second
//! copy of the verifier on a machine that may not have the interpreter to run
//! it. Re-implementing the verifier here in Rust would be a *third*. A
//! directory listing cannot disagree with itself.

use std::fs;
use std::path::{Path, PathBuf};

use sha2::{Digest, Sha256};

use crate::proto::{ListSnapshot, RemoveSnapshot, SnapshotFile, SnapshotListing, SnapshotRemoval};

use super::OpError;

/// Resolve the snapshot directory a request names.
///
/// A hub repository holds `snapshots/<revision>/`, and `refs/main` names the
/// revision when the caller did not. A request naming no revision and a
/// repository with no ref is not an error: it is a repository with nothing
/// checked out, which the listing reports as absent.
fn snapshot_dir(repo_path: &str, revision: &str) -> Option<(PathBuf, String)> {
    let repo = Path::new(repo_path);
    let revision = if revision.is_empty() {
        fs::read_to_string(repo.join("refs").join("main"))
            .ok()?
            .trim()
            .to_string()
    } else {
        revision.to_string()
    };
    if revision.is_empty() {
        return None;
    }
    Some((repo.join("snapshots").join(&revision), revision))
}

pub fn list(request: &ListSnapshot) -> Result<SnapshotListing, OpError> {
    let Some((dir, revision)) = snapshot_dir(&request.repo_path, &request.revision) else {
        return Ok(SnapshotListing {
            revision: String::new(),
            present: false,
            files: Vec::new(),
            bytes_present: 0,
        });
    };

    if !dir.is_dir() {
        return Ok(SnapshotListing {
            revision,
            present: false,
            files: Vec::new(),
            bytes_present: 0,
        });
    }

    let mut files = Vec::new();
    let mut bytes = 0_u64;
    walk(&dir, &dir, request.deep, &mut files, &mut bytes);
    files.sort_by(|a, b| a.path.cmp(&b.path));

    Ok(SnapshotListing {
        revision,
        present: true,
        files,
        bytes_present: bytes,
    })
}

/// Walk a snapshot, recording each file's relative path and size.
///
/// Sizes come from the *resolved* file, because a hub snapshot is symlinks
/// into `blobs/` and the size of a symlink is the length of its target's name.
/// Whether the entry was a symlink is reported separately: a copy that lost
/// them is a different thing on disk, and the control plane wants to know.
fn walk(root: &Path, dir: &Path, deep: bool, files: &mut Vec<SnapshotFile>, bytes: &mut u64) {
    let Ok(entries) = fs::read_dir(dir) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        let is_symlink = fs::symlink_metadata(&path)
            .map(|m| m.file_type().is_symlink())
            .unwrap_or(false);

        if path.is_dir() {
            walk(root, &path, deep, files, bytes);
            continue;
        }

        let size = fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
        *bytes += size;
        files.push(SnapshotFile {
            path: path
                .strip_prefix(root)
                .unwrap_or(&path)
                .to_string_lossy()
                .to_string(),
            size_bytes: size,
            sha256: if deep { sha256(&path) } else { String::new() },
            is_symlink,
        });
    }
}

fn sha256(path: &Path) -> String {
    let Ok(mut file) = fs::File::open(path) else {
        return String::new();
    };
    let mut hasher = Sha256::new();
    if std::io::copy(&mut file, &mut hasher).is_err() {
        return String::new();
    }
    format!("{:x}", hasher.finalize())
}

/// Delete one snapshot, or the whole repository when no revision is named.
///
/// Removing a revision leaves the blobs behind on purpose: they are shared
/// between revisions, and a delete that took them would silently break the
/// other snapshot in the same repository. Reclaiming everything is what
/// naming no revision does.
pub fn remove(request: &RemoveSnapshot) -> Result<SnapshotRemoval, OpError> {
    let repo = PathBuf::from(&request.repo_path);
    if request.repo_path.trim().is_empty() {
        return Err(OpError::new("ValueError", "repo_path is required"));
    }

    let target = if request.revision.is_empty() {
        repo.clone()
    } else {
        match snapshot_dir(&request.repo_path, &request.revision) {
            Some((dir, _)) => dir,
            None => repo.join("snapshots").join(&request.revision),
        }
    };

    if !target.exists() {
        return Ok(SnapshotRemoval {
            removed: false,
            freed_bytes: 0,
            paths: Vec::new(),
        });
    }

    let freed = size_on_disk(&target);
    fs::remove_dir_all(&target).map_err(|error| {
        OpError::new(
            "RuntimeError",
            format!("could not remove {}: {error}", target.display()),
        )
    })?;

    Ok(SnapshotRemoval {
        removed: true,
        freed_bytes: freed,
        paths: vec![target.to_string_lossy().to_string()],
    })
}

/// Bytes a directory tree occupies, following symlinks to the blobs they name.
fn size_on_disk(path: &Path) -> u64 {
    if path.is_file() {
        return fs::metadata(path).map(|m| m.len()).unwrap_or(0);
    }
    let Ok(entries) = fs::read_dir(path) else {
        return 0;
    };
    entries
        .flatten()
        .map(|entry| size_on_disk(&entry.path()))
        .sum()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs as unix_fs;

    fn repo(root: &Path, revision: &str) -> PathBuf {
        let snapshot = root.join("snapshots").join(revision);
        fs::create_dir_all(&snapshot).unwrap();
        fs::create_dir_all(root.join("refs")).unwrap();
        fs::write(root.join("refs").join("main"), revision).unwrap();
        snapshot
    }

    #[test]
    fn lists_a_snapshot_by_its_revision() {
        let tmp = tempfile::tempdir().unwrap();
        let snapshot = repo(tmp.path(), "abc123");
        fs::write(snapshot.join("config.json"), "{}").unwrap();
        fs::write(snapshot.join("model.safetensors"), vec![0u8; 128]).unwrap();

        let listing = list(&ListSnapshot {
            repo_path: tmp.path().to_string_lossy().to_string(),
            revision: "abc123".into(),
            deep: false,
        })
        .unwrap();

        assert!(listing.present);
        assert_eq!(listing.revision, "abc123");
        assert_eq!(listing.files.len(), 2);
        assert_eq!(listing.bytes_present, 130);
    }

    #[test]
    fn falls_back_to_the_ref_when_no_revision_is_named() {
        let tmp = tempfile::tempdir().unwrap();
        let snapshot = repo(tmp.path(), "deadbeef");
        fs::write(snapshot.join("config.json"), "{}").unwrap();

        let listing = list(&ListSnapshot {
            repo_path: tmp.path().to_string_lossy().to_string(),
            revision: String::new(),
            deep: false,
        })
        .unwrap();

        assert_eq!(listing.revision, "deadbeef");
        assert!(listing.present);
    }

    #[test]
    fn a_symlinked_file_reports_the_size_of_what_it_points_at() {
        // A hub snapshot is symlinks into `blobs/`; the size of the link is
        // the length of a filename, which is not what anybody wants to see.
        let tmp = tempfile::tempdir().unwrap();
        let snapshot = repo(tmp.path(), "abc");
        let blobs = tmp.path().join("blobs");
        fs::create_dir_all(&blobs).unwrap();
        fs::write(blobs.join("sha256-x"), vec![7u8; 4096]).unwrap();
        unix_fs::symlink(blobs.join("sha256-x"), snapshot.join("weights.bin")).unwrap();

        let listing = list(&ListSnapshot {
            repo_path: tmp.path().to_string_lossy().to_string(),
            revision: "abc".into(),
            deep: false,
        })
        .unwrap();

        assert_eq!(listing.files[0].size_bytes, 4096);
        assert!(listing.files[0].is_symlink);
    }

    #[test]
    fn a_missing_snapshot_is_absent_rather_than_an_error() {
        let tmp = tempfile::tempdir().unwrap();

        let listing = list(&ListSnapshot {
            repo_path: tmp
                .path()
                .join("nothing-here")
                .to_string_lossy()
                .to_string(),
            revision: "abc".into(),
            deep: false,
        })
        .unwrap();

        assert!(!listing.present);
        assert!(listing.files.is_empty());
    }

    #[test]
    fn a_deep_listing_hashes_every_file() {
        let tmp = tempfile::tempdir().unwrap();
        let snapshot = repo(tmp.path(), "abc");
        fs::write(snapshot.join("config.json"), "{}").unwrap();

        let listing = list(&ListSnapshot {
            repo_path: tmp.path().to_string_lossy().to_string(),
            revision: "abc".into(),
            deep: true,
        })
        .unwrap();

        // sha256 of "{}"
        assert_eq!(
            listing.files[0].sha256,
            "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
        );
    }

    #[test]
    fn removing_a_revision_leaves_the_other_one_alone() {
        let tmp = tempfile::tempdir().unwrap();
        let keep = repo(tmp.path(), "keep");
        let drop = tmp.path().join("snapshots").join("drop");
        fs::create_dir_all(&drop).unwrap();
        fs::write(keep.join("a"), "a").unwrap();
        fs::write(drop.join("b"), vec![0u8; 64]).unwrap();

        let removal = remove(&RemoveSnapshot {
            repo_path: tmp.path().to_string_lossy().to_string(),
            revision: "drop".into(),
        })
        .unwrap();

        assert!(removal.removed);
        assert_eq!(removal.freed_bytes, 64);
        assert!(keep.join("a").exists());
    }

    #[test]
    fn removing_without_a_revision_takes_the_whole_repository() {
        let tmp = tempfile::tempdir().unwrap();
        let snapshot = repo(tmp.path(), "abc");
        fs::write(snapshot.join("a"), vec![0u8; 32]).unwrap();

        let removal = remove(&RemoveSnapshot {
            repo_path: tmp.path().to_string_lossy().to_string(),
            revision: String::new(),
        })
        .unwrap();

        assert!(removal.removed);
        assert!(!tmp.path().exists());
    }

    #[test]
    fn removing_what_is_not_there_is_not_an_error() {
        let tmp = tempfile::tempdir().unwrap();

        let removal = remove(&RemoveSnapshot {
            repo_path: tmp.path().join("gone").to_string_lossy().to_string(),
            revision: String::new(),
        })
        .unwrap();

        assert!(!removal.removed);
        assert_eq!(removal.freed_bytes, 0);
    }
}
