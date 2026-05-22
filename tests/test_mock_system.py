from spark_pulse.mock import system


def test_enrich_gpu_process_tracking_marks_running_deployment_pids() -> None:
    processes = [
        {"pid": 101, "process_name": "engine-a"},
        {"pid": 202, "process_name": "engine-b"},
    ]
    running_deployments = [
        {"pid": 202, "status": "running"},
        {"pid": 303, "status": "pending"},
    ]

    system.enrich_gpu_process_tracking(processes, running_deployments)

    assert processes == [
        {"pid": 101, "process_name": "engine-a", "is_tracked": False},
        {"pid": 202, "process_name": "engine-b", "is_tracked": True},
    ]
