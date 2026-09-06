//! One node's agent: dial the control plane, hold one stream, redial.
//!
//! **The agent dials out.** The control plane therefore listens on exactly one
//! inbound port rather than one per node, identity is authenticated once per
//! connection, and — the part that matters — heartbeat liveness and
//! command-channel liveness become the same fact. There is no separate probe
//! that can disagree with the thing it is probing.
//!
//! Three loops share the stream and exactly one of them writes to it. gRPC
//! forbids concurrent writes, so every outbound message goes through one
//! channel and one writer drains it; the heartbeat, a command result and a
//! progress event cannot interleave halfway through a frame.
//!
//! **A command that is interrupted sends nothing.** If the stream drops while
//! an operation is in flight, no result is written — which is exactly right.
//! The caller learns "unreachable, outcome unknown" rather than a failure we
//! would be inventing, and a rank whose node went quiet keeps its GPU and its
//! ports until an agent confirms otherwise.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result};
use rand::Rng;
use tokio::sync::mpsc;
use tokio_stream::wrappers::ReceiverStream;
use tonic::transport::{Certificate, Channel, ClientTlsConfig, Identity as TlsIdentity};

use crate::executor::{CommandContext, Executor, ProgressSink};
use crate::identity::AgentIdentity;
use crate::proto::agent_message::Body as AgentBody;
use crate::proto::control_message::Body as ControlBody;
use crate::proto::node_session_client::NodeSessionClient;
use crate::proto::{AgentMessage, Command, Heartbeat, Hello};

/// How often the agent reports in. The hub calls a node *suspect* at 15s and
/// *unknown* at 60s, so this is comfortably inside both.
pub const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(5);

/// Reconnect backoff. Jittered on use, so a rack of Sparks that lost the
/// control plane together does not come back in lockstep and knock it over
/// again the moment it returns.
const RECONNECT_MIN: Duration = Duration::from_secs(1);
const RECONNECT_MAX: Duration = Duration::from_secs(15);

/// Outbound queue depth. Deep enough that a burst of pull-progress events
/// never blocks the executor, shallow enough that a stalled stream is noticed
/// rather than buffered indefinitely.
const OUTBOX_DEPTH: usize = 256;

/// The largest message either side will encode or decode. **Must equal
/// `spark_pulse.agent.keepalive.MAX_MESSAGE_BYTES`**, which the control plane
/// configures its own server with.
///
/// tonic's default decode limit is 4 MiB, and nothing said so. A
/// `CopyDirToContainer` carrying a directory of mods is well inside the 64 MiB
/// the protocol was designed around and well outside 4 MiB, so the node
/// refused it — as a decode error on a command the control plane had every
/// reason to believe was deliverable.
pub const MAX_MESSAGE_BYTES: usize = 64 * 1024 * 1024;

/// How often an idle agent pings the control plane, and how long it waits for
/// an answer. **Must match `spark_pulse.agent.keepalive`**: that module's
/// invariant is that the server's minimum ping interval (5s) stays below this,
/// or the server answers with `ENHANCE_YOUR_CALM` and HTTP/2 tells the client
/// to silently double its interval — detection then gets slower every time it
/// happens, over hours, with nothing in any log saying so.
const KEEPALIVE_INTERVAL: Duration = Duration::from_secs(10);
const KEEPALIVE_TIMEOUT: Duration = Duration::from_secs(5);

/// A command that is running, and the flag its operation polls to be told to
/// stop. Mirrors the Python agent's `cancel()` callable exactly.
type CancelFlag = Arc<AtomicBool>;

pub struct Agent {
    identity: Mutex<AgentIdentity>,
    target: String,
    executor: Arc<Executor>,
    heartbeat_interval: Duration,
    running: Arc<Mutex<HashMap<String, CancelFlag>>>,
    node_id: String,
}

impl Agent {
    pub fn new(identity: AgentIdentity, target: String, executor: Arc<Executor>) -> Self {
        Self {
            node_id: identity.meta.node_id.clone(),
            identity: Mutex::new(identity),
            target,
            executor,
            heartbeat_interval: HEARTBEAT_INTERVAL,
            running: Arc::new(Mutex::new(HashMap::new())),
        }
    }

    pub fn with_heartbeat_interval(mut self, interval: Duration) -> Self {
        self.heartbeat_interval = interval;
        self
    }

    pub fn node_id(&self) -> &str {
        &self.node_id
    }

