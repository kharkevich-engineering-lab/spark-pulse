//! The engine caches on this node's disk: measure them, empty them.
//!
//! The Library's Caches section used to be `os.walk` and `shutil.rmtree` in the
//! control plane's own process. On one machine that reads as "the cluster"; on
//! two it is the control node and nothing else, and the page does not say which
//! — the same defect `GetNodeStats` was made for. So the walk lives here, on the
//! node that owns the bytes, and every node answers for itself.
//!
//! What stays in the control plane is the *definitions*: which directories are
//! caches, what they are called, what they hold. Those are facts about the
//! product. What is a fact about the machine is where its `$HOME` is, so the
//! control plane sends `~/.cache/vllm` and the node expands it — a control
//! plane cannot know a peer's home, and a guess applied to somebody else's
//! filesystem is exactly what this operation must not be.
//!
//! Three properties are load-bearing, and the tests below are about them rather
//! than about happy paths:
//!
//! * **Symlinks are never followed.** A hub snapshot is a directory of links
//!   into `blobs/`; following them counts every model twice, and following one
//!   out of a cache during a *clean* deletes something that is not a cache.
//! * **The walk does not leave its filesystem.** A bind mount or an NFS mount
//!   under a cache is somebody else's disk, and `du -x` is what an operator
//!   would have run.
//! * **The walk is bounded.** A cache of millions of small shards is a real
//!   thing, and a scan with no ceiling turns a page's poll into a stalled
//!   worker. The ceiling is reported, so the number reads as a floor rather
//!   than as a total that happens to be wrong.

use std::fs;
use std::os::unix::fs::MetadataExt;
use std::path::{Component, Path, PathBuf};

use crate::proto::{CacheClean, CacheDir, CacheDirResult, CacheScan, CleanCache, ScanCache};

/// Files one scan counts before it stops and says it stopped.
///
/// A million entries is already an absurd cache and takes seconds to walk; the
/// point of the ceiling is that the *next* order of magnitude cannot hold the
/// blocking pool open while `/api/cache` waits on it.
pub const FILE_CEILING: u64 = 1_000_000;

/// The model cache, relative to `$HOME`.
///
/// Refused by `clean` unless the request says otherwise, because removing a
/// model is the Models section's operation — it knows which nodes hold a
/// snapshot and what the manifest says — and a cache sweep that quietly took
/// 48 GB of downloaded weights with it would be indistinguishable from one that
/// freed some JIT artefacts.
const HUB_RELATIVE: &str = ".cache/huggingface/hub";

// ── Scanning ────────────────────────────────────────────────────────────────

pub fn scan(request: &ScanCache) -> CacheScan {
    let home = home_dir();
    CacheScan {
        dirs: request
            .paths
            .iter()
            .map(|path| scan_one(path, home.as_deref()))
            .collect(),
    }
}

fn scan_one(raw: &str, home: Option<&Path>) -> CacheDir {
    let path = match expand(raw, home) {
        Ok(path) => path,
        Err(error) => {
            return CacheDir {
                path: raw.to_string(),
                error,
                ..CacheDir::default()
            }
        }
    };
    let shown = path.to_string_lossy().into_owned();

    // `metadata`, not `symlink_metadata`: the *root* is a path the control
    // plane named, and a cache directory that is itself a symlink (a home with
    // `.cache` moved onto another disk is common enough) is still that cache.
    // Everything inside it is read with `symlink_metadata`, which is where the
    // double counting would otherwise happen.
    let root = match fs::metadata(&path) {
        Ok(root) => root,
        Err(_) => {
            return CacheDir {
                path: shown,
                exists: false,
                ..CacheDir::default()
            }
        }
    };
    if !root.is_dir() {
        return CacheDir {
            path: shown,
            exists: true,
            error: "not a directory".into(),
            ..CacheDir::default()
        };
    }

    let mut walk = Walk::new(root.dev());
    walk.run(&path);
    CacheDir {
        path: shown,
        exists: true,
        bytes: walk.bytes,
        files: walk.files,
        error: walk.error,
        truncated: walk.truncated,
    }
}

/// One directory tree, measured without following links or crossing devices.
struct Walk {
    root_dev: u64,
    bytes: u64,
    files: u64,
    truncated: bool,
    /// The first directory that could not be read. One is enough to explain
    /// why a number is low; a list of ten thousand is not an explanation.
    error: String,
}

impl Walk {
    fn new(root_dev: u64) -> Self {
        Self {
            root_dev,
            bytes: 0,
            files: 0,
            truncated: false,
            error: String::new(),
        }
    }

