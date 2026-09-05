/** The engine's own metrics, and the four things this panel refuses to draw.
 *
 * The panel exists because the engines publish real numbers we were throwing
 * away. Its contract is as much about silence as about numbers: when the
 * engine publishes nothing the panel must say why, in words, rather than
 * render an axis with no line on it — and it must never turn a histogram into
 * a percentile, a counter reset into a negative rate, or an overwritten
 * `started_at` into a restart series.
 */

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import EngineMetricsPanel, { engineSeries } from "@/components/EngineMetrics";
import type { EngineMetricSample, EngineMetricsWindow } from "@/lib/types";

const T0 = 1_700_000_000;

function sample(over: Partial<EngineMetricSample> = {}): EngineMetricSample {
  return {
    t: T0,
    running: 2,
    waiting: 0,
    kv_fraction: 0.42,
    prompt_tokens_total: 1000,
    generation_tokens_total: 500,
    preemptions_total: 3,
    prompt_tokens_per_second: 34,
    generation_tokens_per_second: 19,
    preemptions_per_second: 0,
    counter_reset: false,
    ...over,
  };
}

function window(over: Partial<EngineMetricsWindow> = {}): EngineMetricsWindow {
  return {
    deployment_id: "dep-1",
    available: true,
    reason: null,
    detail: null,
    sample_interval_seconds: 5,
    window_seconds: 3600,
    volatile: true,
    samples: [sample(), sample({ t: T0 + 5, running: 4, waiting: 7 })],
    ...over,
  };
}

describe("before anything has been read", () => {
  it("says it is reading while the first request is in flight", () => {
    render(<EngineMetricsPanel window={null} loading />);

    expect(screen.getByText("Reading the engine's metrics…")).toBeInTheDocument();
  });

  it("says there is nothing yet when it is not", () => {
    render(<EngineMetricsPanel window={null} />);

    expect(screen.getByText("No engine metrics yet.")).toBeInTheDocument();
  });
});

describe("when the engine publishes nothing", () => {
  it("gives the operator the reason instead of an empty chart", () => {
    render(
      <EngineMetricsPanel
        window={window({
          available: false,
          reason: "not_enabled",
          detail:
            "SGLang serves /metrics only when it is started with --enable-metrics.",
          samples: [],
        })}
      />,
    );

    expect(screen.getByText("Engine metrics unavailable")).toBeInTheDocument();
    expect(screen.getByText(/--enable-metrics/)).toBeInTheDocument();
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.queryByText("Running")).toBeNull();
  });

  it("still says something when the backend gave no sentence", () => {
    render(
      <EngineMetricsPanel
        window={window({ available: false, reason: "unreachable", detail: null, samples: [] })}
      />,
    );

    expect(
      screen.getByText("The engine published nothing this build could read."),
    ).toBeInTheDocument();
  });

  it("does not draw samples that arrived before it went unavailable", () => {
    render(
      <EngineMetricsPanel
        window={window({ available: false, reason: "not_running", detail: "Stopped." })}
      />,
    );

    expect(screen.getByTestId("engine-metrics-unavailable")).toBeInTheDocument();
    expect(screen.queryByRole("img")).toBeNull();
  });
});

describe("the live gauges", () => {
  it("shows the newest reading of each one", () => {
    render(<EngineMetricsPanel window={window()} />);

    expect(screen.getByText("Running").parentElement).toHaveTextContent("4");
    expect(screen.getByText("Queued").parentElement).toHaveTextContent("7");
    expect(screen.getByText("Preemptions").parentElement).toHaveTextContent("3");
  });

  it("shows the KV fraction as a percentage, which is a change of unit", () => {
    render(<EngineMetricsPanel window={window()} />);

    expect(screen.getByText("KV cache").parentElement).toHaveTextContent("42.0%");
  });

  it("shows an em dash rather than a zero for a number the engine did not give", () => {
    render(
      <EngineMetricsPanel
        window={window({
          samples: [
            sample(),
            sample({
              t: T0 + 5,
              running: null,
              waiting: null,
              kv_fraction: null,
              preemptions_total: null,
            }),
          ],
        })}
      />,
    );

    expect(screen.getByText("Running").parentElement).toHaveTextContent("—");
    expect(screen.getByText("KV cache").parentElement).toHaveTextContent("—");
    expect(screen.getByText("Preemptions").parentElement).toHaveTextContent("—");
  });

  it("counts the samples and names the cadence", () => {
    render(<EngineMetricsPanel window={window()} />);

    expect(screen.getByText("2 samples, every 5s")).toBeInTheDocument();
  });

  it("does not pluralise a single sample", () => {
    render(<EngineMetricsPanel window={window({ samples: [sample()] })} />);

    expect(screen.getByText("1 sample, every 5s")).toBeInTheDocument();
  });
});

