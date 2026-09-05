//! Exchange a single-use token for a certificate, once.
//!
//! The three things an installer must deliver to a node are the token, the
//! trust bundle and the bundle's pin. Everything here is about those three and
//! the order they are checked in:
//!
//! 1. **The pin is checked before the bundle is used as a root of trust.** A
//!    substituted bundle is then caught by the node rather than trusted by it.
//!    Checking afterwards would mean the TLS handshake had already succeeded
//!    against an authority we had no reason to believe in.
//! 2. **The pin is checked again against what the server returns.** A control
//!    plane that answers with a different bundle than the one it was reached
//!    over is refused — which is the case a pin exists for.
//!
//! The private key is generated here and stays here; only the CSR is sent.
//! That is the opposite of NVIDIA's `discover-sparks`, which copies one shared
//! private key to every node and thereby makes any single Spark a key to all
//! of them.

use std::time::Duration;

use anyhow::{bail, Context, Result};
use tonic::transport::{Certificate, Channel, ClientTlsConfig};

use crate::facts;
use crate::identity::{self, AgentIdentity, IdentityMeta};
use crate::proto::enrollment_client::EnrollmentClient;
use crate::proto::EnrollRequest;

/// How long to wait for the control plane to answer the dial.
const CONNECT_TIMEOUT: Duration = Duration::from_secs(10);

/// And how long for the enrolment itself, which is one CSR signature.
const REQUEST_TIMEOUT: Duration = Duration::from_secs(30);

/// Everything the installer hands over, in one value so none of it can be
/// forgotten at a call site.
pub struct Enrolment<'a> {
    pub target: &'a str,
    pub token: &'a str,
    pub trust_bundle_pem: &'a [u8],
    pub trust_bundle_pin: &'a str,
    pub directory: &'a std::path::Path,
    pub requested_name: &'a str,
    pub docker_version: String,
}

/// Enrol, write the identity out, and return it.
pub async fn enroll(request: Enrolment<'_>) -> Result<AgentIdentity> {
    if !request.trust_bundle_pin.is_empty() {
        let computed = identity::spki_pin(request.trust_bundle_pem)
            .context("computing the pin of the trust bundle we were given")?;
        if computed != request.trust_bundle_pin {
            bail!(
                "the trust bundle does not match the pin it came with; refusing \
                 to enroll against it"
            );
        }
    }

    let pair = identity::build_csr(identity::CSR_COMMON_NAME)?;
    tracing::debug!(target = %request.target, "dialling the enrolment listener");
    let tls =
        ClientTlsConfig::new().ca_certificate(Certificate::from_pem(request.trust_bundle_pem));
    let channel = Channel::from_shared(format!("https://{}", request.target))
        .with_context(|| format!("{} is not a usable target", request.target))?
        .tls_config(tls)
        .context("configuring TLS for enrolment")?
        // A deadline, because the alternative is an install that appears to
        // work and never returns. The installer is watching this process; a
        // named failure after ten seconds is worth far more than a hang.
        .connect_timeout(CONNECT_TIMEOUT)
        .timeout(REQUEST_TIMEOUT)
        .connect()
        .await
        .with_context(|| format!("connecting to the enrolment listener at {}", request.target))?;
    tracing::debug!("connected; sending the enrolment request");

    let issued = EnrollmentClient::new(channel)
        .enroll(EnrollRequest {
            token: request.token.to_string(),
            csr_pem: pair.csr_pem.clone().into_bytes(),
            requested_name: request.requested_name.to_string(),
            facts: Some(facts::collect(request.docker_version)),
        })
        .await
        .map_err(|status| {
            // A refused token is the common case and its message is written for
            // an operator ("already used", "expired 42s ago"), so it is carried
            // through rather than replaced with a transport-shaped one.
            anyhow::anyhow!("enrolment was refused: {}", status.message())
        })?
        .into_inner();

    if !request.trust_bundle_pin.is_empty() && issued.trust_bundle_spki != request.trust_bundle_pin
    {
        bail!(
            "the control plane returned a trust bundle that does not match the \
             pin the installer supplied"
        );
    }
    if issued.node_id.is_empty() {
        bail!("the control plane issued no node id");
    }

    let identity = AgentIdentity {
        directory: request.directory.to_path_buf(),
        meta: IdentityMeta {
            node_id: issued.node_id,
            trust_bundle_pin: issued.trust_bundle_spki,
            cluster_id: issued.cluster_id,
            spiffe_id: issued.spiffe_id,
            epoch: issued.epoch,
            not_before: issued.not_before_unix as f64,
            not_after: issued.not_after_unix as f64,
        },
        key_pem: pair.key_pem.into_bytes(),
        certificate_pem: issued.certificate_pem,
        trust_bundle_pem: issued.trust_bundle_pem,
    };
    identity.save().context("writing the identity out")?;
    Ok(identity)
}
