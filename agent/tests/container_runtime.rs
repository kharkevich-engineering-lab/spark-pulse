//! What a node says about its container runtime (#166).
//!
//! The agent decides how to create an engine container from these facts —
//! Podman's compat API accepts a GPU request and silently drops it (#165) —
//! so each reading is a pure function over what the daemon or the CDI spec
//! said, tested here against the shapes the real ones produce.

use spark_pulse_agent::facts::{
    cdi_devices, cdi_devices_from_json, cdi_devices_from_yaml, podman_rootless,
    rootless_from_security_options, rootless_owner, runtime_from_components,
};

#[test]
fn podman_names_itself_in_the_components() {
    // Podman 4.9.3's compat `/version`, as a rootless desktop answered it.
    let components = ["Podman Engine", "Conmon", "OCI Runtime (runc)"];
    assert_eq!(runtime_from_components(true, &components), "podman");
}

#[test]
fn docker_is_the_engine_that_is_not_podman() {
    let components = ["Engine", "containerd", "runc", "docker-init"];
    assert_eq!(runtime_from_components(true, &components), "docker");
    // A daemon old enough to send no components is still a daemon, and
    // Docker is the one that predates the field.
    assert_eq!(runtime_from_components(true, &[]), "docker");
}

#[test]
fn no_daemon_is_no_runtime() {
    assert_eq!(runtime_from_components(false, &["Podman Engine"]), "");
}

#[test]
fn rootless_is_read_from_the_security_options() {
    // Podman's compat `/info` on the same desktop.
    let podman = [
        "name=apparmor",
        "name=seccomp,profile=default",
        "name=rootless",
    ];
    assert!(rootless_from_security_options(&podman));
    // Rootful Docker on a Spark.
    let docker = [
        "name=apparmor",
        "name=seccomp,profile=builtin",
        "name=cgroupns",
    ];
    assert!(!rootless_from_security_options(&docker));
    // Docker has packed several into one option before; the name still counts.
    assert!(rootless_from_security_options(&[
        "name=seccomp,name=rootless"
    ]));
    assert!(!rootless_from_security_options(&[]));
}

/// The shape `nvidia-ctk cdi generate` writes, cut down: a nested list under
/// each device (`deviceNodes`, `hooks`) and a top-level `containerEdits`
/// whose items must never be read as devices.
const NVIDIA_YAML: &str = r#"---
cdiVersion: 0.6.0
kind: nvidia.com/gpu
devices:
    - name: "0"
      containerEdits:
        deviceNodes:
            - path: /dev/nvidia0
        hooks:
            - hookName: createContainer
              args:
                - nvidia-cdi-hook
    - name: GPU-c52a39b9-8357-e610-9835-2c7621ffb255
      containerEdits:
        deviceNodes:
            - path: /dev/nvidia0
    - name: all
      containerEdits:
        mounts:
            - name: not-a-device
              hostPath: /dev/nvidia0
containerEdits:
    env:
        - NVIDIA_VISIBLE_DEVICES=void
    hooks:
        - name: also-not-a-device
"#;

#[test]
fn every_device_in_a_generated_yaml_spec_is_named_by_kind() {
    assert_eq!(
        cdi_devices_from_yaml(NVIDIA_YAML),
        vec![
            "nvidia.com/gpu=0",
            "nvidia.com/gpu=GPU-c52a39b9-8357-e610-9835-2c7621ffb255",
            "nvidia.com/gpu=all",
        ]
    );
}

#[test]
fn a_yaml_spec_without_a_kind_names_nothing() {
    let spec = "cdiVersion: 0.6.0\ndevices:\n  - name: all\n";
    assert!(cdi_devices_from_yaml(spec).is_empty());
}

#[test]
fn a_json_spec_is_read_the_same_way() {
    let spec = r#"{"cdiVersion":"0.6.0","kind":"nvidia.com/gpu",
        "devices":[{"name":"0","containerEdits":{}},{"name":"all"}],
        "containerEdits":{"hooks":[{"name":"x"}]}}"#;
    assert_eq!(
        cdi_devices_from_json(spec),
        vec!["nvidia.com/gpu=0", "nvidia.com/gpu=all"]
    );
    assert!(cdi_devices_from_json("not json").is_empty());
    assert!(cdi_devices_from_json(r#"{"devices":[{"name":"all"}]}"#).is_empty());
}

#[test]
fn reading_the_spec_directories_never_fails() {
    // A Spark running Docker has no CDI spec; a Podman desktop has one. A
    // list either way, each entry `kind=name`.
    for device in cdi_devices() {
        assert!(device.contains('='), "{device}");
    }
}

#[test]
fn the_platform_is_named_the_way_an_image_index_names_it() {
    use spark_pulse_agent::facts::oci_platform;
    assert_eq!(oci_platform("x86_64"), "linux/amd64");
    assert_eq!(oci_platform("aarch64"), "linux/arm64");
    assert_eq!(oci_platform("riscv64"), "linux/riscv64");
    assert_eq!(oci_platform(""), "");
}

/// #211: rootless is read from who owns the socket, never from `/info`.
#[test]
fn a_socket_owned_by_root_is_rootful_and_any_other_is_rootless() {
    assert!(!rootless_owner(0));
    assert!(rootless_owner(1000));
}

#[test]
fn the_owner_is_read_through_a_symlink() {
    use std::os::unix::fs::MetadataExt;
    let dir = tempfile::tempdir().unwrap();
    let socket = dir.path().join("podman.sock");
    let _listener = std::os::unix::net::UnixListener::bind(&socket).unwrap();
    let link = dir.path().join("docker.sock");
    std::os::unix::fs::symlink(&socket, &link).unwrap();

    // Whoever runs the test owns both; root (a container CI) is the rootful case.
    let ours = std::fs::metadata(&socket).unwrap().uid() != 0;
    assert_eq!(podman_rootless(&socket), ours);
    assert_eq!(podman_rootless(&link), ours);
    assert!(!podman_rootless(&dir.path().join("absent.sock")));
}
