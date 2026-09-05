//! The `spark-pulse.*` label vocabulary, and metadata's round trip through it.
//!
//! Container labels are the **source of truth for reconciliation**: a control
//! plane that restarts rebuilds its whole view of what is running by reading
//! them back off the daemon. So this is not serialisation for convenience —
//! it is the only place any of this survives the process, and it has to agree
//! with `spark_pulse/tools/labels.py` and `ContainerMetadata.to_labels()` key
//! for key.
//!
//! The cluster block is legacy. Nothing writes those labels any more — the
//! orchestrator that did is gone — but the orphan sweep still reads them,
//! because a container an older build left on a host carries no other identity
//! we would recognise.

use std::collections::BTreeMap;

use crate::proto::ContainerMetadata;

pub const PREFIX: &str = "spark-pulse.";

macro_rules! label {
    ($name:ident, $key:literal) => {
        pub const $name: &str = concat!("spark-pulse.", $key);
    };
}

label!(MANAGED, "managed");
label!(NAME, "name");
label!(VERSION, "version");
label!(CREATED_AT, "created_at");
label!(IMAGE, "image");
label!(DEPLOYMENT, "deployment");
label!(RECIPE, "recipe");
label!(MODE, "mode");
label!(MEMORY_LIMIT, "memory_limit_gb");
label!(SHM_SIZE, "shm_size_gb");
label!(PRIVILEGED, "privileged");
label!(GENERATION, "generation");
label!(RANK, "rank");
label!(WORLD_SIZE, "world_size");
label!(CLUSTER, "cluster");
label!(ROLE, "role");
label!(NODE_RANK, "node_rank");
label!(HEAD_IP, "head_ip");
label!(RAY_ENABLED, "ray_enabled");

/// Every managed container carries this, and every listing filters on it.
pub const MANAGED_FILTER: &str = "spark-pulse.managed=true";

/// Render a float the way Python's `str()` does.
///
/// `str(64.0)` is `"64.0"` and `str(96)` — an int that arrived as a float
/// field — is `"96"`. Rust's `{}` gives `"64"` for 64.0, so a container
/// created here would carry `shm_size_gb=64` where Python wrote `64.0`, and
/// `ContainerMetadata.from_labels` would read them back the same but the
/// *label strings* would differ. They are compared in the contract test, and
/// more importantly an operator diffing two containers should not see noise.
fn py_float(value: f64) -> String {
    if value.fract() == 0.0 && value.abs() < 1e16 {
        format!("{value:.1}")
    } else {
        format!("{value}")
    }
}

/// The label set a container carries, whichever service creates it.
///
/// `created_at` is stamped here when the caller did not supply one, because
/// the label is the only place the creation time survives this process and
/// reconciliation rebuilds deployments from labels alone. A caller that
/// already has a time — the deploy planner stamps one for the whole gang —
/// keeps it, so every rank of a gang shares one timestamp.
pub fn prepare(
    metadata: &mut ContainerMetadata,
    name: &str,
    image: &str,
) -> BTreeMap<String, String> {
    if metadata.image.is_empty() {
        metadata.image = image.to_string();
    }
    if metadata.created_at.as_deref().unwrap_or("").is_empty() {
        metadata.created_at = Some(now_iso8601());
    }
    let mut labels = to_labels(metadata);
    labels.insert(NAME.into(), name.into());
    labels
}

