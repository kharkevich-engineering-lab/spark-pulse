//! The two pure functions in `facts` that nothing else would catch getting wrong.
//!
//! **Interface classification** has a live twin in Python:
//! `spark_pulse.tools.discovery._classify_interface`, which the control plane
//! uses to fill a node's record. The fixture here is generated from *that*
//! function, and `tests/test_agent_facts_interop.py` asserts the same fixture
//! from the other side, so neither can drift alone. It matters beyond
//! tidiness because the hardware fingerprint excludes docker interfaces: two
//! implementations that disagree about which are docker produce different
//! fingerprints, and a node whose fingerprint moves reports a reimage on every
//! heartbeat.
//!
//! **The fingerprint itself** is tested for its properties rather than against
//! a golden value. There is no Python twin left to agree with — the Python
//! agent never reached `main`, so no node in the field carries a
//! Python-computed fingerprint — and a golden hash would only assert that the
//! algorithm is the algorithm. What has to hold is what the control plane
//! relies on: stable across reboots and across anything that is configuration
//! rather than hardware, and different when the machine is.

use std::collections::BTreeMap;
use std::path::PathBuf;

use spark_pulse_agent::facts::{classify_interface, fingerprint};
use spark_pulse_agent::proto::NetworkInterface;

fn interface(name: &str) -> NetworkInterface {
    NetworkInterface {
        name: name.to_string(),
        r#type: classify_interface(name).to_string(),
        ..Default::default()
    }
}

fn print(names: &[&str], machine_id: &str, cpus: u32, memory: u64) -> String {
    let interfaces: Vec<NetworkInterface> = names.iter().map(|n| interface(n)).collect();
    fingerprint(&interfaces, machine_id, cpus, memory)
}

// ── Classification, against Python's own answers ────────────────────────────

#[test]
fn every_interface_is_classified_the_way_python_classifies_it() {
    let path =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/interface-classes.json");
    let raw = std::fs::read(&path).unwrap_or_else(|e| panic!("reading {}: {e}", path.display()));
    let fixture: serde_json::Value = serde_json::from_slice(&raw).expect("fixture json");
    let expected: BTreeMap<String, String> =
        serde_json::from_value(fixture["classes"].clone()).expect("classes");

    assert!(expected.len() > 20, "the fixture must cover every branch");
    for (name, kind) in &expected {
        assert_eq!(
            classify_interface(name),
            kind,
            "{name:?} is {kind:?} to the control plane but not to the agent; the \
             fingerprint excludes docker interfaces, so this changes it"
        );
    }
}

// ── The fingerprint's properties ────────────────────────────────────────────

#[test]
fn the_same_machine_prints_the_same_every_time() {
    // Compared against what enrolment recorded on *every* heartbeat. An
    // unstable print is a node that reports a reimage several times a minute.
    let first = print(&["enp1s0", "ib0"], "machine-1", 20, 128 << 30);
    for _ in 0..50 {
        assert_eq!(print(&["enp1s0", "ib0"], "machine-1", 20, 128 << 30), first);
    }
}

#[test]
fn the_order_the_kernel_enumerates_interfaces_in_does_not_matter() {
    // `/sys/class/net` has no guaranteed order, so a print that depended on it
    // would change when nothing about the machine had.
    assert_eq!(
        print(&["enp1s0", "ib0", "ib1"], "m", 20, 1),
        print(&["ib1", "enp1s0", "ib0"], "m", 20, 1)
    );
}

#[test]
fn starting_a_container_does_not_change_the_machine() {
    // The whole reason docker interfaces are excluded: `docker0` and every
    // `br-*` appear and vanish with workloads, and a fingerprint that moved
    // when a container started would be useless for detecting a reimage.
    let bare = print(&["enp1s0"], "m", 20, 1);
    assert_eq!(print(&["enp1s0", "docker0"], "m", 20, 1), bare);
    assert_eq!(print(&["enp1s0", "docker0", "br-1a2b3c"], "m", 20, 1), bare);
}

#[test]
fn different_hardware_prints_differently() {
    let base = print(&["enp1s0"], "m", 20, 1);
    assert_ne!(print(&["enp1s0", "ib0"], "m", 20, 1), base, "an added NIC");
    assert_ne!(print(&["enp2s0"], "m", 20, 1), base, "a renamed NIC");
    assert_ne!(print(&["enp1s0"], "m", 40, 1), base, "twice the CPUs");
    assert_ne!(print(&["enp1s0"], "m", 20, 2), base, "different memory");
    assert_ne!(print(&["enp1s0"], "other", 20, 1), base, "a new machine-id");
}

#[test]
fn a_machine_with_nothing_identifying_prints_nothing() {
    // A CPU count and a memory size are not an identity: every Spark has the
    // same ones. A fingerprint built from those alone would be *identical on
    // every node*, and the control plane would read two machines as one —
    // which is worse than admitting we do not know.
    assert_eq!(print(&[], "", 0, 0), "");
    assert_eq!(
        print(&[], "", 20, 128 << 30),
        "",
        "cpu and memory alone are not an identity"
    );
    assert_eq!(
        print(&["docker0"], "", 20, 1),
        "",
        "and docker interfaces are not either"
    );

    // Either an interface name or a machine-id is enough to be worth printing.
    assert_ne!(print(&[], "machine-1", 0, 0), "");
    assert_ne!(print(&["enp1s0"], "", 0, 0), "");
}

#[test]
fn a_print_is_a_sha256_in_hex() {
    let value = print(&["enp1s0"], "m", 20, 1);
    assert_eq!(value.len(), 64);
    assert!(value
        .chars()
        .all(|c| c.is_ascii_hexdigit() && !c.is_uppercase()));
}

#[test]
fn only_docker_interfaces_are_excluded_not_every_virtual_one() {
    // `veth*` and `tun0` classify as "other", not "docker", so they *are*
    // counted. That is deliberate rather than an oversight: the exclusion
    // mirrors the control plane's classification exactly, and widening it here
    // would be the two implementations disagreeing again.
    let bare = print(&["enp1s0"], "m", 20, 1);
    assert_ne!(print(&["enp1s0", "veth1a2b3c"], "m", 20, 1), bare);
}
