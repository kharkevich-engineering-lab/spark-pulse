//! Compile the *one* proto file, which lives with the control plane.
//!
//! Deliberately not a copy. `spark_pulse/agent/agent.proto` is the contract
//! between this binary and the Python control plane, and a second copy of a
//! contract is a contract that drifts. `cargo build` re-runs when it changes,
//! so a field added on one side fails to compile on the other rather than
//! being silently ignored on the wire.

use std::path::PathBuf;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let proto = PathBuf::from("../spark_pulse/agent/agent.proto");
    let root = proto.parent().unwrap().to_path_buf();

    println!("cargo:rerun-if-changed={}", proto.display());
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-changed=../pyproject.toml");

    // The agent reports *spark-pulse's* version, not the crate's.
    //
    // They are one product and the doctor compares them: a node running an
    // agent whose version differs from the control plane's is reported as
    // needing a reinstall. With two independent version numbers that warning
    // would fire on every node, for ever, and mean nothing. `release.sh`
    // rewrites `pyproject.toml`, so reading it here makes one release process
    // drive both halves.
    println!(
        "cargo:rustc-env=SPARK_PULSE_VERSION={}",
        spark_pulse_version()?
    );

    if !proto.exists() {
        return Err(format!(
            "{} is missing. This crate is built from inside the spark-pulse \
             repository, next to the control plane whose protocol it speaks.",
            proto.display()
        )
        .into());
    }

    tonic_prost_build::configure()
        .build_server(false)
        .build_client(true)
        .compile_protos(&[proto], &[root])?;
    Ok(())
}

/// The `version` under `[project]` in the repository's `pyproject.toml`.
///
/// Parsed rather than pulled from a build tool, because the alternative is a
/// build dependency on a TOML crate to read one line that is always in the
/// same place.
fn spark_pulse_version() -> Result<String, Box<dyn std::error::Error>> {
    let text = std::fs::read_to_string("../pyproject.toml")?;
    let mut in_project = false;
    for line in text.lines() {
        let line = line.trim();
        if line.starts_with('[') {
            in_project = line == "[project]";
            continue;
        }
        if in_project {
            if let Some(rest) = line.strip_prefix("version") {
                if let Some(value) = rest.trim_start().strip_prefix('=') {
                    return Ok(value.trim().trim_matches('"').to_string());
                }
            }
        }
    }
    Err("no version under [project] in ../pyproject.toml".into())
}