/// Serialise metadata to Docker labels. Mirrors `ContainerMetadata.to_labels`.
pub fn to_labels(metadata: &ContainerMetadata) -> BTreeMap<String, String> {
    let mut labels = BTreeMap::new();
    labels.insert(MANAGED.into(), "true".into());
    labels.insert(DEPLOYMENT.into(), metadata.deployment.clone());
    labels.insert(RECIPE.into(), metadata.recipe.clone());
    labels.insert(IMAGE.into(), metadata.image.clone());
    labels.insert(
        MODE.into(),
        metadata.mode.clone().unwrap_or_else(|| "solo".into()),
    );
    labels.insert(
        CREATED_AT.into(),
        metadata.created_at.clone().unwrap_or_default(),
    );
    labels.insert(VERSION.into(), "1".into());
    labels.insert(
        MEMORY_LIMIT.into(),
        match metadata.memory_limit_gb {
            // `if self.memory_limit_gb` in Python: zero is falsy and writes "".
            Some(gb) if gb != 0.0 => py_float(gb),
            _ => String::new(),
        },
    );
    labels.insert(
        SHM_SIZE.into(),
        py_float(metadata.shm_size_gb.unwrap_or(64.0)),
    );
    labels.insert(
        PRIVILEGED.into(),
        bool_label(metadata.privileged.unwrap_or(true)),
    );

    if metadata.generation != 0 {
        // Last, so no profile or user config can shadow which rank of which
        // attempt this container is.
        labels.insert(DEPLOYMENT.into(), metadata.deployment.clone());
        labels.insert(GENERATION.into(), metadata.generation.to_string());
        labels.insert(RANK.into(), metadata.rank.to_string());
        labels.insert(
            WORLD_SIZE.into(),
            metadata.world_size.unwrap_or(1).to_string(),
        );
    }
    if !metadata.cluster.is_empty() {
        labels.insert(CLUSTER.into(), metadata.cluster.clone());
        labels.insert(ROLE.into(), metadata.role.clone());
        labels.insert(NODE_RANK.into(), metadata.node_rank.to_string());
        labels.insert(RAY_ENABLED.into(), bool_label(metadata.ray_enabled));
        if !metadata.head_ip.is_empty() {
            labels.insert(HEAD_IP.into(), metadata.head_ip.clone());
        }
    }
    labels
}

/// Rebuild metadata from labels. Mirrors `ContainerMetadata.from_labels`.
pub fn from_labels(labels: &BTreeMap<String, String>) -> ContainerMetadata {
    let get = |key: &str| -> String {
        labels
            .get(&format!("{PREFIX}{key}"))
            .cloned()
            .unwrap_or_default()
    };
    // Python's `int(raw) if raw.strip().isdigit() else default` — a negative or
    // non-numeric value falls back rather than raising.
    let int_or = |raw: &str, default: u32| -> u32 {
        let trimmed = raw.trim();
        if !trimmed.is_empty() && trimmed.chars().all(|c| c.is_ascii_digit()) {
            trimmed.parse().unwrap_or(default)
        } else {
            default
        }
    };
    let memory = get("memory_limit_gb");
    let mode = get("mode");
    let created = get("created_at");
    let node_rank = get("node_rank");
    ContainerMetadata {
        deployment: get("deployment"),
        recipe: get("recipe"),
        image: get("image"),
        mode: Some(if mode.is_empty() { "solo".into() } else { mode }),
        created_at: (!created.is_empty()).then_some(created),
        memory_limit_gb: memory.parse::<f64>().ok().filter(|_| !memory.is_empty()),
        shm_size_gb: Some(get("shm_size_gb").parse::<f64>().unwrap_or(64.0)),
        privileged: Some(match get("privileged").as_str() {
            "" => true,
            other => other == "true",
        }),
        generation: int_or(&get("generation"), 0),
        rank: int_or(&get("rank"), 0),
        world_size: Some(int_or(&get("world_size"), 1)),
        cluster: get("cluster"),
        role: get("role"),
        node_rank: int_or(&node_rank, 0),
        head_ip: get("head_ip"),
        ray_enabled: get("ray_enabled") == "true",
    }
}

fn bool_label(value: bool) -> String {
    if value { "true" } else { "false" }.into()
}

