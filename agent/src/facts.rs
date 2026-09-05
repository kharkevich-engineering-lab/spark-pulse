//! What this machine says about itself.
//!
//! Two of these fields are interop surface rather than telemetry, and the
//! reasons are worth keeping next to the code:
//!
//! * `machine_id` is **diagnostic only**. DGX Sparks ship duplicates, so it
//!   may never be identity; it is collected so the control plane can warn that
//!   two nodes claim one.
//! * `hardware_fingerprint` is compared against what enrolment recorded on
//!   every heartbeat, so a reimage is *detected* rather than inferred. It must
//!   therefore be the same function `spark_pulse/agent/facts.py` computes, or
//!   a node whose agent was replaced would report a reimage on every beat.
//!   `memory_bytes` is read through `sysconf` for the same reason: `/proc/meminfo`
//!   reports slightly less than the physical total, and "slightly different"
//!   is indistinguishable from "different hardware" once it is inside a hash.
//!
//! Nothing here may fail. An agent that cannot describe its GPUs must still
//! connect and say so; a probe that needs `nvidia-smi` must never be able to
//! stop a node reporting in.

use std::collections::BTreeSet;
use std::fs;
use std::path::Path;

use sha2::{Digest, Sha256};

use crate::proto::{NetworkInterface, NodeFacts};

/// The agent's own version, stamped in at build time.
pub const AGENT_VERSION: &str = env!("CARGO_PKG_VERSION");

const BOOT_ID_PATH: &str = "/proc/sys/kernel/random/boot_id";

/// Places a board serial shows up, most specific first.
///
/// The device-tree entry is where an ARM64 DGX Spark carries it; the DMI ones
/// cover x86 development boxes. `product_uuid` is root-only, so an agent
/// running unprivileged simply falls through to the composite fingerprint.
const SERIAL_PATHS: &[&str] = &[
    "/proc/device-tree/serial-number",
    "/sys/class/dmi/id/product_uuid",
    "/sys/class/dmi/id/board_serial",
    "/sys/class/dmi/id/product_serial",
];

const SERIAL_PLACEHOLDERS: &[&str] = &["none", "unknown", "to be filled by o.e.m."];

fn read_trimmed(path: impl AsRef<Path>) -> Option<String> {
    let raw = fs::read(path).ok()?;
    let text = String::from_utf8_lossy(&raw);
    let value = text.trim().trim_matches('\0').trim().to_string();
    (!value.is_empty()).then_some(value)
}

/// `/etc/machine-id`, or empty. Diagnostic only — see the module docs.
pub fn read_machine_id() -> String {
    for candidate in ["/etc/machine-id", "/var/lib/dbus/machine-id"] {
        if let Some(value) = read_trimmed(candidate) {
            return value;
        }
    }
    String::new()
}

/// The kernel's boot id, or empty. Changes on every reboot.
pub fn read_boot_id() -> String {
    read_trimmed(BOOT_ID_PATH).unwrap_or_default()
}

fn hostname() -> String {
    read_trimmed("/proc/sys/kernel/hostname")
        .or_else(|| std::env::var("HOSTNAME").ok().filter(|v| !v.is_empty()))
        .unwrap_or_default()
}

/// `"<system> <release>"`, matching Python's `platform.uname()` pair.
fn kernel() -> String {
    let system = read_trimmed("/proc/sys/kernel/ostype").unwrap_or_else(|| "Linux".into());
    match read_trimmed("/proc/sys/kernel/osrelease") {
        Some(release) => format!("{system} {release}"),
        None => system,
    }
}

fn os_release() -> String {
    if let Ok(text) = fs::read_to_string("/etc/os-release") {
        for line in text.lines() {
            if let Some(value) = line.strip_prefix("PRETTY_NAME=") {
                return value.trim().trim_matches('"').to_string();
            }
        }
    }
    kernel()
}

