//! The identity is written as one commit, and a crash cannot tear it.
//!
//! A renewal replaces the key, the certificate, the bundle and the metadata
//! together. Written in place, one at a time, a crash between two of them left
//! a node whose key did not match its certificate, or an `identity.json` that
//! was empty — "half an identity", which costs a re-enrolment. A CI run caught
//! the second shape: a reader opened `identity.json` mid-renewal and found zero
//! bytes. So `save` stages every file, marks the staging complete, then
//! renames; `recover` (run by every load and save) finishes a marked commit and
//! discards an unmarked one.

use std::fs;
use std::path::Path;

use spark_pulse_agent::identity::{recover, AgentIdentity, IdentityMeta, COMMIT_MARKER};

fn identity(dir: &Path, generation: &str) -> AgentIdentity {
    AgentIdentity {
        directory: dir.to_path_buf(),
        meta: IdentityMeta {
            node_id: "node-1".into(),
            epoch: generation.len() as u64,
            ..Default::default()
        },
        key_pem: format!("key {generation}").into_bytes(),
        certificate_pem: format!("cert {generation}").into_bytes(),
        trust_bundle_pem: format!("bundle {generation}").into_bytes(),
    }
}

fn leftovers(dir: &Path) -> Vec<String> {
    let mut names: Vec<String> = fs::read_dir(dir)
        .unwrap()
        .flatten()
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .filter(|n| n.ends_with(".next") || n == COMMIT_MARKER)
        .collect();
    names.sort();
    names
}

/// What `save` leaves when the machine dies after staging but before every
/// rename: some files already in place, some still staged.
fn stage(dir: &Path, generation: &str, renamed: &[&str]) {
    let next = identity(dir, generation);
    let meta = format!("{}\n", serde_json::to_string_pretty(&next.meta).unwrap());
    for (name, bytes) in [
        ("ca.pem", next.trust_bundle_pem.clone()),
        ("node.crt", next.certificate_pem.clone()),
        ("node.key", next.key_pem.clone()),
        ("identity.json", meta.into_bytes()),
    ] {
        let target = if renamed.contains(&name) {
            dir.join(name)
        } else {
            dir.join(format!("{name}.next"))
        };
        fs::write(target, bytes).unwrap();
    }
}

#[test]
fn a_save_is_read_back_and_leaves_no_staging() {
    let tmp = tempfile::tempdir().unwrap();
    identity(tmp.path(), "one").save().unwrap();
    let loaded = AgentIdentity::load(tmp.path()).unwrap().unwrap();
    assert_eq!(loaded.key_pem, b"key one");
    assert_eq!(loaded.certificate_pem, b"cert one");
    assert!(
        leftovers(tmp.path()).is_empty(),
        "{:?}",
        leftovers(tmp.path())
    );
}

#[test]
fn a_renewal_replaces_every_file() {
    let tmp = tempfile::tempdir().unwrap();
    identity(tmp.path(), "one").save().unwrap();
    identity(tmp.path(), "two").save().unwrap();
    let loaded = AgentIdentity::load(tmp.path()).unwrap().unwrap();
    assert_eq!(loaded.key_pem, b"key two");
    assert_eq!(loaded.certificate_pem, b"cert two");
    assert_eq!(loaded.trust_bundle_pem, b"bundle two");
    assert_eq!(loaded.meta.epoch, 3);
}

#[test]
fn a_crash_after_the_marker_is_rolled_forward_to_the_new_identity() {
    let tmp = tempfile::tempdir().unwrap();
    identity(tmp.path(), "one").save().unwrap();
    // Died mid-rename: the bundle and certificate are new, the key and the
    // metadata are still staged. In place, this was a mismatched pair.
    stage(tmp.path(), "two", &["ca.pem", "node.crt"]);
    fs::write(tmp.path().join(COMMIT_MARKER), b"").unwrap();

    let loaded = AgentIdentity::load(tmp.path()).unwrap().unwrap();
    assert_eq!(loaded.key_pem, b"key two");
    assert_eq!(loaded.certificate_pem, b"cert two");
    assert_eq!(loaded.meta.epoch, 3);
    assert!(leftovers(tmp.path()).is_empty());
}

#[test]
fn a_crash_before_the_marker_keeps_the_old_identity_whole() {
    let tmp = tempfile::tempdir().unwrap();
    identity(tmp.path(), "one").save().unwrap();
    // Died while staging: nothing was renamed, and no marker says the
    // staging is complete, so it never became the identity.
    stage(tmp.path(), "two", &[]);

    let loaded = AgentIdentity::load(tmp.path()).unwrap().unwrap();
    assert_eq!(loaded.key_pem, b"key one");
    assert_eq!(loaded.certificate_pem, b"cert one");
    assert!(leftovers(tmp.path()).is_empty());
}

#[test]
fn a_first_enrolment_torn_before_the_marker_is_no_identity_not_half_of_one() {
    let tmp = tempfile::tempdir().unwrap();
    stage(tmp.path(), "one", &[]);
    assert!(AgentIdentity::load(tmp.path()).unwrap().is_none());
}

#[test]
fn recovering_twice_is_the_same_as_once() {
    let tmp = tempfile::tempdir().unwrap();
    identity(tmp.path(), "one").save().unwrap();
    stage(tmp.path(), "two", &["ca.pem"]);
    fs::write(tmp.path().join(COMMIT_MARKER), b"").unwrap();
    recover(tmp.path()).unwrap();
    recover(tmp.path()).unwrap();
    let loaded = AgentIdentity::load(tmp.path()).unwrap().unwrap();
    assert_eq!(loaded.key_pem, b"key two");
}

#[test]
fn destroy_takes_a_staging_with_it() {
    let tmp = tempfile::tempdir().unwrap();
    identity(tmp.path(), "one").save().unwrap();
    stage(tmp.path(), "two", &[]);
    fs::write(tmp.path().join(COMMIT_MARKER), b"").unwrap();
    AgentIdentity::destroy(tmp.path()).unwrap();
    assert!(AgentIdentity::load(tmp.path()).unwrap().is_none());
}

#[cfg(unix)]
#[test]
fn the_key_is_private_from_the_moment_it_exists() {
    use std::os::unix::fs::PermissionsExt;
    let tmp = tempfile::tempdir().unwrap();
    identity(tmp.path(), "one").save().unwrap();
    let mode = fs::metadata(tmp.path().join("node.key"))
        .unwrap()
        .permissions()
        .mode();
    assert_eq!(mode & 0o777, 0o600);
}
