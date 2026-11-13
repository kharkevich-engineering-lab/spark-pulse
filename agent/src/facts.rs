//! What this machine says about itself.
//!
//! Two of these fields are interop surface rather than telemetry, and the
//! reasons are worth keeping next to the code:
//!
//! * `machine_id` is **diagnostic only**. DGX Sparks ship duplicates, so it
//!   may never be identity; it is collected so the control plane can warn that
//!   two nodes claim one.
//! * `stable_fingerprint` is compared against what enrolment recorded on
//!   every connection, so a reimage is *detected* rather than inferred. It
//!   must therefore hash only what a reboot cannot move. Memory is not one of
//!   those: `sysconf(_SC_PHYS_PAGES)` is what the kernel has left after its own
//!   reservations, and on a DGX Spark it moved by one 4 KiB page across a
//!   reboot — inside a hash, indistinguishable from different hardware, so a
//!   peer was denied as reimaged. `hardware_fingerprint`, which did hash it, is
//!   still sent unchanged for control planes that know no other field.
//!
//! Nothing here may fail. An agent that cannot describe its GPUs must still
//! connect and say so; a probe that needs `nvidia-smi` must never be able to
//! stop a node reporting in.

use std::collections::BTreeSet;
use std::fs;
use std::path::Path;
use std::sync::OnceLock;

use sha2::{Digest, Sha256};

use crate::proto::{NetworkInterface, NodeFacts, RoceLink};

/// The agent's version, which is *spark-pulse's* version, stamped in at build
/// time from `pyproject.toml`.
///
/// One product, one version. The doctor compares what a node reports against
/// what the control plane is, and reports a difference as needing a reinstall
/// — so an agent versioned independently would make that warning fire on every
/// node for ever and mean nothing.
pub const AGENT_VERSION: &str = env!("SPARK_PULSE_VERSION");

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

/// `uname(2)`, which is what Python's `platform.uname()` and
/// `socket.gethostname()` read underneath.
///
/// Read through `uname` rather than out of `/proc`, even though a node is
/// always Linux: `/proc/sys/kernel/hostname` does not exist on a developer's
/// machine, so a `/proc`-only reader reports an empty hostname there and an
/// empty hostname *feeds the fingerprint's neighbours in the facts an operator
/// reads*. A fact that is right on the node and wrong everywhere else is a
/// fact nobody can test.
fn uname() -> Option<libc::utsname> {
    // SAFETY: uname fills a struct we own; it cannot fail for a valid pointer
    // other than by returning non-zero, which is checked.
    let mut info: libc::utsname = unsafe { std::mem::zeroed() };
    (unsafe { libc::uname(&mut info) } == 0).then_some(info)
}

fn from_c(field: &[libc::c_char]) -> String {
    let bytes: Vec<u8> = field
        .iter()
        .take_while(|c| **c != 0)
        .map(|c| *c as u8)
        .collect();
    String::from_utf8_lossy(&bytes).to_string()
}

fn hostname() -> String {
    match uname() {
        Some(info) => from_c(&info.nodename),
        None => std::env::var("HOSTNAME").unwrap_or_default(),
    }
}

/// The OCI platform name for a `uname` machine: `x86_64` is `linux/amd64`
/// and `aarch64` is `linux/arm64`, the names an image index uses. Anything
/// else is returned as `linux/<machine>`, so a new architecture is reported as
/// itself rather than as nothing. Empty in, empty out. Pure.
pub fn oci_platform(machine: &str) -> String {
    let arch = match machine {
        "" => return String::new(),
        "x86_64" | "amd64" => "amd64",
        "aarch64" | "arm64" => "arm64",
        other => other,
    };
    format!("linux/{arch}")
}

fn platform() -> String {
    match uname() {
        Some(info) => oci_platform(&from_c(&info.machine)),
        None => String::new(),
    }
}

