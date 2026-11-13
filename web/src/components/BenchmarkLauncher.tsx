/** The one way to start a benchmark: from the run it will measure.
 *
 * There were two. The Runs page had a confirm — "benchmark this?" — that ran a
 * fixed throughput-and-latency pass with no options at all. The Benchmarking
 * page had a form with the options, and its first field was a free-text box
 * asking the operator to type a deployment id, followed by a second asking for
 * a recipe id, neither validated against anything. Typing an id that does not
 * exist is not a choice a page should offer; the run is right there on the
 * list, so the launcher opens from it and the target is settled before the
 * dialog appears.
 *
 * What survives from the form is what was actually a choice: which metrics to
 * measure, what context length to measure them at, and which earlier run to
 * diff against.
 */

import { Link } from "react-router-dom";
import { useState } from "react";
import { useI18n } from "@/lib/i18n";
import { runBenchmark } from "@/lib/api";
import { Button, ErrorLine, Field, Input, Modal } from "@/ui";
import { Flame } from "lucide-react";
import { servesChat } from "@/lib/utils";
import type { Deployment } from "@/lib/types";

/** Every metric the backend knows how to measure. */
export const BENCHMARK_TYPES = [
  "throughput",
  "latency",
  "gpu_memory",
  "gpu_utilization",
  "prefill_speed",
] as const;

const DEFAULT_TYPES = ["throughput", "latency"];
const DEFAULT_CONTEXT = 4096;

export interface BenchmarkLauncherProps {
  run: Deployment;
  onClose: () => void;
  /** Fired once the backend has accepted the run. */
  onStarted: () => void;
  /** Whether benchmarking is on (Settings → Features). Off, the dialog says
   *  so and where to turn it on, and does not start what the API refuses. */
  enabled?: boolean;
  /** Whether the benchmark tool is installed; missing, the dialog says what
   *  to install, at the click. Absent is treated as installed. */
  tool?: { available: boolean; reason: string; install: string };
}

export default function BenchmarkLauncher({ run, onClose, onStarted, enabled = true, tool }: BenchmarkLauncherProps) {
  const missing = tool && !tool.available ? tool : null;
  const [copied, setCopied] = useState(false);
  const { t } = useI18n();
  const [types, setTypes] = useState<string[]>(DEFAULT_TYPES);
  const [contextLength, setContextLength] = useState(DEFAULT_CONTEXT);
  const [baseline, setBaseline] = useState("");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The row does not offer this for a run that serves something other than
  // chat, but the launcher is a component somebody else can open; saying why
  // here beats a 409 after the click.
  const chat = servesChat(run.serves);

  const start = async () => {
    setRunning(true);
    setError(null);
    try {
      await runBenchmark({
        deployment_id: run.id,
        baseline_id: baseline || undefined,
        recipe_id: run.recipe_id,
        recipe_name: run.name,
        params: { benchmarks: types, context_length: contextLength },
      });
      onStarted();
    } catch (e) {
      // The dialog stays up: the operator's metric choices are still in it,
      // and a refused run is usually one field away from an accepted one.
      setError(e instanceof Error ? e.message : t("benchmarking.runFailed"));
    } finally {
      setRunning(false);
    }
  };

  return (
    <Modal
      open
      onClose={() => !running && onClose()}
      title={t("runs.benchmarkTitle")}
      icon={<Flame size={20} className="text-blue2" />}
      actions={
        <>
          <Button size="sm" onClick={onClose} disabled={running}>
            {t("common.cancel")}
          </Button>
          <Button
            size="sm"
            variant="primary"
            loading={running}
            disabled={types.length === 0 || !chat || !enabled || Boolean(missing)}
            onClick={start}
          >
            {running ? t("common.working") : t("runs.benchmarkRun")}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <p className="text-[14px] text-muted">{t("runs.benchmarkOn", { name: run.name })}</p>
        {missing && (
          <div
            data-testid="benchmarking-missing"
            className="space-y-2 rounded-sm border border-warning/30 bg-warning/10 p-3 text-[14px] text-warning"
          >
            <p>{t("benchmarking.toolMissing")}</p>
            <button
              type="button"
              onClick={() => {
                void navigator.clipboard?.writeText(missing.install).then(() => {
                  setCopied(true);
                  setTimeout(() => setCopied(false), 1800);
                });
              }}
              aria-label={t("benchmarking.copyInstall", { command: missing.install })}
              className="block w-full rounded-sm border border-line bg-bg px-3 py-2 text-left font-mono text-[13px] text-text hover:border-blue2"
            >
              {copied ? t("benchmarking.installCopied") : missing.install}
            </button>
            <p className="text-[13px] text-muted">{t("benchmarking.toolMissingHint")}</p>
          </div>
        )}
        {!enabled && (
          <p
            data-testid="benchmarking-off"
            className="rounded-sm border border-warning/30 bg-warning/10 p-3 text-[14px] text-warning"
          >
            {t("benchmarking.offNotice")}{" "}
            <Link to="/settings#features" className="font-medium underline" onClick={onClose}>
              {t("benchmarking.turnOn")}
            </Link>
          </p>
        )}
        {!chat && <ErrorLine>{t("serves.benchmarkChatOnly")}</ErrorLine>}

        <div>
          <p className="mb-1.5 text-[13px] font-medium">{t("benchmarking.types")}</p>
          <div className="flex flex-wrap gap-x-4 gap-y-2">
            {BENCHMARK_TYPES.map((type) => (
              <label key={type} className="flex cursor-pointer items-center gap-1.5 text-[14px]">
                <input
                  type="checkbox"
                  className="accent-[var(--blue)]"
                  checked={types.includes(type)}
                  onChange={(e) =>
                    setTypes((prev) =>
                      e.target.checked ? [...prev, type] : prev.filter((x) => x !== type),
                    )
                  }
                />
                <span className="text-muted">{type.replace(/_/g, " ")}</span>
              </label>
            ))}
          </div>
        </div>

        <Field label={t("benchmarking.contextLength")}>
          {(control) => (
            <Input
              {...control}
              mono
              type="number"
              className="w-32"
              value={contextLength}
              onChange={(e) => setContextLength(parseInt(e.target.value) || DEFAULT_CONTEXT)}
            />
          )}
        </Field>

        <Field label={t("benchmarking.baseline")}>
          {(control) => (
            <Input
              {...control}
              mono
              type="text"
              value={baseline}
              onChange={(e) => setBaseline(e.target.value)}
              placeholder={t("benchmarking.baselinePlaceholder")}
            />
          )}
        </Field>

        <ErrorLine>{error}</ErrorLine>
      </div>
    </Modal>
  );
}
