//! What this machine is doing right now.
//!
//! `facts` answers what the node *is* — GPU count, memory size, kernel — once,
//! at enrolment. This answers what it is doing: utilisation, temperature, free
//! memory, the processes holding it. The monitoring page asks every node the
//! same question, and the machine the control plane happens to run on is not a
//! different kind of node.
//!
//! Two rules, both inherited from `facts`:
//!
//! **Nothing here may fail.** A node without `nvidia-smi` still answers, with
//! an empty GPU list and a line in `unavailable` saying why. A panel that
//! shows nothing is a worse answer than one that says what it could not read,
//! and an agent that refuses to report because one probe failed is the worst
//! answer of all.
//!
//! **An absent measurement stays absent.** A DGX Spark's `nvidia-smi` reports
//! `[N/A]` for GPU memory, because the pool is unified. Reporting that as zero
//! would make the monitoring page say a full machine is empty, which is the
//! same defect the pre-flight had to grow a special case for.

use std::fs;
use std::process::Command;

use crate::proto::{DiskStat, GpuProcess, GpuStat, MemoryStat, NodeStats};

/// Fields asked of `nvidia-smi`, in order.
const GPU_QUERY: &str = "index,name,uuid,memory.total,memory.used,memory.free,\
utilization.gpu,temperature.gpu,power.draw";

const PROCESS_QUERY: &str = "pid,process_name,used_gpu_memory";

/// Mount points worth reporting, in the order an operator cares about them.
///
/// Not every mount: a Spark has dozens of overlay and tmpfs entries and none
/// of them is the disk that fills up. These are where images, models and the
/// control plane's own state live.
const MOUNTS: &[&str] = &["/", "/var/lib/docker", "/home"];

pub fn collect() -> NodeStats {
    let mut unavailable = Vec::new();

    let gpus = match nvidia_smi(&["--query-gpu", GPU_QUERY]) {
        Ok(rows) => rows.iter().filter_map(|row| gpu_from(row)).collect(),
        Err(reason) => {
            unavailable.push(format!("GPUs: {reason}"));
            Vec::new()
        }
    };

    let processes = match nvidia_smi(&["--query-compute-apps", PROCESS_QUERY]) {
        Ok(rows) => rows.iter().filter_map(|row| process_from(row)).collect(),
        Err(reason) => {
            unavailable.push(format!("GPU processes: {reason}"));
            Vec::new()
        }
    };

    let memory = match meminfo() {
        Some(memory) => Some(memory),
        None => {
            unavailable.push("memory: /proc/meminfo is unreadable".to_string());
            None
        }
    };

    NodeStats {
        gpus,
        memory,
        disks: disks(),
        processes,
        cpu_count: num_cpus(),
        load_average_1m: load_average().unwrap_or(0.0),
        unavailable,
    }
}

/// Run one `nvidia-smi` query and split its CSV into fields.
///
/// The binary being absent is an answer, not an error to propagate: plenty of
/// machines that run this agent have no NVIDIA driver, and they still have
/// memory and disks worth reporting.
fn nvidia_smi(args: &[&str]) -> Result<Vec<Vec<String>>, String> {
    let output = Command::new("nvidia-smi")
        .args(args)
        .arg("--format=csv,noheader,nounits")
        .output()
        .map_err(|error| format!("nvidia-smi could not be run ({error})"))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        let first = stderr.lines().next().unwrap_or("no output").trim();
        return Err(format!("nvidia-smi failed: {first}"));
    }

    Ok(String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter(|line| !line.trim().is_empty())
        .map(|line| line.split(',').map(|f| f.trim().to_string()).collect())
        .collect())
}

/// `[N/A]`, `[Not Supported]` and an empty field all mean "no measurement".
fn measured(field: Option<&String>) -> Option<&str> {
    let value = field?.trim();
    if value.is_empty() || value.starts_with('[') {
        return None;
    }
    Some(value)
}

fn mib(field: Option<&String>) -> Option<u64> {
    measured(field)?
        .parse::<f64>()
        .ok()
        .map(|v| (v * 1_048_576.0) as u64)
}

fn number(field: Option<&String>) -> Option<f64> {
    measured(field)?.parse::<f64>().ok()
}

fn gpu_from(row: &[String]) -> Option<GpuStat> {
    if row.len() < 3 {
        return None;
    }
    Some(GpuStat {
        index: measured(row.first())?.parse().ok()?,
        name: row.get(1).cloned().unwrap_or_default(),
        uuid: row.get(2).cloned().unwrap_or_default(),
        memory_total_bytes: mib(row.get(3)),
        memory_used_bytes: mib(row.get(4)),
        memory_free_bytes: mib(row.get(5)),
        utilization_percent: number(row.get(6)),
        temperature_celsius: number(row.get(7)),
        power_watts: number(row.get(8)),
    })
}