    /// Iterative, with an explicit stack: a cache is arbitrarily deep and a
    /// recursive walk that overflows the stack takes the whole agent with it.
    fn run(&mut self, root: &Path) {
        let mut stack = vec![root.to_path_buf()];
        while let Some(dir) = stack.pop() {
            if self.truncated {
                return;
            }
            let entries = match fs::read_dir(&dir) {
                Ok(entries) => entries,
                Err(error) => {
                    if self.error.is_empty() {
                        self.error = format!("{}: {error}", dir.display());
                    }
                    continue;
                }
            };
            for entry in entries.flatten() {
                let path = entry.path();
                let Ok(meta) = fs::symlink_metadata(&path) else {
                    continue;
                };
                if meta.file_type().is_symlink() {
                    // An entry, not its target. `snapshots/<rev>/model.safetensors`
                    // is a link into `blobs/`, and the bytes are counted once —
                    // where they are.
                    self.count(0);
                } else if meta.is_dir() {
                    if meta.dev() == self.root_dev {
                        stack.push(path);
                    }
                } else {
                    self.count(meta.len());
                }
                if self.truncated {
                    return;
                }
            }
        }
    }

    fn count(&mut self, bytes: u64) {
        if self.files >= FILE_CEILING {
            self.truncated = true;
            return;
        }
        self.files += 1;
        self.bytes += bytes;
    }
}

// ── Cleaning ────────────────────────────────────────────────────────────────

pub fn clean(request: &CleanCache) -> CacheClean {
    let home = home_dir();
    CacheClean {
        results: request
            .paths
            .iter()
            .map(|path| clean_one(path, home.as_deref(), request.include_hub))
            .collect(),
    }
}

fn clean_one(raw: &str, home: Option<&Path>, include_hub: bool) -> CacheDirResult {
    let refused = |path: String, error: &str| CacheDirResult {
        path,
        removed: false,
        freed_bytes: 0,
        error: error.to_string(),
    };

    let path = match expand(raw, home) {
        Ok(path) => path,
        Err(error) => return refused(raw.to_string(), &error),
    };
    let shown = path.to_string_lossy().into_owned();

    let Some(home) = home else {
        return refused(shown, "the agent has no $HOME, so nothing is under it");
    };
    // Resolved before it is judged: a relative path, a `..`, or a directory
    // that is a symlink out of the home are all ways to name somewhere else,
    // and a check made on the name rather than on the destination catches none
    // of them.
    let home = resolve(home);
    let resolved = resolve(&path);
    if !resolved.is_absolute()
        || resolved == home
        || !resolved.starts_with(&home)
        || resolved.components().any(|c| c == Component::ParentDir)
    {
        return refused(
            shown,
            "refused: a cache to empty must be under the agent user's home",
        );
    }
    let hub = home.join(HUB_RELATIVE);
    if !include_hub && resolved.starts_with(&hub) {
        return refused(
            shown,
            "refused: the hub cache holds downloaded models, which the \
             Models section removes",
        );
    }

    let root = match fs::metadata(&resolved) {
        // Not there is not a failure: the cache was never filled, and there is
        // nothing to free. `removed: false` with no error says exactly that.
        Err(_) => {
            return CacheDirResult {
                path: shown,
                ..CacheDirResult::default()
            }
        }
        Ok(root) => root,
    };
    if !root.is_dir() {
        return refused(shown, "not a directory");
    }

    let entries = match fs::read_dir(&resolved) {
        Ok(entries) => entries,
        Err(error) => return refused(shown, &format!("could not open it: {error}")),
    };

    let mut freed = 0_u64;
    let mut removed = false;
    let mut failure = String::new();
    for entry in entries.flatten() {
        let child = entry.path();
        let Ok(meta) = fs::symlink_metadata(&child) else {
            continue;
        };
        // A symlink is unlinked, never descended into: `remove_dir_all` on a
        // link to a directory is how a cache clean comes to delete a model
        // tree that merely happened to be pointed at.
        let (size, outcome) = if meta.file_type().is_symlink() {
            (0, fs::remove_file(&child))
        } else if meta.is_dir() {
            let mut walk = Walk::new(root.dev());
            walk.run(&child);
            (walk.bytes, fs::remove_dir_all(&child))
        } else {
            (meta.len(), fs::remove_file(&child))
        };
        match outcome {
            Ok(()) => {
                freed += size;
                removed = true;
            }
            Err(error) if failure.is_empty() => {
                failure = format!("{}: {error}", child.display());
            }
            Err(_) => {}
        }
    }

    CacheDirResult {
        path: shown,
        removed,
        freed_bytes: freed,
        error: failure,
    }
}

