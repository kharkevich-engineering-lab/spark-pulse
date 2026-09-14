//! Configuring this node's ConnectX fabric, through NetworkManager.
//!
//! DGX OS manages the ConnectX ports with NetworkManager (the `90-NM-*.yaml`
//! files under `/etc/netplan` are its backend representation), so this drives
//! `nmcli` rather than writing a netplan file. A raw netplan file makes
//! `netplan generate` parse those NM files, and one malformed sibling then
//! blocks an otherwise-valid apply — which is exactly what a field node hit.
//!
//! The control plane computes the addresses (`tools/fabric_plan.py`) and hands
//! each port its final `cidr`, `mtu` and connection name. This is the part
//! that runs on the node: it needs root, and it has exactly one way to get it
//! — `sudo -n nmcli`, the single command the bootstrap sudoers rule grants
//! the agent user. Nothing here runs an arbitrary command; every privileged
//! call is an `nmcli` invocation built here from typed fields.
//!
//! **Verification is not "the command returned".** After the connection is up,
//! the address and MTU are read back from the port and every peer the plan put
//! on the same subnet is pinged over it. A cable that does not go where the
//! plan assumed is a failed ping with the link named, not a success.

use std::process::Command;

use super::OpError;
use crate::proto::{ConfigureFabric, FabricPing, FabricPortState, FabricResult};

/// The one privileged program the agent runs, matching the sudoers grant.
const NMCLI: &str = "nmcli";

/// Run `nmcli` as root via `sudo -n`. The `-n` never prompts: if the sudoers
/// rule is missing the call fails fast rather than hanging on a password.
fn sudo_nmcli(args: &[&str]) -> Result<String, OpError> {
    let output = Command::new("sudo")
        .arg("-n")
        .arg(NMCLI)
        .args(args)
        .output()
        .map_err(|error| {
            OpError::new(
                "NativeRuntimeError",
                format!("could not run sudo nmcli ({error})"),
            )
        })?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        let stdout = String::from_utf8_lossy(&output.stdout);
        let text = if stderr.trim().is_empty() {
            stdout.trim()
        } else {
            stderr.trim()
        };
        return Err(OpError::new(
            "NativeRuntimeError",
            format!("nmcli {}: {}", args.first().copied().unwrap_or(""), text),
        ));
    }
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

