import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  cancelImagePull,
  deleteImage,
  fetchImagePresence,
  fetchImagePull,
  fetchImagePulls,
  fetchImages,
  startImagePull,
  syncImageToNodes,
} from "@/lib/api";
import type { ImageEntry } from "@/lib/types";

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

const REF = "ghcr.io/acme/engine:0.1.0";

const image: ImageEntry = {
  ref: REF,
  repository: "ghcr.io/acme/engine",
  tag: "0.1.0",
  tagged_ref: REF,
  engine: "vllm",
  variant: "default",
  engine_key: "vllm/default",
  version: "0.1.0",
  legacy_tags: ["vllm-node"],
  source: "bundled",
  description: "",
  present: true,
  image_id: "sha256:aaaa",
  size_bytes: 1024,
  created: "2026-01-01T00:00:00Z",
  local_digest: "sha256:aaaa",
  index_digest: "",
  digest_drift: false,
  update_available: false,
};

describe("images api", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("unwraps the catalogue envelope", async () => {
    const fetchMock = mockJson({ images: [image] });
    await expect(fetchImages()).resolves.toEqual([image]);
    expect(fetchMock).toHaveBeenCalledWith("/api/images", expect.objectContaining({ credentials: "include" }));
  });

  it("posts a pull request with the ref in the body", async () => {
    const fetchMock = mockJson({ id: "job1", status: "queued" });
    await startImagePull(REF);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/images/pull");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({ ref: REF });
  });

  it("unwraps the pulls envelope", async () => {
    mockJson({ jobs: [{ id: "job1" }] });
    await expect(fetchImagePulls()).resolves.toEqual([{ id: "job1" }]);
  });

  it("fetches a single pull job", async () => {
    const fetchMock = mockJson({ id: "job1" });
    await fetchImagePull("job1");
    expect(fetchMock.mock.calls[0][0]).toBe("/api/images/pulls/job1");
  });

  it("posts a cancel", async () => {
    const fetchMock = mockJson({ id: "job1", status: "cancelled" });
    await cancelImagePull("job1");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/images/pulls/job1/cancel");
    expect(init.method).toBe("POST");
  });

  it("encodes the ref into the delete query rather than the path", async () => {
    const fetchMock = mockJson({ deleted: REF, image_id: "sha256:aaaa", freed_bytes: 1 });
    await expect(deleteImage(REF)).resolves.toEqual({ deleted: REF, image_id: "sha256:aaaa", freed_bytes: 1 });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`/api/images?ref=${encodeURIComponent(REF)}`);
    expect(init.method).toBe("DELETE");
  });

  it("posts a sync with the ref and node list", async () => {
    const fetchMock = mockJson({ ref: REF, ok: true, results: [] });
    await syncImageToNodes(REF, ["n1", "n2"], "ubuntu");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/images/sync");
    expect(JSON.parse(init.body)).toEqual({ ref: REF, nodes: ["n1", "n2"], ssh_user: "ubuntu" });
  });

  it("encodes both the ref and the nodes in the presence query", async () => {
    const fetchMock = mockJson({ ref: REF, local: true, image_id: "", nodes: [] });
    await fetchImagePresence(REF, ["n1", "n2"]);
    expect(fetchMock.mock.calls[0][0]).toBe(
      `/api/images/presence?ref=${encodeURIComponent(REF)}&nodes=n1%2Cn2`,
    );
  });

  it("surfaces API errors", async () => {
    mockJson({ detail: "in use" }, 409);
    await expect(deleteImage(REF)).rejects.toThrow(/API 409/);
  });
});