fn process_from(row: &[String]) -> Option<GpuProcess> {
    if row.len() < 2 {
        return None;
    }
    let pid: u32 = measured(row.first())?.parse().ok()?;
    Some(GpuProcess {
        pid,
        name: row.get(1).cloned().unwrap_or_default(),
        used_memory_bytes: mib(row.get(2)),
        container_id: container_of(pid).unwrap_or_default(),
    })
}

/// The container a pid belongs to, from its cgroup.
///
/// The control plane decides whether that container is one of *its* own — it
/// is the only side that knows what it started. This just says which container
/// the process is in, or nothing.
fn container_of(pid: u32) -> Option<String> {
    let cgroup = fs::read_to_string(format!("/proc/{pid}/cgroup")).ok()?;
    let marker = cgroup.find("docker-")?;
    let rest = &cgroup[marker + "docker-".len()..];
    let id: String = rest.chars().take_while(|c| c.is_ascii_hexdigit()).collect();
    (id.len() >= 12).then(|| id[..12].to_string())
}

fn meminfo() -> Option<MemoryStat> {
    let text = fs::read_to_string("/proc/meminfo").ok()?;
    let mut total = 0_u64;
    let mut available = 0_u64;
    for line in text.lines() {
        let (key, rest) = line.split_once(':')?;
        let kb: u64 = rest.split_whitespace().next()?.parse().unwrap_or(0);
        match key {
            "MemTotal" => total = kb * 1024,
            "MemAvailable" => available = kb * 1024,
            _ => {}
        }
    }
    (total > 0).then_some(MemoryStat {
        total_bytes: total,
        used_bytes: total.saturating_sub(available),
        available_bytes: available,
    })
}

fn disks() -> Vec<DiskStat> {
    MOUNTS.iter().filter_map(|mount| disk(mount)).collect()
}

fn disk(mount: &str) -> Option<DiskStat> {
    let output = Command::new("df")
        .args(["-B1", "--output=size,used,avail", mount])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&output.stdout);
    let fields: Vec<u64> = text
        .lines()
        .nth(1)?
        .split_whitespace()
        .filter_map(|f| f.parse().ok())
        .collect();
    (fields.len() == 3).then(|| DiskStat {
        mount: mount.to_string(),
        total_bytes: fields[0],
        used_bytes: fields[1],
        free_bytes: fields[2],
    })
}

fn num_cpus() -> u32 {
    std::thread::available_parallelism()
        .map(|n| n.get() as u32)
        .unwrap_or(0)
}

fn load_average() -> Option<f64> {
    fs::read_to_string("/proc/loadavg")
        .ok()?
        .split_whitespace()
        .next()?
        .parse()
        .ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_unified_memory_gpu_reports_no_memory_rather_than_zero() {
        // What a DGX Spark's nvidia-smi actually prints.
        let row: Vec<String> = "0, NVIDIA GB10, GPU-abc, [N/A], [N/A], [N/A], 12, 41, [N/A]"
            .split(',')
            .map(|f| f.trim().to_string())
            .collect();

        let gpu = gpu_from(&row).expect("a row with an index is a GPU");

        assert_eq!(gpu.name, "NVIDIA GB10");
        assert_eq!(gpu.memory_total_bytes, None);
        assert_eq!(gpu.utilization_percent, Some(12.0));
        assert_eq!(gpu.temperature_celsius, Some(41.0));
        assert_eq!(gpu.power_watts, None);
    }

    #[test]
    fn a_gpu_that_reports_memory_reports_it_in_bytes() {
        let row: Vec<String> = "0, NVIDIA A100, GPU-x, 81920, 1024, 80896, 0, 35, 42.5"
            .split(',')
            .map(|f| f.trim().to_string())
            .collect();

        let gpu = gpu_from(&row).unwrap();

        assert_eq!(gpu.memory_total_bytes, Some(81920 * 1_048_576));
        assert_eq!(gpu.memory_used_bytes, Some(1024 * 1_048_576));
        assert_eq!(gpu.power_watts, Some(42.5));
    }

    #[test]
    fn a_truncated_row_is_skipped_rather_than_half_read() {
        let row: Vec<String> = vec!["0".into()];
        assert!(gpu_from(&row).is_none());
    }

    #[test]
    fn a_process_row_carries_its_pid_and_memory() {
        let row: Vec<String> = "2957747, python3, 24576"
            .split(',')
            .map(|f| f.trim().to_string())
            .collect();

        let process = process_from(&row).unwrap();

        assert_eq!(process.pid, 2957747);
        assert_eq!(process.name, "python3");
        assert_eq!(process.used_memory_bytes, Some(24576 * 1_048_576));
    }

    #[test]
    fn collecting_never_panics_without_a_driver() {
        // The point of the module: a node with no nvidia-smi still answers.
        let stats = collect();
        assert!(stats.cpu_count > 0);
    }
}