/// Total physical memory, via `sysconf`, because that is what Python reads.
fn memory_bytes() -> u64 {
    // SAFETY: sysconf takes an int and returns a long; both names are POSIX
    // and neither can trap. A negative return means "unlimited or unknown".
    let page = unsafe { libc::sysconf(libc::_SC_PAGE_SIZE) };
    let pages = unsafe { libc::sysconf(libc::_SC_PHYS_PAGES) };
    if page <= 0 || pages <= 0 {
        return 0;
    }
    (page as u64).saturating_mul(pages as u64)
}

fn cpu_count() -> u32 {
    std::thread::available_parallelism()
        .map(|n| n.get() as u32)
        .unwrap_or(0)
}

/// Classify an interface by name. Same rules, same order, as
/// `spark_pulse.tools.discovery._classify_interface` — a second classification
/// that disagreed would change the fingerprint, which is why
/// `tests/facts.rs` walks the same table.
pub fn classify_interface(name: &str) -> &'static str {
    if name == "lo" {
        return "loopback";
    }
    if name.starts_with("docker") || name.starts_with("br-") {
        return "docker";
    }
    if name.starts_with("ib") || name.starts_with("mlx5") {
        return "infiniband";
    }
    if name.starts_with("eth") || name.starts_with("en") {
        return "ethernet";
    }
    "other"
}

/// The interfaces the kernel enumerates, and the RoCE device names beside them.
fn interfaces() -> (Vec<NetworkInterface>, Vec<String>) {
    let mut found = Vec::new();
    if let Ok(entries) = fs::read_dir("/sys/class/net") {
        for entry in entries.flatten() {
            let name = entry.file_name().to_string_lossy().to_string();
            let base = entry.path();
            let mtu = read_trimmed(base.join("mtu"))
                .and_then(|v| v.parse::<u32>().ok())
                .unwrap_or(0);
            // `operstate` is "up", "down", "unknown"… Loopback reports
            // "unknown" while being perfectly up, so carrier is consulted too.
            let operstate = read_trimmed(base.join("operstate")).unwrap_or_default();
            let is_up = operstate == "up"
                || (operstate == "unknown"
                    && read_trimmed(base.join("carrier")).as_deref() == Some("1"));
            found.push(NetworkInterface {
                name: name.clone(),
                ip: ipv4_for(&name).unwrap_or_default(),
                mtu,
                is_up,
                r#type: classify_interface(&name).to_string(),
            });
        }
    }
    found.sort_by(|a, b| a.name.cmp(&b.name));

    let mut hcas: BTreeSet<String> = BTreeSet::new();
    if let Ok(entries) = fs::read_dir("/sys/class/infiniband") {
        for entry in entries.flatten() {
            hcas.insert(entry.file_name().to_string_lossy().to_string());
        }
    }
    (found, hcas.into_iter().collect())
}

/// The first non-loopback IPv4 address on an interface.
///
/// Read from the kernel rather than by shelling out to `ip`: a fact-gathering
/// path that forks is a fact-gathering path that can hang.
fn ipv4_for(name: &str) -> Option<String> {
    let text = fs::read_to_string("/proc/net/route").ok()?;
    // /proc/net/route only carries routed interfaces, so fall back to the
    // per-interface address via a netlink-free route: getifaddrs.
    let _ = text;
    ipv4_via_getifaddrs(name)
}

