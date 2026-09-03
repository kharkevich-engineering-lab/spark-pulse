import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  cancelModelDownload,
  deleteModel,
  fetchModel,
  fetchModelDownload,
  fetchModelDownloads,
  fetchModelPresence,
  fetchModelSources,
  fetchModels,
  saveModelSources,
  startModelDownload,
  syncModelToNodes,
} from "@/lib/api";
import type { ModelEntry, ModelSource } from "@/lib/types";

function mockJson(body: unknown, status = 200) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const model: ModelEntry = {
  id: "acme/plain-7b",
  source: "hf",
  source_type: "hf_cache",
  path: "/hub/models--acme--plain-7b/snapshots/aaaa",
  revision: "aaaabbbbccccdddd",
  revisions: [],
  size_bytes: 4096,
  last_modified: "2026-01-01T00:00:00+00:00",
  config: null,
  referenced_by: [],
};

describe("models api", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("unwraps the catalogue envelope", async () => {
    const fetchMock = mockJson({ models: [model] });
    await expect(fetchModels()).resolves.toEqual([model]);
    expect(fetchMock).toHaveBeenCalledWith("/api/models", expect.objectContaining({ credentials: "include" }));
  });

  it("fetches a single model by nested id", async () => {
    const fetchMock = mockJson(model);
    await expect(fetchModel("acme/plain-7b")).resolves.toEqual(model);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/models/acme/plain-7b");
  });

  it("unwraps the sources envelope", async () => {
    const sources: ModelSource[] = [{ name: "hf", type: "hf_hub", endpoint: "https://huggingface.co" }];
    mockJson({ sources });
    await expect(fetchModelSources()).resolves.toEqual(sources);
  });

  it("PUTs sources wrapped in an envelope", async () => {
    const sources: ModelSource[] = [{ name: "local", type: "local_path", path: "/models" }];
    const fetchMock = mockJson({ sources });
    await expect(saveModelSources(sources)).resolves.toEqual(sources);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/models/sources");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(init.body)).toEqual({ sources });
  });

  it("posts a download request", async () => {
    const fetchMock = mockJson({ id: "job1", status: "queued" });
    await startModelDownload({ model: "acme/plain-7b", source: "mirror", revision: "v1" });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/models/download");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({ model: "acme/plain-7b", source: "mirror", revision: "v1" });
  });

  it("unwraps the downloads envelope", async () => {
    mockJson({ jobs: [{ id: "job1" }] });
    await expect(fetchModelDownloads()).resolves.toEqual([{ id: "job1" }]);
  });

  it("fetches a single job", async () => {
    const fetchMock = mockJson({ id: "job1" });
    await fetchModelDownload("job1");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/models/downloads/job1");
  });

  it("posts a cancel", async () => {
    const fetchMock = mockJson({ id: "job1", status: "cancelled" });
    await cancelModelDownload("job1");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/models/downloads/job1/cancel");
    expect(init.method).toBe("POST");
  });

  it("posts a sync with the node list", async () => {
    const fetchMock = mockJson({ ok: true, results: [] });
    await syncModelToNodes("acme/plain-7b", ["n1", "n2"], "ubuntu");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/models/acme/plain-7b/sync");
    expect(JSON.parse(init.body)).toEqual({ nodes: ["n1", "n2"], ssh_user: "ubuntu" });
  });

  it("encodes the presence node query", async () => {
    const fetchMock = mockJson({ model: "acme/plain-7b", local: true, nodes: [] });
    await fetchModelPresence("acme/plain-7b", ["n1", "n2"]);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/models/acme/plain-7b/presence?nodes=n1%2Cn2");
  });

  it("sends a DELETE for a model", async () => {
    const fetchMock = mockJson({ deleted: "acme/plain-7b", path: "/hub", freed_bytes: 1 });
    await expect(deleteModel("acme/plain-7b")).resolves.toEqual({ deleted: "acme/plain-7b", path: "/hub", freed_bytes: 1 });
    expect(fetchMock.mock.calls[0][1].method).toBe("DELETE");
  });

  it("surfaces API errors", async () => {
    mockJson({ detail: "in use" }, 409);
    await expect(deleteModel("acme/plain-7b")).rejects.toThrow(/API 409/);
  });
});