    /// Dial, hold, redial. Returns only when `stop` is signalled.
    pub async fn run_forever(&self, mut stop: tokio::sync::watch::Receiver<bool>) {
        let mut delay = RECONNECT_MIN;
        loop {
            if *stop.borrow() {
                return;
            }
            match self.run_once(&mut stop).await {
                Ok(()) => {
                    tracing::info!("session ended cleanly");
                    delay = RECONNECT_MIN;
                }
                Err(error) => {
                    tracing::warn!(target = %self.target, "session ended: {error:#}");
                }
            }
            if *stop.borrow() {
                return;
            }
            let jittered = {
                let factor: f64 = rand::rng().random_range(0.5..1.5);
                delay.mul_f64(factor)
            };
            tracing::debug!(?jittered, "reconnecting");
            tokio::select! {
                _ = tokio::time::sleep(jittered) => {}
                _ = stop.changed() => return,
            }
            delay = std::cmp::min(delay * 2, RECONNECT_MAX);
        }
    }

    /// The mTLS channel, built from the identity on disk.
    ///
    /// Rebuilt on every dial rather than held, which is what makes a renewed
    /// certificate take effect: renewal writes the new one to disk and the
    /// next dial picks it up. The window is weeks wide, so that is never
    /// urgent.
    async fn connect(&self) -> Result<Channel> {
        let (bundle, certificate, key) = {
            let identity = self.identity.lock().unwrap();
            (
                identity.trust_bundle_pem.clone(),
                identity.certificate_pem.clone(),
                identity.key_pem.clone(),
            )
        };
        let tls = ClientTlsConfig::new()
            .ca_certificate(Certificate::from_pem(&bundle))
            .identity(TlsIdentity::from_pem(&certificate, &key));
        Channel::from_shared(format!("https://{}", self.target))
            .with_context(|| format!("{} is not a usable target", self.target))?
            .tls_config(tls)
            .context("configuring mTLS for the session")?
            .connect_timeout(Duration::from_secs(10))
            // An agent holding an idle stream is the *normal* state, so pings
            // have to be permitted without calls or the keepalive never fires
            // and a half-open connection — a NAT that forgot us, a link that
            // went away without a FIN — leaves this agent believing it is
            // connected while the control plane has already given up on it.
            // Nothing then redials, and the node is offline until the process
            // is restarted.
            .http2_keep_alive_interval(KEEPALIVE_INTERVAL)
            .keep_alive_timeout(KEEPALIVE_TIMEOUT)
            .keep_alive_while_idle(true)
            .connect()
            .await
            .with_context(|| format!("dialling {}", self.target))
    }

    /// Hold exactly one session, and return when it ends.
    async fn run_once(&self, stop: &mut tokio::sync::watch::Receiver<bool>) -> Result<()> {
        let channel = self.connect().await?;
        let (outbox, rx) = mpsc::channel::<AgentMessage>(OUTBOX_DEPTH);

        // Hello goes in before the stream is opened, so it is unavoidably the
        // first message on it — which is what the control plane requires, and
        // what lets it bind this connection to an identity before anything
        // else is said.
        outbox
            .send(AgentMessage {
                body: Some(AgentBody::Hello(Hello {
                    node_id: self.node_id.clone(),
                    agent_version: crate::facts::AGENT_VERSION.to_string(),
                    facts: Some(self.executor.collect_facts().await),
                    known_epoch: self.executor.epoch(),
                })),
            })
            .await
            .context("queueing the Hello")?;

        let mut client = NodeSessionClient::new(channel.clone())
            .max_decoding_message_size(MAX_MESSAGE_BYTES)
            .max_encoding_message_size(MAX_MESSAGE_BYTES);
        let mut inbound = client
            .session(ReceiverStream::new(rx))
            .await
            .map_err(|status| {
                anyhow::anyhow!(
                    "the control plane refused the session: {}",
                    status.message()
                )
            })?
            .into_inner();

        let beat = tokio::spawn(heartbeat_loop(
            outbox.clone(),
            Arc::clone(&self.executor),
            self.heartbeat_interval,
        ));
        let renew = tokio::spawn(renewal_loop(channel, self.renewal_state()));

        let result = self.read_loop(&mut inbound, &outbox, stop).await;

        // Everything in flight is abandoned deliberately. A command whose
        // result cannot be delivered must not be reported as anything.
        beat.abort();
        renew.abort();
        for (_, flag) in self.running.lock().unwrap().drain() {
            flag.store(true, Ordering::SeqCst);
        }
        result
    }

