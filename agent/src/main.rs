//! `spark-pulse-agent` — run the node agent.
//!
//! Two modes, and the interesting behaviour is the boundary between them.
//!
//! *Already enrolled* — the identity directory holds a complete identity and
//! the agent simply runs. No token is needed and none may be given.
//!
//! *Not yet enrolled* — a token, a trust bundle and its pin are required; the
//! agent enrols once, writes the identity out, and then runs.
//!
//! **An existing identity plus a token is refused, loudly.** Converging is not
//! possible: a second enrolment mints a second uuid and orphans the first, so
//! the cluster would hold two records for one machine with no way to tell
//! which is live. Refusing names the directory and says what to delete, so the
//! operator makes that choice rather than discovering it. `--rotate` is the
//! explicit form of that choice.
//!
//! Two flags exist for the SSH installer and are worth knowing about anywhere
//! the agent is driven by a program rather than by hand:
//!
//! `--token-file`
//!     Read the token from a file rather than the command line. An argument is
//!     visible in `ps(1)` to every user on the node; a 0600 file the installer
//!     shreds afterwards is not.
//! `--enroll-only`
//!     Enrol, write the identity out, print the node id and exit. The installer
//!     watches enrolment happen and *then* starts a unit carrying no token at
//!     all — so a restart of that unit can never be the refused case above.

use std::path::PathBuf;
use std::process::ExitCode;

use anyhow::{bail, Context, Result};
use clap::Parser;

use spark_pulse_agent::{enroll, identity};

#[derive(Parser, Debug)]
#[command(
    name = "spark-pulse-agent",
    about = "Run the spark-pulse node agent.",
    version
)]
struct Args {
    /// The control plane's session listener (mTLS).
    #[arg(long, value_name = "HOST:PORT")]
    control: String,

    /// The control plane's enrolment listener; required to enrol.
    #[arg(long, value_name = "HOST:PORT", default_value = "")]
    enroll_target: String,

    /// Single-use enrolment token.
    #[arg(long, default_value = "")]
    token: String,

    /// Read the single-use token from this file rather than the command line,
    /// and prefer it — an argument is visible in ps(1) to every user on the
    /// node, which is why the SSH installer uses this.
    #[arg(long, value_name = "PATH")]
    token_file: Option<PathBuf>,

    /// PEM file holding the cluster CA bundle.
    #[arg(long, value_name = "PATH")]
    trust_bundle: Option<PathBuf>,

    /// SPKI pin over the trust bundle, as printed by the control plane.
    #[arg(long, default_value = "")]
    pin: String,

    /// Identity directory (default ~/.config/spark-pulse/agent).
    #[arg(long, value_name = "PATH")]
    dir: Option<PathBuf>,

    /// Operator-facing label for this node.
    #[arg(long, default_value = "")]
    name: String,

    /// Destroy any existing identity and enrol again (Remove, then join).
    #[arg(long)]
    rotate: bool,

    /// Enrol, write the identity out, and exit without running. What an
    /// installer does: the token is spent while the installer is still
    /// watching, and the unit it then starts carries no token at all.
    #[arg(long)]
    enroll_only: bool,

    /// Log at DEBUG rather than INFO.
    #[arg(long)]
    verbose: bool,
}

fn main() -> ExitCode {
    let args = Args::parse();
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| if args.verbose { "debug" } else { "info" }.into()),
        )
        .with_target(false)
        .init();

    // rustls 0.23 wants one process-wide provider chosen explicitly. Doing it
    // here rather than lazily means a build that somehow linked two providers
    // fails at startup instead of at the first handshake.
    if rustls::crypto::ring::default_provider()
        .install_default()
        .is_err()
    {
        tracing::debug!("a rustls crypto provider was already installed");
    }

    let runtime = match tokio::runtime::Runtime::new() {
        Ok(runtime) => runtime,
        Err(error) => {
            eprintln!("could not start the async runtime: {error}");
            return ExitCode::FAILURE;
        }
    };
    match runtime.block_on(run(args)) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            // `{:#}` prints the whole context chain. An operator reading a
            // failed install wants "the trust bundle does not match the pin"
            // and the call that produced it, not just the innermost io error.
            eprintln!("spark-pulse-agent: {error:#}");
            ExitCode::FAILURE
        }
    }
}

/// The token, from the file if one was given, else the argument.
///
/// The file wins when both are present rather than being an error: the
/// installer passes a file, and a unit template that still carried a stale
/// `--token` should not stop the install it is in the middle of.
fn read_token(args: &Args) -> Result<String> {
    if let Some(path) = &args.token_file {
        let raw = std::fs::read_to_string(path)
            .with_context(|| format!("reading the token file {}", path.display()))?;
        return Ok(raw.trim().to_string());
    }
    Ok(args.token.trim().to_string())
}

async fn run(args: Args) -> Result<()> {
    let directory = args
        .dir
        .clone()
        .unwrap_or_else(identity::default_identity_dir);
    let token = read_token(&args)?;

    if args.rotate {
        if identity::AgentIdentity::load(&directory)?.is_some() {
            tracing::warn!(
                directory = %directory.display(),
                "--rotate: destroying this node's identity and enrolling again. \
                 The control plane's record of the old uuid becomes an orphan."
            );
        }
        identity::AgentIdentity::destroy(&directory)?;
    }

    let existing = identity::AgentIdentity::load(&directory)?;
    let identity = match (existing, token.is_empty()) {
        // Enrolled, and nobody offered a token. The ordinary case.
        (Some(identity), true) => identity,

        // Enrolled, *and* a token was offered. Refuse, and say what to do.
        (Some(identity), false) => bail!(
            "{} already holds the identity {}, and a token was supplied. \
             Enrolling again would mint a second uuid for this machine and \
             orphan the first, so this is refused rather than guessed at. \
             Start the agent without a token to use the identity it has, or \
             pass --rotate to destroy it and enrol again.",
            directory.display(),
            identity.meta.node_id
        ),

        // Never enrolled and no token: there is nothing to run as.
        (None, true) => bail!(
            "this node has no identity in {} and no enrolment token was given. \
             An installer supplies --token-file, --trust-bundle and --pin.",
            directory.display()
        ),

        // Never enrolled, and a token. Enrol.
        (None, false) => {
            let target = if args.enroll_target.is_empty() {
                bail!("--enroll-target is required to enrol")
            } else {
                args.enroll_target.clone()
            };
            let bundle_path = args
                .trust_bundle
                .clone()
                .context("--trust-bundle is required to enrol")?;
            let bundle = std::fs::read(&bundle_path)
                .with_context(|| format!("reading {}", bundle_path.display()))?;
            enroll::enroll(enroll::Enrolment {
                target: &target,
                token: &token,
                trust_bundle_pem: &bundle,
                trust_bundle_pin: &args.pin,
                directory: &directory,
                requested_name: &args.name,
                docker_version: String::new(),
            })
            .await?
        }
    };

    if args.enroll_only {
        // The installer reads this line. Keep it on stdout, one field, no
        // decoration: it is parsed, not read.
        println!("{}", identity.meta.node_id);
        return Ok(());
    }

    let _ = &args.control;
    bail!(
        "this build can enrol but cannot yet hold a session; run with \
         --enroll-only. (Session support is the next commit.)"
    )
}
