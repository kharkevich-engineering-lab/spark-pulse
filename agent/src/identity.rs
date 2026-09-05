//! What a node keeps on disk, and the two computations that must agree with
//! the control plane exactly.
//!
//! Four files under one directory, and **the directory is the identity** —
//! field for field what `spark_pulse/agent/store.py` writes, because an agent
//! written in a different language must be able to adopt an identity the
//! Python agent created and vice versa. Uninstall-keeping-identity leaves this
//! directory; *Remove* deletes it. Those are two named actions, never one
//! boolean.
//!
//! Two things here are interop surface rather than implementation detail:
//!
//! * [`spki_pin`] must produce byte-for-byte what
//!   `spark_pulse.agent.identity.spki_pin` produces. It is what stops a node
//!   accepting a trust bundle from something that is not this control plane,
//!   so a pin that merely *looks* right is a security hole rather than a bug.
//!   `tests/pin_interop.rs` checks it against a fixture the Python side
//!   generated.
//! * The CSR must be one the Python CA will sign: P-256, SHA-256, a common
//!   name and nothing else. The CA reads only the subject and the public key,
//!   and deliberately ignores anything a node claims about its own identity —
//!   identity is minted server-side — so this asks for nothing.

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};
use base64::{engine::general_purpose::STANDARD as B64, Engine as _};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

/// The subject a node asks to have certified. A label for `openssl x509
/// -text`; nothing reads it and nothing may depend on it.
pub const CSR_COMMON_NAME: &str = "spark-pulse-node";

/// A private key that stays on the node, and the CSR built from it.
pub struct NodeKeyPair {
    pub key_pem: String,
    pub csr_pem: String,
}

/// Generate a P-256 key and a certificate signing request for it.
///
/// The key never leaves this machine; only the CSR does. That asymmetry is the
/// whole reason enrolment is a CSR exchange rather than the control plane
/// handing out key material — NVIDIA's own `discover-sparks` copies one shared
/// *private* key to every node, and this design exists not to.
pub fn build_csr(common_name: &str) -> Result<NodeKeyPair> {
    let key = rcgen::KeyPair::generate_for(&rcgen::PKCS_ECDSA_P256_SHA256)
        .context("generating a P-256 key")?;
    let mut params = rcgen::CertificateParams::default();
    params.distinguished_name = rcgen::DistinguishedName::new();
    params
        .distinguished_name
        .push(rcgen::DnType::CommonName, common_name);
    let csr = params
        .serialize_request(&key)
        .context("building the certificate signing request")?;
    Ok(NodeKeyPair {
        key_pem: key.serialize_pem(),
        csr_pem: csr.pem().context("encoding the CSR")?,
    })
}

/// base64(sha256) over the sorted DER SubjectPublicKeyInfo of every
/// certificate in a bundle.
///
/// Sorted, so the pin does not depend on the order the certificates were
/// concatenated in. Over the public *keys* rather than the certificates, so
/// re-issuing a CA certificate for the same key — a renewal — does not
/// invalidate every enrolment token in flight. Adding a CA does invalidate it,
/// which is the point.
pub fn spki_pin(bundle_pem: &[u8]) -> Result<String> {
    let mut spkis: Vec<Vec<u8>> = Vec::new();
    for der in certificates_in(bundle_pem)? {
        let (_, cert) = x509_parser::parse_x509_certificate(&der)
            .map_err(|e| anyhow::anyhow!("parsing a certificate in the trust bundle: {e}"))?;
        // `raw` is the complete SubjectPublicKeyInfo DER — the AlgorithmIdentifier
        // and the BIT STRING together — which is what Python's
        // `public_bytes(DER, SubjectPublicKeyInfo)` returns. Hashing only the key
        // bits would agree with nothing.
        spkis.push(cert.tbs_certificate.subject_pki.raw.to_vec());
    }
    if spkis.is_empty() {
        bail!("trust bundle contains no certificates");
    }
    spkis.sort();
    let mut digest = Sha256::new();
    for spki in &spkis {
        digest.update(spki);
    }
    Ok(B64.encode(digest.finalize()))
}

/// Every certificate in a PEM bundle, as DER, in file order.
fn certificates_in(bundle_pem: &[u8]) -> Result<Vec<Vec<u8>>> {
    let mut reader = std::io::BufReader::new(bundle_pem);
    let mut out = Vec::new();
    for item in rustls_pemfile::certs(&mut reader) {
        out.push(item.context("reading the trust bundle")?.to_vec());
    }
    Ok(out)
}

/// The SPIFFE URI SAN a certificate carries, if it has one.
///
/// The control plane refuses a stream whose peer certificate does not name the
/// node id it claims; reading our own back is how the agent notices it has been
/// handed a certificate for somebody else before it tries to use it.
pub fn spiffe_uri(cert_pem: &[u8]) -> Result<Option<String>> {
    let der = certificates_in(cert_pem)?
        .into_iter()
        .next()
        .context("no certificate in the PEM")?;
    let (_, cert) = x509_parser::parse_x509_certificate(&der)
        .map_err(|e| anyhow::anyhow!("parsing the node certificate: {e}"))?;
    for ext in cert.extensions() {
        if let x509_parser::extensions::ParsedExtension::SubjectAlternativeName(san) =
            ext.parsed_extension()
        {
            for name in &san.general_names {
                if let x509_parser::extensions::GeneralName::URI(uri) = name {
                    return Ok(Some((*uri).to_string()));
                }
            }
        }
    }
    Ok(None)
}

// ── The identity directory ──────────────────────────────────────────────────