fn ipv4_via_getifaddrs(want: &str) -> Option<String> {
    use std::ffi::CStr;
    let mut head: *mut libc::ifaddrs = std::ptr::null_mut();
    // SAFETY: getifaddrs allocates a list we free below; we only read fields
    // after null-checking each pointer, and never retain any of them.
    if unsafe { libc::getifaddrs(&mut head) } != 0 {
        return None;
    }
    let mut answer = None;
    let mut cursor = head;
    while !cursor.is_null() {
        let entry = unsafe { &*cursor };
        cursor = entry.ifa_next;
        if entry.ifa_name.is_null() || entry.ifa_addr.is_null() {
            continue;
        }
        let name = unsafe { CStr::from_ptr(entry.ifa_name) }.to_string_lossy();
        if name != want {
            continue;
        }
        let addr = unsafe { &*entry.ifa_addr };
        if addr.sa_family as i32 != libc::AF_INET {
            continue;
        }
        let sin = unsafe { &*(entry.ifa_addr as *const libc::sockaddr_in) };
        let octets = u32::from_be(sin.sin_addr.s_addr).to_be_bytes();
        let text = format!("{}.{}.{}.{}", octets[0], octets[1], octets[2], octets[3]);
        if text != "127.0.0.1" {
            answer = Some(text);
            break;
        }
    }
    // SAFETY: `head` came from getifaddrs and is freed exactly once.
    unsafe { libc::freeifaddrs(head) };
    answer
}

fn board_serial() -> String {
    for candidate in SERIAL_PATHS {
        if let Some(value) = read_trimmed(candidate) {
            if !SERIAL_PLACEHOLDERS.contains(&value.to_lowercase().as_str()) {
                return value;
            }
        }
    }
    String::new()
}

/// A stable-across-reboots, unstable-across-reimage hardware fingerprint.
///
/// A board serial when the hardware exposes one, otherwise a composite of the
/// things that describe the machine rather than its configuration: the
/// interface *names* the kernel enumerates, the CPU count, the memory size and
/// the machine-id.
///
/// Deliberately not built from the hostname or an IP address. Both change
/// under DHCP and under a rename, and a fingerprint that moved when a node was
/// renamed would deny a node for being renamed.
pub fn fingerprint(
    interfaces: &[NetworkInterface],
    machine_id: &str,
    cpu_count: u32,
    memory_bytes: u64,
) -> String {
    let serial = board_serial();
    if !serial.is_empty() {
        return hex(Sha256::digest(format!("serial:{serial}").as_bytes()));
    }
    let mut names: Vec<&str> = interfaces
        .iter()
        .filter(|i| !i.name.is_empty() && i.r#type != "docker")
        .map(|i| i.name.as_str())
        .collect();
    names.sort_unstable();
    let mut parts: Vec<String> = names.into_iter().map(str::to_string).collect();
    parts.push(cpu_count.to_string());
    parts.push(memory_bytes.to_string());
    parts.push(machine_id.to_string());
    let material = parts.join("|");
    if material.trim_matches('|').is_empty() {
        return String::new();
    }
    hex(Sha256::digest(material.as_bytes()))
}

fn hex(bytes: impl AsRef<[u8]>) -> String {
    bytes.as_ref().iter().map(|b| format!("{b:02x}")).collect()
}

/// Describe this machine. Never fails.
pub fn collect(docker_version: String) -> NodeFacts {
    let machine_id = read_machine_id();
    let (interfaces, infiniband_interfaces) = interfaces();
    let cpus = cpu_count();
    let memory = memory_bytes();
    NodeFacts {
        hostname: hostname(),
        boot_id: read_boot_id(),
        machine_id: machine_id.clone(),
        os_release: os_release(),
        kernel: kernel(),
        agent_version: AGENT_VERSION.to_string(),
        docker_version,
        cpu_count: cpus,
        memory_bytes: memory,
        gpu_count: gpu_count(),
        hardware_fingerprint: fingerprint(&interfaces, &machine_id, cpus, memory),
        interfaces,
        infiniband_interfaces,
    }
}

/// How many GPUs the driver reports.
///
/// Read from the driver's own proc entry rather than by running `nvidia-smi`:
/// this is called on every heartbeat, and a fact that forks a process on every
/// beat is a fact that can hang the beat.
fn gpu_count() -> u32 {
    let Ok(entries) = fs::read_dir("/proc/driver/nvidia/gpus") else {
        return 0;
    };
    entries.flatten().count() as u32
}
