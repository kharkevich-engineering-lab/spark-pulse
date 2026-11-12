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

use spark_pulse_agent::facts::{
    classify_interface, composite_fingerprint, fingerprint, identifying_names,
    legacy_composite_fingerprint, port_is_active, roce_links,
};
use spark_pulse_agent::proto::NetworkInterface;

fn interface(name: &str) -> NetworkInterface {
    NetworkInterface {
        name: name.to_string(),
        r#type: classify_interface(name).to_string(),
        ..Default::default()
    }
}

/// The fingerprint of a machine described by these names, through the *pure*
/// half.
///
/// Not through `fingerprint`, deliberately. That one reads this machine's
/// board serial first, and every cloud VM publishes one — so a property test
/// routed through it passes on a laptop and asserts nothing on a CI runner,
/// which is exactly what happened before this was split.
fn print(names: &[&str], machine_id: &str, cpus: u32) -> String {
    let interfaces: Vec<NetworkInterface> = names.iter().map(|n| interface(n)).collect();
    let identifying = identifying_names(&interfaces);
    composite_fingerprint(&identifying, machine_id, cpus)
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
    let first = print(&["enp1s0", "ib0"], "machine-1", 20);
    for _ in 0..50 {
        assert_eq!(print(&["enp1s0", "ib0"], "machine-1", 20), first);
    }
}

#[test]
fn the_order_the_kernel_enumerates_interfaces_in_does_not_matter() {
    // `/sys/class/net` has no guaranteed order, so a print that depended on it
    // would change when nothing about the machine had.
    assert_eq!(
        print(&["enp1s0", "ib0", "ib1"], "m", 20),
        print(&["ib1", "enp1s0", "ib0"], "m", 20)
    );
}

#[test]
fn starting_a_container_does_not_change_the_machine() {
    // The whole reason docker interfaces are excluded: `docker0` and every
    // `br-*` appear and vanish with workloads, and a fingerprint that moved
    // when a container started would be useless for detecting a reimage.
    let bare = print(&["enp1s0"], "m", 20);
    assert_eq!(print(&["enp1s0", "docker0"], "m", 20), bare);
    assert_eq!(print(&["enp1s0", "docker0", "br-1a2b3c"], "m", 20), bare);
    assert_eq!(
        print(&["enp1s0", "docker0", "br-1a2b3c", "veth1a2b3c"], "m", 20),
        bare
    );
}

#[test]
fn different_hardware_prints_differently() {
    let base = print(&["enp1s0"], "m", 20);
    assert_ne!(print(&["enp1s0", "ib0"], "m", 20), base, "an added NIC");
    assert_ne!(print(&["enp2s0"], "m", 20), base, "a renamed NIC");
    assert_ne!(print(&["enp1s0"], "m", 40), base, "twice the CPUs");
    assert_ne!(print(&["enp1s0"], "other", 20), base, "a new machine-id");
}

#[test]
fn a_machine_with_nothing_identifying_prints_nothing() {
    // A CPU count is not an identity: every Spark has the same one. A fingerprint built from those alone would be *identical on
    // every node*, and the control plane would read two machines as one —
    // which is worse than admitting we do not know.
    assert_eq!(print(&[], "", 0), "");
    assert_eq!(
        print(&[], "", 20),
        "",
        "a cpu count alone is not an identity"
    );
    assert_eq!(
        print(&["docker0"], "", 20),
        "",
        "and docker interfaces are not either"
    );

    // Either an interface name or a machine-id is enough to be worth printing.
    assert_ne!(print(&[], "machine-1", 0), "");
    assert_ne!(print(&["enp1s0"], "", 0), "");
}

#[test]
fn a_print_is_a_sha256_in_hex() {
    let value = print(&["enp1s0"], "m", 20);
    assert_eq!(value.len(), 64);
    assert!(value
        .chars()
        .all(|c| c.is_ascii_hexdigit() && !c.is_uppercase()));
}

#[test]
fn a_containers_veth_is_docker_and_does_not_count() {
    // A `veth*` is the host end of a container's cable and it comes and goes
    // with the container. It was once classified "other" and therefore
    // counted, and the consequence was observed on a control node: stopping a
    // container that had been running since before enrolment changed the
    // fingerprint, the ledger denied the node as reimaged, and the control
    // plane could not start.
    let bare = print(&["enp1s0"], "m", 20);
    assert_eq!(print(&["enp1s0", "veth1a2b3c"], "m", 20), bare);
    assert_eq!(
        print(&["enp1s0", "veth8bf252c", "br-1a2b3c"], "m", 20),
        bare
    );
}

