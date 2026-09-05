//! Getting bytes from the control plane into a container on this node.
//!
//! **Nothing here shells out**, and that is the one place this agent
//! deliberately differs from the Python service it replaces.
//! `DockerService.copy_to_container` runs `docker cp`, which means every node
//! needs the Docker *CLI* installed beside the daemon, and that a copy can
//! fail because a binary is missing rather than because the copy failed. This
//! speaks the Engine API's archive endpoint over the same socket everything
//! else uses, so a node needs a daemon and nothing else.
//!
//! The endpoint takes a tar and a destination *directory*, which is what
//! `docker cp` builds internally: to land a file at `/workspace/exec-script.sh`
//! you upload a tar containing one entry named `exec-script.sh` to
//! `/workspace`. That is done here rather than staged through a temporary file
//! on disk, so a payload never touches the node's filesystem at all.

use std::io::Read;
use std::path::Path;

use bollard::query_parameters::UploadToContainerOptionsBuilder;
use bollard::Docker;

use super::{DockerResult, OpError};

/// Split a destination into the directory to unpack into and the name to
/// unpack as. `/workspace/exec-script.sh` → (`/workspace`, `exec-script.sh`).
fn destination(remote_path: &str) -> DockerResult<(String, String)> {
    let path = Path::new(remote_path);
    let name = path
        .file_name()
        .and_then(|n| n.to_str())
        .filter(|n| !n.is_empty())
        .ok_or_else(|| {
            OpError::new(
                "ValueError",
                format!("{remote_path} does not name a file to copy to"),
            )
        })?;
    let parent = path
        .parent()
        .and_then(|p| p.to_str())
        .filter(|p| !p.is_empty())
        .unwrap_or("/");
    Ok((parent.to_string(), name.to_string()))
}

/// Copy one file's bytes into a container.
pub async fn copy_file(
    docker: &Docker,
    container: &str,
    remote_path: &str,
    content: &[u8],
    mode: Option<u32>,
) -> DockerResult<bool> {
    let (directory, name) = destination(remote_path)?;
    let mut builder = tar::Builder::new(Vec::new());
    let mut header = tar::Header::new_gnu();
    header.set_size(content.len() as u64);
    // The thing most often copied this way is a serve script, which has to be
    // executable when it lands — so the source's permission bits travel with
    // it rather than being reinvented at the far end.
    header.set_mode(mode.unwrap_or(0o644));
    header.set_cksum();
    builder
        .append_data(&mut header, &name, content)
        .map_err(|error| OpError::new("OSError", format!("building the archive: {error}")))?;
    let archive = builder
        .into_inner()
        .map_err(|error| OpError::new("OSError", format!("finishing the archive: {error}")))?;
    upload(docker, container, &directory, archive).await
}

/// Copy a directory tree, arriving as a gzipped tar, into a container.
pub async fn copy_dir(
    docker: &Docker,
    container: &str,
    remote_path: &str,
    tar_gz: &[u8],
) -> DockerResult<bool> {
    let (directory, name) = destination(remote_path)?;
    let mut decoder = flate2::read::GzDecoder::new(tar_gz);
    let mut raw = Vec::new();
    decoder
        .read_to_end(&mut raw)
        .map_err(|error| OpError::new("OSError", format!("decompressing the archive: {error}")))?;

    // Re-emitted under the destination's own name rather than uploaded as it
    // arrived, because the entries are relative to the source directory and
    // the destination may be called something else.
    let mut source = tar::Archive::new(raw.as_slice());
    let mut builder = tar::Builder::new(Vec::new());
    for entry in source
        .entries()
        .map_err(|error| OpError::new("OSError", format!("reading the archive: {error}")))?
    {
        let mut entry =
            entry.map_err(|error| OpError::new("OSError", format!("reading an entry: {error}")))?;
        let path = entry
            .path()
            .map_err(|error| OpError::new("OSError", format!("reading an entry path: {error}")))?
            .into_owned();
        // The archive comes from the control plane over an authenticated
        // channel — but an agent unpacking as root is not where anyone should
        // be relying on that. Python uses tarfile's "data" filter for the same
        // reason; this is the same rule, stated.
        if path.is_absolute()
            || path
                .components()
                .any(|c| c == std::path::Component::ParentDir)
        {
            return Err(OpError::new(
                "ValueError",
                format!("the archive contains an unsafe path: {}", path.display()),
            ));
        }
        let mut header = entry.header().clone();
        let target = Path::new(&name).join(&path);
        let mut bytes = Vec::new();
        entry
            .read_to_end(&mut bytes)
            .map_err(|error| OpError::new("OSError", format!("reading an entry: {error}")))?;
        header.set_size(bytes.len() as u64);
        header.set_cksum();
        builder
            .append_data(&mut header, &target, bytes.as_slice())
            .map_err(|error| OpError::new("OSError", format!("building the archive: {error}")))?;
    }
    let archive = builder
        .into_inner()
        .map_err(|error| OpError::new("OSError", format!("finishing the archive: {error}")))?;
    upload(docker, container, &directory, archive).await
}

async fn upload(
    docker: &Docker,
    container: &str,
    directory: &str,
    archive: Vec<u8>,
) -> DockerResult<bool> {
    let options = UploadToContainerOptionsBuilder::default()
        .path(directory)
        .build();
    match docker
        .upload_to_container(container, Some(options), bollard::body_full(archive.into()))
        .await
    {
        Ok(()) => Ok(true),
        Err(error) => {
            // False rather than an error, matching `DockerService`: every
            // caller treats a failed copy as a failed deploy step and logs the
            // reason itself, and a raise here would change which of them fires.
            tracing::error!(%container, %directory, %error, "copy into container failed");
            Ok(false)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::destination;

    #[test]
    fn a_destination_splits_into_a_directory_and_a_name() {
        assert_eq!(
            destination("/workspace/exec-script.sh").unwrap(),
            ("/workspace".to_string(), "exec-script.sh".to_string())
        );
        assert_eq!(
            destination("/tmp/mod").unwrap(),
            ("/tmp".to_string(), "mod".to_string())
        );
        // A bare name lands at the root, which is what `docker cp x c:/y` does.
        assert_eq!(
            destination("/y").unwrap(),
            ("/".to_string(), "y".to_string())
        );
    }

    #[test]
    fn a_destination_with_no_name_is_refused() {
        assert!(destination("/").is_err());
        assert!(destination("").is_err());
    }
}