/// Docker's `label` filter list: `key` matches presence, `key=value` a value.
///
/// Docker accepts only its own filter keys, so extra label constraints belong
/// inside the `label` list rather than beside it.
pub fn label_filter(wanted: &BTreeMap<String, String>) -> Vec<String> {
    let mut out = vec![MANAGED_FILTER.to_string()];
    for (key, value) in wanted {
        out.push(if value.is_empty() {
            key.clone()
        } else {
            format!("{key}={value}")
        });
    }
    out
}

/// Whether a container's labels satisfy every filter. An empty filter value
/// matches any container carrying that key.
///
/// Applied *after* the daemon's own filter, because Docker's label filter and
/// this have to agree and only one of them is ours.
pub fn labels_match(labels: &BTreeMap<String, String>, wanted: &BTreeMap<String, String>) -> bool {
    wanted.iter().all(|(key, value)| {
        if value.is_empty() {
            labels.contains_key(key)
        } else {
            labels.get(key) == Some(value)
        }
    })
}

/// An RFC 3339 timestamp in the shape Python's
/// `datetime.now(timezone.utc).isoformat()` produces: microseconds, `+00:00`.
fn now_iso8601() -> String {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    let (secs, micros) = (now.as_secs() as i64, now.subsec_micros());
    let (y, mo, d, h, mi, s) = civil_from_unix(secs);
    format!("{y:04}-{mo:02}-{d:02}T{h:02}:{mi:02}:{s:02}.{micros:06}+00:00")
}

/// Days-from-civil, inverted — Howard Hinnant's algorithm. No date crate for
/// one timestamp, and no local timezone to get wrong.
fn civil_from_unix(secs: i64) -> (i64, u32, u32, u32, u32, u32) {
    let days = secs.div_euclid(86_400);
    let rem = secs.rem_euclid(86_400);
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    let year = if m <= 2 { y + 1 } else { y };
    (
        year,
        m,
        d,
        (rem / 3600) as u32,
        ((rem % 3600) / 60) as u32,
        (rem % 60) as u32,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn floats_render_the_way_python_renders_them() {
        // A container created here must carry byte-identical label strings to
        // one created by the Python service, or an operator diffing two ranks
        // of one gang sees a difference that is not there.
        assert_eq!(py_float(64.0), "64.0");
        assert_eq!(py_float(96.0), "96.0");
        assert_eq!(py_float(0.5), "0.5");
    }

    #[test]
    fn metadata_survives_the_round_trip() {
        let mut metadata = ContainerMetadata {
            deployment: "dep-1".into(),
            recipe: "qwen3".into(),
            image: "img:1".into(),
            generation: 3,
            rank: 2,
            world_size: Some(4),
            ..Default::default()
        };
        let labels = prepare(&mut metadata, "c1", "img:1");
        let back = from_labels(&labels);
        assert_eq!(back.deployment, "dep-1");
        assert_eq!(back.generation, 3);
        assert_eq!(back.rank, 2);
        assert_eq!(back.world_size, Some(4));
        assert_eq!(labels[NAME], "c1");
        assert_eq!(labels[MANAGED], "true");
        // Stamped, because reconciliation sorts deployments by it.
        assert!(!labels[CREATED_AT].is_empty());
    }

    #[test]
    fn a_timestamp_is_the_shape_python_writes() {
        let stamp = now_iso8601();
        assert!(stamp.ends_with("+00:00"), "{stamp}");
        assert_eq!(
            stamp.len(),
            "2026-09-05T04:00:00.000000+00:00".len(),
            "{stamp}"
        );
        assert!(stamp.starts_with("20"), "{stamp}");
    }

    #[test]
    fn an_empty_filter_value_matches_presence() {
        let labels: BTreeMap<String, String> = [(CLUSTER.to_string(), "c".to_string())]
            .into_iter()
            .collect();
        let presence: BTreeMap<String, String> =
            [(CLUSTER.to_string(), String::new())].into_iter().collect();
        assert!(labels_match(&labels, &presence));
        let other: BTreeMap<String, String> =
            [(ROLE.to_string(), String::new())].into_iter().collect();
        assert!(!labels_match(&labels, &other));
    }
}