    async fn read_loop(
        &self,
        inbound: &mut tonic::Streaming<crate::proto::ControlMessage>,
        outbox: &mpsc::Sender<AgentMessage>,
        stop: &mut tokio::sync::watch::Receiver<bool>,
    ) -> Result<()> {
        loop {
            let message = tokio::select! {
                message = inbound.message() => message,
                _ = stop.changed() => return Ok(()),
            };
            let Some(message) = message.context("reading from the control plane")? else {
                return Ok(()); // Clean end of stream.
            };
            match message.body {
                Some(ControlBody::Welcome(welcome)) => {
                    self.executor.note_epoch(welcome.epoch);
                    tracing::info!(
                        cluster = %welcome.cluster_id,
                        epoch = welcome.epoch,
                        node = %self.node_id,
                        "session established"
                    );
                }
                Some(ControlBody::Command(command)) => {
                    self.start_command(command, outbox.clone());
                }
                Some(ControlBody::Cancel(cancel)) => {
                    // Only for a command actually running here. A cancel for
                    // one that already finished is dropped rather than
                    // remembered, so nothing accumulates for the life of the
                    // process.
                    if let Some(flag) = self.running.lock().unwrap().get(&cancel.command_id) {
                        tracing::debug!(command = %cancel.command_id, "cancelled");
                        flag.store(true, Ordering::SeqCst);
                    }
                }
                None => {}
            }
        }
    }

    /// Start a command on its own task.
    ///
    /// Concurrently with the heartbeat, which is the point: a pull takes
    /// minutes, and an agent that ran it inline would stop reporting in and be
    /// declared unreachable *while it was busy doing exactly what it was
    /// asked*. In Python this needed a worker thread because DockerService is
    /// synchronous; here the Docker client is async, so it is simply a task.
    fn start_command(&self, command: Command, outbox: mpsc::Sender<AgentMessage>) {
        let id = command.command_id.clone();
        let flag: CancelFlag = Arc::new(AtomicBool::new(false));
        self.running
            .lock()
            .unwrap()
            .insert(id.clone(), Arc::clone(&flag));
        let executor = Arc::clone(&self.executor);
        let running = Arc::clone(&self.running);
        let context = CommandContext {
            cancel: Arc::clone(&flag),
            progress: Some(ProgressSink::new(id.clone(), outbox.clone())),
        };
        tokio::spawn(async move {
            let result = executor.execute(command, context).await;
            running.lock().unwrap().remove(&id);
            // A closed outbox means the stream went away while we worked. The
            // result is dropped rather than logged as an error: the caller has
            // already been told "unknown", which is the truth.
            if outbox
                .send(AgentMessage {
                    body: Some(AgentBody::Result(result)),
                })
                .await
                .is_err()
            {
                tracing::debug!(command = %id, "the session ended before the result could be sent");
            }
        });
    }
}

async fn heartbeat_loop(
    outbox: mpsc::Sender<AgentMessage>,
    executor: Arc<Executor>,
    interval: Duration,
) {
    let mut seq = 0u64;
    let mut ticker = tokio::time::interval(interval);
    // The first tick fires immediately; the Hello has already carried facts,
    // so skip it and beat on the interval.
    ticker.tick().await;
    loop {
        ticker.tick().await;
        seq += 1;
        let message = AgentMessage {
            body: Some(AgentBody::Heartbeat(Heartbeat {
                seq,
                sent_unix: now_unix(),
                facts: Some(executor.collect_facts().await),
            })),
        };
        if outbox.send(message).await.is_err() {
            return;
        }
    }
}

fn now_unix() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

// ── Certificate renewal ─────────────────────────────────────────────────────

/// Renewal fractions, matching `spark_pulse.agent.identity.renewal_delay`.
///
/// Jittered across a wide band rather than a fixed point, because a rack of
/// Sparks enrolled in the same hour would otherwise all renew in the same
/// minute ninety days later.
const RENEWAL_FRACTION_MIN: f64 = 0.50;
const RENEWAL_FRACTION_MAX: f64 = 0.80;

/// After a failed renewal, before trying again. A node certificate is valid
/// for ninety days, so there is a great deal of time to retry in — and no
/// reason to hammer a control plane that is having a bad day.
const RENEWAL_RETRY: Duration = Duration::from_secs(300);

