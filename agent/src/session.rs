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

/// A command that is running, and the flag its operation polls to be told to
/// stop. Mirrors the Python agent's `cancel()` callable exactly.
type CancelFlag = Arc<AtomicBool>;

pub struct Agent {
    identity: AgentIdentity,
    target: String,
    executor: Arc<Executor>,
    heartbeat_interval: Duration,
    running: Arc<Mutex<HashMap<String, CancelFlag>>>,
}

impl Agent {
    pub fn new(identity: AgentIdentity, target: String, executor: Arc<Executor>) -> Self {
        Self {
            identity,
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
        &self.identity.meta.node_id
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
        let tls = ClientTlsConfig::new()
            .ca_certificate(Certificate::from_pem(&self.identity.trust_bundle_pem))
            .identity(TlsIdentity::from_pem(
                &self.identity.certificate_pem,
                &self.identity.key_pem,
            ));
        Channel::from_shared(format!("https://{}", self.target))
            .with_context(|| format!("{} is not a usable target", self.target))?
            .tls_config(tls)
            .context("configuring mTLS for the session")?
            .connect_timeout(Duration::from_secs(10))
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
                    node_id: self.identity.meta.node_id.clone(),
                    agent_version: crate::facts::AGENT_VERSION.to_string(),
                    facts: Some(self.executor.collect_facts().await),
                    known_epoch: self.executor.epoch(),
                })),
            })
            .await
            .context("queueing the Hello")?;

        let mut client = NodeSessionClient::new(channel);
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

        let result = self.read_loop(&mut inbound, &outbox, stop).await;

        // Everything in flight is abandoned deliberately. A command whose
        // result cannot be delivered must not be reported as anything.
        beat.abort();
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
                        node = %self.identity.meta.node_id,
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