#[test]
fn other_virtual_interfaces_still_count() {
    // `tun0`, `tailscale0` and the like classify as "other", so they *are*
    // counted. The exclusion mirrors the control plane's classification
    // exactly, and widening it here alone would be the two implementations
    // disagreeing again — the fixture test above is what keeps them together.
    let bare = print(&["enp1s0"], "m", 20);
    assert_ne!(print(&["enp1s0", "tun0"], "m", 20), bare);
}

// ── The legacy print, and why it is not the one compared ────────────────────

/// What `gx10-b90f` reported, as a DGX Spark with no readable board serial.
const B90F: [&str; 7] = [
    "enP2p1s0f0np0",
    "enP2p1s0f1np1",
    "enP7s7",
    "enp1s0f0np0",
    "enp1s0f1np1",
    "lo",
    "wlP9s9",
];
const B90F_MACHINE_ID: &str = "d103edc98a4b40419003c632707f59b6";

#[test]
fn a_reboot_that_moves_memory_by_a_page_does_not_move_the_stable_print() {
    // Observed: `sysconf` said 130594021376 bytes at enrolment and
    // 130594025472 after a reboot, one 4 KiB page more. The legacy print
    // moved with it and the ledger denied the node as reimaged; the stable
    // print takes no memory size at all, so there is nothing to move.
    let before = legacy_composite_fingerprint(&B90F, B90F_MACHINE_ID, 20, 130_594_021_376);
    let after = legacy_composite_fingerprint(&B90F, B90F_MACHINE_ID, 20, 130_594_025_472);
    assert_ne!(before, after, "the legacy print is the one that broke");
    assert_eq!(
        composite_fingerprint(&B90F, B90F_MACHINE_ID, 20),
        composite_fingerprint(&B90F, B90F_MACHINE_ID, 20)
    );
}

#[test]
fn the_legacy_print_is_still_exactly_the_old_algorithm() {
    // The one golden value here, and deliberately: a control plane from
    // before `stable_fingerprint` compares this field against what it
    // recorded at enrolment, so it has to be the old bytes, not an
    // improvement. This is the value that node's ledger entry held.
    assert_eq!(
        legacy_composite_fingerprint(&B90F, B90F_MACHINE_ID, 20, 130_594_021_376),
        "bb5826adaac9fe690a388a37f111080e5c986218d33728eccf919d0787404fa9"
    );
}

#[test]
fn the_two_prints_never_coincide() {
    // A control plane must not be able to mistake one for the other.
    assert_ne!(
        composite_fingerprint(&B90F, B90F_MACHINE_ID, 20),
        legacy_composite_fingerprint(&B90F, B90F_MACHINE_ID, 20, 130_594_021_376)
    );
    assert_eq!(legacy_composite_fingerprint(&[], "", 20, 1), "");
}

// ── And the branch above it ─────────────────────────────────────────────────

#[test]
fn the_whole_fingerprint_is_stable_whichever_branch_this_machine_takes() {
    // `fingerprint` reads a board serial when the hardware has one and falls
    // through to the composite when it does not. Which branch runs depends on
    // the machine, so what can be asserted everywhere is the property the
    // control plane actually depends on: ask twice, get the same answer.
    let interfaces = [interface("enp1s0"), interface("docker0")];
    let first = fingerprint(&interfaces, "machine-1", 20);
    for _ in 0..20 {
        assert_eq!(fingerprint(&interfaces, "machine-1", 20), first);
    }
    assert!(first.is_empty() || first.len() == 64);
}

#[test]
fn docker_interfaces_are_not_identifying() {
    // The filter `fingerprint` applies before hashing, asserted on its own so
    // it is covered on hardware whose serial short-circuits the rest.
    let interfaces = [
        interface("enp1s0"),
        interface("docker0"),
        interface("br-1a2b3c"),
        interface("ib0"),
    ];
    assert_eq!(identifying_names(&interfaces), vec!["enp1s0", "ib0"]);
}

// ── RoCE links, what ibdev2netdev prints ────────────────────────────────────

#[test]
fn a_port_is_up_when_sysfs_says_active() {
    // `/sys/class/infiniband/<hca>/ports/1/state` on a Spark with one cable:
    // `4: ACTIVE` on the cabled port, `1: DOWN` on the other.
    assert!(port_is_active("4: ACTIVE"));
    assert!(port_is_active("4: ACTIVE\n"));
    assert!(!port_is_active("1: DOWN"));
    assert!(!port_is_active("2: INIT"));
    assert!(!port_is_active(""));
}

#[test]
fn reading_the_links_never_fails() {
    // A developer's machine has no /sys/class/infiniband; a Spark has four
    // devices. Either way this is a list, never a panic.
    let links = roce_links();
    for link in &links {
        assert!(!link.hca.is_empty());
    }
}
