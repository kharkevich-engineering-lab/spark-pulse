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