/// `identity.json`, exactly as `store.py` writes it.
///
/// Field names and types are interop surface: a node enrolled by one agent
/// must be readable by the other. Unknown keys are ignored rather than
/// rejected so a newer control plane can add one without stranding a node
/// that has not been upgraded yet.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct IdentityMeta {
    pub node_id: String,
    #[serde(default)]
    pub trust_bundle_pin: String,
    #[serde(default)]
    pub cluster_id: String,
    #[serde(default)]
    pub spiffe_id: String,
    #[serde(default)]
    pub epoch: u64,
    #[serde(default)]
    pub not_before: f64,
    #[serde(default)]
    pub not_after: f64,
}

/// The four files, and the operations on them.
#[derive(Debug, Clone)]
pub struct AgentIdentity {
    pub directory: PathBuf,
    pub meta: IdentityMeta,
    pub key_pem: Vec<u8>,
    pub certificate_pem: Vec<u8>,
    pub trust_bundle_pem: Vec<u8>,
}

/// The default identity directory, overridable the same way Python overrides
/// it — systemd units and tests both set `SPARK_PULSE_AGENT_DIR`.
pub fn default_identity_dir() -> PathBuf {
    if let Ok(dir) = std::env::var("SPARK_PULSE_AGENT_DIR") {
        if !dir.is_empty() {
            return PathBuf::from(dir);
        }
    }
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".into());
    PathBuf::from(home)
        .join(".config")
        .join("spark-pulse")
        .join("agent")
}

struct Paths {
    key: PathBuf,
    cert: PathBuf,
    bundle: PathBuf,
    meta: PathBuf,
}

fn paths(directory: &Path) -> Paths {
    Paths {
        key: directory.join("node.key"),
        cert: directory.join("node.crt"),
        bundle: directory.join("ca.pem"),
        meta: directory.join("identity.json"),
    }
}

impl AgentIdentity {
    /// The identity in `directory`, or `None` if the node has never enrolled.
    ///
    /// A *partial* directory raises rather than answering `None`. Half an
    /// identity is a failed install, and reporting "never enrolled" for it
    /// would let an installer enrol the machine a second time and orphan the
    /// first uuid — after which the cluster holds two records for one machine
    /// and nobody can tell which is live.
    pub fn load(directory: &Path) -> Result<Option<Self>> {
        let p = paths(directory);
        let all = [
            ("node.key", &p.key),
            ("node.crt", &p.cert),
            ("ca.pem", &p.bundle),
            ("identity.json", &p.meta),
        ];
        let present: Vec<&str> = all
            .iter()
            .filter(|(_, path)| path.exists())
            .map(|(name, _)| *name)
            .collect();
        if present.is_empty() {
            return Ok(None);
        }
        if present.len() != all.len() {
            let missing: Vec<&str> = all
                .iter()
                .map(|(name, _)| *name)
                .filter(|name| !present.contains(name))
                .collect();
            bail!(
                "{} holds a partial agent identity; missing {:?}. Remove the \
                 directory to re-enroll, or restore the missing files.",
                directory.display(),
                missing
            );
        }
        let meta: IdentityMeta = serde_json::from_slice(&fs::read(&p.meta)?)
            .with_context(|| format!("reading {}", p.meta.display()))?;
        Ok(Some(Self {
            directory: directory.to_path_buf(),
            meta,
            key_pem: fs::read(&p.key)?,
            certificate_pem: fs::read(&p.cert)?,
            trust_bundle_pem: fs::read(&p.bundle)?,
        }))
    }

    /// Write the identity out, with the key 0600 from the moment it exists.
    pub fn save(&self) -> Result<()> {
        fs::create_dir_all(&self.directory)?;
        set_mode(&self.directory, 0o700)?;
        let p = paths(&self.directory);
        write_private(&p.key, &self.key_pem)?;
        fs::write(&p.cert, &self.certificate_pem)?;
        fs::write(&p.bundle, &self.trust_bundle_pem)?;
        fs::write(
            &p.meta,
            format!("{}\n", serde_json::to_string_pretty(&self.meta)?),
        )?;
        Ok(())
    }

    /// The *Remove* action: wipe the identity. Re-enrolment is then required.
    pub fn destroy(directory: &Path) -> Result<()> {
        let p = paths(directory);
        for path in [&p.key, &p.cert, &p.bundle, &p.meta] {
            match fs::remove_file(path) {
                Ok(()) => {}
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                Err(e) => return Err(e).with_context(|| format!("removing {}", path.display())),
            }
        }
        Ok(())
    }

    /// Whether a bundle matches the pin recorded at enrolment.
    ///
    /// An identity with no pin recorded accepts any bundle: that is an
    /// identity created before pinning existed, and refusing it would strand
    /// the node rather than protect it.
    pub fn verify_pin(&self, bundle_pem: &[u8]) -> bool {
        if self.meta.trust_bundle_pin.is_empty() {
            return true;
        }
        matches!(spki_pin(bundle_pem), Ok(pin) if pin == self.meta.trust_bundle_pin)
    }
}

#[cfg(unix)]
fn set_mode(path: &Path, mode: u32) -> Result<()> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(mode))?;
    Ok(())
}

#[cfg(not(unix))]
fn set_mode(_path: &Path, _mode: u32) -> Result<()> {
    Ok(())
}

/// Create a file 0600 *before* anything is written to it, so the private key
/// is never briefly world-readable.
fn write_private(path: &Path, bytes: &[u8]) -> Result<()> {
    let mut options = fs::OpenOptions::new();
    options.write(true).create(true).truncate(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut handle = options
        .open(path)
        .with_context(|| format!("writing {}", path.display()))?;
    handle.write_all(bytes)?;
    Ok(())
}