describe("the chart", () => {
  it("will not draw a line from one sample, and says why", () => {
    render(<EngineMetricsPanel window={window({ samples: [sample()] })} />);

    expect(
      screen.getByText(
        "One sample so far — a second is needed before a rate or a line exists.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("draws the gauges and the differenced rates once there are two", () => {
    render(<EngineMetricsPanel window={window()} />);

    expect(screen.getByRole("heading", { name: "Recent load" })).toBeInTheDocument();
    expect(screen.getByText("Requests running")).toBeInTheDocument();
    expect(screen.getByText("Queue depth")).toBeInTheDocument();
    expect(screen.getByText("KV cache used")).toBeInTheDocument();
    expect(screen.getByText("Output tokens/s")).toBeInTheDocument();
  });

  it("says out loud that the window is memory only", () => {
    render(<EngineMetricsPanel window={window()} />);

    expect(
      screen.getByText(/Restarting Spark Pulse loses this window/),
    ).toBeInTheDocument();
  });

  it("says why there are no percentiles rather than inventing one", () => {
    render(<EngineMetricsPanel window={window()} />);

    expect(
      screen.getByText(/Latency percentiles are not shown/),
    ).toBeInTheDocument();
  });
});

describe("a counter reset", () => {
  const reset = window({
    samples: [
      sample(),
      sample({
        t: T0 + 5,
        prompt_tokens_total: 4,
        generation_tokens_total: 2,
        prompt_tokens_per_second: null,
        generation_tokens_per_second: null,
        counter_reset: true,
      }),
      sample({ t: T0 + 10, generation_tokens_per_second: 12 }),
    ],
  });

  it("is explained as an engine restart, not drawn as a dip", () => {
    render(<EngineMetricsPanel window={reset} />);

    expect(screen.getByTestId("engine-metrics-reset")).toHaveTextContent(
      /counters restarted 1 time/,
    );
    expect(screen.getByTestId("engine-metrics-reset")).toHaveTextContent(
      /unknown rather than zero/,
    );
  });

  it("pluralises several resets", () => {
    render(
      <EngineMetricsPanel
        window={window({
          samples: [
            sample(),
            sample({ t: T0 + 5, counter_reset: true }),
            sample({ t: T0 + 10, counter_reset: true }),
          ],
        })}
      />,
    );

    expect(screen.getByTestId("engine-metrics-reset")).toHaveTextContent(
      /restarted 2 times/,
    );
  });

  it("says nothing about resets when the counters only grew", () => {
    render(<EngineMetricsPanel window={window()} />);

    expect(screen.queryByTestId("engine-metrics-reset")).toBeNull();
  });

  it("breaks the rate line at the reset", () => {
    const [outputs] = engineSeries(reset).filter((s) => s.label === "Output tokens/s");

    // The reset sample carries no rate, so it is not a point at all; the
    // series declares the instant so the line is cut rather than bridged.
    expect(outputs.samples.map((s) => s.t)).toEqual([T0 * 1000, (T0 + 10) * 1000]);
    expect(outputs.breaks).toContain((T0 + 5) * 1000);
  });
});

describe("engineSeries", () => {
  it("puts the timestamps in milliseconds, because the chart's axis is", () => {
    const [running] = engineSeries(window());

    expect(running.samples[0].t).toBe(T0 * 1000);
  });

  it("scales the KV fraction to a percentage and leaves the counts alone", () => {
    const series = engineSeries(window());
    const kv = series.find((s) => s.label === "KV cache used")!;
    const running = series.find((s) => s.label === "Requests running")!;

    expect(kv.samples[0].value).toBeCloseTo(42);
    expect(running.samples[0].value).toBe(2);
  });

  it("drops a series the engine never reported rather than charting zeroes", () => {
    const series = engineSeries(
      window({
        samples: [
          sample({ waiting: null, kv_fraction: null }),
          sample({ t: T0 + 5, waiting: null, kv_fraction: null }),
        ],
      }),
    );

    expect(series.map((s) => s.label)).not.toContain("Queue depth");
    expect(series.map((s) => s.label)).not.toContain("KV cache used");
    expect(series.map((s) => s.label)).toContain("Requests running");
  });

  it("keeps a sample whose value is zero, which is a measurement", () => {
    const series = engineSeries(
      window({
        samples: [sample({ waiting: 0 }), sample({ t: T0 + 5, waiting: 0 })],
      }),
    );

    expect(series.find((s) => s.label === "Queue depth")!.samples).toHaveLength(2);
  });
});