// ── Paths ───────────────────────────────────────────────────────────────────

/// The path with every symlink and `..` in it resolved, as far as it exists.
///
/// `fs::canonicalize` alone is not enough: it refuses a path that is not there,
/// and a cache directory that was never created is the ordinary case — so the
/// deepest ancestor that *does* exist is resolved and the rest is rejoined.
/// Resolving only one side would be worse than resolving neither: comparing a
/// canonical `$HOME` against a lexical target is how `/var/…` and
/// `/private/var/…` come to disagree and every clean is refused.
fn resolve(path: &Path) -> PathBuf {
    let mut suffix: Vec<std::ffi::OsString> = Vec::new();
    let mut current = path.to_path_buf();
    loop {
        if let Ok(real) = fs::canonicalize(&current) {
            let mut out = real;
            out.extend(suffix.iter().rev());
            return out;
        }
        let (Some(parent), Some(name)) = (current.parent(), current.file_name()) else {
            return path.to_path_buf();
        };
        suffix.push(name.to_os_string());
        current = parent.to_path_buf();
        if current.as_os_str().is_empty() {
            return path.to_path_buf();
        }
    }
}

fn home_dir() -> Option<PathBuf> {
    std::env::var_os("HOME")
        .filter(|home| !home.is_empty())
        .map(PathBuf::from)
}

