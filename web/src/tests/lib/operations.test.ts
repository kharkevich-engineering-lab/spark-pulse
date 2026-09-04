/** The operation state machine.
 *
 * This is the one rule every long-running workflow shares — a deploy of any
 * node count, a mod application, a reconciliation. What it is for is refusing
 * the transitions that would let a finished operation come back to life: a
 * cancel button on a deployment that already succeeded, a retry that restarts
 * something already rolled back. So the table is asserted whole rather than
 * one happy path at a time; a transition quietly added to a terminal state is
 * exactly the regression worth catching.
 */

import { describe, it, expect } from "vitest";
import { canTransition, OperationState, VALID_TRANSITIONS } from "@/lib/operations";

const ALL_STATES = Object.values(OperationState);

/** States nothing may follow: the operation is over. */
const TERMINAL = [
  OperationState.SUCCESS,
  OperationState.ROLLED_BACK,
  OperationState.CANCELLED,
];

describe("canTransition", () => {
  it("walks the ordinary life of an operation", () => {
    expect(canTransition(OperationState.IDLE, OperationState.PENDING)).toBe(true);
    expect(canTransition(OperationState.PENDING, OperationState.RUNNING)).toBe(true);
    expect(canTransition(OperationState.RUNNING, OperationState.SUCCESS)).toBe(true);
  });

  it("lets a failure roll back or be retried", () => {
    expect(canTransition(OperationState.FAILED, OperationState.ROLLING_BACK)).toBe(true);
    expect(canTransition(OperationState.FAILED, OperationState.PENDING)).toBe(true);
    expect(canTransition(OperationState.ROLLING_BACK, OperationState.ROLLED_BACK)).toBe(true);
  });

  it("allows a cancel only while something is still in flight", () => {
    expect(canTransition(OperationState.PENDING, OperationState.CANCELLED)).toBe(true);
    expect(canTransition(OperationState.RUNNING, OperationState.CANCELLED)).toBe(true);
    expect(canTransition(OperationState.IDLE, OperationState.CANCELLED)).toBe(false);
    expect(canTransition(OperationState.FAILED, OperationState.CANCELLED)).toBe(false);
  });

  it.each(TERMINAL)("leaves %s final", (state) => {
    expect(VALID_TRANSITIONS[state]).toEqual([]);
    for (const target of ALL_STATES) {
      expect(canTransition(state, target)).toBe(false);
    }
  });

  it("refuses the jumps that skip the work", () => {
    expect(canTransition(OperationState.IDLE, OperationState.RUNNING)).toBe(false);
    expect(canTransition(OperationState.PENDING, OperationState.SUCCESS)).toBe(false);
    expect(canTransition(OperationState.RUNNING, OperationState.ROLLED_BACK)).toBe(false);
    expect(canTransition(OperationState.ROLLING_BACK, OperationState.RUNNING)).toBe(false);
  });

  it("never claims a state can transition to itself", () => {
    for (const state of ALL_STATES) {
      expect(canTransition(state, state)).toBe(false);
    }
  });

  // The table is indexed by a value that arrives over SSE, so a state the
  // frontend has never heard of has to read as "no transitions", not throw.
  it("answers false for a state that is not in the table at all", () => {
    expect(canTransition("teleported" as OperationState, OperationState.RUNNING)).toBe(false);
  });

  it("declares every state, so none can be reached with no rule", () => {
    expect(Object.keys(VALID_TRANSITIONS).sort()).toEqual([...ALL_STATES].sort());
    for (const targets of Object.values(VALID_TRANSITIONS)) {
      for (const target of targets) {
        expect(ALL_STATES).toContain(target);
      }
    }
  });
});