/// Read-only `nmcli`, no privilege — listing connections needs none.
fn nmcli(args: &[&str]) -> Result<String, String> {
    let output = Command::new(NMCLI)
        .args(args)
        .output()
        .map_err(|error| format!("could not run nmcli ({error})"))?;
    if !output.status.success() {
        return Err(format!(
            "nmcli {}: {}",
            args.first().copied().unwrap_or(""),
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

/// `name -> device` for every NetworkManager connection profile.
fn connections() -> Vec<(String, String)> {
    let listing = match nmcli(&["-t", "-f", "NAME,DEVICE", "connection", "show"]) {
        Ok(text) => text,
        Err(_) => return Vec::new(),
    };
    parse_connections(&listing)
}

/// Split `nmcli -t -f NAME,DEVICE connection show` output into `(name, device)`.
///
/// The name may itself contain a colon (`-t` does not quote it), so the device
/// is taken from the *last* colon, not the first.
fn parse_connections(listing: &str) -> Vec<(String, String)> {
    listing
        .lines()
        .filter(|line| !line.trim().is_empty())
        .filter_map(|line| {
            let idx = line.rfind(':')?;
            Some((line[..idx].to_string(), line[idx + 1..].to_string()))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::parse_connections;

    #[test]
    fn a_connection_name_may_itself_contain_a_colon() {
        // `nmcli -t` does not quote the name, so the device is the last field.
        let listing = "Wired connection 1:enp1s0f1np1\nspark:pulse:enP2p1s0f1np1\n\n";
        assert_eq!(
            parse_connections(listing),
            vec![
                ("Wired connection 1".to_string(), "enp1s0f1np1".to_string()),
                ("spark:pulse".to_string(), "enP2p1s0f1np1".to_string()),
            ]
        );
    }

    #[test]
    fn a_profile_with_no_device_is_skipped_cleanly() {
        // A profile not bound to a device shows an empty last field, never a panic.
        assert_eq!(
            parse_connections("lo:lo\nunbound:\n"),
            vec![
                ("lo".to_string(), "lo".to_string()),
                ("unbound".to_string(), "".to_string()),
            ]
        );
    }
}

/// The IPv4 address currently on a device, `<ip>/<prefix>`, or empty.
fn address_of(netdev: &str) -> String {
    let output = Command::new("ip")
        .args(["-o", "-f", "inet", "addr", "show", "dev", netdev])
        .output();
    let text = match output {
        Ok(o) if o.status.success() => String::from_utf8_lossy(&o.stdout).into_owned(),
        _ => return String::new(),
    };
    // `2: enp1s0f1np1    inet 192.168.177.11/24 brd ... scope global ...`
    let mut fields = text.split_whitespace();
    while let Some(token) = fields.next() {
        if token == "inet" {
            return fields.next().unwrap_or("").to_string();
        }
    }
    String::new()
}

/// The MTU on a device, or 0 when it cannot be read.
fn mtu_of(netdev: &str) -> u32 {
    std::fs::read_to_string(format!("/sys/class/net/{netdev}/mtu"))
        .ok()
        .and_then(|text| text.trim().parse().ok())
        .unwrap_or(0)
}

/// Ping `address` from `netdev`; two packets, a short deadline.
fn ping(netdev: &str, address: &str) -> bool {
    Command::new("ping")
        .args(["-c", "2", "-W", "2", "-I", netdev, address])
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

/// Apply the fabric config: one NetworkManager connection per port, the
/// competing profiles on those ports quieted, then read back and ping.
pub fn configure(request: &ConfigureFabric) -> Result<FabricResult, OpError> {
    let mut steps: Vec<String> = Vec::new();
    let existing = connections();
    let ours: std::collections::BTreeSet<&str> = request
        .interfaces
        .iter()
        .map(|i| i.netdev.as_str())
        .collect();

    // Quiet any other profile that would autoconnect on a port we are about to
    // own, so a leftover DHCP profile does not fight the static one.
    for (name, device) in &existing {
        if !ours.contains(device.as_str()) {
            continue;
        }
        if request
            .interfaces
            .iter()
            .any(|i| &i.connection_name == name)
        {
            continue; // our own profile, handled below
        }
        if sudo_nmcli(&["connection", "modify", name, "connection.autoconnect", "no"]).is_ok() {
            let _ = sudo_nmcli(&["connection", "down", name]);
            steps.push(format!("quieted the profile {name:?} on {device}"));
        }
    }

    for interface in &request.interfaces {
        let name = interface.connection_name.as_str();
        let mtu = interface.mtu.to_string();
        let known = existing.iter().any(|(n, _)| n == name);
        if known {
            sudo_nmcli(&[
                "connection",
                "modify",
                name,
                "ipv4.method",
                "manual",
                "ipv4.addresses",
                &interface.cidr,
                "ipv6.method",
                "disabled",
                "802-3-ethernet.mtu",
                &mtu,
                "connection.autoconnect",
                "yes",
            ])?;
            steps.push(format!("modified connection {name:?}"));
        } else {
            sudo_nmcli(&[
                "connection",
                "add",
                "type",
                "ethernet",
                "con-name",
                name,
                "ifname",
                &interface.netdev,
                "ipv4.method",
                "manual",
                "ipv4.addresses",
                &interface.cidr,
                "ipv6.method",
                "disabled",
                "802-3-ethernet.mtu",
                &mtu,
                "connection.autoconnect",
                "yes",
            ])?;
            steps.push(format!("added connection {name:?} on {}", interface.netdev));
        }
        sudo_nmcli(&["connection", "up", name])?;
        steps.push(format!("brought {name:?} up"));
    }

    let mut ports: Vec<FabricPortState> = Vec::new();
    for interface in &request.interfaces {
        let cidr = address_of(&interface.netdev);
        let mtu = mtu_of(&interface.netdev);
        ports.push(FabricPortState {
            address_ok: cidr == interface.cidr,
            cidr,
            mtu,
            mtu_ok: mtu == interface.mtu,
            netdev: interface.netdev.clone(),
        });
    }

    let pings = request
        .peers
        .iter()
        .map(|peer| FabricPing {
            reachable: ping(&peer.netdev, &peer.address),
            netdev: peer.netdev.clone(),
            address: peer.address.clone(),
        })
        .collect();

    Ok(FabricResult {
        ports,
        pings,
        steps,
    })
}