/// `"<system> <release>"`, matching Python's `platform.uname()` pair.
fn kernel() -> String {
    match uname() {
        Some(info) => format!("{} {}", from_c(&info.sysname), from_c(&info.release)),
        None => String::new(),
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

/// Total physical memory, via `sysconf`. Reported; only the legacy print hashes it.
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
    // `veth*` is the host end of a container's cable. It is docker, and it
    // has to be: the fingerprint excludes docker interfaces precisely so a
    // container starting or stopping does not change it, and a veth counted
    // as hardware made exactly that happen.
    // Podman's bridges are the same thing under its own names: `podman*`
    // (netavark) and `cni-podman*` (CNI), each made when a network first gets
    // a container. The class stays "docker": it means a container runtime's.
    const CONTAINER_PREFIXES: &[&str] = &["docker", "br-", "veth", "podman", "cni-podman"];
    if CONTAINER_PREFIXES
        .iter()
        .any(|prefix| name.starts_with(prefix))
    {
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
///
/// Names come from `getifaddrs`, which every unix has, and the details that
/// only Linux publishes — MTU and link state, under `/sys/class/net` — are
/// read on top when they are there. Enumerating from `/sys` alone would mean
/// an agent that reports no interfaces at all anywhere but Linux, and
/// therefore an empty hardware fingerprint on a control plane that is a
/// developer's machine. A fact that is right on a node and absent everywhere
/// it can be tested is a fact nobody can test.
fn interfaces() -> (Vec<NetworkInterface>, Vec<String>) {
    let mut found: Vec<NetworkInterface> = Vec::new();
    for name in interface_names() {
        let base = Path::new("/sys/class/net").join(&name);
        let mtu = read_trimmed(base.join("mtu"))
            .and_then(|v| v.parse::<u32>().ok())
            .unwrap_or(0);
        // `operstate` is "up", "down", "unknown"… Loopback reports "unknown"
        // while being perfectly up, so carrier is consulted too.
        let operstate = read_trimmed(base.join("operstate")).unwrap_or_default();
        let is_up = operstate == "up"
            || (operstate == "unknown"
                && read_trimmed(base.join("carrier")).as_deref() == Some("1"));
        let (ip, prefix_length) = ipv4_for(&name).unwrap_or_default();
        found.push(NetworkInterface {
            ip,
            prefix_length,
            mtu,
            is_up,
            r#type: classify_interface(&name).to_string(),
            name,
        });
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

/// Every interface name this machine has, once each.
fn interface_names() -> Vec<String> {
    use std::ffi::CStr;

    let mut names: BTreeSet<String> = BTreeSet::new();
    let mut head: *mut libc::ifaddrs = std::ptr::null_mut();
    // SAFETY: getifaddrs allocates a list freed below; each pointer is
    // null-checked before it is read and none is retained.
    if unsafe { libc::getifaddrs(&mut head) } == 0 {
        let mut cursor = head;
        while !cursor.is_null() {
            let entry = unsafe { &*cursor };
            cursor = entry.ifa_next;
            if !entry.ifa_name.is_null() {
                names.insert(
                    unsafe { CStr::from_ptr(entry.ifa_name) }
                        .to_string_lossy()
                        .into_owned(),
                );
            }
        }
        unsafe { libc::freeifaddrs(head) };
    }
    // `/sys` can list an interface `getifaddrs` does not — one that is down
    // and has no address at all — and those still describe the hardware.
    if let Ok(entries) = fs::read_dir("/sys/class/net") {
        for entry in entries.flatten() {
            names.insert(entry.file_name().to_string_lossy().into_owned());
        }
    }
    names.into_iter().collect()
}

/// The first non-loopback IPv4 address on an interface, with its prefix length.
///
/// Read from the kernel rather than by shelling out to `ip`: a fact-gathering
/// path that forks is a fact-gathering path that can hang.
fn ipv4_for(name: &str) -> Option<(String, u32)> {
    ipv4_via_getifaddrs(name)
}

fn ipv4_via_getifaddrs(want: &str) -> Option<(String, u32)> {
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
            let prefix = if entry.ifa_netmask.is_null() {
                0
            } else {
                // SAFETY: for an AF_INET address the netmask is a sockaddr_in
                // of the same family, and it was null-checked above.
                let mask = unsafe { &*(entry.ifa_netmask as *const libc::sockaddr_in) };
                u32::from_be(mask.sin_addr.s_addr).count_ones()
            };
            answer = Some((text, prefix));
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
/// things that describe the machine rather than its configuration.
///
/// **This consults the machine**, which is why the composite half is a
/// separate function: on hardware that publishes a serial — including every
/// cloud VM, which is where tests run — the serial wins and the arguments are
/// never read. A test of the composite that went through here would pass on a
/// laptop and assert nothing on a CI runner.
pub fn fingerprint(interfaces: &[NetworkInterface], machine_id: &str, cpu_count: u32) -> String {
    let serial = serial_fingerprint();
    if !serial.is_empty() {
        return serial;
    }
    composite_fingerprint(&identifying_names(interfaces), machine_id, cpu_count)
}

/// The fingerprint control planes before `stable_fingerprint` compare.
///
/// The same serial branch, and a composite that also hashes the memory size —
/// which is exactly what made it unstable. Kept only so a rollback to such a
/// control plane, which reads nothing else, finds what it recorded.
pub fn legacy_fingerprint(
    interfaces: &[NetworkInterface],
    machine_id: &str,
    cpu_count: u32,
    memory_bytes: u64,
) -> String {
    let serial = serial_fingerprint();
    if !serial.is_empty() {
        return serial;
    }
    legacy_composite_fingerprint(
        &identifying_names(interfaces),
        machine_id,
        cpu_count,
        memory_bytes,
    )
}

fn serial_fingerprint() -> String {
    let serial = board_serial();
    if serial.is_empty() {
        return serial;
    }
    hex(Sha256::digest(format!("serial:{serial}").as_bytes()))
}

/// The interface names that describe the *hardware*, sorted.
///
/// Docker's interfaces are excluded because they appear and vanish with
/// workloads: a fingerprint that changed when a container started would be
/// useless for detecting a reimage.
///
/// **Which names those are is frozen here, not read from
/// [`classify_interface`].** The ledger compares this print exactly, so a
/// rule that changed under it would deny every enrolled node it newly
/// excludes as reimaged on upgrade. Podman's bridges (#169) are container
/// interfaces to the classifier and still count here; dropping them from the
/// print waits for a versioned print the ledger can migrate (item 3 of the
/// v2 rollout), the way `stable_fingerprint` replaced the memory-hashing one.
pub fn identifying_names(interfaces: &[NetworkInterface]) -> Vec<&str> {
    let mut names: Vec<&str> = interfaces
        .iter()
        .filter(|i| !i.name.is_empty() && !excluded_from_print(&i.name))
        .map(|i| i.name.as_str())
        .collect();
    names.sort_unstable();
    names
}

/// The container interfaces the stable print has always excluded. Frozen.
fn excluded_from_print(name: &str) -> bool {
    ["docker", "br-", "veth"]
        .iter()
        .any(|prefix| name.starts_with(prefix))
}

/// The fingerprint of a machine with no board serial. Pure.
///
/// Deliberately not built from the hostname or an IP address. Both change
/// under DHCP and under a rename, and a fingerprint that moved when a node was
/// renamed would deny a node for being renamed — the failure §3.1 says every
/// name-keyed system in the survey documented. Nor from the memory size, which
/// the kernel reports net of reservations that differ from one boot to the
/// next.
pub fn composite_fingerprint(names: &[&str], machine_id: &str, cpu_count: u32) -> String {
    // Nothing that *identifies* this machine: no interface names and no
    // machine-id. The CPU count is not identifying on its own — every Spark
    // has the same one — so a fingerprint built from it alone would be
    // identical on every node, and the control plane would read two different
    // machines as one. Better to say we do not know.
    if names.is_empty() && machine_id.is_empty() {
        return String::new();
    }
    let mut parts: Vec<String> = names.iter().map(|n| n.to_string()).collect();
    parts.push(cpu_count.to_string());
    parts.push(machine_id.to_string());
    hex(Sha256::digest(parts.join("|").as_bytes()))
}

/// The first composite, memory size included. Pure; see [`legacy_fingerprint`].
pub fn legacy_composite_fingerprint(
    names: &[&str],
    machine_id: &str,
    cpu_count: u32,
    memory_bytes: u64,
) -> String {
    if names.is_empty() && machine_id.is_empty() {
        return String::new();
    }
    let mut parts: Vec<String> = names.iter().map(|n| n.to_string()).collect();
    parts.push(cpu_count.to_string());
    parts.push(memory_bytes.to_string());
    parts.push(machine_id.to_string());
    hex(Sha256::digest(parts.join("|").as_bytes()))
}

fn hex(bytes: impl AsRef<[u8]>) -> String {
    bytes.as_ref().iter().map(|b| format!("{b:02x}")).collect()
}

/// What `ibdev2netdev` prints, read from sysfs rather than by running it.
///
/// Every RoCE device under `/sys/class/infiniband` names the netdev it
/// drives under `device/net/`, and its first port's `state` reads
/// `4: ACTIVE` when the cable is up. That is the whole of what the fabric
/// logic on the control plane needs, and a fact read from sysfs cannot hang
/// a heartbeat the way a forked `ibdev2netdev` could.
pub fn roce_links() -> Vec<RoceLink> {
    let mut links: Vec<RoceLink> = Vec::new();
    let Ok(devices) = fs::read_dir("/sys/class/infiniband") else {
        return links;
    };
    let mut names: Vec<String> = devices
        .flatten()
        .map(|e| e.file_name().to_string_lossy().into_owned())
        .collect();
    names.sort();
    for hca in names {
        let base = Path::new("/sys/class/infiniband").join(&hca);
        let netdev = fs::read_dir(base.join("device/net"))
            .ok()
            .and_then(|mut entries| entries.next())
            .and_then(|entry| entry.ok())
            .map(|entry| entry.file_name().to_string_lossy().into_owned())
            .unwrap_or_default();
        let state = read_trimmed(base.join("ports/1/state")).unwrap_or_default();
        links.push(RoceLink {
            hca,
            netdev,
            is_up: port_is_active(&state),
        });
    }
    links
}

/// `4: ACTIVE` is up; everything else (`1: DOWN`, `2: INIT`, empty) is not.
pub fn port_is_active(state: &str) -> bool {
    state.to_ascii_uppercase().contains("ACTIVE")
}

/// SHA-256 of the running binary, read once.
///
/// The control plane packages one agent per target and knows each one's
/// digest; comparing bytes is how it says "this node runs what I ship"
/// without trusting a version string somebody baked in. Empty when the
/// executable cannot be read, which is a fact too.
pub fn binary_sha256() -> String {
    static DIGEST: OnceLock<String> = OnceLock::new();
    DIGEST
        .get_or_init(|| {
            std::env::current_exe()
                .ok()
                .and_then(|path| fs::read(path).ok())
                .map(|bytes| hex(Sha256::digest(&bytes)))
                .unwrap_or_default()
        })
        .clone()
}

/// What the container runtime on the agent's socket says about itself.
///
/// Asked of the daemon by the executor, which owns the connection; empty
/// (the default) when nothing answers, which is also what enrolment sends —
/// it runs before the agent has connected to anything.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ContainerRuntime {
    /// `docker`, `podman`, or empty when no daemon answered.
    pub name: String,
    pub version: String,
    pub rootless: bool,
}

/// Which runtime a `/version` answer came from, by its components' names.
///
/// Podman's Docker-compatible API answers `/version` with Docker's shape, and
/// the one place it says what it is is a component named `Podman Engine`.
/// Docker names its own `Engine`. A daemon that answered with no components
/// at all is still a daemon, and Docker is the one that predates the field.
/// Pure.
pub fn runtime_from_components(answered: bool, components: &[&str]) -> &'static str {
    if !answered {
        return "";
    }
    if components
        .iter()
        .any(|name| name.to_ascii_lowercase().contains("podman"))
    {
        return "podman";
    }
    "docker"
}

/// Whether `/info`'s SecurityOptions say the daemon runs rootless. Pure.
///
/// Rootless Docker and rootless Podman both say `name=rootless`.
pub fn rootless_from_security_options(options: &[&str]) -> bool {
    options
        .iter()
        .any(|option| option.split(',').any(|part| part.trim() == "name=rootless"))
}

/// Whether a Podman socket is a rootless one, by where it is. Pure.
///
/// Rootless Podman listens under the user's runtime directory
/// (`$XDG_RUNTIME_DIR`, which systemd makes `/run/user/<uid>`); rootful
/// Podman under `/run/podman`. Read from the path because the daemon's own
/// answer is `/info`, which Podman takes seconds to give (#211).
pub fn podman_rootless_from_socket(socket: &std::path::Path, runtime_dir: Option<&str>) -> bool {
    if socket.starts_with("/run/user") {
        return true;
    }
    runtime_dir
        .filter(|dir| !dir.is_empty())
        .is_some_and(|dir| socket.starts_with(dir))
}

/// Where CDI specs live: the static and the generated directory, as the CDI
/// spec defines them. `/var/run` is normally a link to `/run`, so a device
/// may be found twice; [`cdi_devices`] keeps each name once.
const CDI_SPEC_DIRS: &[&str] = &["/etc/cdi", "/var/run/cdi", "/run/cdi"];

/// Every CDI device this machine's spec directories name, sorted, once each.
///
/// Read on every heartbeat, so it reads files and never runs `nvidia-ctk`. A
/// directory or spec that cannot be read contributes nothing.
pub fn cdi_devices() -> Vec<String> {
    let mut found: BTreeSet<String> = BTreeSet::new();
    for dir in CDI_SPEC_DIRS {
        let Ok(entries) = fs::read_dir(dir) else {
            continue;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            let extension = path
                .extension()
                .and_then(|e| e.to_str())
                .unwrap_or_default()
                .to_ascii_lowercase();
            let parsed = match extension.as_str() {
                "json" => fs::read_to_string(&path)
                    .map(|text| cdi_devices_from_json(&text))
                    .unwrap_or_default(),
                "yaml" | "yml" => fs::read_to_string(&path)
                    .map(|text| cdi_devices_from_yaml(&text))
                    .unwrap_or_default(),
                _ => Vec::new(),
            };
            found.extend(parsed);
        }
    }
    found.into_iter().collect()
}

/// The `kind=name` of every device in a CDI spec written as JSON. Pure.
pub fn cdi_devices_from_json(text: &str) -> Vec<String> {
    let Ok(spec) = serde_json::from_str::<serde_json::Value>(text) else {
        return Vec::new();
    };
    let Some(kind) = spec.get("kind").and_then(|k| k.as_str()) else {
        return Vec::new();
    };
    spec.get("devices")
        .and_then(|d| d.as_array())
        .map(|devices| {
            devices
                .iter()
                .filter_map(|d| d.get("name").and_then(|n| n.as_str()))
                .filter(|name| !name.is_empty())
                .map(|name| format!("{kind}={name}"))
                .collect()
        })
        .unwrap_or_default()
}

/// The `kind=name` of every device in a CDI spec written as YAML. Pure.
///
/// Not a YAML parser, and it does not need to be one: a spec has a top-level
/// `kind:` and a top-level `devices:` list whose items each start with
/// `- name:`, which is the shape `nvidia-ctk cdi generate` writes. Only the
/// items at the list's own indentation count, so a `name:` nested deeper
/// (a mount, a hook) is never read as a device. Pulling in a YAML crate for
/// two keys would add more to a static binary than this whole module.
pub fn cdi_devices_from_yaml(text: &str) -> Vec<String> {
    let mut kind = String::new();
    let mut names: Vec<String> = Vec::new();
    let mut in_devices = false;
    let mut item_indent: Option<usize> = None;
    for line in text.lines() {
        let trimmed = line.trim_start();
        if trimmed.is_empty() || trimmed.starts_with('#') {
            continue;
        }
        let indent = line.len() - trimmed.len();
        if indent == 0 {
            in_devices = trimmed.starts_with("devices:");
            if let Some(value) = trimmed.strip_prefix("kind:") {
                kind = unquote(value);
            }
            continue;
        }
        if !in_devices {
            continue;
        }
        let Some(item) = trimmed.strip_prefix("- ") else {
            continue;
        };
        let item_indent = *item_indent.get_or_insert(indent);
        if indent != item_indent {
            continue;
        }
        if let Some(value) = item.trim_start().strip_prefix("name:") {
            let name = unquote(value);
            if !name.is_empty() {
                names.push(name);
            }
        }
    }
    if kind.is_empty() {
        return Vec::new();
    }
    names
        .into_iter()
        .map(|name| format!("{kind}={name}"))
        .collect()
}

fn unquote(value: &str) -> String {
    value
        .trim()
        .trim_matches(|c| c == '"' || c == '\'')
        .to_string()
}

/// Describe this machine. Never fails.
pub fn collect(docker_version: String, runtime: ContainerRuntime) -> NodeFacts {
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
        hardware_fingerprint: legacy_fingerprint(&interfaces, &machine_id, cpus, memory),
        stable_fingerprint: fingerprint(&interfaces, &machine_id, cpus),
        interfaces,
        infiniband_interfaces,
        roce_links: roce_links(),
        binary_sha256: binary_sha256(),
        container_runtime: runtime.name,
        runtime_version: runtime.version,
        rootless: runtime.rootless,
        cdi_devices: cdi_devices(),
        platform: platform(),
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
