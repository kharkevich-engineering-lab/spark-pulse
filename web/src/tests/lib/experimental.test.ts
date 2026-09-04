/** The multi-node experimental copy: the one place the UI admits what has not
 * been run, and the one function that decides what counts as multi-node.
 *
 * These assertions are deliberately about content. The module exists so an
 * operator reads named risks instead of the word "experimental", and the only
 * thing that stops it decaying back into a disclaimer is a test that fails
 * when a risk is dropped or softened. Striking an item is allowed — once it
 * has been observed on hardware — and is meant to be a deliberate edit here as
 * well as there.
 *
 * The split between the two lists is the point of the module now. "We know
 * what this should do and have not run it" and "nobody knows what this should
 * do" are different risks, and a list that runs them together tells an
 * operator neither.
 */

import { describe, expect, it } from "vitest";
import {
  MULTI_NODE_BADGE_TITLE,
  MULTI_NODE_REASON,
  MULTI_NODE_SPECIFIED,
  MULTI_NODE_TITLE,
  MULTI_NODE_UNPROVEN,
  MULTI_NODE_UNSPECIFIED,
  nodeCount,
} from "@/lib/experimental";

describe("the unproven list", () => {
  it("is not empty, because nothing has been observed on hardware yet", () => {
    expect(MULTI_NODE_UNPROVEN.length).toBeGreaterThan(0);
  });

  it("states every item as a specific claim, not as the word experimental", () => {
    for (const item of MULTI_NODE_UNPROVEN) {
      expect(item).not.toMatch(/^\s*experimental\.?\s*$/i);
      // A risk an operator can act on is a sentence, not a label.
      expect(item.length).toBeGreaterThan(40);
      expect(item.trim()).toMatch(/\.$/);
      expect(item.trim().split(/\s+/).length).toBeGreaterThan(6);
    }
  });

  it("names the subsystems that have never met a second machine", () => {
    const prose = MULTI_NODE_UNPROVEN.join(" ").toLowerCase();
    // Each of these is a distinct failure mode with its own remedy. Striking
    // one is a hardware claim, so it must be a deliberate edit here too.
    expect(prose).toMatch(/rendezvous/);
    expect(prose).toMatch(/nccl/);
    expect(prose).toMatch(/interface pinning/);
    expect(prose).toMatch(/ssh/);
    expect(prose).toMatch(/three or four nodes/);
  });

  it("repeats no item, because the banner keys its list by the text", () => {
    expect(new Set(MULTI_NODE_UNPROVEN).size).toBe(MULTI_NODE_UNPROVEN.length);
  });
});

describe("unproven and unknown are shown as different things", () => {
  it("keeps both kinds populated and disjoint", () => {
    expect(MULTI_NODE_SPECIFIED.length).toBeGreaterThan(0);
    expect(MULTI_NODE_UNSPECIFIED.length).toBeGreaterThan(0);
    const overlap = MULTI_NODE_SPECIFIED.filter((item) =>
      MULTI_NODE_UNSPECIFIED.includes(item),
    );
    expect(overlap).toEqual([]);
  });

  it("renders every item under exactly one of the two labels", () => {
    expect(MULTI_NODE_UNPROVEN.length).toBe(
      MULTI_NODE_SPECIFIED.length + MULTI_NODE_UNSPECIFIED.length,
    );
    for (const item of MULTI_NODE_UNPROVEN) {
      expect(item).toMatch(/^(Specified, unconfirmed|Documented nowhere) — /);
    }
  });

  it("labels the settled ones as settled and the unknown ones as unknown", () => {
    const specified = MULTI_NODE_UNPROVEN.filter((i) =>
      i.startsWith("Specified, unconfirmed"),
    );
    const unspecified = MULTI_NODE_UNPROVEN.filter((i) =>
      i.startsWith("Documented nowhere"),
    );
    expect(specified.length).toBe(MULTI_NODE_SPECIFIED.length);
    expect(unspecified.length).toBe(MULTI_NODE_UNSPECIFIED.length);
  });

  it("keeps the two questions an operator would actually ask apart", () => {
    const settled = MULTI_NODE_SPECIFIED.join(" ").toLowerCase();
    const unknown = MULTI_NODE_UNSPECIFIED.join(" ").toLowerCase();
    // A published flag set we have implemented, versus a knob nobody
    // documents. These must not drift into the same bucket.
    expect(settled).toMatch(/rendezvous/);
    expect(unknown).toMatch(/nccl_ib_merge_nics/);
    expect(unknown).toMatch(/start order|starting workers/);
    expect(settled).not.toMatch(/nccl_ib_merge_nics/);
  });
});

describe("the reason", () => {
  it("says why nothing has been run: there is only one machine", () => {
    expect(MULTI_NODE_REASON).toMatch(/only one DGX Spark/i);
    expect(MULTI_NODE_REASON).toMatch(/two machines/i);
  });

  it("distinguishes what is exercised in simulation from what is observed", () => {
    expect(MULTI_NODE_REASON).toMatch(/simulation/i);
    expect(MULTI_NODE_REASON).toMatch(/observed/i);
  });

  it("explains the two groups before the list starts", () => {
    expect(MULTI_NODE_REASON).toMatch(/published source/i);
    expect(MULTI_NODE_REASON).toMatch(/documented nowhere/i);
  });

  it("hands off to the list rather than trailing away as a paragraph", () => {
    expect(MULTI_NODE_REASON.trim()).toMatch(/:$/);
  });
});

describe("the title and the tooltip", () => {
  it("admits the feature is unverified rather than calling it verified", () => {
    expect(MULTI_NODE_TITLE).toMatch(/unverified|not verified|unproven/i);
    expect(MULTI_NODE_TITLE).toMatch(/implemented/i);
  });

  it("gives the one-line tooltip the same reason as the banner", () => {
    expect(MULTI_NODE_BADGE_TITLE).toMatch(/never been run on two machines/i);
    expect(MULTI_NODE_BADGE_TITLE).toMatch(/no second DGX Spark/i);
  });

  it("claims hardware verification nowhere", () => {
    const prose = [MULTI_NODE_TITLE, MULTI_NODE_BADGE_TITLE, MULTI_NODE_REASON]
      .join(" ")
      .toLowerCase();
    expect(prose).not.toMatch(/verified on (real )?hardware/);
    expect(prose).not.toMatch(/tested on two machines/);
  });
});

describe("nodeCount", () => {
  it("counts a record with no multi-node fields as this machine alone", () => {
    expect(nodeCount({})).toBe(1);
  });

  it("takes the recorded count when there is one", () => {
    expect(nodeCount({ node_count: 4, nodes: ["a", "b"] })).toBe(4);
  });

  it("falls back to the named nodes when the count is missing or zero", () => {
    expect(nodeCount({ nodes: ["10.0.0.10", "10.0.0.11", "10.0.0.12"] })).toBe(3);
    expect(nodeCount({ node_count: 0, nodes: ["10.0.0.10", "10.0.0.11"] })).toBe(2);
  });

  it("treats nulls as absent rather than as zero nodes", () => {
    expect(nodeCount({ node_count: null, nodes: null })).toBe(1);
  });

  it("never reports zero, which would read as a deployment running nowhere", () => {
    expect(nodeCount({ node_count: 0, nodes: [] })).toBe(1);
  });
});