/// `~/.cache/vllm` against this node's own home.
///
/// The control plane sends the tilde form because it cannot know a peer's
/// home; an absolute path is passed through unchanged, which is what lets a
/// test — and an operator debugging one node — name a directory directly.
fn expand(raw: &str, home: Option<&Path>) -> Result<PathBuf, String> {
    let raw = raw.trim();
    if raw.is_empty() {
        return Err("empty path".into());
    }
    let Some(rest) = raw.strip_prefix('~') else {
        return Ok(PathBuf::from(raw));
    };
    let home = home.ok_or_else(|| format!("cannot expand {raw}: the agent has no $HOME"))?;
    let rest = rest.trim_start_matches('/');
    if rest.is_empty() {
        return Ok(home.to_path_buf());
    }
    Ok(home.join(rest))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs as unix_fs;

    fn scan_path(path: &Path, home: Option<&Path>) -> CacheDir {
        scan_one(&path.to_string_lossy(), home)
    }

    #[test]
    fn a_directory_is_measured_through_its_subdirectories() {
        let tmp = tempfile::tempdir().unwrap();
        fs::write(tmp.path().join("a.bin"), vec![0u8; 100]).unwrap();
        let nested = tmp.path().join("nested");
        fs::create_dir(&nested).unwrap();
        fs::write(nested.join("b.bin"), vec![0u8; 50]).unwrap();

        let dir = scan_path(tmp.path(), None);

        assert!(dir.exists);
        assert_eq!(dir.files, 2);
        assert_eq!(dir.bytes, 150);
        assert!(!dir.truncated);
        assert_eq!(dir.error, "");
    }

    #[test]
    fn a_directory_that_was_never_created_is_absent_rather_than_an_error() {
        let tmp = tempfile::tempdir().unwrap();

        let dir = scan_path(&tmp.path().join("never"), None);

        assert!(!dir.exists);
        assert_eq!(dir.bytes, 0);
        assert_eq!(dir.error, "");
    }

    #[test]
    fn a_symlink_is_counted_once_and_never_followed() {
        // What a hub cache is: `blobs/` holds the bytes and `snapshots/` is
        // links into it. Following them reports a 48 GB cache as 96 GB.
        let tmp = tempfile::tempdir().unwrap();
        let blobs = tmp.path().join("blobs");
        let snapshots = tmp.path().join("snapshots");
        fs::create_dir_all(&blobs).unwrap();
        fs::create_dir_all(&snapshots).unwrap();
        fs::write(blobs.join("sha256-x"), vec![7u8; 4096]).unwrap();
        unix_fs::symlink(blobs.join("sha256-x"), snapshots.join("weights.bin")).unwrap();

        let dir = scan_path(tmp.path(), None);

        assert_eq!(dir.bytes, 4096, "the blob is counted where the bytes are");
        assert_eq!(dir.files, 2, "the link is an entry, worth no bytes");
    }

    #[test]
    fn a_symlink_to_a_directory_is_not_descended_into() {
        let tmp = tempfile::tempdir().unwrap();
        let outside = tmp.path().join("outside");
        fs::create_dir(&outside).unwrap();
        fs::write(outside.join("big"), vec![0u8; 8192]).unwrap();
        let cache = tmp.path().join("cache");
        fs::create_dir(&cache).unwrap();
        unix_fs::symlink(&outside, cache.join("link")).unwrap();

        let dir = scan_path(&cache, None);

        assert_eq!(dir.bytes, 0);
        assert_eq!(dir.files, 1);
    }

    #[test]
    fn the_walk_stops_at_its_ceiling_and_says_so() {
        let tmp = tempfile::tempdir().unwrap();
        for index in 0..8 {
            fs::write(tmp.path().join(format!("{index}")), b"x").unwrap();
        }

        let root = fs::metadata(tmp.path()).unwrap();
        let mut walk = Walk::new(root.dev());
        // The production ceiling is a million; a test that wrote a million
        // files would be testing the filesystem. The *mechanism* is what
        // matters, so it is started close to its limit.
        walk.files = FILE_CEILING - 3;
        walk.run(tmp.path());

        assert!(walk.truncated, "a walk that stopped early must say it did");
        assert_eq!(walk.files, FILE_CEILING);
    }

    #[test]
    fn a_path_that_is_not_a_directory_says_so_without_a_number() {
        let tmp = tempfile::tempdir().unwrap();
        let file = tmp.path().join("a-file");
        fs::write(&file, b"x").unwrap();

        let dir = scan_path(&file, None);

        assert!(dir.exists);
        assert_eq!(dir.error, "not a directory");
        assert_eq!(dir.bytes, 0);
    }

    #[test]
    fn a_tilde_path_is_expanded_against_the_agents_own_home() {
        let tmp = tempfile::tempdir().unwrap();
        let cache = tmp.path().join(".cache").join("vllm");
        fs::create_dir_all(&cache).unwrap();
        fs::write(cache.join("graph"), vec![0u8; 64]).unwrap();

        let dir = scan_one("~/.cache/vllm", Some(tmp.path()));

        assert!(dir.exists);
        assert_eq!(dir.bytes, 64);
        assert!(dir.path.ends_with(".cache/vllm"));
        assert!(
            !dir.path.contains('~'),
            "the node resolves it, and says where"
        );
    }

    #[test]
    fn a_tilde_path_with_no_home_is_an_error_rather_than_a_guess() {
        let dir = scan_one("~/.cache/vllm", None);

        assert!(!dir.exists);
        assert!(dir.error.contains("$HOME"));
    }

    #[test]
    fn scan_answers_every_path_it_was_given_in_order() {
        let tmp = tempfile::tempdir().unwrap();
        fs::create_dir_all(tmp.path().join(".triton")).unwrap();

        let out = scan(&ScanCache {
            paths: vec!["~/.triton".into(), "~/.cache/nothing".into()],
        });

        assert_eq!(out.dirs.len(), 2);
    }

    // ── Cleaning ────────────────────────────────────────────────────────────

    fn clean_in(home: &Path, relative: &str, include_hub: bool) -> CacheDirResult {
        clean_one(&format!("~/{relative}"), Some(home), include_hub)
    }

    #[test]
    fn the_contents_go_and_the_directory_stays() {
        // The directory itself must survive: Docker recreates a missing
        // bind-mount source owned by root, and an operator's cache that turns
        // root-owned is how the next deploy fails with [Errno 13].
        let tmp = tempfile::tempdir().unwrap();
        let cache = tmp.path().join(".cache").join("vllm");
        fs::create_dir_all(cache.join("graphs")).unwrap();
        fs::write(cache.join("a.bin"), vec![0u8; 100]).unwrap();
        fs::write(cache.join("graphs").join("b.bin"), vec![0u8; 50]).unwrap();

        let result = clean_in(tmp.path(), ".cache/vllm", false);

        assert!(result.removed);
        assert_eq!(result.freed_bytes, 150);
        assert_eq!(result.error, "");
        assert!(cache.is_dir());
        assert_eq!(fs::read_dir(&cache).unwrap().count(), 0);
    }

    #[test]
    fn a_symlink_out_of_the_cache_is_unlinked_not_followed() {
        let tmp = tempfile::tempdir().unwrap();
        let precious = tmp.path().join("models");
        fs::create_dir_all(&precious).unwrap();
        fs::write(precious.join("weights"), vec![0u8; 4096]).unwrap();
        let cache = tmp.path().join(".cache").join("vllm");
        fs::create_dir_all(&cache).unwrap();
        unix_fs::symlink(&precious, cache.join("escape")).unwrap();

        let result = clean_in(tmp.path(), ".cache/vllm", false);

        assert!(result.removed);
        assert!(precious.join("weights").exists(), "the target must survive");
        assert!(!cache.join("escape").exists());
    }

    #[test]
    fn a_directory_outside_the_home_is_refused() {
        let tmp = tempfile::tempdir().unwrap();
        let home = tmp.path().join("home");
        fs::create_dir_all(&home).unwrap();
        let elsewhere = tmp.path().join("etc");
        fs::create_dir_all(&elsewhere).unwrap();
        fs::write(elsewhere.join("passwd"), b"root:x:0:0").unwrap();

        let result = clean_one(&elsewhere.to_string_lossy(), Some(&home), false);

        assert!(!result.removed);
        assert!(result.error.contains("under the agent user's home"));
        assert!(elsewhere.join("passwd").exists());
    }

    #[test]
    fn the_home_itself_is_refused() {
        let tmp = tempfile::tempdir().unwrap();
        fs::write(tmp.path().join("something"), b"x").unwrap();

        let result = clean_one("~", Some(tmp.path()), false);

        assert!(!result.removed);
        assert!(tmp.path().join("something").exists());
    }

    #[test]
    fn a_dot_dot_escape_is_refused() {
        let tmp = tempfile::tempdir().unwrap();
        let home = tmp.path().join("home");
        fs::create_dir_all(&home).unwrap();
        let elsewhere = tmp.path().join("etc");
        fs::create_dir_all(&elsewhere).unwrap();
        fs::write(elsewhere.join("passwd"), b"root:x:0:0").unwrap();

        let result = clean_one("~/../etc", Some(&home), false);

        assert!(!result.removed);
        assert!(elsewhere.join("passwd").exists());
    }

    #[test]
    fn the_hub_cache_is_refused_unless_the_request_names_it() {
        let tmp = tempfile::tempdir().unwrap();
        let hub = tmp.path().join(HUB_RELATIVE);
        fs::create_dir_all(&hub).unwrap();
        fs::write(hub.join("model"), vec![0u8; 2048]).unwrap();

        let refused = clean_in(tmp.path(), HUB_RELATIVE, false);

        assert!(!refused.removed);
        assert!(refused.error.contains("Models section"));
        assert!(hub.join("model").exists());

        let allowed = clean_in(tmp.path(), HUB_RELATIVE, true);

        assert!(allowed.removed);
        assert_eq!(allowed.freed_bytes, 2048);
        assert!(hub.is_dir());
    }

    #[test]
    fn a_directory_below_the_hub_is_refused_too() {
        let tmp = tempfile::tempdir().unwrap();
        let inside = tmp.path().join(HUB_RELATIVE).join("models--a--b");
        fs::create_dir_all(&inside).unwrap();
        fs::write(inside.join("x"), b"x").unwrap();

        let result = clean_in(tmp.path(), &format!("{HUB_RELATIVE}/models--a--b"), false);

        assert!(!result.removed);
        assert!(inside.join("x").exists());
    }

    #[test]
    fn a_cache_that_was_never_created_is_not_a_failure() {
        let tmp = tempfile::tempdir().unwrap();

        let result = clean_in(tmp.path(), ".cache/flashinfer", false);

        assert!(!result.removed);
        assert_eq!(result.freed_bytes, 0);
        assert_eq!(result.error, "", "nothing to free is not an error");
    }

    #[test]
    fn a_cache_that_is_already_empty_frees_nothing_and_says_nothing() {
        let tmp = tempfile::tempdir().unwrap();
        fs::create_dir_all(tmp.path().join(".triton")).unwrap();

        let result = clean_in(tmp.path(), ".triton", false);

        assert!(!result.removed);
        assert_eq!(result.error, "");
    }

    #[test]
    fn clean_answers_every_path_it_was_given() {
        let tmp = tempfile::tempdir().unwrap();
        fs::create_dir_all(tmp.path().join(".triton")).unwrap();
        fs::write(tmp.path().join(".triton").join("a"), vec![0u8; 9]).unwrap();
        // Absolute, because `clean` reads the real `$HOME` rather than a
        // fixture's — the per-path helper is where the home is injectable.
        let out = clean(&CleanCache {
            paths: vec!["/nonexistent-cache-for-a-test".into()],
            include_hub: false,
        });

        assert_eq!(out.results.len(), 1);
        assert!(!out.results[0].removed);
    }
}
