/** Formatting helpers. These render into the UI verbatim, so the rounding and
 *  the unit are the behaviour, not an implementation detail. */

import { describe, it, expect, afterEach, vi } from "vitest";
import { cn, formatDuration, formatSize, timeAgo } from "@/lib/utils";

describe("cn", () => {
  it("joins the classes that apply and drops the ones that do not", () => {
    expect(cn("px-2", false && "hidden", undefined, "text-sm")).toBe("px-2 text-sm");
  });

  it("accepts the conditional object form", () => {
    expect(cn("base", { active: true, disabled: false })).toBe("base active");
  });
});

describe("formatSize", () => {
  // Nothing on disk is "0.0 B" — an empty cache says "0 B".
  it("says 0 B rather than a rounded zero", () => {
    expect(formatSize(0)).toBe("0 B");
  });

  it("steps up a unit at each power of 1024", () => {
    expect(formatSize(512)).toBe("512.0 B");
    expect(formatSize(1024)).toBe("1.0 KB");
    expect(formatSize(1024 * 1024)).toBe("1.0 MB");
    expect(formatSize(1024 ** 3)).toBe("1.0 GB");
    expect(formatSize(1024 ** 4)).toBe("1.0 TB");
  });

  // An engine image is tens of gigabytes and the pre-flight quotes it to the
  // operator as a wait; one decimal is what makes 26.4 GB readable.
  it("keeps one decimal place", () => {
    expect(formatSize(28_346_055_987)).toBe("26.4 GB");
  });
});

describe("formatDuration", () => {
  it("stays in seconds under a minute", () => {
    expect(formatDuration(0)).toBe("0s");
    expect(formatDuration(59)).toBe("59s");
  });

  it("splits into minutes and seconds under an hour", () => {
    expect(formatDuration(60)).toBe("1m 0s");
    expect(formatDuration(3599)).toBe("59m 59s");
  });

  it("splits into hours and minutes above that, dropping the seconds", () => {
    expect(formatDuration(3600)).toBe("1h 0m");
    expect(formatDuration(7_265)).toBe("2h 1m");
  });
});

describe("timeAgo", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("reads a timestamp as an elapsed duration", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-04T12:00:00Z"));
    expect(timeAgo("2026-09-04T11:58:30Z")).toBe("1m 30s ago");
  });
});