/// What the renewal task needs: where to write, and what it holds now.
type Shared = Arc<Mutex<AgentIdentity>>;

impl Agent {
    fn renewal_state(&self) -> Option<Shared> {
        // Only meaningful once we know when the certificate expires. An
        // identity with no window recorded is one written before expiry was
        // tracked; renewing on a guess would be worse than leaving it.
        let identity = self.identity.lock().unwrap();
        (identity.meta.not_after > 0.0).then(|| Arc::new(Mutex::new(identity.clone())))
    }
}

/// Renew the node's certificate before it expires, over the channel the
/// *current* certificate already authenticated.
///
/// That is what makes renewal need no token and no operator. Without it a node
/// simply stops being able to connect ninety days after it was installed —
/// silently, and long after anyone would connect the two events.
///
/// The new certificate is written to disk and takes effect on the next dial.
/// The window is weeks wide, so that is never urgent, and reconnecting on
/// purpose to pick it up would be a self-inflicted outage.
async fn renewal_loop(channel: Channel, state: Option<Shared>) {
    let Some(state) = state else { return };
    loop {
        let delay = {
            let identity = state.lock().unwrap();
            renewal_delay(identity.meta.not_before, identity.meta.not_after)
        };
        tokio::time::sleep(delay).await;
        match renew_once(&channel, &state).await {
            Ok(node_id) => tracing::info!(node = %node_id, "renewed certificate"),
            Err(error) => {
                tracing::warn!("certificate renewal failed: {error:#}");
                tokio::time::sleep(RENEWAL_RETRY).await;
            }
        }
    }
}

async fn renew_once(channel: &Channel, state: &Shared) -> Result<String> {
    use crate::proto::enrollment_client::EnrollmentClient;
    use crate::proto::RenewRequest;

    let pair = crate::identity::build_csr(crate::identity::CSR_COMMON_NAME)?;
    let issued = EnrollmentClient::new(channel.clone())
        .max_decoding_message_size(MAX_MESSAGE_BYTES)
        .max_encoding_message_size(MAX_MESSAGE_BYTES)
        .renew(RenewRequest {
            csr_pem: pair.csr_pem.clone().into_bytes(),
            facts: None,
        })
        .await
        .map_err(|status| anyhow::anyhow!("the control plane refused: {}", status.message()))?
        .into_inner();

    let mut identity = state.lock().unwrap();
    // The pin is *checked*, not replaced. A renewal that arrives carrying a
    // different trust bundle is the one thing a pin exists to catch, and
    // adopting it would delete the protection at exactly the moment it
    // mattered.
    if !identity.verify_pin(&issued.trust_bundle_pem) {
        anyhow::bail!(
            "the trust bundle offered on renewal does not match the pin recorded \
             at enrolment; refusing it"
        );
    }
    identity.key_pem = pair.key_pem.into_bytes();
    identity.certificate_pem = issued.certificate_pem;
    identity.trust_bundle_pem = issued.trust_bundle_pem;
    identity.meta.not_before = issued.not_before_unix as f64;
    identity.meta.not_after = issued.not_after_unix as f64;
    identity.meta.epoch = issued.epoch;
    identity.save().context("writing the renewed identity")?;
    Ok(identity.meta.node_id.clone())
}

/// How long to wait before renewing: jittered over 50–80% of the lifetime.
///
/// Never negative — a certificate already past its renewal point renews now.
fn renewal_delay(not_before: f64, not_after: f64) -> Duration {
    let lifetime = (not_after - not_before).max(0.0);
    let fraction = rand::rng().random_range(RENEWAL_FRACTION_MIN..RENEWAL_FRACTION_MAX);
    let renew_at = not_before + lifetime * fraction;
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    Duration::from_secs_f64((renew_at - now).max(0.0))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn renewal_lands_inside_the_certificates_life() {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs_f64();
        let lifetime = 90.0 * 86_400.0;
        for _ in 0..200 {
            let delay = renewal_delay(now, now + lifetime).as_secs_f64();
            assert!(delay >= lifetime * RENEWAL_FRACTION_MIN - 1.0, "{delay}");
            assert!(delay <= lifetime * RENEWAL_FRACTION_MAX + 1.0, "{delay}");
        }
    }

    #[test]
    fn an_overdue_certificate_renews_now_rather_than_never() {
        let long_ago = 1_000_000.0;
        assert_eq!(renewal_delay(long_ago, long_ago + 10.0), Duration::ZERO);
    }
}
